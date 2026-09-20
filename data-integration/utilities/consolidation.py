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
from sqlalchemy.exc import IntegrityError
from sqlalchemy import bindparam
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
from utilities.vocabulary.loader import VocabularyLoader, match_value
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_IN_VARS = 900
_GRID_NAME_RE = re.compile(r'^\s*-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?\s*$')

# Dimensions whose natural-key duplicates should cascade into fact tables
# when consolidated. Maps model class - (fact_table, fk_column) pairs.
_DIMENSION_FACT_MAP = {
    DimDisease: {
        'fact_tables': ['fact_disease_case', 'fact_disease_burden', 'fact_intervention'],
        'fk_columns':  ['disease_code', 'disease_code', 'target_disease_code'],
    },
    DimVector: {
        'fact_tables': ['fact_vector_observation', 'fact_insecticide_resistance', 'fact_bionomics'],
        'fk_columns':  ['vector_code', 'vector_code', 'vector_code'],
    },
    DimInsecticide: {
        'fact_tables': ['fact_insecticide_resistance'],
        'fk_columns':  ['insecticide_code'],
    },
    DimSource: {
        'fact_tables': [
            'fact_vector_observation', 'fact_disease_case', 'fact_insecticide_resistance',
            'fact_bionomics', 'fact_intervention', 'fact_socioeconomic_condition',
            'fact_health_facility', 'fact_disease_burden', 'fact_environment',
        ],
        'fk_columns': ['source_code'] * 9,
    },
}


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


