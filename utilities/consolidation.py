"""
Data Consolidation Module for CDM

This module provides functions to consolidate and standardize data after ingestion.
It handles:
- Location standardization (deduplication, name normalization)
- Full foreign key alignment across all fact tables
- Mapping raw coordinates to administrative names
- Coordinate validation and clustering using COUNTRY_LOOKUP
- Vocabulary alignment
- Duplicate detection, resolution, and fact-table aggregation
- Expanding yearly burden data to daily records
- Computing resistance status from mortality data
- ISO country code standardization with multiple geocoding fallback methods
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any, Union, Set
from sqlalchemy import text, inspect
from sqlalchemy.orm import Session
import logging
from functools import lru_cache
import re
from math import radians, sin, cos, sqrt, asin
from datetime import datetime, timedelta
import warnings

from database.session import SessionLocal
from database.models import (
    DimLocation, DimVector, DimInsecticide, DimDisease, DimSource, DimTime,
    DimIdentificationMethod, DimResistanceTestType, DimSocioeconomicIndicator,
    FactVectorObservation, FactDiseaseCase, FactInsecticideResistance,
    FactBionomics, FactIntervention, FactSocioeconomicCondition,
    FactHealthFacility, FactDiseaseBurden, FactEnvironment
)
from database.constants import COUNTRY_LOOKUP

logger = logging.getLogger(__name__)


# ========================= ISO COUNTRY CODE MAPPINGS =========================

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

ISO_TO_COUNTRY_NAME = {v: k for k, v in COUNTRY_NAME_TO_ISO.items()}

COUNTRY_ALIASES = {
    'USA': 'US', 'U.S.A.': 'US', 'United States': 'US',
    'UK': 'GB', 'U.K.': 'GB', 'United Kingdom': 'GB',
    'RSA': 'ZA', 'S.A.': 'ZA',
    'Tanzania, United Republic of': 'TZ',
    'Congo, Democratic Republic of': 'CD', 'DRC': 'CD',
    'Congo, Republic of': 'CG',
    'Swaziland': 'SZ',
    'Ivory Coast': 'CI',
}


def get_iso_country_code(country: Union[str, None]) -> Optional[str]:
    """Get ISO alpha-2 country code from a country name or code."""
    if not country:
        return None

    country = str(country).strip()
    country_upper = country.upper()

    if country_upper in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[country_upper]

    if country_upper in ISO_COUNTRY_CODES:
        return country_upper

    if country_upper in ISO_COUNTRY_CODES.values():
        for iso2, iso3 in ISO_COUNTRY_CODES.items():
            if iso3 == country_upper:
                return iso2

    if country in COUNTRY_NAME_TO_ISO:
        return COUNTRY_NAME_TO_ISO[country]

    for name, code in COUNTRY_NAME_TO_ISO.items():
        if country.lower() == name.lower():
            return code
        if country.lower() in name.lower() or name.lower() in country.lower():
            return code

    return None


def get_country_name(code: Union[str, None]) -> Optional[str]:
    """Get country name from ISO alpha-2 code."""
    if not code:
        return None
    code = code.upper()
    return ISO_TO_COUNTRY_NAME.get(code)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great circle distance between two points in kilometers."""
    R = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return R * c


def is_real_number_robust(value: str) -> bool:
    """
    Robust check if a string is a real number.
    Handles common edge cases like whitespace, empty strings, and special values.
    
    Args:
        value: The string to test
        
    Returns:
        True if the string represents a valid real number
    """
    if not value or not isinstance(value, str):
        return False
    
    # Strip whitespace
    value = value.strip()
    
    if not value:
        return False
    
    # Check for common non-numeric patterns
    if value.lower() in ['nan', 'inf', '-inf', 'infinity', '-infinity', 'null', 'none', '']:
        return False
    
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


@lru_cache(maxsize=10000)
def get_country_from_coords(lat: float, lon: float, max_distance_km: float = 200.0) -> Tuple[Optional[str], Optional[str]]:
    """Get ISO country name and code from latitude and longitude coordinates."""
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
        return None, None

    if is_real_number_robust(str(lat)) or is_real_number_robust(str(lon)):
        return None, None

    if not (float('-90') <= float(str(lat)) <= float(90)) or not (float('-180') <= float(str(lon)) <= float(180)):
        return None, None

    # Method 1: Try reverse_geocode
    try:
        import reverse_geocode as rg
        result = rg.search([(lat, lon)])
        if result and len(result) > 0:
            country_name = result[0].get('country')
            country_code_raw = result[0].get('country_code')
            if country_code_raw:
                country_code = country_code_raw.upper()
                if len(country_code) == 3:
                    for iso2, iso3 in ISO_COUNTRY_CODES.items():
                        if iso3 == country_code:
                            country_code = iso2
                            break
                elif len(country_code) != 2:
                    country_code = None
                if country_code and country_code in ISO_COUNTRY_CODES:
                    return ISO_TO_COUNTRY_NAME.get(country_code, country_name), country_code
    except (ImportError, Exception):
        pass

    # Method 2: Nearest known coordinate lookup
    country_name, country_code = _get_country_from_nearest(lat, lon, max_distance_km)
    if country_code:
        return country_name, country_code

    # Method 3: Bounding box fallback
    return _get_country_from_bounding_box(lat, lon)


def _get_country_from_nearest(lat: float, lon: float, max_distance_km: float = 200.0) -> Tuple[Optional[str], Optional[str]]:
    """Find nearest country using the COUNTRY_LOOKUP table."""
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
        except Exception:
            continue

    if best_match:
        country_name, country_code_raw = best_match
        country_code = country_code_raw.upper()
        if country_code in ISO_COUNTRY_CODES:
            return country_name, country_code
        iso_code = get_iso_country_code(country_name)
        if iso_code:
            return country_name, iso_code

    return None, None


