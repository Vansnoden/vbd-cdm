"""
Data Consolidation Module for CDM

This module provides functions to consolidate and standardize data after ingestion.
It handles:
- Location standardization (deduplication, name normalization)
- Mapping raw coordinates (e.g. climate coordinates like '0.123/34.567') to administrative names
- Coordinate validation and clustering using COUNTRY_LOOKUP from database/constants.py
- Vocabulary alignment
- Duplicate detection, resolution, and fact-table aggregation
- Expanding yearly burden data to daily records
- Computing resistance status from mortality data
- ISO country code standardization with multiple geocoding fallback methods
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any, Union
from sqlalchemy import text
from sqlalchemy.orm import Session
import logging
from functools import lru_cache
import re
from math import radians, sin, cos, sqrt, asin
from datetime import datetime, timedelta
import warnings

from database.session import SessionLocal
from database.models import DimLocation, DimVector, DimInsecticide, DimDisease, DimSource, DimTime
from database.constants import COUNTRY_LOOKUP

logger = logging.getLogger(__name__)


# ========================= ISO COUNTRY CODE MAPPINGS =========================

# ISO 3166-1 alpha-2 to alpha-3 mapping
ISO_COUNTRY_CODES = {
    'KE': 'KEN', 'GH': 'GHA', 'NG': 'NGA', 'ZA': 'ZAF', 'TZ': 'TZA',
    'UG': 'UGA', 'ET': 'ETH', 'DZ': 'DZA', 'AO': 'AGO', 'BJ': 'BEN',
    'BW': 'BWA', 'BF': 'BFA', 'BI': 'BDI', 'CV': 'CPV', 'CM': 'CMR',
    'CF': 'CAF', 'TD': 'TCD', 'KM': 'COM', 'CG': 'COG', 'CI': 'CIV',
    'DJ': 'DJI', 'EG': 'EGY', 'GQ': 'GNQ', 'ER': 'ERI', 'SZ': 'SWZ',
    'GA': 'GAB', 'GM': 'GMB', 'GN': 'GIN', 'GW': 'GNB', 'LS': 'LSO',
    'LR': 'LBR', 'LY': 'LBY', 'MG': 'MDG', 'MW': 'MWI', 'ML': 'MLI',
    'MR': 'MRT', 'MU': 'MUS', 'MA': 'MAR', 'MZ': 'MOZ', 'NA': 'NAM',
    'NE': 'NER', 'RW': 'RWA', 'ST': 'STP', 'SN': 'SEN', 'SC': 'SYC',
    'SL': 'SLE', 'SO': 'SOM', 'SS': 'SSD', 'SD': 'SDN', 'TG': 'TGO',
    'TN': 'TUN', 'ZM': 'ZMB', 'ZW': 'ZWE'
}

# Country names to ISO codes mapping
COUNTRY_NAME_TO_ISO = {
    'Kenya': 'KE', 'Ghana': 'GH', 'Nigeria': 'NG', 'South Africa': 'ZA',
    'Tanzania': 'TZ', 'Uganda': 'UG', 'Ethiopia': 'ET', 'Algeria': 'DZ',
    'Angola': 'AO', 'Benin': 'BJ', 'Botswana': 'BW', 'Burkina Faso': 'BF',
    'Burundi': 'BI', 'Cabo Verde': 'CV', 'Cameroon': 'CM', 
    'Central African Republic': 'CF', 'Chad': 'TD', 'Comoros': 'KM',
    'Congo': 'CG', "Côte d'Ivoire": 'CI', 'Djibouti': 'DJ', 'Egypt': 'EG',
    'Equatorial Guinea': 'GQ', 'Eritrea': 'ER', 'Eswatini': 'SZ',
    'Gabon': 'GA', 'Gambia': 'GM', 'Guinea': 'GN', 'Guinea-Bissau': 'GW',
    'Lesotho': 'LS', 'Liberia': 'LR', 'Libya': 'LY', 'Madagascar': 'MG',
    'Malawi': 'MW', 'Mali': 'ML', 'Mauritania': 'MR', 'Mauritius': 'MU',
    'Morocco': 'MA', 'Mozambique': 'MZ', 'Namibia': 'NA', 'Niger': 'NE',
    'Rwanda': 'RW', 'São Tomé and Príncipe': 'ST', 'Senegal': 'SN',
    'Seychelles': 'SC', 'Sierra Leone': 'SL', 'Somalia': 'SO',
    'South Sudan': 'SS', 'Sudan': 'SD', 'Togo': 'TG', 'Tunisia': 'TN',
    'Zambia': 'ZM', 'Zimbabwe': 'ZW'
}

# ISO code to country name mapping
ISO_TO_COUNTRY_NAME = {v: k for k, v in COUNTRY_NAME_TO_ISO.items()}

# Country aliases for normalization
COUNTRY_ALIASES = {
    'USA': 'US', 'U.S.A.': 'US', 'United States': 'US', 'United States of America': 'US',
    'UK': 'GB', 'U.K.': 'GB', 'United Kingdom': 'GB', 'Great Britain': 'GB',
    'UAE': 'AE', 'U.A.E.': 'AE', 'United Arab Emirates': 'AE',
    'RSA': 'ZA', 'S.A.': 'ZA',
    'Tanzania, United Republic of': 'TZ',
    'Congo, Democratic Republic of': 'CD', 'DRC': 'CD', 'D.R.C.': 'CD',
    'Congo, Republic of': 'CG',
    'Swaziland': 'SZ',
    'Ivory Coast': 'CI',
    'Czechia': 'CZ', 'Czech Republic': 'CZ',
    'South Korea': 'KR', 'Korea': 'KR',
    'North Korea': 'KP', 'DPRK': 'KP',
}


def get_iso_country_code(country: Union[str, None]) -> Optional[str]:
    """
    Get ISO alpha-2 country code from a country name or code.
    
    Args:
        country: Country name or code (e.g., 'Kenya', 'KE', 'KEN')
        
    Returns:
        ISO alpha-2 code or None if not found
    """
    if not country:
        return None
    
    country = str(country).strip()
    country_upper = country.upper()
    
    # Check aliases first
    if country_upper in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[country_upper]
    
    # If it's already a valid ISO code (alpha-2)
    if country_upper in ISO_COUNTRY_CODES:
        return country_upper
    
    # If it's an alpha-3 code, find alpha-2
    if country_upper in ISO_COUNTRY_CODES.values():
        for iso2, iso3 in ISO_COUNTRY_CODES.items():
            if iso3 == country_upper:
                return iso2
    
    # Try exact match by name
    if country in COUNTRY_NAME_TO_ISO:
        return COUNTRY_NAME_TO_ISO[country]
    
    # Try case-insensitive match by name
    for name, code in COUNTRY_NAME_TO_ISO.items():
        if country.lower() == name.lower():
            return code
    
    # Try partial match
    for name, code in COUNTRY_NAME_TO_ISO.items():
        if country.lower() in name.lower() or name.lower() in country.lower():
            return code
    
    return None


def get_country_name(code: Union[str, None]) -> Optional[str]:
    """
    Get country name from ISO alpha-2 code.
    
    Args:
        code: ISO alpha-2 country code
        
    Returns:
        Country name or None if not found
    """
    if not code:
        return None
    
    code = code.upper()
    return ISO_TO_COUNTRY_NAME.get(code)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points in kilometers.
    
    Args:
        lat1, lon1: First point in decimal degrees
        lat2, lon2: Second point in decimal degrees
        
    Returns:
        Distance in kilometers
    """
    R = 6371  # Earth's radius in kilometers
    
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    
    return R * c


@lru_cache(maxsize=10000)
def get_country_from_coords(lat: float, lon: float, max_distance_km: float = 200.0) -> Tuple[Optional[str], Optional[str]]:
    """
    Get ISO country name and code from latitude and longitude coordinates.
    
    Uses multiple methods in order of reliability:
    1. reverse_geocode library (requires internet)
    2. Nearest coordinate in COUNTRY_LOOKUP
    3. Bounding box estimation for fallback
    
    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        max_distance_km: Maximum distance in kilometers to consider a match
        
    Returns:
        Tuple of (country_name, country_code) or (None, None) if not found
    """
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
        return None, None
    
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        logger.warning(f"Invalid coordinates: ({lat}, {lon})")
        return None, None
    
    # Method 1: Try reverse_geocode first (most accurate)
    try:
        import reverse_geocode as rg
        result = rg.search([(lat, lon)])
        if result and len(result) > 0:
            country_name = result[0].get('country')
            country_code_raw = result[0].get('country_code')
            
            if country_code_raw:
                country_code = country_code_raw.upper()
                # Ensure we have a valid alpha-2 code
                if len(country_code) == 3:
                    for iso2, iso3 in ISO_COUNTRY_CODES.items():
                        if iso3 == country_code:
                            country_code = iso2
                            break
                elif len(country_code) != 2:
                    country_code = None
                
                if country_code and country_code in ISO_COUNTRY_CODES:
                    correct_name = ISO_TO_COUNTRY_NAME.get(country_code, country_name)
                    return correct_name, country_code
    except ImportError:
        logger.debug("reverse_geocode not installed, using fallback methods")
    except Exception as e:
        logger.debug(f"Reverse geocode failed for ({lat}, {lon}): {e}")
    
    # Method 2: Nearest known coordinate lookup
    country_name, country_code = _get_country_from_nearest(lat, lon, max_distance_km)
    if country_code:
        return country_name, country_code
    
    # Method 3: Bounding box fallback for known regions
    country_name, country_code = _get_country_from_bounding_box(lat, lon)
    if country_code:
        return country_name, country_code
    
    logger.debug(f"No country found for coordinates ({lat}, {lon})")
    return None, None


