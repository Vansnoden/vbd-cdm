# database/geoutils.py - Enhanced version

import reverse_geocode as rg
import numpy as np
from typing import Tuple, Optional, Dict, List
import pandas as pd
from functools import lru_cache
import logging
from math import radians, sin, cos, sqrt, asin

from database.constants import COUNTRY_LOOKUP

logger = logging.getLogger(__name__)

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
def get_country_from_coords(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Get ISO country name and code from latitude and longitude coordinates.
    
    Uses multiple methods in order of reliability:
    1. reverse_geocode library (requires internet)
    2. Nearest coordinate in COUNTRY_LOOKUP
    3. Bounding box estimation for fallback
    
    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        
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
        result = rg.search([(lat, lon)])
        if result and len(result) > 0:
            country_name = result[0].get('country')
            country_code_raw = result[0].get('country_code')
            
            # Normalize to ISO alpha-2
            if country_code_raw:
                country_code = country_code_raw.upper()
                # Ensure we have the correct ISO code
                if len(country_code) == 3:
                    # Convert alpha-3 to alpha-2
                    for iso2, iso3 in ISO_COUNTRY_CODES.items():
                        if iso3 == country_code:
                            country_code = iso2
                            break
                elif len(country_code) == 2:
                    # Already alpha-2, just verify
                    pass
                else:
                    country_code = None
            
            if country_code and country_name:
                # Verify the mapping
                if country_code in COUNTRY_NAME_TO_ISO.values():
                    # Get the correct country name for this ISO code
                    correct_name = ISO_TO_COUNTRY_NAME.get(country_code)
                    if correct_name:
                        country_name = correct_name
                return country_name, country_code
                
    except Exception as e:
        logger.debug(f"Reverse geocode failed for ({lat}, {lon}): {e}")
    
    # Method 2: Nearest known coordinate lookup
    country_name, country_code = _get_country_from_nearest(lat, lon)
    if country_code:
        return country_name, country_code
    
    # Method 3: Bounding box fallback for known regions
    country_name, country_code = _get_country_from_bounding_box(lat, lon)
    if country_code:
        return country_name, country_code
    
    logger.warning(f"No country found for coordinates ({lat}, {lon})")
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
        # Normalize code
        country_code = country_code_raw.upper()
        return country_name, country_code
    
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


def get_iso_country_code(country: str) -> Optional[str]:
    """
    Get ISO alpha-2 country code from a country name or code.
    
    Args:
        country: Country name or code (e.g., 'Kenya', 'KE', 'KEN')
        
    Returns:
        ISO alpha-2 code or None if not found
    """
    if not country:
        return None
    
    country = str(country).strip().upper()
    
    # If it's already a valid ISO code
    if country in ISO_COUNTRY_CODES:
        return country
    if country in ISO_COUNTRY_CODES.values():
        # Reverse lookup: code is alpha-3, find alpha-2
        for iso2, iso3 in ISO_COUNTRY_CODES.items():
            if iso3 == country:
                return iso2
    
    # Try to match by name
    country_names = {
        name.upper(): code for name, code in COUNTRY_NAME_TO_ISO.items()
    }
    if country in country_names:
        return country_names[country]
    
    # Try partial match
    for name, code in COUNTRY_NAME_TO_ISO.items():
        if country in name.upper():
            return code
    
    return None


def get_country_name(code: str) -> Optional[str]:
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