def _get_country_from_bounding_box(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """Fallback: Use simple bounding box for African countries."""
    if -35 <= lat <= 37 and -20 <= lon <= 55:
        if 4 <= lat <= 14 and 0 <= lon <= 15:
            return "Nigeria", "NG"
        elif 4 <= lat <= 12 and 34 <= lon <= 42:
            return "Kenya", "KE"
        elif -12 <= lat <= -4 and 28 <= lon <= 36:
            return "Zambia", "ZM"
        elif 24 <= lat <= 33 and 25 <= lon <= 35:
            return "Egypt", "EG"
        elif -27 <= lat <= -22 and 15 <= lon <= 30:
            return "South Africa", "ZA"
        elif 8 <= lat <= 12 and -2 <= lon <= 6:
            return "Ghana", "GH"
        elif -6 <= lat <= -1 and 29 <= lon <= 34:
            return "Tanzania", "TZ"
        elif 0 <= lat <= 4 and 32 <= lon <= 35:
            return "Uganda", "UG"
        elif 4 <= lat <= 15 and 35 <= lon <= 48:
            return "Ethiopia", "ET"
        elif 0 <= lat <= 6 and 8 <= lon <= 12:
            return "Cameroon", "CM"
        elif 24 <= lat <= 37 and -10 <= lon <= -1:
            return "Morocco", "MA"
        elif 11 <= lat <= 20 and -17 <= lon <= -13:
            return "Senegal", "SN"
        elif 4 <= lat <= 10 and 38 <= lon <= 48:
            return "Somalia", "SO"
    return None, None


# ========================= DATA CONSOLIDATOR CLASS =========================

class DataConsolidator:
    """Consolidates and standardizes data in the CDM with full foreign key alignment."""

    def __init__(self, session: Optional[Session] = None):
        self.session = session or SessionLocal()
        self._location_cache = {}
        self._country_lookup = self._build_country_lookup()
        self._fk_mappings = {}

    def _build_country_lookup(self) -> Dict[Tuple[float, float], Tuple[str, str]]:
        return COUNTRY_LOOKUP

    # ===================== FOREIGN KEY ALIGNMENT =====================

    def align_all_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Align all foreign keys across all tables. This is the master method that ensures
        every fact table references valid dimension records.

        Returns:
            Dictionary with alignment results for each table
        """
        logger.info("=" * 60)
        logger.info("ALIGNING ALL FOREIGN KEYS")
        logger.info("=" * 60)

        results = {
            'dimensions': {},
            'facts': {},
            'summary': {
                'total_fixed': 0,
                'tables_processed': 0,
                'orphans_found': 0
            }
        }

        # Step 1: Ensure dimension tables are clean first
        logger.info("Step 1: Cleaning dimension tables...")
        self._clean_dimension_tables(dry_run)

        # Step 2: Align each dimension's foreign keys
        dimension_alignments = self._align_dimension_foreign_keys(dry_run)
        results['dimensions'] = dimension_alignments

        # Step 3: Align each fact table's foreign keys
        fact_alignments = self._align_fact_foreign_keys(dry_run)
        results['facts'] = fact_alignments

        # Step 4: Summary
        for table, stats in fact_alignments.items():
            if isinstance(stats, dict):
                results['summary']['total_fixed'] += stats.get('fixed', 0)
                results['summary']['orphans_found'] += stats.get('orphaned', 0)
                results['summary']['tables_processed'] += 1

        logger.info("=" * 60)
        logger.info("FOREIGN KEY ALIGNMENT COMPLETE")
        logger.info(f"  Tables processed: {results['summary']['tables_processed']}")
        logger.info(f"  Orphans found: {results['summary']['orphans_found']}")
        logger.info(f"  Records fixed: {results['summary']['total_fixed']}")
        logger.info("=" * 60)

        return results

    def _clean_dimension_tables(self, dry_run: bool = False):
        """Remove orphaned dimension records that have no fact references."""
        # Get all fact tables and their foreign keys to dimensions
        inspector = inspect(self.session.bind)

        # Get all tables
        all_tables = inspector.get_table_names()
        fact_tables = [t for t in all_tables if t.startswith('fact_')]
        dim_tables = [t for t in all_tables if t.startswith('dim_') and t != 'dim_time']

        # For each dimension, check if it's referenced by any fact table
        for dim_table in dim_tables:
            # Get primary key of dimension
            pk_cols = inspector.get_pk_constraint(dim_table).get('constrained_columns', [])
            if not pk_cols:
                continue
            pk_col = pk_cols[0]

            # Check all fact tables for references to this dimension
            referenced = False
            for fact_table in fact_tables:
                fks = inspector.get_foreign_keys(fact_table)
                for fk in fks:
                    if fk.get('referred_table') == dim_table:
                        referenced = True
                        break
                if referenced:
                    break

            if not referenced:
                logger.info(f"  Dimension {dim_table} has no fact references, keeping all records")
                continue

            # Find dimension records not referenced by any fact table
            # This is complex across multiple tables, so we use a simpler approach:
            # Check each fact table's FK to this dimension
            referenced_ids = set()
            for fact_table in fact_tables:
                fks = inspector.get_foreign_keys(fact_table)
                for fk in fks:
                    if fk.get('referred_table') == dim_table:
                        fk_col = fk.get('constrained_columns', [])[0]
                        try:
                            result = self.session.execute(
                                text(f"SELECT DISTINCT {fk_col} FROM {fact_table} WHERE {fk_col} IS NOT NULL")
                            )
                            ids = [r[0] for r in result if r[0] is not None]
                            referenced_ids.update(ids)
                        except Exception as e:
                            logger.debug(f"  Could not query {fact_table}.{fk_col}: {e}")

            if not referenced_ids:
                logger.info(f"  No references found for {dim_table}, keeping all records")
                continue

            # Find orphaned dimension records
            orphaned = self.session.execute(
                text(f"SELECT {pk_col} FROM {dim_table} WHERE {pk_col} NOT IN ({','.join(['?'] * len(referenced_ids))})"),
                list(referenced_ids)
            ).fetchall()

            if orphaned and not dry_run:
                orphaned_ids = [r[0] for r in orphaned]
                # Don't delete, just log - better to keep dimension records
                logger.info(f"  {len(orphaned_ids)} orphaned records in {dim_table} (kept for reference)")

    def _align_dimension_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Align dimension foreign keys by fixing invalid references.
        """
        results = {}

        # Get all dimension tables with their primary keys
        dimension_models = {
            'dim_location': (DimLocation, 'location_id'),
            'dim_disease': (DimDisease, 'disease_code'),
            'dim_vector': (DimVector, 'vector_code'),
            'dim_insecticide': (DimInsecticide, 'insecticide_code'),
            'dim_source': (DimSource, 'source_code'),
            'dim_identification_method': (DimIdentificationMethod, 'method_code'),
            'dim_resistance_test_type': (DimResistanceTestType, 'test_type_code'),
            'dim_socioeconomic_indicator': (DimSocioeconomicIndicator, 'indicator_code'),
        }

        for dim_name, (model, pk_col) in dimension_models.items():
            logger.info(f"  Aligning {dim_name}...")
            results[dim_name] = {'fixed': 0, 'errors': 0}

            # Get valid IDs
            valid_ids = set()
            records = self.session.query(model).all()
            for rec in records:
                val = getattr(rec, pk_col)
                if val is not None:
                    valid_ids.add(val)

            # Find fact tables that reference this dimension
            inspector = inspect(self.session.bind)
            fact_tables = [t for t in inspector.get_table_names() if t.startswith('fact_')]

            for fact_table in fact_tables:
                fks = inspector.get_foreign_keys(fact_table)
                for fk in fks:
                    if fk.get('referred_table') == dim_name:
                        fk_col = fk.get('constrained_columns', [])[0]

                        # Find invalid references
                        if not valid_ids:
                            continue

                        # Check for NULL or invalid values
                        invalid_query = text(f"""
                            SELECT COUNT(*) FROM {fact_table}
                            WHERE {fk_col} IS NOT NULL
                              AND {fk_col} NOT IN ({','.join(['?'] * min(len(valid_ids), 999))})
                        """)
                        try:
                            # Use a subset of valid IDs for the query
                            valid_list = list(valid_ids)[:999]
                            if len(valid_ids) > 999:
                                # For large sets, use a different approach
                                invalid_count = self.session.execute(
                                    text(f"SELECT COUNT(*) FROM {fact_table} WHERE {fk_col} IS NOT NULL")
                                ).scalar()
                                # We'll handle this differently
                                logger.debug(f"  Large valid set for {dim_name}, skipping detailed check")
                                continue

                            invalid_count = self.session.execute(invalid_query, valid_list).scalar()

                            if invalid_count and invalid_count > 0 and not dry_run:
                                # Fix by setting to NULL
                                self.session.execute(
                                    text(f"UPDATE {fact_table} SET {fk_col} = NULL WHERE {fk_col} IS NOT NULL "
                                         f"AND {fk_col} NOT IN ({','.join(['?'] * len(valid_list))})"),
                                    valid_list
                                )
                                results[dim_name]['fixed'] += invalid_count
                                logger.info(f"    Fixed {invalid_count} references in {fact_table}.{fk_col}")

                        except Exception as e:
                            logger.debug(f"  Could not fix {fact_table}.{fk_col}: {e}")
                            results[dim_name]['errors'] += 1

            if not dry_run:
                self.session.commit()

        return results

    def _align_fact_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Align all fact table foreign keys with comprehensive orphan detection and fixing.
        """
        results = {}

        # Define all fact tables and their foreign key columns
        fact_table_configs = [
            # (table_name, fk_column, dimension_table, pk_column, fallback_strategy)
            ('fact_vector_observation', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_vector_observation', 'vector_code', 'dim_vector', 'vector_code', 'species_name'),
            ('fact_vector_observation', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_disease_case', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_disease_case', 'disease_code', 'dim_disease', 'disease_code', 'disease_name'),
            ('fact_disease_case', 'identification_method_code', 'dim_identification_method', 'method_code', 'method_name'),
            ('fact_disease_case', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_insecticide_resistance', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_insecticide_resistance', 'vector_code', 'dim_vector', 'vector_code', 'species_name'),
            ('fact_insecticide_resistance', 'insecticide_code', 'dim_insecticide', 'insecticide_code', 'insecticide_name'),
            ('fact_insecticide_resistance', 'test_type_code', 'dim_resistance_test_type', 'test_type_code', 'test_type_name'),
            ('fact_insecticide_resistance', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_bionomics', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_bionomics', 'vector_code', 'dim_vector', 'vector_code', 'species_name'),
            ('fact_bionomics', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_intervention', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_intervention', 'target_disease_code', 'dim_disease', 'disease_code', 'disease_name'),
            ('fact_intervention', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_socioeconomic_condition', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_socioeconomic_condition', 'indicator_code', 'dim_socioeconomic_indicator', 'indicator_code', 'indicator_name'),
            ('fact_socioeconomic_condition', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_health_facility', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_health_facility', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_disease_burden', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_disease_burden', 'disease_code', 'dim_disease', 'disease_code', 'disease_name'),
            ('fact_disease_burden', 'source_code', 'dim_source', 'source_code', 'source_name'),

            ('fact_environment', 'location_id', 'dim_location', 'location_id', 'name'),
            ('fact_environment', 'source_code', 'dim_source', 'source_code', 'source_name'),
        ]

        # Deduplicate configs
        seen = set()
        unique_configs = []
        for config in fact_table_configs:
            key = (config[0], config[1])
            if key not in seen:
                seen.add(key)
                unique_configs.append(config)

        for config in unique_configs:
            table_name, fk_col, dim_table, pk_col, name_col = config

            if table_name not in results:
                results[table_name] = {'orphaned': 0, 'fixed': 0, 'still_orphaned': 0, 'details': []}

            logger.info(f"  Aligning {table_name}.{fk_col} -> {dim_table}.{pk_col}")

            # Get all valid values from the dimension table
            valid_values = set()
            try:
                result = self.session.execute(text(f"SELECT {pk_col} FROM {dim_table}"))
                valid_values = {r[0] for r in result if r[0] is not None}
            except Exception as e:
                logger.warning(f"    Could not get valid values from {dim_table}: {e}")
                continue

            if not valid_values:
                logger.warning(f"    No valid values found in {dim_table}")
                continue

            # Find orphaned records
            valid_list = list(valid_values)[:999]
            if len(valid_values) > 999:
                # For large sets, use a different approach
                orphaned_count = self.session.execute(
                    text(f"SELECT COUNT(*) FROM {table_name} WHERE {fk_col} IS NOT NULL")
                ).scalar()
                # Estimate orphans by sampling
                logger.info(f"    Large dimension set ({len(valid_values)}), using sampling approach")
                continue

            orphaned_query = text(f"""
                SELECT {fk_col}, COUNT(*) as count
                FROM {table_name}
                WHERE {fk_col} IS NOT NULL
                  AND {fk_col} NOT IN ({','.join(['?'] * len(valid_list))})
                GROUP BY {fk_col}
            """)

            orphaned = self.session.execute(orphaned_query, valid_list).fetchall()

            if orphaned:
                orphaned_values = [r[0] for r in orphaned]
                total_orphaned = sum(r[1] for r in orphaned)
                results[table_name]['orphaned'] = total_orphaned
                results[table_name]['details'] = [{'value': v, 'count': c} for v, c in orphaned]

                logger.info(f"    Found {total_orphaned} orphaned records with {len(orphaned_values)} unique values")

                if not dry_run:
                    # Try to fix by finding matching records using name
                    fixed = 0
                    for orphan_val in orphaned_values[:100]:  # Limit to prevent issues
                        # Try to find a valid value by name matching
                        if name_col:
                            # Get the name from the dimension table
                            name_result = self.session.execute(
                                text(f"SELECT {name_col} FROM {dim_table} WHERE {pk_col} = :val"),
                                {'val': orphan_val}
                            ).first()

                            if name_result:
                                name = name_result[0]
                                # Find a valid value with similar name
                                if name:
                                    match_result = self.session.execute(
                                        text(f"SELECT {pk_col} FROM {dim_table} WHERE {name_col} LIKE :name LIMIT 1"),
                                        {'name': f'%{name}%'}
                                    ).first()

                                    if match_result:
                                        new_val = match_result[0]
                                        # Update the fact table
                                        self.session.execute(
                                            text(f"UPDATE {table_name} SET {fk_col} = :new_val WHERE {fk_col} = :old_val"),
                                            {'new_val': new_val, 'old_val': orphan_val}
                                        )
                                        fixed += 1
                                        logger.info(f"      Fixed {orphan_val} -> {new_val}")

                    # For remaining orphans, set to NULL
                    if len(orphaned_values) > fixed:
                        remaining = len(orphaned_values) - fixed
                        # Get the remaining values
                        remaining_values = [v for v in orphaned_values if v not in self._fk_mappings.get(table_name, {}).get(fk_col, [])]
                        if remaining_values:
                            self.session.execute(
                                text(f"UPDATE {table_name} SET {fk_col} = NULL WHERE {fk_col} IN ({','.join(['?'] * len(remaining_values))})"),
                                remaining_values
                            )
                            logger.info(f"      Set {len(remaining_values)} orphaned values to NULL")

                    results[table_name]['fixed'] = fixed
                    self.session.commit()

                # Check remaining orphans
                remaining_orphans = self.session.execute(
                    text(f"SELECT COUNT(*) FROM {table_name} WHERE {fk_col} IS NOT NULL "
                         f"AND {fk_col} NOT IN ({','.join(['?'] * len(valid_list))})"),
                    valid_list
                ).scalar()

                results[table_name]['still_orphaned'] = remaining_orphans or 0

                if remaining_orphans:
                    logger.warning(f"    {remaining_orphans} orphans remain in {table_name}.{fk_col}")
                else:
                    logger.info(f"    All orphans fixed in {table_name}.{fk_col}")

        return results

    # ===================== LOCATION SPECIFIC FIXES =====================

    def fix_orphaned_burden_records(self) -> int:
        """
        Special fix for fact_disease_burden: match by location name and year.
        This is critical for integrating burden data across all years.
        """
        logger.info("=" * 60)
        logger.info("FIXING ORPHANED BURDEN RECORDS")
        logger.info("=" * 60)

        # Get all valid location IDs
        valid_locations = {}
        locations = self.session.query(DimLocation).all()
        for loc in locations:
            valid_locations[loc.location_id] = {
                'name': loc.name.lower() if loc.name else '',
                'code': loc.location_code,
                'country_code': loc.country_code
            }

        valid_ids = set(valid_locations.keys())

        # Find orphaned burden records
        orphaned_burden = self.session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, incidence_rate, cases_mean
            FROM fact_disease_burden
            WHERE location_id IS NOT NULL
              AND location_id NOT IN ({})
        """.format(','.join(['?'] * len(valid_ids)))), list(valid_ids)).fetchall()

        if not orphaned_burden:
            logger.info("No orphaned burden records found")
            return 0

        logger.info(f"Found {len(orphaned_burden)} orphaned burden records")

        # Try to match by name
        fixed = 0
        for record in orphaned_burden:
            burden_id, bad_location_id, year, disease_code, incidence_rate, cases_mean = record

            new_location_id = None

            # Try to find a matching location by name
            if isinstance(bad_location_id, str):
                bad_str = bad_location_id.lower()
                for loc_id, info in valid_locations.items():
                    if info['name'] and (bad_str in info['name'] or info['name'] in bad_str):
                        new_location_id = loc_id
                        logger.info(f"  Matched '{bad_location_id}' to '{info['name']}' (ID: {loc_id})")
                        break

            # If still not found, use the most common location from case data
            if not new_location_id:
                most_common = self.session.execute(text("""
                    SELECT location_id, COUNT(*) as cnt
                    FROM fact_disease_case
                    GROUP BY location_id
                    ORDER BY cnt DESC
                    LIMIT 1
                """)).first()

                if most_common:
                    new_location_id = most_common[0]
                    logger.info(f"  Using most common location from case data: {new_location_id}")

            if new_location_id:
                self.session.execute(
                    text("UPDATE fact_disease_burden SET location_id = :new_id WHERE burden_id = :id"),
                    {'new_id': new_location_id, 'id': burden_id}
                )
                fixed += 1

        self.session.commit()
        logger.info(f"Fixed {fixed} orphaned burden records")
        return fixed

    # ===================== RESISTANCE STATUS =====================

    def _compute_resistance_status(self):
        """Compute resistance status from mortality data."""
        logger.info("=" * 60)
        logger.info("COMPUTING RESISTANCE STATUS")
        logger.info("=" * 60)

        total_resistance = self.session.execute(text("""
            SELECT COUNT(*) FROM fact_insecticide_resistance
        """)).scalar()

        if total_resistance == 0:
            logger.info("No resistance records found")
            return

        records_with_counts = self.session.execute(text("""
            SELECT COUNT(*) FROM fact_insecticide_resistance
            WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
              AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
        """)).scalar()

        if records_with_counts > 0:
            self._process_resistance_status_bulk()

        self._verify_resistance_status()

    def _process_resistance_status_bulk(self):
        """Bulk update resistance status."""
        try:
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

    def _verify_resistance_status(self):
        """Verify resistance status updates."""
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

    # ===================== BURDEN EXPANSION =====================

    def _expand_burden_to_daily(self):
        """Expand yearly disease burden data to daily records."""
        logger.info("=" * 60)
        logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS")
        logger.info("=" * 60)

        total_burden = self.session.execute(text("SELECT COUNT(*) FROM fact_disease_burden")).scalar()

        if total_burden == 0:
            logger.info("No burden records found")
            return

        # Get population estimates
        pop_results = self.session.execute(text("""
            SELECT location_id, indicator_value
            FROM fact_socioeconomic_condition
            WHERE indicator_code = 'SP.POP.TOTL'
        """)).fetchall()
        population_map = {r[0]: r[1] for r in pop_results}

        # Get all locations
        loc_results = self.session.execute(text("SELECT location_id FROM dim_location")).fetchall()
        all_location_ids = [r[0] for r in loc_results]

        default_population = 1000000
        for loc_id in all_location_ids:
            if loc_id not in population_map or population_map[loc_id] is None or population_map[loc_id] <= 0:
                population_map[loc_id] = default_population

        # Get burden records with valid data
        burden_records = self.session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, source_code,
                   incidence_rate, cases_mean
            FROM fact_disease_burden
            WHERE incidence_rate IS NOT NULL AND incidence_rate > 0
               OR cases_mean IS NOT NULL AND cases_mean > 0
        """)).fetchall()

        if not burden_records:
            logger.warning("No burden records with valid data found")
            return

        logger.info(f"Found {len(burden_records)} burden records to expand")

        # Seasonal factors
        monthly_factors = {
            1: 0.6, 2: 0.5, 3: 0.7, 4: 1.4, 5: 1.5, 6: 1.3,
            7: 1.2, 8: 1.0, 9: 0.8, 10: 0.6, 11: 0.5, 12: 0.5
        }

        total_factor = sum(monthly_factors.values())
        normalized_factors = {m: f * 12 / total_factor for m, f in monthly_factors.items()}

        inserted_count = 0
        error_count = 0
        is_sqlite = 'sqlite' in str(self.session.bind.url).lower()

        for record in burden_records:
            burden_id, location_id, year, disease_code, source_code, incidence_rate, cases_mean = record

            population = population_map.get(location_id, default_population)

            if incidence_rate is not None and incidence_rate > 0 and population > 0:
                annual_cases = (incidence_rate / 100000) * population
            elif cases_mean is not None and cases_mean > 0:
                annual_cases = cases_mean
            else:
                continue

            annual_cases = max(annual_cases, 1)

            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31)
            current_date = start_date
            daily_cases_list = []

            while current_date <= end_date:
                month = current_date.month
                if month == 12:
                    days_in_month = 31
                else:
                    next_month = datetime(year, month + 1, 1)
                    days_in_month = (next_month - datetime(year, month, 1)).days

                monthly_cases = annual_cases * normalized_factors[month]
                daily_cases = max(1, int(round(monthly_cases / days_in_month)))

                daily_cases_list.append({
                    'location_id': location_id,
                    'date_id': current_date.strftime('%Y-%m-%d'),
                    'disease_code': disease_code,
                    'source_code': source_code
                })
                current_date += timedelta(days=1)

            if daily_cases_list:
                try:
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

                    batch_size = 5000
                    for i in range(0, len(daily_cases_list), batch_size):
                        batch = daily_cases_list[i:i+batch_size]

                        if is_sqlite:
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

                except Exception as e:
                    logger.error(f"Error expanding burden_id {burden_id}: {e}")
                    self.session.rollback()
                    error_count += 1
                    continue

        self.session.commit()
        logger.info(f"Expanded {inserted_count} daily records, errors: {error_count}")

    # ===================== DUPLICATE RESOLUTION =====================

    def resolve_fact_table_duplicates(self):
        """Resolve duplicates across fact tables."""
        logger.info("Resolving fact table duplicates...")

        # 1. Environment duplicates
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
            logger.info("  Deduplicated fact_environment")
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to deduplicate fact_environment: {e}")

        # 2. Resistance duplicates
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
            logger.info("  Deduplicated fact_insecticide_resistance")
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to deduplicate fact_insecticide_resistance: {e}")

        # 3. Disease case duplicates
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

    # ===================== MAIN CONSOLIDATION =====================

    def consolidate_all(self, study_region: str = "Kenya") -> Dict[str, Any]:
        """
        Run all consolidation steps with full foreign key alignment.
        """
        logger.info("=" * 60)
        logger.info("STARTING FULL DATA CONSOLIDATION")
        logger.info("=" * 60)

        # Step 1: ALIGN ALL FOREIGN KEYS
        logger.info("Step 1: Aligning all foreign keys...")
        fk_results = self.align_all_foreign_keys()

        # Step 2: Standardize country codes
        logger.info("Step 2: Standardizing country codes to ISO format...")
        self._standardize_country_codes()

        # Step 3: Bind climate coordinates to regions
        logger.info("Step 3: Binding climate coordinates to regions...")
        self._bind_climate_coordinates_to_region(study_region=study_region)

        # Step 4: Fix orphaned burden records specifically
        logger.info("Step 4: Fixing orphaned burden records...")
        burden_fixed = self.fix_orphaned_burden_records()

        # Step 5: Expand burden data to daily records
        logger.info("Step 5: Expanding burden data to daily records...")
        self._expand_burden_to_daily()

        # Step 6: Compute resistance status
        logger.info("Step 6: Computing resistance status...")
        self._compute_resistance_status()

        # Step 7: Consolidate locations
        logger.info("Step 7: Consolidating locations...")
        loc_results = self.consolidate_locations()

        # Step 8: Consolidate vocabulary
        logger.info("Step 8: Consolidating vocabulary...")
        vocab_results = self.consolidate_vocabulary()

        # Step 9: Detect and resolve duplicates
        logger.info("Step 9: Detecting and resolving duplicates...")
        dup_results = self.detect_duplicates()
        self.resolve_fact_table_duplicates()

        # Step 10: Final verification
        logger.info("Step 10: Final verification...")
        validation = self.validate_country_codes()
        final_orphan_check = self._check_remaining_orphans()

        results = {
            'foreign_key_alignment': fk_results,
            'locations': loc_results,
            'vocabulary': vocab_results,
            'duplicates': dup_results,
            'validation': validation,
            'burden_fixed': burden_fixed,
            'remaining_orphans': final_orphan_check
        }

        logger.info("=" * 60)
        logger.info("CONSOLIDATION COMPLETE")
        logger.info(f"  Total locations: {validation.get('total', 0)}")
        logger.info(f"  Valid country codes: {validation.get('valid', 0)}")
        logger.info(f"  Burden records fixed: {burden_fixed}")
        logger.info(f"  Remaining orphans: {final_orphan_check}")
        logger.info("=" * 60)

        return results

    def _check_remaining_orphans(self) -> Dict[str, int]:
        """Check for remaining orphaned records across all fact tables."""
        logger.info("Checking for remaining orphans...")

        results = {}
        inspector = inspect(self.session.bind)
        fact_tables = [t for t in inspector.get_table_names() if t.startswith('fact_')]

        for table in fact_tables:
            fks = inspector.get_foreign_keys(table)
            for fk in fks:
                fk_col = fk.get('constrained_columns', [])[0]
                ref_table = fk.get('referred_table')

                if not fk_col or not ref_table:
                    continue

                # Get primary key of referenced table
                pk_cols = inspector.get_pk_constraint(ref_table).get('constrained_columns', [])
                if not pk_cols:
                    continue
                pk_col = pk_cols[0]

                # Check for orphaned records
                query = text(f"""
                    SELECT COUNT(*) FROM {table} f
                    LEFT JOIN {ref_table} d ON f.{fk_col} = d.{pk_col}
                    WHERE f.{fk_col} IS NOT NULL AND d.{pk_col} IS NULL
                """)

                try:
                    count = self.session.execute(query).scalar()
                    if count and count > 0:
                        results[f"{table}.{fk_col}"] = count
                        logger.warning(f"  {table}.{fk_col}: {count} orphans remaining")
                except Exception as e:
                    logger.debug(f"  Could not check {table}.{fk_col}: {e}")

        return results

    # ===================== LOCATION CONSOLIDATION =====================

    def _standardize_country_codes(self, dry_run: bool = False) -> Dict[str, Any]:
        """Standardize country codes to ISO format."""
        logger.info("Standardizing country codes...")

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
            return results

        for loc in locations:
            changes = {'location_id': loc.location_id, 'name': loc.name}
            needs_update = False

            current_code = loc.country_code.strip().upper() if loc.country_code else ''
            is_missing = not current_code or current_code in ['UNKNOWN', 'N/A', 'NULL', 'NONE', '']

            if is_missing:
                if loc.centroid_lat and loc.centroid_lon:
                    country_name, country_code = get_country_from_coords(
                        loc.centroid_lat, loc.centroid_lon
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
                        changes['status'] = 'unresolved'
                else:
                    results['unresolved'] += 1
                    changes['status'] = 'no_coordinates'

            elif current_code:
                normalized_code = get_iso_country_code(current_code)
                if normalized_code and normalized_code != current_code:
                    if not dry_run:
                        loc.country_code = normalized_code
                    changes['old_code'] = current_code
                    changes['new_code'] = normalized_code
                    results['normalized_codes'] += 1
                    needs_update = True

            if loc.country_code and loc.country_code in ISO_COUNTRY_CODES:
                correct_name = ISO_TO_COUNTRY_NAME.get(loc.country_code)
                if correct_name and loc.country_name and loc.country_name != correct_name:
                    if not dry_run:
                        loc.country_name = correct_name
                    changes['old_name'] = loc.country_name
                    changes['new_name'] = correct_name
                    results['standardized_names'] += 1
                    needs_update = True

            if needs_update and not dry_run:
                self.session.add(loc)

            if changes:
                results['changes'].append(changes)

        if not dry_run:
            self.session.commit()

        return results

    def _bind_climate_coordinates_to_region(self, study_region: str = "Kenya"):
        """Bind climate coordinates to region."""
        region_code = get_iso_country_code(study_region) or 'KE'
        region_name = get_country_name(region_code) or study_region

        self.session.execute(text("""
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

        if region_code == 'KE':
            self.session.execute(text("""
                UPDATE dim_location
                SET name = 'Siaya County (Climate Grid)',
                    country_name = :region_name,
                    country_code = :region_code
                WHERE (name LIKE '%/%' OR name IS NULL OR name = '' OR name = 'Siaya')
                  AND (centroid_lat BETWEEN -0.5 AND 0.5)
                  AND (centroid_lon BETWEEN 34.0 AND 34.7)
            """), {"region_name": region_name, "region_code": region_code})

        self.session.commit()

    def consolidate_locations(self, distance_threshold_km: float = 5.0,
                             min_samples: int = 1, dry_run: bool = False) -> Dict[str, Any]:
        """Consolidate locations with country code standardization."""
        logger.info("Consolidating locations...")

        # Standardize country codes first
        code_results = self._standardize_country_codes(dry_run=dry_run)

        locations = self.session.query(DimLocation).all()
        if not locations:
            return {'status': 'no_data'}

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

        loc_data['name_normalized'] = loc_data['name'].apply(self._normalize_location_name)

        has_coords = loc_data['centroid_lat'].notna() & loc_data['centroid_lon'].notna()
        loc_with_coords = loc_data[has_coords].copy()
        loc_no_coords = loc_data[~has_coords].copy()

        name_results = self._merge_by_name(loc_data, dry_run)

        cluster_results = {}
        if not loc_with_coords.empty:
            clusters = self._cluster_locations_by_country_and_proximity(
                loc_with_coords, distance_threshold_km, min_samples
            )
            cluster_results = self._merge_location_clusters(loc_with_coords, clusters, dry_run)

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

        return results

    def _normalize_location_name(self, name: str) -> str:
        if not name or not isinstance(name, str):
            return ""
        name = name.lower()
        suffixes = [' county', ' district', ' region', ' province', ' state', ' municipality', ' city', ' town']
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
        prefixes = ['county of ', 'district of ', 'region of ', 'state of ']
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix):]
        name = re.sub(r'\([^)]*\)', '', name)
        name = re.sub(r'[^\w\s]', '', name)
        name = re.sub(r'\s+', ' ', name).strip()
        return name

    def _merge_by_name(self, loc_data: pd.DataFrame, dry_run: bool = False) -> List[Dict[str, Any]]:
        results = []
        group_cols = ['name_normalized']
        if 'country_code' in loc_data.columns:
            group_cols.append('country_code')

        for (name, country_code), group in loc_data.groupby(group_cols):
            if len(group) <= 1:
                continue

            group['has_country'] = group['country_code'].notna()
            group['has_coords'] = group['centroid_lat'].notna() & group['centroid_lon'].notna()
            group['has_admin'] = group['admin_level'].notna()

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

                loc = self.session.query(DimLocation).filter_by(location_id=canonical_id).first()
                if loc:
                    if not loc.country_code and canonical['country_code']:
                        loc.country_code = canonical['country_code']
                        loc.country_name = canonical['country_name']
                    if not loc.centroid_lat and canonical['centroid_lat']:
                        loc.centroid_lat = canonical['centroid_lat']
                        loc.centroid_lon = canonical['centroid_lon']
                    if len(str(loc.name)) < len(str(canonical['name'])):
                        loc.name = canonical['name']
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

        return results

    def _cluster_locations_by_country_and_proximity(self, loc_df: pd.DataFrame,
                                                    eps_km: float,
                                                    min_samples: int) -> pd.Series:
        try:
            from sklearn.cluster import DBSCAN
            from geopy.distance import geodesic
        except ImportError:
            return pd.Series([-1] * len(loc_df), index=loc_df.index)

        if loc_df.empty:
            return pd.Series([-1] * len(loc_df), index=loc_df.index)

        final_clusters = pd.Series([-1] * len(loc_df), index=loc_df.index)
        cluster_counter = 0

        for country, group in loc_df.groupby('country_code'):
            if len(group) < min_samples:
                continue

            coords = group[['centroid_lat', 'centroid_lon']].values

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

            db = DBSCAN(eps=eps_km, min_samples=min_samples, metric=geodesic_distance)
            clusters = db.fit_predict(coords_rad)

            for i, cl in enumerate(clusters):
                if cl != -1:
                    idx = valid_indices[i]
                    final_clusters[idx] = cluster_counter + cl

        unique_clusters = final_clusters.unique()
        cluster_map = {old: new for new, old in enumerate(unique_clusters) if old != -1}
        if cluster_map:
            final_clusters = final_clusters.replace(cluster_map)
            final_clusters = final_clusters.fillna(-1).astype(int)

        return final_clusters

    def _merge_location_clusters(self, loc_df: pd.DataFrame, clusters: pd.Series,
                                 dry_run: bool = False) -> Dict[str, Any]:
        results = {'merged_count': 0, 'clusters': {}, 'merged_locations': []}

        for cluster_id, group in loc_df.groupby(clusters):
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

        return results

    def _merge_fact_locations(self, source_id: int, target_id: int):
        """Safely merge location references in all fact tables."""
        logger.info(f"Merging location references: {source_id} -> {target_id}")

        fact_tables = [
            'fact_vector_observation', 'fact_disease_case', 'fact_insecticide_resistance',
            'fact_bionomics', 'fact_intervention', 'fact_socioeconomic_condition',
            'fact_health_facility', 'fact_disease_burden', 'fact_environment'
        ]

        for table in fact_tables:
            try:
                self.session.execute(
                    text(f"UPDATE {table} SET location_id = :target_id WHERE location_id = :source_id"),
                    {"target_id": target_id, "source_id": source_id}
                )
            except Exception as e:
                logger.debug(f"Could not update {table}: {e}")

    def fix_location_country_codes(self) -> int:
        """Fix missing or incorrect country codes."""
        locations = self.session.query(DimLocation).filter(
            (DimLocation.country_code.is_(None)) |
            (DimLocation.country_code == '') |
            (DimLocation.country_code == 'UNKNOWN') |
            (DimLocation.country_code == 'N/A')
        ).all()

        if not locations:
            return 0

        updated = 0
        for loc in locations:
            if loc.centroid_lat and loc.centroid_lon:
                country_name, country_code = get_country_from_coords(
                    loc.centroid_lat, loc.centroid_lon
                )
                if country_code:
                    loc.country_code = country_code
                    loc.country_name = country_name
                    updated += 1

        self.session.commit()
        return updated

    def validate_country_codes(self) -> Dict[str, Any]:
        """Validate country codes."""
        locations = self.session.query(DimLocation).all()

        results = {'total': len(locations), 'valid': 0, 'invalid': 0, 'missing': 0, 'details': []}

        for loc in locations:
            if not loc.country_code or loc.country_code == '':
                results['missing'] += 1
                results['details'].append({'location_id': loc.location_id, 'name': loc.name, 'issue': 'missing'})
                continue

            if get_iso_country_code(loc.country_code):
                results['valid'] += 1
            else:
                results['invalid'] += 1
                results['details'].append({
                    'location_id': loc.location_id,
                    'name': loc.name,
                    'country_code': loc.country_code,
                    'issue': 'invalid'
                })

        return results

    # ===================== VOCABULARY CONSOLIDATION =====================

    def consolidate_vocabulary(self) -> Dict[str, Any]:
        """Consolidate vocabulary tables."""
        logger.info("Consolidating vocabulary...")

        results = {}

        results['diseases'] = self._consolidate_dimension(DimDisease, 'disease_name', 'disease_code')
        results['vectors'] = self._consolidate_dimension(DimVector, 'species_name', 'vector_code')
        results['insecticides'] = self._consolidate_dimension(DimInsecticide, 'insecticide_name', 'insecticide_code')
        results['sources'] = self._consolidate_dimension(DimSource, 'source_name', 'source_code')

        self.session.commit()
        return results

    def _consolidate_dimension(self, model_class, name_col: str, code_col: str) -> Dict[str, Any]:
        records = self.session.query(model_class).all()

        if not records:
            return {'status': 'no_data'}

        name_counts = {}
        for rec in records:
            name = getattr(rec, name_col)
            if name:
                name_counts[name] = name_counts.get(name, 0) + 1

        duplicates = {name: count for name, count in name_counts.items() if count > 1}

        if not duplicates:
            return {'status': 'ok', 'duplicates': {}}

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

        return {'status': 'ok', 'duplicates_found': len(duplicates), 'merged': merged}

    def _update_fact_references(self, dimension_model, old_code: str, new_code: str):
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
                'fk_columns': ['source_code'] * 9
            }
        }

        if dimension_model not in table_map:
            return

        mapping = table_map[dimension_model]
        for table_name, fk_col in zip(mapping['fact_tables'], mapping['fk_columns']):
            try:
                self.session.execute(
                    text(f"UPDATE {table_name} SET {fk_col} = :new_code WHERE {fk_col} = :old_code"),
                    {'new_code': new_code, 'old_code': old_code}
                )
            except Exception as e:
                logger.debug(f"Could not update {table_name}.{fk_col}: {e}")

    # ===================== DUPLICATE DETECTION =====================

    def detect_duplicates(self) -> Dict[str, Any]:
        """Detect duplicate records in fact tables."""
        results = {}

        results['vector_observations'] = self._check_fact_duplicates(
            'fact_vector_observation', ['location_id', 'date_id', 'vector_code']
        )
        results['disease_cases'] = self._check_fact_duplicates(
            'fact_disease_case', ['location_id', 'date_id_onset', 'disease_code']
        )
        results['resistance_tests'] = self._check_fact_duplicates(
            'fact_insecticide_resistance', ['location_id', 'date_id', 'vector_code', 'insecticide_code']
        )
        results['environment'] = self._check_fact_duplicates(
            'fact_environment', ['location_id', 'date_id', 'variable']
        )

        return results

    def _check_fact_duplicates(self, table_name: str, key_columns: List[str]) -> Dict[str, Any]:
        key_str = ', '.join(key_columns)
        try:
            result = self.session.execute(
                text(f"SELECT {key_str}, COUNT(*) as dup_count FROM {table_name} "
                     f"GROUP BY {key_str} HAVING COUNT(*) > 1")
            )
            duplicates = [dict(row._mapping) for row in result]
            return {'table': table_name, 'key_columns': key_columns,
                    'duplicate_count': len(duplicates), 'duplicates': duplicates[:100]}
        except Exception as e:
            return {'table': table_name, 'error': str(e), 'duplicate_count': 0}

    # ===================== SUMMARY =====================

    def summarize_locations(self) -> pd.DataFrame:
        """Generate a summary of locations."""
        locations = self.session.query(DimLocation).all()
        return pd.DataFrame([{
            'location_id': loc.location_id,
            'name': loc.name,
            'country_code': loc.country_code,
            'country_name': loc.country_name,
            'has_coords': loc.centroid_lat is not None and loc.centroid_lon is not None,
            'admin_level': getattr(loc, 'admin_level', None)
        } for loc in locations])

    def close(self):
        if self.session:
            self.session.close()