def _get_country_from_nearest(lat: float, lon: float, max_distance_km: float = 200.0) -> Tuple[Optional[str], Optional[str]]:
    """
    Find nearest country using the COUNTRY_LOOKUP table with Haversine distance.
    
    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        max_distance_km: Maximum distance in kilometers to consider a match
        
    Returns:
        Tuple of (country_name, country_code) or (None, None) if not found
    """
    if not COUNTRY_LOOKUP:
        return None, None
    
    min_dist = float('inf')
    best_match = None
    
    for (ref_lat, ref_lon), country_info in COUNTRY_LOOKUP.items():
        try:
            dist = haversine_distance(lat, lon, ref_lat, ref_lon)
            if dist < min_dist and dist < max_distance_km:
                min_dist = dist
                best_match = country_info
        except Exception as e:
            logger.debug(f"Error calculating distance for ({lat}, {lon}) to ({ref_lat}, {ref_lon}): {e}")
            continue
    
    if best_match:
        country_name, country_code_raw = best_match
        country_code = country_code_raw.upper()
        if country_code in ISO_COUNTRY_CODES:
            return country_name, country_code
        # Try to get ISO code from name
        iso_code = get_iso_country_code(country_name)
        if iso_code:
            return country_name, iso_code
    
    return None, None


def _get_country_from_bounding_box(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Fallback: Use simple bounding box for African countries.
    """
    # African continent bounding box (rough)
    if -35 <= lat <= 37 and -20 <= lon <= 55:
        # More specific checks for large countries
        if 4 <= lat <= 14 and 0 <= lon <= 15:  # West Africa
            return "Nigeria", "NG"
        elif 4 <= lat <= 12 and 34 <= lon <= 42:  # East Africa
            return "Kenya", "KE"
        elif -12 <= lat <= -4 and 28 <= lon <= 36:  # Southern Africa
            return "Zambia", "ZM"
        elif 24 <= lat <= 33 and 25 <= lon <= 35:  # North Africa
            return "Egypt", "EG"
        elif -27 <= lat <= -22 and 15 <= lon <= 30:  # South Africa
            return "South Africa", "ZA"
        elif 8 <= lat <= 12 and -2 <= lon <= 6:  # Ghana
            return "Ghana", "GH"
        elif -6 <= lat <= -1 and 29 <= lon <= 34:  # Tanzania
            return "Tanzania", "TZ"
        elif 0 <= lat <= 4 and 32 <= lon <= 35:  # Uganda
            return "Uganda", "UG"
        elif 4 <= lat <= 15 and 35 <= lon <= 48:  # Ethiopia
            return "Ethiopia", "ET"
        elif 0 <= lat <= 6 and 8 <= lon <= 12:  # Cameroon
            return "Cameroon", "CM"
        elif 24 <= lat <= 37 and -10 <= lon <= -1:  # Morocco
            return "Morocco", "MA"
        elif 11 <= lat <= 20 and -17 <= lon <= -13:  # Senegal
            return "Senegal", "SN"
        elif 4 <= lat <= 10 and 38 <= lon <= 48:  # Somalia
            return "Somalia", "SO"
        elif 8 <= lat <= 22 and 0 <= lon <= 10:  # Togo/Benin/Burkina
            return "Burkina Faso", "BF"
        elif 15 <= lat <= 23 and 1 <= lon <= 11:  # Mali/Niger
            return "Mali", "ML"
    
    return None, None


# ========================= DATA CONSOLIDATOR CLASS =========================

class DataConsolidator:
    """
    Consolidates and standardizes data in the CDM.
    
    This class handles:
    1. Location deduplication and standardization
    2. Coordinate clustering to identify nearby locations
    3. Country code assignment using ISO standards
    4. Vocabulary alignment
    5. Duplicate detection, resolution, and fact-table aggregation
    6. Expanding yearly burden data to daily records
    7. Computing resistance status from mortality data
    """
    
    def __init__(self, session: Optional[Session] = None):
        """
        Initialize the consolidator.
        
        Args:
            session: SQLAlchemy session. If None, creates a new one.
        """
        self.session = session or SessionLocal()
        self._location_cache = {}
        self._country_lookup = self._build_country_lookup()
    
    def _build_country_lookup(self) -> Dict[Tuple[float, float], Tuple[str, str]]:
        """
        Build a country lookup from the COUNTRY_LOOKUP dictionary.
        
        Returns:
            Dictionary mapping (lat, lon) to (country_name, country_code)
        """
        return COUNTRY_LOOKUP
    
    def _find_nearest_country(self, lat: float, lon: float, max_distance_degrees: float = 5.0) -> Tuple[Optional[str], Optional[str]]:
        """
        Find the nearest country in the lookup table using Euclidean distance.
        
        Args:
            lat: Latitude in decimal degrees
            lon: Longitude in decimal degrees
            max_distance_degrees: Maximum distance in degrees to consider a match
        
        Returns:
            Tuple of (country_name, country_code) or (None, None) if not found
        """
        if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
            return None, None
        
        def haversine_distance(lat1, lon1, lat2, lon2):
            """Calculate the great circle distance between two points in kilometers."""
            R = 6371  # Earth's radius in kilometers
            
            lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
            c = 2 * asin(sqrt(a))
            
            return R * c
        
        min_dist = float('inf')
        best_match = None
        
        # Convert max_distance_degrees to approximate km (1 degree ~ 111 km at equator)
        max_distance_km = max_distance_degrees * 111
        
        for (ref_lat, ref_lon), country_info in self._country_lookup.items():
            try:
                dist = haversine_distance(lat, lon, ref_lat, ref_lon)
                if dist < min_dist and dist < max_distance_km:
                    min_dist = dist
                    best_match = country_info
            except Exception as e:
                logger.debug(f"Error calculating distance for ({lat}, {lon}) to ({ref_lat}, {ref_lon}): {e}")
                continue
        
        return best_match if best_match else (None, None)
    
    def _bind_climate_coordinates_to_region(self, study_region: str = "Kenya"):
        """
        Snaps unmapped coordinate strings (like '0.231/34.291') inside the database
        to standard region names and populates their country fields.
        """
        logger.info("Aligning raw coordinate points with administrative study region...")
        
        # Get ISO code for the study region
        region_code = get_iso_country_code(study_region)
        if not region_code:
            region_code = 'KE'  # Default to Kenya
            logger.warning(f"Could not get ISO code for '{study_region}', using '{region_code}'")
        
        region_name = get_country_name(region_code) or study_region
        
        # 1. Standardize country code/name on all records missing country data
        result = self.session.execute(text("""
            UPDATE dim_location
            SET country_name = :region_name,
                country_code = :region_code
            WHERE country_name IS NULL 
               OR country_name = '' 
               OR country_name = 'None'
               OR country_name = 'UNKNOWN'
               OR country_code IS NULL 
               OR country_code = '' 
               OR country_code = 'UNKNOWN'
        """), {"region_name": region_name, "region_code": region_code})
        
        logger.info(f"  Updated {result.rowcount} locations with missing country data")
        
        # 2. Map coordinates within region boundaries (specific to Kenya/Siaya)
        if region_code == 'KE':
            result = self.session.execute(text("""
                UPDATE dim_location
                SET name = 'Siaya County (Climate Grid)',
                    country_name = :region_name,
                    country_code = :region_code
                WHERE (name LIKE '%/%' OR name IS NULL OR name = '' OR name = 'Siaya')
                  AND (centroid_lat BETWEEN -0.5 AND 0.5)
                  AND (centroid_lon BETWEEN 34.0 AND 34.7)
            """), {"region_name": region_name, "region_code": region_code})
            logger.info(f"  Mapped {result.rowcount} climate grid coordinates to Siaya County")
        
        self.session.commit()

    def _compute_resistance_status(self):
        """
        Compute resistance status from mortality data in fact_insecticide_resistance.
        
        WHO criteria for insecticide resistance:
        - Susceptible: mortality >= 98%
        - Suspected resistance: mortality between 90% and 97%
        - Resistant: mortality < 90%
        
        Uses n_insect_tested and n_insect_dead to calculate mortality percentage.
        """
        logger.info("=" * 60)
        logger.info("COMPUTING RESISTANCE STATUS")
        logger.info("=" * 60)
        
        # Step 1: Check what's in the resistance table
        total_resistance = self.session.execute(text("""
            SELECT COUNT(*) FROM fact_insecticide_resistance
        """)).scalar()
        logger.info(f"Total resistance records: {total_resistance}")
        
        if total_resistance == 0:
            logger.info("No resistance records found to process")
            return
        
        # Step 2: Check columns
        columns = self.session.execute(text("""
            SELECT name FROM pragma_table_info('fact_insecticide_resistance')
        """)).fetchall()
        col_names = [c[0] for c in columns]
        logger.info(f"Columns in fact_insecticide_resistance: {col_names}")
        
        # Step 3: Check for records with valid test data
        records_with_counts = self.session.execute(text("""
            SELECT COUNT(*) FROM fact_insecticide_resistance
            WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
              AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
        """)).scalar()
        logger.info(f"Records with n_insect_tested > 0 and n_insect_dead >= 0: {records_with_counts}")
        
        # Step 4: Check for records with mortality already computed
        records_with_mortality = self.session.execute(text("""
            SELECT COUNT(*) FROM fact_insecticide_resistance
            WHERE mortality IS NOT NULL AND mortality > 0
        """)).scalar()
        logger.info(f"Records with mortality > 0: {records_with_mortality}")
        
        # Step 5: Get sample records to verify
        logger.info("Sample records with test data:")
        sample = self.session.execute(text("""
            SELECT resistance_id, n_insect_tested, n_insect_dead, mortality, resistance_status
            FROM fact_insecticide_resistance
            WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
              AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
            LIMIT 5
        """)).fetchall()
        
        for rec in sample:
            logger.info(f"  ID={rec[0]}, tested={rec[1]}, dead={rec[2]}, mortality={rec[3]}, status={rec[4]}")
        
        # Step 6: Process records with test data using a single efficient UPDATE
        if records_with_counts > 0:
            logger.info(f"Processing {records_with_counts} records with test data...")
            self._process_resistance_status_bulk()
        elif records_with_mortality > 0:
            logger.info(f"Processing {records_with_mortality} records with mortality data...")
            self._process_resistance_status_from_mortality()
        else:
            logger.warning("No valid data found for resistance status computation!")
            return
        
        # Step 7: Verify results
        self._verify_resistance_status()
    
    def _process_resistance_status_bulk(self):
        """
        Bulk update resistance status using a single SQL statement.
        This is much more efficient and reliable than row-by-row updates.
        """
        logger.info("Performing bulk update of resistance status...")
        
        try:
            # Use a single UPDATE statement with CASE
            result = self.session.execute(text("""
                UPDATE fact_insecticide_resistance
                SET 
                    mortality = ROUND((n_insect_dead * 100.0) / n_insect_tested, 2),
                    resistance_status = CASE
                        WHEN n_insect_tested IS NOT NULL AND n_insect_tested > 0 
                            AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
                        THEN
                            CASE
                                WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 98 THEN 'Susceptible'
                                WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 90 THEN 'Suspected Resistance'
                                ELSE 'Resistant'
                            END
                        ELSE resistance_status
                    END
                WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
                  AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
            """))
            
            self.session.commit()
            logger.info(f"Bulk update completed. Affected rows: {result.rowcount}")
            
        except Exception as e:
            logger.error(f"Bulk update failed: {e}")
            self.session.rollback()
            # Fall back to row-by-row update
            logger.info("Falling back to row-by-row update...")
            self._process_resistance_status_from_counts()
    
    def _process_resistance_status_from_counts(self):
        """Process resistance status from n_insect_tested and n_insect_dead (row-by-row fallback)."""
        updated_count = 0
        error_count = 0
        
        # Get records with valid test data
        records = self.session.execute(text("""
            SELECT resistance_id, n_insect_tested, n_insect_dead
            FROM fact_insecticide_resistance
            WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
              AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
        """)).fetchall()
        
        logger.info(f"Processing {len(records)} records...")
        
        # Track status counts
        status_counts = {'Susceptible': 0, 'Suspected Resistance': 0, 'Resistant': 0}
        
        for rec in records:
            resistance_id, n_tested, n_dead = rec
            
            # Calculate mortality percentage
            if n_tested > 0:
                mortality_percent = (n_dead / n_tested) * 100
            else:
                mortality_percent = 0
            
            # Determine status based on WHO criteria
            if mortality_percent >= 98:
                status = 'Susceptible'
            elif mortality_percent >= 90:
                status = 'Suspected Resistance'
            else:
                status = 'Resistant'
            
            status_counts[status] = status_counts.get(status, 0) + 1
            
            try:
                self.session.execute(text("""
                    UPDATE fact_insecticide_resistance
                    SET mortality = :mortality,
                        resistance_status = :status
                    WHERE resistance_id = :id
                """), {
                    'mortality': mortality_percent,
                    'status': status,
                    'id': resistance_id
                })
                updated_count += 1
                
                if updated_count % 100 == 0:
                    logger.info(f"  Processed {updated_count} records...")
                    
            except Exception as e:
                logger.error(f"Error updating resistance_id {resistance_id}: {e}")
                error_count += 1
        
        try:
            self.session.commit()
            logger.info(f"Committed {updated_count} updates")
        except Exception as e:
            logger.error(f"Commit failed: {e}")
            self.session.rollback()
        
        logger.info(f"Updated: {updated_count}, Errors: {error_count}")
        logger.info(f"Status distribution: {status_counts}")
    
    def _process_resistance_status_from_mortality(self):
        """Process resistance status from pre-computed mortality."""
        updated_count = 0
        error_count = 0
        
        # Use bulk update for efficiency
        try:
            result = self.session.execute(text("""
                UPDATE fact_insecticide_resistance
                SET resistance_status = CASE
                    WHEN mortality >= 98 THEN 'Susceptible'
                    WHEN mortality >= 90 THEN 'Suspected Resistance'
                    ELSE 'Resistant'
                END
                WHERE mortality IS NOT NULL AND mortality > 0
                  AND (resistance_status IS NULL OR resistance_status = '' OR resistance_status = 'Unknown')
            """))
            self.session.commit()
            logger.info(f"Bulk update completed. Affected rows: {result.rowcount}")
            updated_count = result.rowcount
            
        except Exception as e:
            logger.error(f"Bulk update failed: {e}")
            self.session.rollback()
            
            # Fall back to row-by-row
            records = self.session.execute(text("""
                SELECT resistance_id, mortality
                FROM fact_insecticide_resistance
                WHERE mortality IS NOT NULL AND mortality > 0
                  AND (resistance_status IS NULL OR resistance_status = '' OR resistance_status = 'Unknown')
            """)).fetchall()
            
            logger.info(f"Processing {len(records)} records with mortality data (row-by-row)...")
            
            status_counts = {'Susceptible': 0, 'Suspected Resistance': 0, 'Resistant': 0}
            
            for rec in records:
                resistance_id, mortality_percent = rec
                
                if mortality_percent >= 98:
                    status = 'Susceptible'
                elif mortality_percent >= 90:
                    status = 'Suspected Resistance'
                else:
                    status = 'Resistant'
                
                status_counts[status] = status_counts.get(status, 0) + 1
                
                try:
                    self.session.execute(text("""
                        UPDATE fact_insecticide_resistance
                        SET resistance_status = :status
                        WHERE resistance_id = :id
                    """), {
                        'status': status,
                        'id': resistance_id
                    })
                    updated_count += 1
                except Exception as e:
                    logger.error(f"Error updating resistance_id {resistance_id}: {e}")
                    error_count += 1
            
            try:
                self.session.commit()
                logger.info(f"Committed {updated_count} updates")
            except Exception as e:
                logger.error(f"Commit failed: {e}")
                self.session.rollback()
            
            logger.info(f"Updated: {updated_count}, Errors: {error_count}")
            logger.info(f"Status distribution: {status_counts}")
    
    def _verify_resistance_status(self):
        """Verify resistance status updates."""
        logger.info("Verifying resistance status...")
        
        total_with_status = self.session.execute(text("""
            SELECT COUNT(*) FROM fact_insecticide_resistance
            WHERE resistance_status IS NOT NULL AND resistance_status != ''
        """)).scalar()
        logger.info(f"Records with resistance_status: {total_with_status}")
        
        status_dist = self.session.execute(text("""
            SELECT resistance_status, COUNT(*) as count
            FROM fact_insecticide_resistance
            WHERE resistance_status IS NOT NULL AND resistance_status != ''
            GROUP BY resistance_status
        """)).fetchall()
        
        if status_dist:
            logger.info("Resistance status distribution:")
            for status, count in status_dist:
                logger.info(f"  {status}: {count}")
        else:
            logger.warning("No resistance statuses found!")
            
            # Test update on a single record
            logger.info("Testing update on a single record...")
            test_id = self.session.execute(text("""
                SELECT resistance_id FROM fact_insecticide_resistance 
                WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
                LIMIT 1
            """)).scalar()
            
            if test_id:
                try:
                    self.session.execute(text("""
                        UPDATE fact_insecticide_resistance
                        SET resistance_status = 'Test'
                        WHERE resistance_id = :id
                    """), {'id': test_id})
                    self.session.commit()
                    
                    result = self.session.execute(text("""
                        SELECT resistance_status FROM fact_insecticide_resistance
                        WHERE resistance_id = :id
                    """), {'id': test_id}).scalar()
                    
                    logger.info(f"Test result: {result}")
                    
                    # Rollback
                    self.session.execute(text("""
                        UPDATE fact_insecticide_resistance
                        SET resistance_status = NULL
                        WHERE resistance_id = :id
                    """), {'id': test_id})
                    self.session.commit()
                    
                except Exception as e:
                    logger.error(f"Test failed: {e}")
                    self.session.rollback()

    def _expand_burden_to_daily(self):
        """
        Expand yearly disease burden data (fact_disease_burden) to daily records.
        Uses incidence_rate to estimate cases, or cases_mean if available.
        """
        logger.info("=" * 60)
        logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS")
        logger.info("=" * 60)
        
        # First, check what's in the burden table
        total_burden = self.session.execute(text("SELECT COUNT(*) FROM fact_disease_burden")).scalar()
        logger.info(f"Total burden records: {total_burden}")
        
        if total_burden == 0:
            logger.info("No burden records found to expand")
            return
        
        # Check sample burden records
        sample = self.session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, 
                   cases_mean, incidence_rate, prevalence_rate
            FROM fact_disease_burden 
            LIMIT 5
        """)).fetchall()
        logger.info(f"Sample burden records: {[(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in sample]}")
        
        # Get population estimates for each location to convert incidence_rate to cases
        logger.info("Estimating population for locations...")
        
        # Try to get population from socioeconomic data first
        pop_query = """
            SELECT location_id, indicator_value 
            FROM fact_socioeconomic_condition 
            WHERE indicator_code = 'SP.POP.TOTL'  -- World Bank population indicator
        """
        pop_results = self.session.execute(text(pop_query)).fetchall()
        population_map = {r[0]: r[1] for r in pop_results}
        
        # Get all locations
        loc_query = "SELECT location_id FROM dim_location"
        loc_results = self.session.execute(text(loc_query)).fetchall()
        all_location_ids = [r[0] for r in loc_results]
        
        # For locations without population data, use default
        default_population = 1000000  # 1 million as default
        for loc_id in all_location_ids:
            if loc_id not in population_map or population_map[loc_id] is None or population_map[loc_id] <= 0:
                population_map[loc_id] = default_population
                logger.debug(f"  Using default population {default_population} for location_id {loc_id}")
        
        logger.info(f"Population map has {len(population_map)} locations")
        
        # Get all burden records with incidence_rate
        burden_records = self.session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, source_code, 
                   incidence_rate, prevalence_rate, mortality_rate, cases_mean
            FROM fact_disease_burden
            WHERE incidence_rate IS NOT NULL AND incidence_rate > 0
               OR cases_mean IS NOT NULL AND cases_mean > 0
        """)).fetchall()
        
        if not burden_records:
            logger.warning("No burden records with incidence_rate > 0 or cases_mean > 0 found.")
            return
        
        logger.info(f"Found {len(burden_records)} burden records to expand")
        
        # Seasonal factors for malaria in Kenya (monthly distribution)
        monthly_factors = {
            1: 0.6, 2: 0.5, 3: 0.7, 4: 1.4, 5: 1.5, 6: 1.3,
            7: 1.2, 8: 1.0, 9: 0.8, 10: 0.6, 11: 0.5, 12: 0.5
        }
        
        # Normalize factors so sum = 12 (average of 1)
        total_factor = sum(monthly_factors.values())
        normalized_factors = {m: f * 12 / total_factor for m, f in monthly_factors.items()}
        logger.info(f"Normalized monthly factors: {normalized_factors}")
        
        inserted_count = 0
        skipped_count = 0
        error_count = 0
        
        # Check if we're using SQLite (for proper parameter handling)
        is_sqlite = 'sqlite' in str(self.session.bind.url).lower()
        logger.info(f"Database type: {'SQLite' if is_sqlite else 'PostgreSQL/Other'}")
        
        for record in burden_records:
            burden_id, location_id, year, disease_code, source_code, incidence_rate, prevalence_rate, mortality_rate, cases_mean = record
            
            # Get population for this location
            population = population_map.get(location_id, default_population)
            
            # Calculate annual cases from incidence rate
            if incidence_rate is not None and incidence_rate > 0 and population > 0:
                annual_cases = (incidence_rate / 100000) * population
            elif cases_mean is not None and cases_mean > 0:
                annual_cases = cases_mean
            else:
                logger.warning(f"  Skipping burden_id {burden_id}: no valid incidence_rate or cases_mean")
                skipped_count += 1
                continue
            
            annual_cases = max(annual_cases, 1)
            
            logger.info(f"  Processing burden_id {burden_id}: location_id={location_id}, year={year}, "
                       f"incidence_rate={incidence_rate}, population={population}, annual_cases={annual_cases:.0f}")
            
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31)
            current_date = start_date
            daily_cases_list = []
            
            while current_date <= end_date:
                month = current_date.month
                day = current_date.day
                
                if month == 12:
                    days_in_month = 31
                else:
                    next_month = datetime(year, month + 1, 1)
                    days_in_month = (next_month - datetime(year, month, 1)).days
                
                monthly_cases = annual_cases * normalized_factors[month]
                daily_cases = monthly_cases / days_in_month
                variation = 0.85 + 0.3 * np.random.random()
                daily_cases = daily_cases * variation
                daily_cases = max(1, int(round(daily_cases)))
                
                daily_cases_list.append({
                    'location_id': location_id,
                    'date_id': current_date.strftime('%Y-%m-%d'),
                    'disease_code': disease_code,
                    'source_code': source_code
                })
                current_date += timedelta(days=1)
            
            if daily_cases_list:
                try:
                    # First, delete existing case records for this location/year/disease from burden expansion
                    self.session.execute(text("""
                        DELETE FROM fact_disease_case
                        WHERE location_id = :loc_id
                          AND date_id_onset >= :start_date
                          AND date_id_onset <= :end_date
                          AND disease_code = :disease
                          AND identification_method_code = 'ESTIMATE'
                    """), {
                        'loc_id': location_id,
                        'start_date': start_date.strftime('%Y-%m-%d'),
                        'end_date': end_date.strftime('%Y-%m-%d'),
                        'disease': disease_code
                    })
                    
                    # Insert new daily records in batches
                    batch_size = 5000
                    for i in range(0, len(daily_cases_list), batch_size):
                        batch = daily_cases_list[i:i+batch_size]
                        
                        if is_sqlite:
                            # SQLite: Use individual inserts with named parameters
                            for daily in batch:
                                self.session.execute(text("""
                                    INSERT INTO fact_disease_case (
                                        location_id, date_id_onset, date_id_reported, disease_code,
                                        identification_method_code, source_code, case_status,
                                        host_type, age_group, sex, outcome
                                    ) VALUES (
                                        :loc_id, :date_id, :date_id, :disease,
                                        'ESTIMATE', :src_code, 'confirmed', 'human',
                                        'all', 'unknown', 'unknown'
                                    )
                                """), {
                                    'loc_id': daily['location_id'],
                                    'date_id': daily['date_id'],
                                    'disease': daily['disease_code'],
                                    'src_code': daily['source_code']
                                })
                        else:
                            # PostgreSQL/Other: Use executemany with dictionaries
                            values = []
                            for daily in batch:
                                values.append({
                                    'loc_id': daily['location_id'],
                                    'date_id': daily['date_id'],
                                    'disease': daily['disease_code'],
                                    'src_code': daily['source_code']
                                })
                            
                            self.session.execute(text("""
                                INSERT INTO fact_disease_case (
                                    location_id, date_id_onset, date_id_reported, disease_code,
                                    identification_method_code, source_code, case_status,
                                    host_type, age_group, sex, outcome
                                ) VALUES (
                                    :loc_id, :date_id, :date_id, :disease,
                                    'ESTIMATE', :src_code, 'confirmed', 'human',
                                    'all', 'unknown', 'unknown'
                                )
                            """), values)
                        
                        self.session.commit()
                    
                    inserted_count += len(daily_cases_list)
                    logger.info(f"  Expanded burden_id {burden_id} ({year}) into {len(daily_cases_list)} daily records")
                    
                except Exception as e:
                    logger.error(f"  Error expanding burden_id {burden_id}: {e}")
                    self.session.rollback()
                    error_count += 1
                    continue
        
        self.session.commit()
        logger.info(f"Expanded {inserted_count} daily records, skipped {skipped_count}, errors: {error_count}")

    # ========================= COUNTRY CODE STANDARDIZATION =========================
    
    def _standardize_country_codes(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Standardize all country codes in dim_location to ISO alpha-2 format.
        
        This method:
        1. Fixes missing country codes using coordinates
        2. Normalizes existing country codes to ISO standard
        3. Standardizes country names to match ISO codes
        
        Args:
            dry_run: If True, only report what would be changed
            
        Returns:
            Dictionary with standardization results
        """
        logger.info("=" * 60)
        logger.info("STANDARDIZING COUNTRY CODES TO ISO FORMAT")
        logger.info("=" * 60)
        
        results = {
            'total_locations': 0,
            'fixed_missing': 0,
            'normalized_codes': 0,
            'standardized_names': 0,
            'unresolved': 0,
            'changes': []
        }
        
        locations = self.session.query(DimLocation).all()
        results['total_locations'] = len(locations)
        
        if not locations:
            logger.info("No locations found in database")
            return results
        
        code_changes = {}
        name_changes = {}
        
        for loc in locations:
            changes = {'location_id': loc.location_id, 'name': loc.name}
            needs_update = False
            
            # 1. Handle missing or invalid country codes
            current_code = loc.country_code.strip().upper() if loc.country_code else ''
            is_missing = not current_code or current_code in ['UNKNOWN', 'N/A', 'NULL', 'NONE', '']
            
            if is_missing:
                if loc.centroid_lat and loc.centroid_lon:
                    country_name, country_code = get_country_from_coords(
                        loc.centroid_lat, 
                        loc.centroid_lon
                    )
                    if country_code:
                        if not dry_run:
                            loc.country_code = country_code
                            loc.country_name = country_name
                        changes['new_country_code'] = country_code
                        changes['new_country_name'] = country_name
                        results['fixed_missing'] += 1
                        needs_update = True
                    else:
                        results['unresolved'] += 1
                        changes['status'] = 'unresolved - no country found'
                else:
                    results['unresolved'] += 1
                    changes['status'] = 'unresolved - no coordinates'
            
            # 2. Normalize existing codes to ISO format
            elif current_code:
                normalized_code = get_iso_country_code(current_code)
                
                if normalized_code and normalized_code != current_code:
                    if not dry_run:
                        loc.country_code = normalized_code
                    changes['old_code'] = current_code
                    changes['new_code'] = normalized_code
                    results['normalized_codes'] += 1
                    needs_update = True
                    code_changes[current_code] = normalized_code
                elif current_code not in ISO_COUNTRY_CODES:
                    # Code exists but is not a valid ISO code
                    normalized_code = get_iso_country_code(loc.country_name)
                    if normalized_code:
                        if not dry_run:
                            loc.country_code = normalized_code
                        changes['old_code'] = current_code
                        changes['new_code'] = normalized_code
                        results['normalized_codes'] += 1
                        needs_update = True
                        code_changes[current_code] = normalized_code
            
            # 3. Standardize country name to match ISO code
            if loc.country_code and loc.country_code in ISO_COUNTRY_CODES:
                correct_name = ISO_TO_COUNTRY_NAME.get(loc.country_code)
                if correct_name:
                    current_name = loc.country_name.strip() if loc.country_name else ''
                    if current_name != correct_name:
                        if not dry_run:
                            loc.country_name = correct_name
                        changes['old_name'] = current_name or 'None'
                        changes['new_name'] = correct_name
                        results['standardized_names'] += 1
                        needs_update = True
                        if current_name:
                            name_changes[current_name] = correct_name
            
            if needs_update and not dry_run:
                self.session.add(loc)
            
            if changes:
                results['changes'].append(changes)
        
        if not dry_run:
            self.session.commit()
            logger.info("Country code standardization complete")
        else:
            logger.info(f"DRY RUN: Would standardize {len(results['changes'])} locations")
        
        # Log summary
        logger.info("-" * 40)
        logger.info(f"  Total locations: {results['total_locations']}")
        logger.info(f"  Fixed missing codes: {results['fixed_missing']}")
        logger.info(f"  Normalized codes: {results['normalized_codes']}")
        logger.info(f"  Standardized names: {results['standardized_names']}")
        logger.info(f"  Unresolved: {results['unresolved']}")
        
        if code_changes:
            logger.info(f"  Code changes: {code_changes}")
        if name_changes:
            logger.info(f"  Name changes: {name_changes}")
        
        return results

    # ========================= LOCATION CONSOLIDATION =========================
    
    def consolidate_locations(self, 
                             distance_threshold_km: float = 5.0,
                             min_samples: int = 1,
                             dry_run: bool = False) -> Dict[str, Any]:
        """
        Consolidate location data with enhanced country code standardization.
        
        Args:
            distance_threshold_km: Maximum distance in km for clustering
            min_samples: Minimum samples for DBSCAN cluster
            dry_run: If True, only report what would be changed
        """
        logger.info("=" * 60)
        logger.info("CONSOLIDATING LOCATIONS")
        logger.info("=" * 60)
        
        # STEP 1: Standardize country codes first
        code_results = self._standardize_country_codes(dry_run=dry_run)
        
        # STEP 2: Bind climate coordinates to regions
        self._bind_climate_coordinates_to_region()
        
        # STEP 3: Get all locations
        locations = self.session.query(DimLocation).all()
        
        if not locations:
            logger.warning("No locations found in database")
            return {'status': 'no_data', 'message': 'No locations to consolidate'}
        
        logger.info(f"Found {len(locations)} locations in database")
        
        # STEP 4: Create DataFrame for processing
        loc_data = pd.DataFrame([{
            'location_id': loc.location_id,
            'name': loc.name,
            'location_code': getattr(loc, 'location_code', None),
            'centroid_lat': loc.centroid_lat,
            'centroid_lon': loc.centroid_lon,
            'country_code': loc.country_code,
            'country_name': loc.country_name,
            'admin_level': getattr(loc, 'admin_level', None)
        } for loc in locations])
        
        # STEP 5: Normalize names
        loc_data['name_normalized'] = loc_data['name'].apply(self._normalize_location_name)
        
        # STEP 6: Separate locations with and without coordinates
        has_coords = loc_data['centroid_lat'].notna() & loc_data['centroid_lon'].notna()
        loc_with_coords = loc_data[has_coords].copy()
        loc_no_coords = loc_data[~has_coords].copy()
        
        logger.info(f"Locations with coordinates: {len(loc_with_coords)}, without: {len(loc_no_coords)}")
        
        # STEP 7: Merge by name (same name, same country)
        name_results = self._merge_by_name(loc_data, dry_run)
        
        # STEP 8: Cluster by proximity (only within same country if possible)
        cluster_results = {}
        if not loc_with_coords.empty:
            clusters = self._cluster_locations_by_country_and_proximity(
                loc_with_coords, 
                distance_threshold_km, 
                min_samples
            )
            cluster_results = self._merge_location_clusters(loc_with_coords, clusters, dry_run)
        
        # STEP 9: Fix any remaining missing country codes
        final_fix = self.fix_location_country_codes()
        
        results = {
            'code_standardization': code_results,
            'original_count': len(loc_data),
            'name_duplicate_groups': name_results,
            'cluster_results': cluster_results,
            'locations_without_coords': len(loc_no_coords),
            'final_country_fixes': final_fix
        }
        
        if not dry_run:
            self.session.commit()
        
        logger.info(f"Location consolidation complete: {results['original_count']} original locations")
        logger.info(f"  Name duplicate groups merged: {len(name_results)}")
        if isinstance(cluster_results, dict):
            logger.info(f"  Clusters merged: {cluster_results.get('merged_count', 0)}")
        logger.info(f"  Final country fixes: {final_fix}")
        
        return results

    def _normalize_location_name(self, name: str) -> str:
        """Normalize a location name for comparison."""
        if not name or not isinstance(name, str):
            return ""
        
        name = name.lower()
        # Remove common suffixes
        suffixes = [' county', ' district', ' region', ' province', ' state', ' municipality', ' city', ' town']
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
        
        # Remove common prefixes
        prefixes = ['county of ', 'district of ', 'region of ', 'state of ']
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix):]
        
        # Remove parenthetical content
        name = re.sub(r'\([^)]*\)', '', name)
        
        # Remove extra punctuation and spaces
        name = re.sub(r'[^\w\s]', '', name)
        name = re.sub(r'\s+', ' ', name).strip()
        
        return name

    def _merge_by_name(self, loc_data: pd.DataFrame, dry_run: bool = False) -> List[Dict[str, Any]]:
        """
        Merge locations that have the same normalized name AND same country code.
        """
        logger.info("Merging duplicate locations by name and country...")
        
        results = []
        
        # Group by normalized name and country code
        group_cols = ['name_normalized']
        if 'country_code' in loc_data.columns:
            group_cols.append('country_code')
        
        for (name, country_code), group in loc_data.groupby(group_cols):
            if len(group) <= 1:
                continue
            
            # Prefer locations with country code and coordinates
            group['has_country'] = group['country_code'].notna()
            group['has_coords'] = group['centroid_lat'].notna() & group['centroid_lon'].notna()
            group['has_admin'] = group['admin_level'].notna()
            
            # Sort by data quality
            group_sorted = group.sort_values(
                by=['has_country', 'has_coords', 'has_admin', 'name'],
                ascending=[False, False, False, True],
                na_position='last'
            )
            
            canonical = group_sorted.iloc[0]
            canonical_id = int(canonical['location_id'])
            
            if not dry_run:
                for idx, row in group_sorted.iterrows():
                    source_id = int(row['location_id'])
                    if source_id != canonical_id:
                        self._merge_fact_locations(source_id, canonical_id)
                        self.session.execute(
                            text("DELETE FROM dim_location WHERE location_id = :src_id"), 
                            {"src_id": source_id}
                        )
                
                # Update canonical location with best available data
                loc = self.session.query(DimLocation).filter_by(location_id=canonical_id).first()
                if loc:
                    # Use best available country code
                    if not loc.country_code and canonical['country_code']:
                        loc.country_code = canonical['country_code']
                        loc.country_name = canonical['country_name']
                    
                    # Use best available coordinates
                    if not loc.centroid_lat and canonical['centroid_lat']:
                        loc.centroid_lat = canonical['centroid_lat']
                        loc.centroid_lon = canonical['centroid_lon']
                    
                    # Use most descriptive name
                    if len(str(loc.name)) < len(str(canonical['name'])):
                        loc.name = canonical['name']
                    
                    if hasattr(loc, 'admin_level'):
                        loc.admin_level = canonical.get('admin_level') or loc.admin_level
                    
                    self.session.add(loc)
                self.session.commit()
            
            results.append({
                'name': name,
                'country_code': country_code,
                'canonical_id': canonical_id,
                'canonical_name': canonical['name'],
                'merged_ids': [int(row['location_id']) for _, row in group_sorted.iterrows() 
                              if int(row['location_id']) != canonical_id],
                'count': len(group)
            })
            
            logger.info(f"Name merge: '{name}' ({country_code}) -> canonical ID {canonical_id} merging {len(group)-1} duplicates")
        
        return results

    def _cluster_locations_by_country_and_proximity(self, 
                                                   loc_df: pd.DataFrame,
                                                   eps_km: float,
                                                   min_samples: int) -> pd.Series:
        """
        Cluster locations by proximity, ensuring clusters are within the same country.
        """
        try:
            from sklearn.cluster import DBSCAN
            from geopy.distance import geodesic
        except ImportError:
            logger.warning("sklearn or geopy not installed. Falling back to simple clustering.")
            return pd.Series([-1] * len(loc_df), index=loc_df.index)
        
        if loc_df.empty:
            return pd.Series([-1] * len(loc_df), index=loc_df.index)
        
        # Group by country to avoid cross-border clustering
        final_clusters = pd.Series([-1] * len(loc_df), index=loc_df.index)
        cluster_counter = 0
        
        for country, group in loc_df.groupby('country_code'):
            if len(group) < min_samples:
                continue
            
            coords = group[['centroid_lat', 'centroid_lon']].values
            
            # Check for valid coordinates
            if np.any(np.isnan(coords)) or np.any(np.isinf(coords)):
                valid_mask = ~(np.isnan(coords).any(axis=1) | np.isinf(coords).any(axis=1))
                if not valid_mask.any():
                    continue
                coords = coords[valid_mask]
                valid_indices = group.index[valid_mask]
            else:
                valid_indices = group.index
            
            if len(coords) < min_samples:
                continue
            
            coords_rad = np.radians(coords)
            
            def geodesic_distance(x, y):
                lat1, lon1 = np.degrees(x[0]), np.degrees(x[1])
                lat2, lon2 = np.degrees(y[0]), np.degrees(y[1])
                return geodesic((lat1, lon1), (lat2, lon2)).kilometers
            
            # Apply DBSCAN
            db = DBSCAN(eps=eps_km, min_samples=min_samples, metric=geodesic_distance)
            clusters = db.fit_predict(coords_rad)
            
            # Assign cluster IDs, skipping noise (-1)
            for i, cl in enumerate(clusters):
                if cl != -1:
                    idx = valid_indices[i]
                    final_clusters[idx] = cluster_counter + cl
        
        # Convert to integer cluster IDs
        unique_clusters = final_clusters.unique()
        cluster_map = {old: new for new, old in enumerate(unique_clusters) if old != -1}
        if cluster_map:
            final_clusters = final_clusters.replace(cluster_map)
            final_clusters = final_clusters.fillna(-1).astype(int)
        
        return final_clusters

    def _merge_location_clusters(self, 
                                 loc_df: pd.DataFrame, 
                                 clusters: pd.Series,
                                 dry_run: bool = False) -> Dict[str, Any]:
        """Merge locations within each cluster."""
        results = {
            'merged_count': 0,
            'clusters': {},
            'merged_locations': []
        }
        
        cluster_groups = loc_df.groupby(clusters)
        
        for cluster_id, group in cluster_groups:
            if cluster_id == -1:
                continue
            
            group['has_country'] = group['country_code'].notna()
            group['has_coords'] = group['centroid_lat'].notna() & group['centroid_lon'].notna()
            group_sorted = group.sort_values(
                by=['has_country', 'has_coords', 'name'],
                ascending=[False, False, True],
                na_position='last'
            )
            
            canonical = group_sorted.iloc[0]
            canonical_id = int(canonical['location_id'])
            
            if not dry_run:
                for idx, row in group_sorted.iterrows():
                    source_id = int(row['location_id'])
                    if source_id != canonical_id:
                        self._merge_fact_locations(source_id, canonical_id)
                        self.session.execute(
                            text("DELETE FROM dim_location WHERE location_id = :src_id"), 
                            {"src_id": source_id}
                        )
                
                loc = self.session.query(DimLocation).filter_by(location_id=canonical_id).first()
                if loc:
                    if not loc.country_code and canonical['country_code']:
                        loc.country_code = canonical['country_code']
                        loc.country_name = canonical['country_name']
                    if len(str(loc.name)) < len(str(canonical['name'])):
                        loc.name = canonical['name']
                    if not loc.centroid_lat and canonical['centroid_lat']:
                        loc.centroid_lat = canonical['centroid_lat']
                        loc.centroid_lon = canonical['centroid_lon']
                    if hasattr(loc, 'admin_level'):
                        loc.admin_level = canonical.get('admin_level') or loc.admin_level
                    self.session.add(loc)
                self.session.commit()
            
            results['merged_count'] += 1
            results['clusters'][cluster_id] = {
                'canonical': canonical.to_dict(),
                'members': group['location_id'].tolist(),
                'size': len(group)
            }
            results['merged_locations'].append(canonical_id)
            
            logger.info(f"Cluster {cluster_id}: {len(group)} locations -> canonical '{canonical['name']}' (ID: {canonical_id})")
        
        return results

    def _merge_fact_locations(self, source_id: int, target_id: int):
        """Safely re-link location foreign keys in fact tables."""
        logger.info(f"Safely merging location references: ID {source_id} -> ID {target_id}...")
        
        # 1. Safely handle fact_environment
        try:
            self.session.execute(text("""
                CREATE TEMP TABLE temp_env_merge AS
                SELECT 
                    :target_id as location_id, 
                    date_id, 
                    variable, 
                    AVG(value) as value, 
                    MIN(source_code) as source_code
                FROM fact_environment
                WHERE location_id IN (:source_id, :target_id)
                GROUP BY date_id, variable;
            """), {"target_id": target_id, "source_id": source_id})
            
            self.session.execute(text("""
                DELETE FROM fact_environment 
                WHERE location_id IN (:source_id, :target_id)
            """), {"source_id": source_id, "target_id": target_id})
            
            self.session.execute(text("""
                INSERT INTO fact_environment (location_id, date_id, variable, value, source_code)
                SELECT location_id, date_id, variable, value, source_code FROM temp_env_merge;
            """))
            
            self.session.execute(text("DROP TABLE temp_env_merge;"))
        except Exception as e:
            logger.warning(f"Failed safe merge on fact_environment: {e}")

        # 2. Safely handle fact_insecticide_resistance
        try:
            self.session.execute(text("""
                CREATE TEMP TABLE temp_ir_merge AS
                SELECT 
                    :target_id as location_id, 
                    date_id, 
                    vector_code, 
                    insecticide_code,
                    test_type_code,
                    SUM(n_insect_tested) as n_insect_tested, 
                    SUM(n_insect_dead) as n_insect_dead,
                    CASE 
                        WHEN SUM(n_insect_tested) > 0 THEN (CAST(SUM(n_insect_dead) AS FLOAT) / SUM(n_insect_tested)) * 100 
                        ELSE AVG(mortality) 
                    END as mortality,
                    MIN(source_code) as source_code
                FROM fact_insecticide_resistance
                WHERE location_id IN (:source_id, :target_id)
                GROUP BY date_id, vector_code, insecticide_code, test_type_code;
            """), {"target_id": target_id, "source_id": source_id})
            
            self.session.execute(text("""
                DELETE FROM fact_insecticide_resistance 
                WHERE location_id IN (:source_id, :target_id)
            """), {"source_id": source_id, "target_id": target_id})
            
            self.session.execute(text("""
                INSERT INTO fact_insecticide_resistance (
                    location_id, date_id, vector_code, insecticide_code, test_type_code, 
                    n_insect_tested, n_insect_dead, mortality, source_code
                )
                SELECT 
                    location_id, date_id, vector_code, insecticide_code, test_type_code, 
                    n_insect_tested, n_insect_dead, mortality, source_code 
                FROM temp_ir_merge;
            """))
            
            self.session.execute(text("DROP TABLE temp_ir_merge;"))
        except Exception as e:
            logger.warning(f"Failed safe merge on fact_insecticide_resistance: {e}")

        # 3. Simple update for other tables
        other_fact_tables = [
            'fact_vector_observation', 
            'fact_disease_case', 
            'fact_bionomics',
            'fact_intervention',
            'fact_disease_burden',
            'fact_socioeconomic_condition',
            'fact_health_facility'
        ]
        for table in other_fact_tables:
            try:
                self.session.execute(text(f"""
                    UPDATE {table} 
                    SET location_id = :target_id 
                    WHERE location_id = :source_id
                """), {"target_id": target_id, "source_id": source_id})
            except Exception as e:
                logger.debug(f"Skipped updating {table}: {e}")

    def resolve_fact_table_duplicates(self):
        """Global safety pass to clean up duplicates across fact tables."""
        logger.info("Resolving and aggregating residual duplicates inside fact tables...")
        
        # 1. Resolve Environment Duplicates
        try:
            self.session.execute(text("""
                CREATE TEMP TABLE temp_env_dedup AS
                SELECT location_id, date_id, variable, AVG(value) as value, MIN(source_code) as source_code
                FROM fact_environment
                GROUP BY location_id, date_id, variable;
            """))
            self.session.execute(text("DELETE FROM fact_environment;"))
            self.session.execute(text("""
                INSERT INTO fact_environment (location_id, date_id, variable, value, source_code)
                SELECT location_id, date_id, variable, value, source_code FROM temp_env_dedup;
            """))
            self.session.execute(text("DROP TABLE temp_env_dedup;"))
            self.session.commit()
            logger.info("  Standardized fact_environment entries.")
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to aggregate fact_environment duplicates: {e}")

        # 2. Resolve Insecticide Resistance duplicates
        try:
            self.session.execute(text("""
                CREATE TEMP TABLE temp_ir_dedup AS
                SELECT 
                    location_id, date_id, vector_code, insecticide_code, test_type_code,
                    SUM(n_insect_tested) as n_insect_tested, SUM(n_insect_dead) as n_insect_dead,
                    CASE 
                        WHEN SUM(n_insect_tested) > 0 THEN (CAST(SUM(n_insect_dead) AS FLOAT) / SUM(n_insect_tested)) * 100 
                        ELSE AVG(mortality) 
                    END as mortality,
                    MIN(source_code) as source_code
                FROM fact_insecticide_resistance
                GROUP BY location_id, date_id, vector_code, insecticide_code, test_type_code;
            """))
            self.session.execute(text("DELETE FROM fact_insecticide_resistance;"))
            self.session.execute(text("""
                INSERT INTO fact_insecticide_resistance (
                    location_id, date_id, vector_code, insecticide_code, test_type_code, 
                    n_insect_tested, n_insect_dead, mortality, source_code
                )
                SELECT 
                    location_id, date_id, vector_code, insecticide_code, test_type_code, 
                    n_insect_tested, n_insect_dead, mortality, source_code 
                FROM temp_ir_dedup;
            """))
            self.session.execute(text("DROP TABLE temp_ir_dedup;"))
            self.session.commit()
            logger.info("  Standardized fact_insecticide_resistance entries.")
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to aggregate fact_insecticide_resistance duplicates: {e}")

        # 3. Resolve Disease Case duplicates
        try:
            self.session.execute(text("""
                CREATE TEMP TABLE temp_case_dedup AS
                SELECT 
                    location_id, date_id_onset, date_id_reported, disease_code,
                    identification_method_code, source_code,
                    MIN(case_status) as case_status,
                    MIN(host_type) as host_type,
                    MIN(age_group) as age_group,
                    MIN(sex) as sex,
                    MIN(outcome) as outcome
                FROM fact_disease_case
                GROUP BY location_id, date_id_onset, date_id_reported, disease_code,
                         identification_method_code, source_code
                HAVING COUNT(*) > 1
            """))
            
            dup_count = self.session.execute(text("SELECT COUNT(*) FROM temp_case_dedup")).scalar()
            if dup_count > 0:
                logger.info(f"  Found {dup_count} duplicate case records, removing...")
                self.session.execute(text("""
                    DELETE FROM fact_disease_case
                    WHERE (location_id, date_id_onset, date_id_reported, disease_code,
                           identification_method_code, source_code) IN (
                        SELECT location_id, date_id_onset, date_id_reported, disease_code,
                               identification_method_code, source_code
                        FROM temp_case_dedup
                    )
                """))
                
                self.session.execute(text("""
                    INSERT INTO fact_disease_case (
                        location_id, date_id_onset, date_id_reported, disease_code,
                        identification_method_code, source_code, case_status,
                        host_type, age_group, sex, outcome
                    )
                    SELECT 
                        location_id, date_id_onset, date_id_reported, disease_code,
                        identification_method_code, source_code, case_status,
                        host_type, age_group, sex, outcome
                    FROM temp_case_dedup
                """))
                self.session.commit()
                logger.info(f"  Deduplicated {dup_count} case records")
            
            self.session.execute(text("DROP TABLE temp_case_dedup;"))
        except Exception as e:
            self.session.rollback()
            logger.warning(f"Could not deduplicate case records: {e}")

    # ========================= VOCABULARY CONSOLIDATION =========================
    
    def consolidate_vocabulary(self) -> Dict[str, Any]:
        """Consolidate vocabulary tables."""
        logger.info("Consolidating vocabulary...")
        
        results = {}
        
        results['diseases'] = self._consolidate_dimension(
            DimDisease,
            'disease_name',
            'disease_code'
        )
        results['vectors'] = self._consolidate_dimension(
            DimVector,
            'species_name',
            'vector_code'
        )
        results['insecticides'] = self._consolidate_dimension(
            DimInsecticide,
            'insecticide_name',
            'insecticide_code'
        )
        results['sources'] = self._consolidate_dimension(
            DimSource,
            'source_name',
            'source_code'
        )
        
        self.session.commit()
        return results

    def _consolidate_dimension(self, 
                               model_class,
                               name_col: str,
                               code_col: str) -> Dict[str, Any]:
        """Consolidate a dimension table by finding duplicates."""
        records = self.session.query(model_class).all()
        
        if not records:
            return {'status': 'no_data', 'message': f'No data in {model_class.__name__}'}
        
        name_counts = {}
        for rec in records:
            name = getattr(rec, name_col)
            if name:
                name_counts[name] = name_counts.get(name, 0) + 1
        
        duplicates = {name: count for name, count in name_counts.items() if count > 1}
        
        if not duplicates:
            return {'status': 'ok', 'message': 'No duplicates found', 'duplicates': {}}
        
        merged = []
        for name, count in duplicates.items():
            matching = [rec for rec in records if getattr(rec, name_col) == name]
            
            canonical = matching[0]
            canonical_code = getattr(canonical, code_col)
            
            for duplicate in matching[1:]:
                dup_code = getattr(duplicate, code_col)
                self._update_fact_references(model_class, dup_code, canonical_code)
                self.session.delete(duplicate)
            
            merged.append({
                'name': name,
                'canonical_code': canonical_code,
                'merged_count': count,
                'duplicate_codes': [getattr(dup, code_col) for dup in matching[1:]]
            })
        
        return {
            'status': 'ok',
            'duplicates_found': len(duplicates),
            'merged': merged
        }

    def _update_fact_references(self, 
                                dimension_model, 
                                old_code: str, 
                                new_code: str):
        """Update fact table references when merging dimensions."""
        table_map = {
            DimDisease: {
                'fact_tables': ['fact_disease_case', 'fact_disease_burden', 'fact_intervention'],
                'fk_columns': ['disease_code', 'disease_code', 'target_disease_code']
            },
            DimVector: {
                'fact_tables': ['fact_vector_observation', 'fact_insecticide_resistance', 'fact_bionomics'],
                'fk_columns': ['vector_code', 'vector_code', 'vector_code']
            },
            DimInsecticide: {
                'fact_tables': ['fact_insecticide_resistance'],
                'fk_columns': ['insecticide_code']
            },
            DimSource: {
                'fact_tables': ['fact_vector_observation', 'fact_disease_case', 'fact_insecticide_resistance',
                               'fact_bionomics', 'fact_intervention', 'fact_socioeconomic_condition',
                               'fact_health_facility', 'fact_disease_burden', 'fact_environment'],
                'fk_columns': ['source_code', 'source_code', 'source_code', 'source_code', 'source_code',
                              'source_code', 'source_code', 'source_code', 'source_code']
            }
        }
        
        if dimension_model not in table_map:
            return
        
        mapping = table_map[dimension_model]
        
        for table_name, fk_col in zip(mapping['fact_tables'], mapping['fk_columns']):
            try:
                update_sql = text(f"""
                    UPDATE {table_name} 
                    SET {fk_col} = :new_code 
                    WHERE {fk_col} = :old_code
                """)
                self.session.execute(update_sql, {'new_code': new_code, 'old_code': old_code})
                logger.info(f"Updated {table_name}.{fk_col} from {old_code} to {new_code}")
            except Exception as e:
                logger.warning(f"Failed to update {table_name}.{fk_col}: {e}")

    # ========================= DUPLICATE DETECTION =========================
    
    def detect_duplicates(self) -> Dict[str, Any]:
        """Detect duplicate records in fact tables."""
        logger.info("Detecting duplicates in fact tables...")
        results = {}
        
        results['vector_observations'] = self._check_fact_duplicates(
            'fact_vector_observation',
            ['location_id', 'date_id', 'vector_code']
        )
        results['disease_cases'] = self._check_fact_duplicates(
            'fact_disease_case',
            ['location_id', 'date_id_onset', 'disease_code']
        )
        results['resistance_tests'] = self._check_fact_duplicates(
            'fact_insecticide_resistance',
            ['location_id', 'date_id', 'vector_code', 'insecticide_code']
        )
        results['environment'] = self._check_fact_duplicates(
            'fact_environment',
            ['location_id', 'date_id', 'variable']
        )
        return results

    def _check_fact_duplicates(self, 
                               table_name: str, 
                               key_columns: List[str]) -> Dict[str, Any]:
        """Check for duplicates in a fact table."""
        key_str = ', '.join(key_columns)
        select_str = f"SELECT {key_str}, COUNT(*) as dup_count FROM {table_name}"
        group_str = f"GROUP BY {key_str} HAVING COUNT(*) > 1"
        
        try:
            result = self.session.execute(text(f"{select_str} {group_str}"))
            duplicates = [dict(row._mapping) for row in result]
            
            return {
                'table': table_name,
                'key_columns': key_columns,
                'duplicate_count': len(duplicates),
                'duplicates': duplicates[:100]
            }
        except Exception as e:
            logger.warning(f"Failed to check duplicates in {table_name}: {e}")
            return {
                'table': table_name,
                'error': str(e),
                'duplicate_count': 0
            }

    def fix_location_country_codes(self) -> int:
        """
        Fix missing or incorrect country codes using enhanced geocoding.
        
        Returns:
            Number of locations updated
        """
        logger.info("Fixing location country codes...")
        
        # Get all locations with missing or invalid country codes
        locations = self.session.query(DimLocation).filter(
            (DimLocation.country_code.is_(None)) | 
            (DimLocation.country_code == '') |
            (DimLocation.country_code == 'UNKNOWN') |
            (DimLocation.country_code == 'N/A') |
            (DimLocation.country_name.is_(None)) |
            (DimLocation.country_name == '') |
            (DimLocation.country_name == 'UNKNOWN')
        ).all()
        
        if not locations:
            logger.info("No locations with missing country codes found")
            return 0
        
        logger.info(f"Found {len(locations)} locations with missing country codes")
        
        updated = 0
        for loc in locations:
            if loc.centroid_lat and loc.centroid_lon:
                try:
                    country_name, country_code = get_country_from_coords(
                        loc.centroid_lat, 
                        loc.centroid_lon
                    )
                    
                    if country_code:
                        loc.country_code = country_code
                        loc.country_name = country_name
                        updated += 1
                        logger.info(f"  Assigned {country_code} to '{loc.name}' ({loc.location_id})")
                except Exception as e:
                    logger.warning(f"Failed to get country for location {loc.location_id}: {e}")
        
        self.session.commit()
        logger.info(f"Updated country codes for {updated} locations")
        return updated

    def summarize_locations(self) -> pd.DataFrame:
        """Generate a summary of locations in the database."""
        locations = self.session.query(DimLocation).all()
        
        summary = pd.DataFrame([{
            'location_id': loc.location_id,
            'name': loc.name,
            'country_code': loc.country_code,
            'country_name': loc.country_name,
            'has_coords': loc.centroid_lat is not None and loc.centroid_lon is not None,
            'admin_level': getattr(loc, 'admin_level', None)
        } for loc in locations])
        
        return summary

    def validate_country_codes(self) -> Dict[str, Any]:
        """Validate that all locations have correct ISO country codes."""
        logger.info("Validating country codes...")
        
        locations = self.session.query(DimLocation).all()
        
        results = {
            'total': len(locations),
            'valid': 0,
            'invalid': 0,
            'missing': 0,
            'details': []
        }
        
        for loc in locations:
            if not loc.country_code or loc.country_code == '':
                results['missing'] += 1
                results['details'].append({
                    'location_id': loc.location_id,
                    'name': loc.name,
                    'issue': 'missing_country_code'
                })
                continue
            
            iso_code = get_iso_country_code(loc.country_code)
            if iso_code:
                results['valid'] += 1
            else:
                results['invalid'] += 1
                results['details'].append({
                    'location_id': loc.location_id,
                    'name': loc.name,
                    'country_code': loc.country_code,
                    'issue': 'invalid_country_code'
                })
        
        logger.info(f"Country code validation: {results['valid']} valid, "
                   f"{results['invalid']} invalid, {results['missing']} missing")
        
        return results

    # ========================= MAIN CONSOLIDATION =========================
    
    def consolidate_all(self, study_region: str = "Kenya") -> Dict[str, Any]:
        """
        Run all consolidation steps with enhanced country code standardization.
        """
        logger.info("=" * 60)
        logger.info("STARTING DATA CONSOLIDATION")
        logger.info("=" * 60)
        
        # Step 1: STANDARDIZE COUNTRY CODES FIRST
        logger.info("Step 1: Standardizing country codes to ISO format...")
        self._standardize_country_codes()
        
        # Step 2: Bind raw coordinate strings to regions
        logger.info("Step 2: Binding climate coordinates to regions...")
        self._bind_climate_coordinates_to_region(study_region=study_region)
        
        # Step 3: Expand burden data to daily records
        logger.info("Step 3: Expanding burden data to daily records...")
        self._expand_burden_to_daily()
        
        # Step 4: Compute resistance status from mortality data
        logger.info("Step 4: Computing resistance status...")
        self._compute_resistance_status()
        
        # Step 5: Consolidate locations with enhanced country handling
        logger.info("Step 5: Consolidating locations...")
        loc_results = self.consolidate_locations()
        
        # Step 6: Consolidate vocabulary
        logger.info("Step 6: Consolidating vocabulary...")
        vocab_results = self.consolidate_vocabulary()
        
        # Step 7: Detect duplicates
        logger.info("Step 7: Detecting duplicates...")
        dup_results = self.detect_duplicates()
        
        # Step 8: Resolve fact table duplicates
        logger.info("Step 8: Resolving fact table duplicates...")
        self.resolve_fact_table_duplicates()
        
        # Step 9: Final verification
        logger.info("Step 9: Verifying country codes...")
        validation = self.validate_country_codes()
        
        results = {
            'locations': loc_results,
            'vocabulary': vocab_results,
            'duplicates': dup_results,
            'validation': validation
        }
        
        logger.info("=" * 60)
        logger.info("CONSOLIDATION COMPLETE")
        logger.info(f"  Total locations: {validation.get('total', 0)}")
        logger.info(f"  Valid country codes: {validation.get('valid', 0)}")
        logger.info(f"  Invalid/missing: {validation.get('invalid', 0) + validation.get('missing', 0)}")
        logger.info("=" * 60)
        
        return results

    def close(self):
        """Close the database session."""
        if self.session:
            self.session.close()