def _chunked_in(
    session,
    base_sql: str,
    static_params: dict,
    values,
    expanding_name: str = "vals",
    chunk_size: int = MAX_IN_VARS,
):
    """
    Run a statement containing `IN :vals` in chunks of values.

    Usage:
        rows = _chunked_in(
            session,
            "SELECT location_id FROM fact_environment "
            "WHERE location_id IN :vals",
            {},
            valid_ids,
        )
    """
    values = list(values)
    if not values:
        return []
    stmt = text(base_sql).bindparams(bindparam(expanding_name, expanding=True))
    out = []
    for i in range(0, len(values), chunk_size):
        chunk = values[i:i + chunk_size]
        params = dict(static_params)
        params[expanding_name] = chunk
        out.extend(session.execute(stmt, params).fetchall())
    return out


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
def get_country_from_coords(
    lat: float,
    lon: float,
    max_distance_km: float = 200.0,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Get ISO country name and code from latitude and longitude coordinates.

    Resolution order:
        1. reverse_geocode (requires internet, most accurate)
        2. Nearest coordinate in COUNTRY_LOOKUP (Haversine, offline)
        3. Bounding-box fallback for well-known African regions

    Returns (None, None) if the coordinates are missing, non-numeric,
    out of range, or cannot be resolved by any method.
    """
    # 1. Null / NaN guard (covers None, float('nan'), pd.NA, np.nan)
    try:
        if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
            return None, None
    except (TypeError, ValueError):
        return None, None

    # 2. Numeric guard — FIXED. Previously this condition was inverted,
    #    so valid coordinates were rejected and invalid ones accepted.
    if not is_real_number_robust(str(lat)) or not is_real_number_robust(str(lon)):
        return None, None

    # 3. Coerce to float once and reuse.
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None, None

    # 4. Bounds check.
    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
        return None, None

    # 5. Method 1: reverse_geocode (online).
    try:
        import reverse_geocode as rg
        result = rg.search([(lat_f, lon_f)])
        if result:
            country_name = result[0].get("country")
            country_code_raw = result[0].get("country_code")
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
                    return (
                        ISO_TO_COUNTRY_NAME.get(country_code, country_name),
                        country_code,
                    )
    except ImportError:
        # reverse_geocode not installed — fall through to offline methods.
        pass
    except Exception as exc:  # network / lookup error
        logger.debug(f"reverse_geocode failed for ({lat_f}, {lon_f}): {exc}")

    # 6. Method 2: nearest known coordinate.
    country_name, country_code = _get_country_from_nearest(lat_f, lon_f, max_distance_km)
    if country_code:
        return country_name, country_code

    # 7. Method 3: bounding box fallback.
    return _get_country_from_bounding_box(lat_f, lon_f)
    

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
            orphaned = _chunked_in(
                self.session,
                f"SELECT {pk_col} FROM {dim_table} WHERE {pk_col} NOT IN :vals",
                {},
                referenced_ids,
            )

            if orphaned and not dry_run:
                orphaned_ids = [r[0] for r in orphaned]
                # Don't delete, just log - better to keep dimension records
                logger.info(f"  {len(orphaned_ids)} orphaned records in {dim_table} (kept for reference)")

    
    def _align_dimension_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Align FK references to dimension tables by NULLing invalid values.

        NOT NULL columns are skipped here — they are handled by
        _align_fact_foreign_keys, which knows how to redirect or delete
        orphan rows. Attempting to NULL them would raise IntegrityError.
        """
        results = {}

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

        inspector = inspect(self.session.bind)
        fact_tables = [t for t in inspector.get_table_names() if t.startswith('fact_')]

        for dim_name, (model, pk_col) in dimension_models.items():
            logger.info(f"  Aligning {dim_name}...")
            results[dim_name] = {'fixed': 0, 'errors': 0, 'skipped_notnull': 0}

            valid_ids = set()
            for rec in self.session.query(model).all():
                val = getattr(rec, pk_col)
                if val is not None:
                    valid_ids.add(val)

            if not valid_ids:
                continue

            valid_list = list(valid_ids)

            for fact_table in fact_tables:
                fks = inspector.get_foreign_keys(fact_table)
                for fk in fks:
                    if fk.get('referred_table') != dim_name:
                        continue
                    fk_col = fk.get('constrained_columns', [])[0]
                    if not fk_col:
                        continue

                    col_info = {c['name']: c for c in inspector.get_columns(fact_table)}
                    nullable = col_info.get(fk_col, {}).get('nullable', True)
                    if not nullable:
                        logger.debug(
                            f"    Skipping {fact_table}.{fk_col}: NOT NULL, "
                            f"handled by fact-level aligner"
                        )
                        results[dim_name]['skipped_notnull'] += 1
                        continue

                    try:
                        invalid_count = 0
                        for i in range(0, len(valid_list), MAX_IN_VARS):
                            chunk = valid_list[i:i + MAX_IN_VARS]
                            stmt = text(
                                f"SELECT COUNT(*) FROM {fact_table} "
                                f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals"
                            ).bindparams(bindparam("vals", expanding=True))
                            invalid_count += self.session.execute(stmt, {"vals": chunk}).scalar() or 0

                        if invalid_count == 0:
                            continue

                        if dry_run:
                            results[dim_name]['fixed'] += invalid_count
                            continue

                        for i in range(0, len(valid_list), MAX_IN_VARS):
                            chunk = valid_list[i:i + MAX_IN_VARS]
                            stmt = text(
                                f"UPDATE {fact_table} SET {fk_col} = NULL "
                                f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals"
                            ).bindparams(bindparam("vals", expanding=True))
                            self.session.execute(stmt, {"vals": chunk})

                        results[dim_name]['fixed'] += invalid_count
                        logger.info(f"    Nulled {invalid_count} invalid refs in {fact_table}.{fk_col}")

                    except Exception as e:
                        logger.warning(f"    Could not align {fact_table}.{fk_col}: {e}")
                        results[dim_name]['errors'] += 1

            if not dry_run:
                self.session.commit()

        return results
    

    def _align_fact_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Align all fact table foreign keys with orphan detection and fixing.

        Strategy per (fact_table.fk_col -> dim_table.pk_col):
        1. Build the set of valid FK values from the dimension table.
        2. Query orphans in chunks.
        3. For each orphan value, try name-based repair via dim_table.name_col.
            - Bulk UPDATE first. On unique-constraint collision, fall back to
            row-by-row: update rows that can be reassigned; delete rows that
            duplicate an already valid row.
        4. Resolve any remaining orphans:
            - If fk_col is nullable: set to NULL.
            - If fk_col is NOT NULL: delete the rows and log the count.
        5. Recount remaining orphans.
        """
        from collections import Counter

        results = {}

        fact_table_configs = [
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

        # Deduplicate (table, fk_col) pairs.
        seen = set()
        unique_configs = []
        for cfg in fact_table_configs:
            key = (cfg[0], cfg[1])
            if key not in seen:
                seen.add(key)
                unique_configs.append(cfg)

        inspector = inspect(self.session.bind)

        for table_name, fk_col, dim_table, pk_col, name_col in unique_configs:
            if table_name not in results:
                results[table_name] = {
                    'orphaned': 0, 'fixed': 0,
                    'still_orphaned': 0, 'details': [],
                }

            logger.info(f"  Aligning {table_name}.{fk_col} -> {dim_table}.{pk_col}")

            # --- 1. Valid FK values ------------------------------------------
            try:
                rows = self.session.execute(text(f"SELECT {pk_col} FROM {dim_table}")).fetchall()
                valid_values = {r[0] for r in rows if r[0] is not None}
            except Exception as e:
                logger.warning(f"    Could not get valid values from {dim_table}: {e}")
                continue

            if not valid_values:
                logger.warning(f"    No valid values found in {dim_table}")
                continue

            valid_list = list(valid_values)

            # --- 2. Orphans (chunked) ----------------------------------------
            orphan_counter: Counter = Counter()
            query_error = False
            for i in range(0, len(valid_list), MAX_IN_VARS):
                chunk = valid_list[i:i + MAX_IN_VARS]
                stmt = text(
                    f"SELECT {fk_col}, COUNT(*) FROM {table_name} "
                    f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals "
                    f"GROUP BY {fk_col}"
                ).bindparams(bindparam("vals", expanding=True))
                try:
                    for value, count in self.session.execute(stmt, {"vals": chunk}).fetchall():
                        orphan_counter[value] += count
                except Exception as e:
                    logger.warning(f"    Could not query orphans for {table_name}.{fk_col}: {e}")
                    query_error = True
                    break

            if query_error:
                continue

            if not orphan_counter:
                continue

            # --- Re-verify candidates against the FULL valid set ------------
            # The chunked query above produces false positives: a value V that
            # IS in valid_list lives in exactly one chunk C. Every OTHER chunk
            # runs `NOT IN chunk_X`, which does not exclude V, so V shows up
            # as an "orphan" for every chunk except its own. Re-verify each
            # candidate against dim_table as a whole before trusting it.
            candidates = list(orphan_counter.keys())
            truly_orphan: Counter = Counter()
            for i in range(0, len(candidates), MAX_IN_VARS):
                chunk = candidates[i:i + MAX_IN_VARS]
                present_stmt = text(
                    f"SELECT {pk_col} FROM {dim_table} WHERE {pk_col} IN :vals"
                ).bindparams(bindparam("vals", expanding=True))
                present = {
                    r[0] for r in self.session.execute(present_stmt, {"vals": chunk}).fetchall()
                }
                for val in chunk:
                    if val not in present:
                        truly_orphan[val] = orphan_counter[val]

            orphan_counter = truly_orphan
            if not orphan_counter:
                continue

            orphaned_values = list(orphan_counter.keys())
            total_orphaned = sum(orphan_counter.values())

            results[table_name]['orphaned'] = total_orphaned
            results[table_name]['details'] = [
                {'value': v, 'count': orphan_counter[v]} for v in orphaned_values[:100]
            ]

            logger.info(
                f"    Found {total_orphaned} orphaned records "
                f"with {len(orphaned_values)} unique values"
            )

            if dry_run:
                continue

            # PK column of the fact table (for row-by-row fallback).
            pk_cols_list = inspector.get_pk_constraint(table_name).get('constrained_columns', [])
            single_pk = pk_cols_list[0] if len(pk_cols_list) == 1 else None

            # --- 3. Repair ---------------------------------------------------
            fixed_values: List[Any] = []
            remaining_values: List[Any] = []

            for orphan_val in orphaned_values:
                new_val = None
                
                if name_col and isinstance(orphan_val, str):
                    try:
                        match_result = self.session.execute(
                            text(f"SELECT {pk_col} FROM {dim_table} "
                                f"WHERE LOWER({name_col}) LIKE LOWER(:pat) LIMIT 1"),
                            {'pat': f'%{orphan_val}%'}
                        ).first()
                        if match_result:
                            new_val = match_result[0]
                    except Exception as e:
                        logger.debug(f"    Name-based repair failed for {orphan_val}: {e}")

                if new_val is None:
                    remaining_values.append(orphan_val)
                    continue

                # Try the bulk UPDATE first.
                try:
                    self.session.execute(
                        text(
                            f"UPDATE {table_name} SET {fk_col} = :new_val "
                            f"WHERE {fk_col} = :old_val"
                        ),
                        {'new_val': new_val, 'old_val': orphan_val}
                    )
                    self.session.commit()
                    fixed_values.append(orphan_val)
                    continue
                except IntegrityError:
                    self.session.rollback()

                if single_pk is None:
                    logger.warning(
                        f"      Bulk redirect {orphan_val} -> {new_val} collided and "
                        f"{table_name} has no single-column PK; leaving rows as orphans"
                    )
                    remaining_values.append(orphan_val)
                    continue

                # Row-by-row fallback.
                try:
                    row_ids = [
                        r[0] for r in self.session.execute(
                            text(f"SELECT {single_pk} FROM {table_name} WHERE {fk_col} = :old_val"),
                            {'old_val': orphan_val}
                        ).fetchall()
                    ]
                except Exception as e:
                    logger.warning(f"      Could not list rows to fix for {orphan_val}: {e}")
                    remaining_values.append(orphan_val)
                    continue

                updated = 0
                deleted = 0
                still_orphan = False

                for row_id in row_ids:
                    try:
                        self.session.execute(
                            text(
                                f"UPDATE {table_name} SET {fk_col} = :new_val "
                                f"WHERE {single_pk} = :row_id"
                            ),
                            {'new_val': new_val, 'row_id': row_id}
                        )
                        self.session.commit()
                        updated += 1
                    except IntegrityError:
                        self.session.rollback()
                        try:
                            self.session.execute(
                                text(f"DELETE FROM {table_name} WHERE {single_pk} = :row_id"),
                                {'row_id': row_id}
                            )
                            self.session.commit()
                            deleted += 1
                        except Exception as e:
                            self.session.rollback()
                            logger.warning(
                                f"      Could not delete duplicate row "
                                f"{single_pk}={row_id}: {e}"
                            )
                            still_orphan = True

                logger.info(
                    f"      Redirect {orphan_val} -> {new_val}: "
                    f"{updated} rows updated, {deleted} duplicates deleted"
                    + ("" if not still_orphan else "; some rows remain untouched")
                )

                if still_orphan:
                    remaining_values.append(orphan_val)
                else:
                    fixed_values.append(orphan_val)

            # --- 4. Resolve remaining orphans --------------------------------
            col_info = {c['name']: c for c in inspector.get_columns(table_name)}
            fk_nullable = col_info.get(fk_col, {}).get('nullable', True)

            deleted_total = 0
            quarantined_total = 0
            if remaining_values:
                if fk_nullable:
                    # Nullable: safe to set NULL, no data loss.
                    for i in range(0, len(remaining_values), MAX_IN_VARS):
                        chunk = remaining_values[i:i + MAX_IN_VARS]
                        stmt = text(
                            f"UPDATE {table_name} SET {fk_col} = NULL "
                            f"WHERE {fk_col} IN :vals"
                        ).bindparams(bindparam("vals", expanding=True))
                        self.session.execute(stmt, {"vals": chunk})
                else:
                    # NOT NULL: copy to a quarantine table first, then delete.
                    # This preserves the rows for forensic inspection.
                    qname = f"_quarantine_{table_name}"
                    self.session.execute(text(
                        f"CREATE TABLE IF NOT EXISTS {qname} AS "
                        f"SELECT *, "
                        f"CAST(NULL AS TEXT) AS _orphan_fk_col, "
                        f"CAST(NULL AS TEXT) AS _orphan_value, "
                        f"CAST(NULL AS TIMESTAMP) AS _quarantined_at "
                        f"FROM {table_name} WHERE 1=0"
                    ))

                    for i in range(0, len(remaining_values), MAX_IN_VARS):
                        chunk = remaining_values[i:i + MAX_IN_VARS]

                        ins = text(
                            f"INSERT INTO {qname} "
                            f"SELECT *, :fk_col_name, CAST({fk_col} AS TEXT), "
                            f"CURRENT_TIMESTAMP "
                            f"FROM {table_name} WHERE {fk_col} IN :vals"
                        ).bindparams(bindparam("vals", expanding=True))
                        self.session.execute(
                            ins,
                            {"vals": chunk, "fk_col_name": fk_col},
                        )

                        stmt = text(
                            f"DELETE FROM {table_name} WHERE {fk_col} IN :vals"
                        ).bindparams(bindparam("vals", expanding=True))
                        result = self.session.execute(stmt, {"vals": chunk})
                        deleted_total += result.rowcount or 0
                        quarantined_total += result.rowcount or 0

            if remaining_values:
                self.session.commit()
                if deleted_total:
                    logger.warning(
                        f"    Quarantined and deleted {deleted_total} orphan rows "
                        f"from {table_name}.{fk_col}. "
                        f"Inspect: SELECT _orphan_value, COUNT(*) "
                        f"FROM _quarantine_{table_name} "
                        f"GROUP BY _orphan_value;"
                    )

            results[table_name]['fixed'] = sum(orphan_counter[v] for v in fixed_values)

            # --- 5. Recount remaining orphans --------------------------------
            remaining_orphans = 0
            for i in range(0, len(valid_list), MAX_IN_VARS):
                chunk = valid_list[i:i + MAX_IN_VARS]
                stmt = text(
                    f"SELECT COUNT(*) FROM {table_name} "
                    f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals"
                ).bindparams(bindparam("vals", expanding=True))
                remaining_orphans += self.session.execute(stmt, {"vals": chunk}).scalar() or 0

            results[table_name]['still_orphaned'] = remaining_orphans

            if remaining_orphans:
                logger.warning(
                    f"    {remaining_orphans} orphans remain in {table_name}.{fk_col}"
                )
            else:
                logger.info(f"    All orphans fixed in {table_name}.{fk_col}")

        return results

    
    # ===================== LOCATION SPECIFIC FIXES =====================

    def fix_orphaned_burden_records(self) -> int:
        """
        Fix fact_disease_burden rows whose location_id does not resolve to a
        valid dim_location entry.

        Repair strategy per row:
        1. If location_id is a string, try to name-match against dim_location.name.
        2. Otherwise fall back to the most common location_id in fact_disease_case.
        """
        logger.info("=" * 60)
        logger.info("FIXING ORPHANED BURDEN RECORDS")
        logger.info("=" * 60)

        valid_locations = {}
        for loc in self.session.query(DimLocation).all():
            valid_locations[loc.location_id] = {
                'name': (loc.name or '').lower(),
                'code': loc.location_code,
                'country_code': loc.country_code,
            }

        valid_ids = list(valid_locations.keys())
        if not valid_ids:
            logger.info("No valid locations found; nothing to fix")
            return 0

        orphaned_burden = _chunked_in(
            self.session,
            """
            SELECT burden_id, location_id, year, disease_code,
                incidence_rate, cases_mean
            FROM fact_disease_burden
            WHERE location_id IS NOT NULL AND location_id NOT IN :vals
            """,
            {},
            valid_ids,
        )

        if not orphaned_burden:
            logger.info("No orphaned burden records found")
            return 0

        logger.info(f"Found {len(orphaned_burden)} orphaned burden records")

        # Fallback location (computed once).
        fallback_location_id = None
        most_common = self.session.execute(text("""
            SELECT location_id, COUNT(*) AS cnt
            FROM fact_disease_case
            WHERE location_id IS NOT NULL
            GROUP BY location_id
            ORDER BY cnt DESC
            LIMIT 1
        """)).first()
        if most_common:
            fallback_location_id = most_common[0]

        fixed = 0
        for record in orphaned_burden:
            (burden_id, bad_location_id, year, disease_code,
            incidence_rate, cases_mean) = record

            new_location_id = None

            # Try name-based match.
            if isinstance(bad_location_id, str):
                bad_str = bad_location_id.lower()
                for loc_id, info in valid_locations.items():
                    if info['name'] and (bad_str in info['name'] or info['name'] in bad_str):
                        new_location_id = loc_id
                        logger.info(
                            f"  Matched '{bad_location_id}' -> "
                            f"'{info['name']}' (ID: {loc_id})"
                        )
                        break

            # Fallback.
            if new_location_id is None and fallback_location_id is not None:
                new_location_id = fallback_location_id
                logger.info(
                    f"  Using fallback location {fallback_location_id} "
                    f"for burden_id {burden_id}"
                )

            if new_location_id is not None:
                self.session.execute(
                    text(
                        "UPDATE fact_disease_burden "
                        "SET location_id = :new_id WHERE burden_id = :id"
                    ),
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
    def _expand_burden_to_daily(self, force: bool = False):
        """
        Expand yearly disease burden data to daily records.

        Each day gets one row in fact_disease_case with the daily case count
        stored in the case_count column.

        Parameters
        ----------
        force : bool
            If True, delete and re-insert existing ESTIMATE rows for each
            burden record. If False (default), skip burden records that
            already have ESTIMATE rows, making the method idempotent.
        """
        logger.info("=" * 60)
        logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS")
        logger.info("=" * 60)

        total_burden = self.session.execute(
            text("SELECT COUNT(*) FROM fact_disease_burden")
        ).scalar()

        if total_burden == 0:
            logger.info("No burden records found")
            return

        # ---------------- Population map ----------------
        pop_results = self.session.execute(text("""
            SELECT location_id, indicator_value
            FROM fact_socioeconomic_condition
            WHERE indicator_code = 'SP.POP.TOTL'
        """)).fetchall()
        population_map = {r[0]: r[1] for r in pop_results}

        loc_results = self.session.execute(
            text("SELECT location_id FROM dim_location")
        ).fetchall()
        all_location_ids = [r[0] for r in loc_results]

        default_population = 1_000_000
        for loc_id in all_location_ids:
            if (loc_id not in population_map
                    or population_map[loc_id] is None
                    or population_map[loc_id] <= 0):
                population_map[loc_id] = default_population

        # ---------------- Burden records ----------------
        burden_records = self.session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, source_code,
                incidence_rate, cases_mean
            FROM fact_disease_burden
            WHERE (incidence_rate IS NOT NULL AND incidence_rate > 0)
            OR (cases_mean IS NOT NULL AND cases_mean > 0)
        """)).fetchall()

        if not burden_records:
            logger.warning("No burden records with valid data found")
            return

        # TODO: just a patch
        self.session.execute(text("""
            INSERT INTO dim_identification_method
                (method_code, method_name, method_type)
            VALUES
                ('ESTIMATE', 'Estimated', 'modeled')
            ON CONFLICT (method_code) DO NOTHING
        """))
        self.session.commit()

        logger.info(f"Found {len(burden_records)} burden records to expand")

        # ---------------- Seasonal factors ----------------
        # CHANGED (bug fix): the original formula multiplied by 12, which made the
        # factors sum to 12 rather than 1. Because monthly_cases = annual_cases *
        # normalized_factor[month] is then summed over the year, this inflated
        # the yearly total by 12x. The correct normalisation divides by the total
        # so that SUM(normalized_factors) == 1, which guarantees that the daily
        # counts across the year sum to approximately annual_cases.
        monthly_factors = {
            1: 0.6, 2: 0.5, 3: 0.7, 4: 1.4, 5: 1.5, 6: 1.3,
            7: 1.2, 8: 1.0, 9: 0.8, 10: 0.6, 11: 0.5, 12: 0.5,
        }
        total_factor = sum(monthly_factors.values())
        normalized_factors = {
            m: f / total_factor for m, f in monthly_factors.items()
        }

        is_sqlite = 'sqlite' in str(self.session.bind.url).lower()

        inserted_count = 0
        skipped_count = 0
        already_present_count = 0
        error_count = 0

        for record in burden_records:
            (burden_id, location_id, year, disease_code,
            source_code, incidence_rate, cases_mean) = record

            population = population_map.get(location_id, default_population)

            if incidence_rate is not None and incidence_rate > 0 and population > 0:
                annual_cases = (incidence_rate / 100_000) * population
            elif cases_mean is not None and cases_mean > 0:
                annual_cases = cases_mean
            else:
                skipped_count += 1
                continue

            annual_cases = max(annual_cases, 1)

            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31)
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')

            # ---------------- Idempotency guard ----------------
            # CHANGED: check for existing ESTIMATE rows before doing any work.
            existing = self.session.execute(text("""
                SELECT COUNT(*) FROM fact_disease_case
                WHERE location_id = :loc_id
                AND date_id_onset >= :start_date
                AND date_id_onset <= :end_date
                AND disease_code = :disease
                AND identification_method_code = 'ESTIMATE'
            """), {
                'loc_id': location_id,
                'start_date': start_str,
                'end_date': end_str,
                'disease': disease_code,
            }).scalar()

            if existing and existing > 0 and not force:
                logger.info(
                    f"  Skipping burden_id {burden_id} "
                    f"(location={location_id}, year={year}, disease={disease_code}): "
                    f"{existing} ESTIMATE rows already exist."
                )
                already_present_count += 1
                continue

            # ---------------- Build the daily list ----------------
            daily_cases_list = []
            current_date = start_date
            while current_date <= end_date:
                month = current_date.month
                # CHANGED: correct days_in_month for December.
                if month == 12:
                    next_month = datetime(year + 1, 1, 1)
                else:
                    next_month = datetime(year, month + 1, 1)
                days_in_month = (next_month - datetime(year, month, 1)).days

                monthly_cases = annual_cases * normalized_factors[month]
                daily_cases = max(1, int(round(monthly_cases / days_in_month)))

                daily_cases_list.append({
                    'loc_id': location_id,
                    'date_id': current_date.strftime('%Y-%m-%d'),
                    'disease': disease_code,
                    'src_code': source_code,
                    'case_count': daily_cases,     # CHANGED: keep the value
                })
                current_date += timedelta(days=1)

            if not daily_cases_list:
                continue

            try:
                # Delete any existing ESTIMATE rows if we got here. When
                # existing == 0 this is a harmless no-op; when force=True it
                # clears the previous expansion.
                if existing and existing > 0:
                    self.session.execute(text("""
                        DELETE FROM fact_disease_case
                        WHERE location_id = :loc_id
                        AND date_id_onset >= :start_date
                        AND date_id_onset <= :end_date
                        AND disease_code = :disease
                        AND identification_method_code = 'ESTIMATE'
                    """), {
                        'loc_id': location_id,
                        'start_date': start_str,
                        'end_date': end_str,
                        'disease': disease_code,
                    })

                batch_size = 5000
                for i in range(0, len(daily_cases_list), batch_size):
                    batch = daily_cases_list[i:i + batch_size]

                    if is_sqlite:
                        # pysqlite does not reliably expand a list-of-dicts into
                        # multiple rows via executemany on a text() clause, so
                        # loop explicitly. Each row already carries :case_count.
                        for row in batch:
                            self.session.execute(text("""
                                INSERT INTO fact_disease_case (
                                    location_id, date_id_onset, date_id_reported,
                                    disease_code, identification_method_code,
                                    source_code, case_status, host_type,
                                    age_group, sex, outcome, case_count
                                ) VALUES (
                                    :loc_id, :date_id, :date_id,
                                    :disease, 'ESTIMATE', :src_code,
                                    'confirmed', 'human',
                                    'all', 'unknown', 'unknown', :case_count
                                )
                            """), row)
                    else:
                        # PostgreSQL (psycopg2) handles executemany natively.
                        # CHANGED: case_count is now included in the column list
                        # and the VALUES clause. Previously it was omitted here
                        # entirely, so PostgreSQL inserts silently defaulted to 1.
                        self.session.execute(text("""
                            INSERT INTO fact_disease_case (
                                location_id, date_id_onset, date_id_reported,
                                disease_code, identification_method_code,
                                source_code, case_status, host_type,
                                age_group, sex, outcome, case_count
                            ) VALUES (
                                :loc_id, :date_id, :date_id,
                                :disease, 'ESTIMATE', :src_code,
                                'confirmed', 'human',
                                'all', 'unknown', 'unknown', :case_count
                            )
                        """), batch)

                self.session.commit()
                inserted_count += len(daily_cases_list)
                logger.info(
                    f"  Expanded burden_id {burden_id} ({year}) into "
                    f"{len(daily_cases_list)} daily records"
                )

            except Exception as e:
                logger.error(f"Error expanding burden_id {burden_id}: {e}")
                self.session.rollback()
                error_count += 1
                continue

        logger.info("=" * 60)
        logger.info("BURDEN EXPANSION COMPLETE")
        logger.info(f"  Inserted:          {inserted_count} daily records")
        logger.info(f"  Already present:   {already_present_count} records "
                    f"(use force=True to overwrite)")
        logger.info(f"  Skipped (no data): {skipped_count} records")
        logger.info(f"  Errors:            {error_count}")
        logger.info("=" * 60)
    

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

        # Step 4: Standardize vocabularies 
        logger.info("Step 4: Standardizing vocabularies...")
        vocab_results = self.standardize_vocabularies()

        # Step 5: Fix orphaned burden records specifically
        logger.info("Step 5: Fixing orphaned burden records...")
        burden_fixed = self.fix_orphaned_burden_records()

        # Step 6: Expand burden data to daily records
        logger.info("Step 6: Expanding burden data to daily records...")
        self._expand_burden_to_daily()

        # Step 7: Compute resistance status
        logger.info("Step 7: Computing resistance status...")
        self._compute_resistance_status()

        # Step 8: Consolidate locations
        logger.info("Step 8: Consolidating locations...")
        loc_results = self.consolidate_locations()

        # Step 9: Consolidate vocabulary
        logger.info("Step 9: Consolidating vocabulary...")
        vocab_results = self.consolidate_vocabulary()

        # Step 10: Detect and resolve duplicates
        logger.info("Step 10: Detecting and resolving duplicates...")
        dup_results = self.detect_duplicates()
        self.resolve_fact_table_duplicates()

        # Step 11: Final verification
        logger.info("Step 11: Final verification...")
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
                            min_samples: int = 1, dry_run: bool = False):
        """Consolidate locations with country code standardization.

        Grid cells (admin_level='grid') are preserved as-is: they are uniquely
        identified by location_code and their shared `name` is a label, not an
        identity. All other locations are eligible for name-merge and
        cluster-merge.
        """
        logger.info("Consolidating locations...")

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
            'admin_level': getattr(loc, 'admin_level', None),
        } for loc in locations])

        # Split on admin_level, not on name.
        #
        # Grid cells carry admin_level='grid'. They are uniquely identified by
        # location_code and are never candidates for name-merge or cluster-merge.
        # Their `name` may be shared (e.g. all pixels renamed to "Siaya") without
        # implying they are the same location.
        is_grid = (
            loc_data['admin_level']
            .fillna('')
            .astype(str)
            .str.strip()
            .str.lower()
            .eq('grid')
        )
        loc_grid = loc_data[is_grid].copy()
        loc_non_grid = loc_data[~is_grid].copy()

        logger.info(
            f"  Grid cells preserved as-is: {len(loc_grid)}; "
            f"non-grid locations eligible for merge: {len(loc_non_grid)}"
        )

        # Precompute the normalized name ONCE for the non-grid slice, before any
        # merge function that depends on it runs.
        if not loc_non_grid.empty:
            loc_non_grid['name_normalized'] = loc_non_grid['name'].apply(
                self._normalize_location_name
            )

        # --- Name-based merge (only on non-grid) -------------------------------
        name_results = (
            self._merge_by_name(loc_non_grid, dry_run)
            if not loc_non_grid.empty else []
        )

        # --- Refresh the location snapshot from the DB -------------------------
        # _merge_by_name deletes rows from dim_location. Re-query so the second
        # pass does not try to merge facts into a now-dead canonical.
        if not dry_run:
            fresh_locations = self.session.query(DimLocation).all()
            loc_data = pd.DataFrame([{
                'location_id': loc.location_id,
                'name': loc.name,
                'location_code': getattr(loc, 'location_code', None),
                'centroid_lat': loc.centroid_lat,
                'centroid_lon': loc.centroid_lon,
                'country_code': loc.country_code,
                'country_name': loc.country_name,
                'admin_level': getattr(loc, 'admin_level', None),
            } for loc in fresh_locations])

            is_grid = (
                loc_data['admin_level']
                .fillna('')
                .astype(str)
                .str.strip()
                .str.lower()
                .eq('grid')
            )
            loc_non_grid = loc_data[~is_grid].copy()
            if not loc_non_grid.empty:
                loc_non_grid['name_normalized'] = loc_non_grid['name'].apply(
                    self._normalize_location_name
                )

        # --- Cluster-based merge (only on non-grid, coord-bearing) -------------
        cluster_results = {}
        if not loc_non_grid.empty:
            has_coords = (
                loc_non_grid['centroid_lat'].notna()
                & loc_non_grid['centroid_lon'].notna()
            )
            loc_with_coords = loc_non_grid[has_coords].copy()
            if not loc_with_coords.empty:
                clusters = self._cluster_locations_by_country_and_proximity(
                    loc_with_coords, distance_threshold_km, min_samples
                )
                cluster_results = self._merge_location_clusters(
                    loc_with_coords, clusters, dry_run
                )

        final_fix = self.fix_location_country_codes()

        results = {
            'code_standardization': code_results,
            'original_count': len(loc_data),
            'grid_count': len(loc_grid),
            'non_grid_count': len(loc_non_grid),
            'name_duplicate_groups': name_results,
            'cluster_results': cluster_results,
            'final_country_fixes': final_fix,
        }

        if not dry_run:
            self.session.commit()

        return results


    def _is_grid_location(self, name) -> bool:
        """True if `name` is a bare `lat/lon` or `lon/lat` grid cell string."""
        if not name or not isinstance(name, str):
            return False
        return bool(_GRID_NAME_RE.match(name))


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

            # Defensive check: if the canonical no longer exists in the DB
            # (e.g. it was deleted by a previous merge in this same run),
            # skip this cluster rather than moving facts into a dead ID.
            canonical_alive = self.session.query(DimLocation).filter_by(
                location_id=canonical_id
            ).first() is not None
            if not canonical_alive:
                logger.warning(
                    f"    Skipping cluster {cluster_id}: canonical {canonical_id} "
                    f"no longer exists in dim_location."
                )
                continue

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
        """Reassign fact rows from source to target; on unique-key collision,
        delete the losing rows and log it, rather than leaving orphans."""
        from sqlalchemy.exc import IntegrityError

        fact_tables = [
            'fact_vector_observation', 'fact_disease_case', 'fact_insecticide_resistance',
            'fact_bionomics', 'fact_intervention', 'fact_socioeconomic_condition',
            'fact_health_facility', 'fact_disease_burden', 'fact_environment',
        ]

        for table in fact_tables:
            try:
                result = self.session.execute(
                    text(f"UPDATE {table} SET location_id = :target_id "
                        f"WHERE location_id = :source_id"),
                    {"target_id": target_id, "source_id": source_id},
                )
                self.session.commit()
                if result.rowcount:
                    logger.info(
                        f"    Moved {result.rowcount} rows in {table}: "
                        f"location {source_id} -> {target_id}"
                    )
            except IntegrityError:
                self.session.rollback()
                result = self.session.execute(
                    text(f"DELETE FROM {table} WHERE location_id = :source_id"),
                    {"source_id": source_id},
                )
                self.session.commit()
                logger.warning(
                    f"    DELETED {result.rowcount} rows from {table} "
                    f"(merge {source_id}->{target_id} collided on unique key)"
                )
            except Exception as e:
                self.session.rollback()
                logger.error(f"    Merge failed for {table}: {e}")
                raise


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
        from sqlalchemy.exc import IntegrityError
        # mapping = table_map.get(dimension_model)
        mapping = _DIMENSION_FACT_MAP.get(dimension_model)
        if not mapping:
            return
        for table_name, fk_col in zip(mapping['fact_tables'], mapping['fk_columns']):
            try:
                result = self.session.execute(
                    text(f"UPDATE {table_name} SET {fk_col} = :new_code "
                        f"WHERE {fk_col} = :old_code"),
                    {'new_code': new_code, 'old_code': old_code},
                )
                self.session.commit()
                if result.rowcount:
                    logger.info(f"    Reassigned {result.rowcount} rows in "
                                f"{table_name}.{fk_col}: {old_code} -> {new_code}")
            except IntegrityError:
                self.session.rollback()
                result = self.session.execute(
                    text(f"DELETE FROM {table_name} WHERE {fk_col} = :old_code"),
                    {'old_code': old_code},
                )
                self.session.commit()
                logger.warning(
                    f"    DELETED {result.rowcount} rows from {table_name} "
                    f"(reassign {old_code}->{new_code} collided)"
                )
            except Exception as e:
                self.session.rollback()
                logger.error(f"    Reassign failed for {table_name}.{fk_col}: {e}")
                raise

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


    def standardize_vocabularies(self,
                                vocab_dir: str = "vocabularies",
                                fuzzy_threshold: float = 0.85,
                                dry_run: bool = False) -> Dict[str, Any]:
        """
        Standardize dimension values against external vocabularies.

        Steps per (table, column, domain):
            1. Load the vocabulary concepts into dim_vocabulary + dim_concept.
            2. For each distinct raw value in the source column, attempt a match:
                a. exact match on name_normalized
                b. normalized match (strip diacritics, expand abbreviations)
                c. fuzzy match (token_set_ratio >= fuzzy_threshold)
            3. Record the mapping in fact_concept_map.
            4. Update the dimension's standard_code / concept_id if match is confident.
        """
        logger.info("=" * 60)
        logger.info("STANDARDIZING VOCABULARIES")
        logger.info("=" * 60)

        loader = VocabularyLoader(self.session)

        # --- 1. Load reference vocabularies --------------------------------
        vocab_files = {
            "GADM":   (f"{vocab_dir}/gadm_locations.csv", "location"),
            "IDOMAL": (f"{vocab_dir}/idomal_diseases.csv", "disease"),
            "NCBI":   (f"{vocab_dir}/ncbi_vectors.csv",   "vector"),
        }
        for vocab_id, (path, domain) in vocab_files.items():
            if Path(path).exists():
                loader.load_csv(vocab_id, path, domain)
                logger.info(f"  Loaded {vocab_id} vocabulary from {path}")

        # --- 2. Match each dimension column --------------------------------
        match_targets = [
            ("dim_location", "name",        "location", "GADM"),
            ("dim_vector",   "species_name","vector",   "NCBI"),
            ("dim_disease",  "disease_name","disease",  "IDOMAL"),
        ]

        results = {}
        for table, column, domain, vocab_id in match_targets:
            logger.info(f"  Matching {table}.{column} → {vocab_id} …")
            results[f"{table}.{column}"] = self._match_column(
                table, column, domain, vocab_id, loader, fuzzy_threshold, dry_run
            )

        logger.info("=" * 60)
        logger.info("VOCABULARY STANDARDIZATION COMPLETE")
        for k, v in results.items():
            logger.info(f"  {k}: {v['matched']}/{v['total']} matched "
                        f"({v['exact']} exact, {v['fuzzy']} fuzzy, {v['unmatched']} unmatched)")
        logger.info("=" * 60)

        return results


    def _match_column(
        self,
        table: str,
        column: str,
        domain: str,
        vocab_id: str,
        loader,
        threshold: float,
        dry_run: bool,
    ) -> Dict[str, int]:
        """
        Match every distinct value in one dimension column against one vocabulary.

        For each raw value:
            1. Ask the loader for the best concept (exact / normalized / fuzzy).
            2. Record the outcome in `fact_concept_map` (upserted on the natural
            key (source_table, source_column, source_value)).
            3. On a confident match — exact or normalized — stamp the dimension
            row with both `standard_code` (the external concept code) and
            `concept_id` (the FK into `dim_concept`).

        Each value is processed inside its own SAVEPOINT, so a single bad row
        cannot abort the whole column.

        Parameters
        ----------
        table : str
            Dimension table name, e.g. 'dim_location'.
        column : str
            Column to match on, e.g. 'name'.
        domain : str
            Vocabulary domain, e.g. 'location', 'vector', 'disease'.
        vocab_id : str
            Vocabulary identifier, e.g. 'GADM', 'NCBI', 'IDOMAL'.
        loader : VocabularyLoader
            Object whose `.match(value, vocab_id, domain, threshold=...)`
            returns `(concept_dict_or_None, method, score)`.
        threshold : float
            Fuzzy-match score cutoff in [0, 1].
        dry_run : bool
            If True, perform matching and gather stats but write nothing.

        Returns
        -------
        dict
            Counts: total, matched, exact, normalized, fuzzy, unmatched,
            errors, concept_ids_set.
        """
        # ---- 1. Fetch distinct raw values -----------------------------------
        try:
            rows = self.session.execute(
                text(
                    f"SELECT DISTINCT {column} FROM {table} "
                    f"WHERE {column} IS NOT NULL"
                )
            ).fetchall()
        except Exception as e:
            logger.error(f"  Could not read distinct values from {table}.{column}: {e}")
            return {
                "total": 0, "matched": 0, "exact": 0, "normalized": 0,
                "fuzzy": 0, "unmatched": 0, "errors": 1, "concept_ids_set": 0,
            }

        raw_values = [r[0] for r in rows]

        stats: Dict[str, int] = {
            "total": len(raw_values),
            "matched": 0,
            "exact": 0,
            "normalized": 0,
            "fuzzy": 0,
            "unmatched": 0,
            "errors": 0,
            "concept_ids_set": 0,
        }

        if not raw_values:
            return stats

        # ---- 2. Match each distinct value -----------------------------------
        for value in raw_values:
            # 2a. Ask the loader for the best concept.
            try:
                concept, method, score = loader.match(
                    value, vocab_id, domain, threshold=threshold
                )
            except Exception as e:
                logger.warning(
                    f"  Match failed for '{value}' against {vocab_id}: {e}"
                )
                stats["errors"] += 1
                continue

            concept_code = concept.get("concept_code") if concept else None
            confident = method in ("exact", "normalized")

            # 2b. Update statistics regardless of dry_run.
            if concept:
                stats["matched"] += 1
                if method == "exact":
                    stats["exact"] += 1
                elif method == "normalized":
                    stats["normalized"] += 1
                elif method == "fuzzy":
                    stats["fuzzy"] += 1
            else:
                stats["unmatched"] += 1

            if dry_run:
                continue

            # 2c. Persist inside a per-value SAVEPOINT. Any failure here rolls
            #     back just this value; the outer transaction keeps going.
            try:
                with self.session.begin_nested():
                    # Record the mapping. Upsert on the natural key; the
                    # existing `is_approved` flag is preserved (we do not
                    # overwrite a human's decision on re-run).
                    self.session.execute(
                        text("""
                            INSERT INTO fact_concept_map
                                (source_table, source_column, source_value,
                                vocabulary_id, concept_code, match_method,
                                match_score, is_approved)
                            VALUES
                                (:t, :c, :v, :vocab, :code, :method, :score, :approved)
                            ON CONFLICT (source_table, source_column, source_value)
                            DO UPDATE SET
                                vocabulary_id = EXCLUDED.vocabulary_id,
                                concept_code  = EXCLUDED.concept_code,
                                match_method  = EXCLUDED.match_method,
                                match_score   = EXCLUDED.match_score
                        """),
                        {
                            "t": table,
                            "c": column,
                            "v": value,
                            "vocab": vocab_id if concept else None,
                            "code": concept_code,
                            "method": method,
                            "score": score,
                            # exact and normalized auto-approve; fuzzy is
                            # queued for human review.
                            "approved": confident,
                        },
                    )

                    # On a confident match, stamp the dimension row with
                    # both the textual code and the FK into dim_concept.
                    if concept and confident:
                        cid_row = self.session.execute(
                            text(
                                "SELECT concept_id FROM dim_concept "
                                "WHERE vocabulary_id = :v AND concept_code = :c"
                            ),
                            {"v": vocab_id, "c": concept_code},
                        ).first()
                        concept_id = cid_row[0] if cid_row else None

                        self.session.execute(
                            text(
                                f"UPDATE {table} "
                                f"SET standard_code = :code, "
                                f"    concept_id    = COALESCE(:cid, concept_id) "
                                f"WHERE {column} = :value"
                            ),
                            {
                                "code": concept_code,
                                "cid": concept_id,
                                "value": value,
                            },
                        )
                        if concept_id is not None:
                            stats["concept_ids_set"] += 1
            except Exception as e:
                # SAVEPOINT already rolled back; the session is still usable.
                logger.warning(
                    f"  Could not record match for {table}.{column} = "
                    f"'{value}': {e}"
                )
                stats["errors"] += 1

        # ---- 3. Commit the outer transaction --------------------------------
        if not dry_run:
            try:
                self.session.commit()
            except Exception as e:
                logger.error(f"  Commit failed for {table}.{column}: {e}")
                self.session.rollback()
                stats["errors"] += 1

        return stats


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