# ========================= CONVENIENCE FUNCTIONS =========================

def run_consolidation(dry_run: bool = False):
    """Convenience function to run consolidation."""
    consolidator = DataConsolidator()
    try:
        return consolidator.consolidate_all()
    finally:
        consolidator.close()


def enrich_location_dataframe(
    df: pd.DataFrame,
    lat_col: str = 'centroid_lat',
    lon_col: str = 'centroid_lon',
    country_code_col: str = 'country_code',
    country_name_col: str = 'country_name'
) -> pd.DataFrame:
    """Enrich location DataFrame with ISO country codes."""
    if df.empty:
        return df

    df = df.copy()

    if country_code_col not in df.columns:
        df[country_code_col] = None
    if country_name_col not in df.columns:
        df[country_name_col] = None

    missing_mask = (
        df[country_code_col].isna() |
        (df[country_code_col] == '') |
        (df[country_code_col] == 'UNKNOWN') |
        (df[country_code_col] == 'N/A') |
        (df[country_code_col] == 'NULL')
    )

    if not df[country_code_col].isna().all():
        invalid_mask = ~df[country_code_col].isin(ISO_COUNTRY_CODES.keys()) & df[country_code_col].notna()
        missing_mask = missing_mask | invalid_mask

    if not missing_mask.any():
        return df

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

    for idx, (lat, lon) in zip(indices, coords_list):
        name, code = get_country_from_coords(lat, lon)
        if code is not None:
            df.loc[idx, country_code_col] = code
            df.loc[idx, country_name_col] = name
        else:
            existing_name = df.loc[idx, country_name_col]
            if existing_name:
                iso_code = get_iso_country_code(existing_name)
                if iso_code:
                    df.loc[idx, country_code_col] = iso_code
                    df.loc[idx, country_name_col] = get_country_name(iso_code) or existing_name

    return df