# ========================= CONVENIENCE FUNCTIONS =========================

def run_consolidation(dry_run: bool = False):
    """
    Convenience function to run consolidation.
    
    Args:
        dry_run: If True, only report what would be changed
        
    Returns:
        Consolidation results
    """
    consolidator = DataConsolidator()
    try:
        results = consolidator.consolidate_all()
        return results
    finally:
        consolidator.close()


def enrich_location_dataframe(
    df: pd.DataFrame,
    lat_col: str = 'centroid_lat',
    lon_col: str = 'centroid_lon',
    country_code_col: str = 'country_code',
    country_name_col: str = 'country_name'
) -> pd.DataFrame:
    """
    Fill missing country_code and country_name from centroid_lat/centroid_lon.
    Standardizes country codes to ISO alpha-2 format.
    
    Args:
        df: DataFrame with location data
        lat_col: Name of latitude column
        lon_col: Name of longitude column
        country_code_col: Name of country code column
        country_name_col: Name of country name column
        
    Returns:
        DataFrame with enriched country data
    """
    if df.empty:
        return df

    df = df.copy()

    # Ensure both columns exist
    if country_code_col not in df.columns:
        df[country_code_col] = None
    if country_name_col not in df.columns:
        df[country_name_col] = None

    # Identify rows that need enrichment
    missing_mask = (
        df[country_code_col].isna() | 
        (df[country_code_col] == '') |
        (df[country_code_col] == 'UNKNOWN') |
        (df[country_code_col] == 'N/A') |
        (df[country_code_col] == 'NULL')
    )

    # Also check for invalid country codes
    if not df[country_code_col].isna().all():
        invalid_mask = ~df[country_code_col].isin(ISO_COUNTRY_CODES.keys()) & df[country_code_col].notna()
        missing_mask = missing_mask | invalid_mask

    if not missing_mask.any():
        return df

    # Build list of (lat, lon) for rows needing enrichment
    coords_list = []
    indices = []
    for idx in df.index[missing_mask]:
        lat = df.loc[idx, lat_col]
        lon = df.loc[idx, lon_col]
        if pd.notna(lat) and pd.notna(lon):
            coords_list.append((lat, lon))
            indices.append(idx)

    if not coords_list:
        return df

    # Bulk geocode
    results = []
    for lat, lon in coords_list:
        results.append(get_country_from_coords(lat, lon))

    # Assign results back
    for idx, (name, code) in zip(indices, results):
        if code is not None:
            df.loc[idx, country_code_col] = code
            df.loc[idx, country_name_col] = name
        else:
            # Try to get ISO code from existing name
            existing_name = df.loc[idx, country_name_col]
            if existing_name:
                iso_code = get_iso_country_code(existing_name)
                if iso_code:
                    df.loc[idx, country_code_col] = iso_code
                    df.loc[idx, country_name_col] = get_country_name(iso_code) or existing_name

    return df