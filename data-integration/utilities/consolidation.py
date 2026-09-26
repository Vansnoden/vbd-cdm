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
# =============================================================================
# Burden-expansion parameters
#
# Single source of truth for the seasonal and weekly shape of the synthetic
# daily case series. Previously these values lived only in fix_cases.py.
# =============================================================================

# Tropical seasonal curve: dry-season trough in Jan–Feb and Nov–Dec,
# transmission peak in the long-rains window Apr–Jun.
_BURDEN_SEASONAL_FACTORS = {
    1: 0.55, 2: 0.50, 3: 0.65, 4: 1.35, 5: 1.50, 6: 1.40,
    7: 1.30, 8: 1.10, 9: 0.85, 10: 0.65, 11: 0.55, 12: 0.50,
}

# Day-of-week multipliers. Monday = 1 in isocalendar(); Saturday = 6,
# Sunday = 7 — reported case counts dip on weekends.
_BURDEN_WEEKLY_PATTERNS = {
    1: 0.95, 2: 1.00, 3: 1.05, 4: 1.10, 5: 1.10, 6: 0.90, 7: 0.85,
}


def _burden_seasonal_factor(month: int) -> float:
    return _BURDEN_SEASONAL_FACTORS.get(month, 1.0)


def _burden_weekly_factor(date: "datetime") -> float:
    return _BURDEN_WEEKLY_PATTERNS.get(date.isocalendar()[2], 1.0)


def _burden_generate_daily_case_counts(
    annual_cases: float,
    year: int,
    location_id: int,
    base_variation: float = 0.25,
    seed: Optional[int] = None,
) -> pd.DataFrame:
    """
    Expand an annual case total into a realistic daily series.

    The shape is the product of six multiplicative factors:

        seasonal(month) * weekly(dow) * intra_month(day_of_month, month)
            * lognormal_noise(sigma = base_variation * 0.7)
            * holiday(christmas, new year, easter)
            * spike(random, ~0.5% of days)

    The factors are normalised so their sum equals the number of days in
    the year. Multiplying by `annual_cases` therefore preserves the annual
    total exactly, up to integer rounding.

    The random seed is combined with `location_id` and `year`, so two
    locations in the same year do not share a random sequence, but the
    same (location, year) reproduces identically across runs.

    Returns
    -------
    pd.DataFrame
        Columns: date_id, year, month, case_count, location_id.
        One row per day of `year`.
    """
    if seed is not None:
        np.random.seed(seed + int(location_id) + int(year))

    start_date = datetime(year, 1, 1)
    end_date   = datetime(year, 12, 31)
    date_range = pd.date_range(start_date, end_date)

    daily_factors: List[float] = []
    for date in date_range:
        month = date.month

        seasonal_factor = _burden_seasonal_factor(month)
        weekly_factor   = _burden_weekly_factor(date)

        day_in_month = date.day
        days_in_month = (
            date.replace(month=month % 12 + 1, day=1)
            - date.replace(day=1)
        ).days
        intra_month_phase  = (day_in_month / max(days_in_month, 1)) * 2 * np.pi
        intra_month_factor = 1.0 + 0.08 * np.sin(intra_month_phase + month * 0.5)

        noise = np.random.lognormal(0, base_variation * 0.7)

        holiday_factor = 1.0
        if date.month == 12 and date.day in (25, 26):
            holiday_factor = 0.80
        elif date.month == 1 and date.day == 1:
            holiday_factor = 0.75
        elif date.month == 4 and 1 <= date.day <= 10:
            holiday_factor = 0.85
        elif date.month == 5 and date.day <= 5:
            holiday_factor = 0.85

        spike_factor = 1.0
        if np.random.random() < 0.005:
            spike_factor = 1.0 + np.random.exponential(0.5)

        daily_factors.append(
            seasonal_factor * weekly_factor * intra_month_factor
            * noise * holiday_factor * spike_factor
        )

    # Normalise so sum(factors) == number of days; annual total preserved.
    total_factor = sum(daily_factors)
    if total_factor > 0:
        daily_factors = [f / total_factor * len(date_range) for f in daily_factors]

    rows = []
    for date, factor in zip(date_range, daily_factors):
        daily_cases = max(1, int(round(annual_cases * factor)))
        rows.append({
            "date_id":     date.strftime("%Y-%m-%d"),
            "year":        year,
            "month":       date.month,
            "case_count":  daily_cases,
            "location_id": location_id,
        })

    return pd.DataFrame(rows)

_GRID_NAME_RE = re.compile(r'^\s*-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?\s*$')
_SPECIES_QUALIFIER_RE = re.compile(r"\s+(complex|subgroup|group)$")
_SPECIES_STRAIN_RE    = re.compile(r"\s+(?:s\s*form|m\s*form|form)\b.*$")


# =============================================================================
# Fact-table aggregation rules
#
# When two rows collide on a natural key after a dimension FK is reassigned
# (species merge, location merge, source merge), they must be AGGREGATED
# rather than one being silently deleted. This was the bug that discarded
# ~60% of fact_vector_observation rows in an earlier revision.
#
# Each entry declares, for one fact table:
#   natural_key : columns that identify "the same row" after the FK change
#   sums        : columns to SUM (counts)
#   means       : columns to AVG (rates, densities)
#   first       : columns to take MIN of (categorical labels)
#
# Column policy rationale:
#   - Counts (n_insect_tested, count, case_count) — SUM
#   - Rates / densities (mortality, infection_rate, density_per_trap) — AVG
#     or recompute from summed counts
#   - Categorical labels (feeding_preference, sex, life_stage) — MIN
#     (assumes colliding rows agree; audit with the query in the docs)
# =============================================================================

_FACT_AGGREGATION_SPEC = {
    "fact_vector_observation": {
        "natural_key": ["location_id", "date_id", "vector_code", "source_code",
                        "life_stage", "sex", "collection_method"],
        "sums":  ["count"],
        "means": ["density_per_trap", "infection_rate"],
        "first": ["tested_for_pathogen", "infection_status"],
    },
    "fact_insecticide_resistance": {
        "natural_key": ["location_id", "date_id", "vector_code", "insecticide_code",
                        "test_type_code", "source_code", "concentration"],
        "sums": [
            "n_insect_tested", "n_insect_susceptible",
            "n_insect_resistant", "n_insect_dead",
            "vgsc_n_insect_tested",
            "vgsc995l_vgsc995l_n", "vgsc995l_vgsc995f_n",
            "vgsc995f_vgsc995f_n", "vgsc995l_vgsc995s_n",
            "vgsc995s_vgsc995s_n", "vgsc995f_vgsc995s_n",
        ],
        "means": [],
        "first": ["exposure_time", "vgsc_generation"],
        # mortality and resistance_status are functions of the summed
        # counts. They are NULLed out during aggregation and recomputed
        # afterwards by _compute_resistance_status(). Storing the pre-
        # aggregation values would leave a stale rate paired with the
        # summed counts.
        "derived": ["mortality", "resistance_status"],
    },
    "fact_bionomics": {
        "natural_key": ["location_id", "date_id", "vector_code", "source_code"],
        "sums":  [],
        "means": ["biting_rate"],
        "first": ["feeding_preference", "biting_location",
                  "resting_behavior", "larval_site_type"],
    },
    "fact_environment": {
        "natural_key": ["location_id", "date_id", "variable"],
        "sums":  [],
        "means": ["value"],
        "first": ["source_code"],
    },
    "fact_disease_case": {
        "natural_key": ["location_id", "date_id_onset", "disease_code",
                        "identification_method_code", "source_code",
                        "case_status", "age_group", "sex"],
        "sums":  ["case_count"],
        "means": [],
        # age_years is NOT part of the natural key (age_group is), so
        # colliding rows can legitimately disagree. MIN is the safest
        # non-lossy choice for an integer age; AVG would change the
        # column's meaning. Adding it here is required — without it, the
        # column is silently dropped by the aggregation INSERT.
        "first": ["date_id_reported", "host_type", "outcome", "age_years"],
    },
    "fact_disease_burden": {
        "natural_key": ["location_id", "year", "disease_code", "pathogen_species"],
        "sums":  ["cases_mean"],
        "means": ["incidence_rate", "prevalence_rate", "mortality_rate"],
        "first": ["source_code"],
    },
    "fact_intervention": {
        "natural_key": ["location_id", "date_start", "target_disease_code",
                        "source_code", "intervention_type"],
        "sums":  [],
        "means": ["coverage"],
        "first": ["date_end"],
    },
    "fact_socioeconomic_condition": {
        "natural_key": ["location_id", "year", "indicator_code"],
        "sums":  [],
        "means": ["indicator_value"],
        "first": ["source_code"],
    },
    # fact_health_facility has no aggregation policy — falls back to
    # plain UPDATE + collision log.
}


# =============================================================================
# Dimension -> fact FK cascade map
#
# When a dimension row is merged (two vector_codes collapse into one, two
# sources into one, etc.), every fact table that carries that FK must be
# updated. This map declares the (fact_table, fk_column) pairs for each
# dimension model class.
#
# Without this map, _update_fact_references() would have no work to do —
# the fact tables would keep stale FK values pointing at dimension rows
# that are about to be deleted.
# =============================================================================

_DIMENSION_FACT_MAP = {
    DimDisease: {
        'fact_tables': ['fact_disease_case', 'fact_disease_burden', 'fact_intervention'],
        'fk_columns':  ['disease_code', 'disease_code', 'target_disease_code'],
    },
    DimVector: {
        'fact_tables': ['fact_vector_observation',
                        'fact_insecticide_resistance',
                        'fact_bionomics'],
        'fk_columns':  ['vector_code', 'vector_code', 'vector_code'],
    },
    DimInsecticide: {
        'fact_tables': ['fact_insecticide_resistance'],
        'fk_columns':  ['insecticide_code'],
    },
    DimSource: {
        'fact_tables': [
            'fact_vector_observation', 'fact_disease_case',
            'fact_insecticide_resistance', 'fact_bionomics',
            'fact_intervention', 'fact_socioeconomic_condition',
            'fact_health_facility', 'fact_disease_burden',
            'fact_environment',
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
    """Robust check if a string is a real number."""
    if not value or not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
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
    """Get ISO country name and code from coordinates."""
    try:
        if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
            return None, None
    except (TypeError, ValueError):
        return None, None

    if not is_real_number_robust(str(lat)) or not is_real_number_robust(str(lon)):
        return None, None

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None, None

    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
        return None, None

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
                    return (ISO_TO_COUNTRY_NAME.get(country_code, country_name),
                            country_code)
    except ImportError:
        pass
    except Exception as exc:
        logger.debug(f"reverse_geocode failed for ({lat_f}, {lon_f}): {exc}")

    country_name, country_code = _get_country_from_nearest(lat_f, lon_f, max_distance_km)
    if country_code:
        return country_name, country_code
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
    """Fallback bounding box for African countries."""
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
    """Consolidates and standardizes data in the CDM."""

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
        Align every fact-to-dimension foreign key.

        Order matters and is deliberate:

            Step 1  _clean_dimension_tables
                    Logs dimension rows with no fact references. Read-only.

            Step 2  _align_fact_foreign_keys
                    Attempts to REPAIR orphans by name-matching the raw FK
                    value against the dimension's name column. Only the
                    values it cannot repair are NULLed or quarantined.

            Step 3  _align_dimension_foreign_keys
                    Catches any FK pair that step 2 did not cover — either
                    because the pair is not in the hardcoded repair list, or
                    because the fact alignment was skipped for that table.
                    NULLs the remaining unresolvable values in nullable
                    columns.

        Running step 3 before step 2 (the previous order) nulled every
        orphan before step 2 could see it, silently disabling the repair
        pass. That is why so many location/vector/source orphans survived
        consolidation in earlier revisions.
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
                'orphans_found': 0,
            },
        }

        logger.info("Step 1: Auditing dimension tables...")
        self._clean_dimension_tables(dry_run)

        logger.info("Step 2: Repairing fact FKs (name-match before NULL)...")
        fact_alignments = self._align_fact_foreign_keys(dry_run)
        results['facts'] = fact_alignments

        logger.info("Step 3: Sweeping remaining unresolved FKs...")
        dimension_alignments = self._align_dimension_foreign_keys(dry_run)
        results['dimensions'] = dimension_alignments

        for table, stats in fact_alignments.items():
            if isinstance(stats, dict):
                results['summary']['total_fixed']    += stats.get('fixed', 0)
                results['summary']['orphans_found']  += stats.get('orphaned', 0)
                results['summary']['tables_processed'] += 1

        logger.info("=" * 60)
        logger.info("FOREIGN KEY ALIGNMENT COMPLETE")
        logger.info(
            f"  Repaired:        {results['summary']['total_fixed']} rows"
        )
        logger.info(
            f"  Orphans found:   {results['summary']['orphans_found']}"
        )
        logger.info(
            f"  Tables touched:  {results['summary']['tables_processed']}"
        )
        logger.info("=" * 60)

        return results


    def _clean_dimension_tables(self, dry_run: bool = False):
        inspector = inspect(self.session.bind)
        all_tables = inspector.get_table_names()
        fact_tables = [t for t in all_tables if t.startswith('fact_')]
        dim_tables = [t for t in all_tables if t.startswith('dim_') and t != 'dim_time']

        for dim_table in dim_tables:
            pk_cols = inspector.get_pk_constraint(dim_table).get('constrained_columns', [])
            if not pk_cols:
                continue
            pk_col = pk_cols[0]

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
                continue

            orphaned = _chunked_in(
                self.session,
                f"SELECT {pk_col} FROM {dim_table} WHERE {pk_col} NOT IN :vals",
                {},
                referenced_ids,
            )

            if orphaned and not dry_run:
                logger.info(f"  {len(orphaned)} orphaned records in {dim_table} (kept for reference)")

    def _align_dimension_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
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
                    except Exception as e:
                        logger.warning(f"    Could not align {fact_table}.{fk_col}: {e}")
                        results[dim_name]['errors'] += 1

            if not dry_run:
                self.session.commit()

        return results

    def _align_fact_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
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
                results[table_name] = {'orphaned': 0, 'fixed': 0, 'still_orphaned': 0, 'details': []}

            try:
                rows = self.session.execute(text(f"SELECT {pk_col} FROM {dim_table}")).fetchall()
                valid_values = {r[0] for r in rows if r[0] is not None}
            except Exception as e:
                logger.warning(f"    Could not get valid values from {dim_table}: {e}")
                continue

            if not valid_values:
                continue

            valid_list = list(valid_values)
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

            candidates = list(orphan_counter.keys())
            truly_orphan: Counter = Counter()
            for i in range(0, len(candidates), MAX_IN_VARS):
                chunk = candidates[i:i + MAX_IN_VARS]
                present_stmt = text(
                    f"SELECT {pk_col} FROM {dim_table} WHERE {pk_col} IN :vals"
                ).bindparams(bindparam("vals", expanding=True))
                present = {r[0] for r in self.session.execute(present_stmt, {"vals": chunk}).fetchall()}
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

            if dry_run:
                continue

            pk_cols_list = inspector.get_pk_constraint(table_name).get('constrained_columns', [])
            single_pk = pk_cols_list[0] if len(pk_cols_list) == 1 else None

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
                    except Exception:
                        pass

                if new_val is None:
                    remaining_values.append(orphan_val)
                    continue

                try:
                    self.session.execute(
                        text(f"UPDATE {table_name} SET {fk_col} = :new_val WHERE {fk_col} = :old_val"),
                        {'new_val': new_val, 'old_val': orphan_val}
                    )
                    self.session.commit()
                    fixed_values.append(orphan_val)
                    continue
                except IntegrityError:
                    self.session.rollback()

                if single_pk is None:
                    remaining_values.append(orphan_val)
                    continue

                try:
                    row_ids = [r[0] for r in self.session.execute(
                        text(f"SELECT {single_pk} FROM {table_name} WHERE {fk_col} = :old_val"),
                        {'old_val': orphan_val}
                    ).fetchall()]
                except Exception:
                    remaining_values.append(orphan_val)
                    continue

                still_orphan = False
                for row_id in row_ids:
                    try:
                        self.session.execute(
                            text(f"UPDATE {table_name} SET {fk_col} = :new_val WHERE {single_pk} = :row_id"),
                            {'new_val': new_val, 'row_id': row_id}
                        )
                        self.session.commit()
                    except IntegrityError:
                        self.session.rollback()
                        still_orphan = True
                        break

                if still_orphan:
                    remaining_values.append(orphan_val)
                else:
                    fixed_values.append(orphan_val)

            col_info = {c['name']: c for c in inspector.get_columns(table_name)}
            fk_nullable = col_info.get(fk_col, {}).get('nullable', True)

            if remaining_values:
                if fk_nullable:
                    for i in range(0, len(remaining_values), MAX_IN_VARS):
                        chunk = remaining_values[i:i + MAX_IN_VARS]
                        stmt = text(
                            f"UPDATE {table_name} SET {fk_col} = NULL WHERE {fk_col} IN :vals"
                        ).bindparams(bindparam("vals", expanding=True))
                        self.session.execute(stmt, {"vals": chunk})
                else:
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
                        self.session.execute(
                            text(f"INSERT INTO {qname} "
                                 f"SELECT *, :fk_col_name, CAST({fk_col} AS TEXT), CURRENT_TIMESTAMP "
                                 f"FROM {table_name} WHERE {fk_col} IN :vals"
                                 ).bindparams(bindparam("vals", expanding=True)),
                            {"vals": chunk, "fk_col_name": fk_col},
                        )
                        self.session.execute(
                            text(f"DELETE FROM {table_name} WHERE {fk_col} IN :vals"
                                 ).bindparams(bindparam("vals", expanding=True)),
                            {"vals": chunk},
                        )
                self.session.commit()

            results[table_name]['fixed'] = sum(orphan_counter[v] for v in fixed_values)

            remaining_orphans = 0
            for i in range(0, len(valid_list), MAX_IN_VARS):
                chunk = valid_list[i:i + MAX_IN_VARS]
                stmt = text(
                    f"SELECT COUNT(*) FROM {table_name} "
                    f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals"
                ).bindparams(bindparam("vals", expanding=True))
                remaining_orphans += self.session.execute(stmt, {"vals": chunk}).scalar() or 0

            results[table_name]['still_orphaned'] = remaining_orphans

            # `remaining_orphans` is measured after this method NULLed (or
            # quarantined) everything it could not repair by name. A non-zero
            # count here therefore means rows survived with a non-null FK that
            # still does not resolve — which should not happen unless the FK
            # column is non-nullable and the quarantine path was skipped.
            if remaining_orphans:
                logger.error(
                    f"    {table_name}.{fk_col}: {remaining_orphans} rows still "
                    f"carry an unresolvable FK after repair and NULL/quarantine. "
                    f"Manual review required."
                )
            else:
                logger.info(
                    f"    {table_name}.{fk_col}: repair complete "
                    f"({results[table_name]['fixed']} fixed, "
                    f"{results[table_name]['orphaned']} orphaned)"
                )

        return results

    # ===================== LOCATION SPECIFIC FIXES =====================

    def fix_orphaned_burden_records(self) -> int:
        """
        Repair fact_disease_burden rows whose location_id does not resolve to
        a valid dim_location entry.

        Each orphan is mapped to a target location (name-match, then the most
        common location in fact_disease_case as fallback), then reassigned
        via _aggregate_and_reassign. When the reassignment collides on the
        (location_id, year, disease_code) unique constraint, the rows are
        aggregated (SUM cases_mean, AVG rates) rather than the orphan being
        dropped.
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

        # _chunked_in double-counts when chunked; deduplicate by burden_id.
        seen = set()
        deduped = []
        for row in orphaned_burden:
            bid = row[0]
            if bid in seen:
                continue
            seen.add(bid)
            deduped.append(row)
        orphaned_burden = deduped

        if not orphaned_burden:
            logger.info("No orphaned burden records found")
            return 0

        logger.info(f"Found {len(orphaned_burden)} orphaned burden records")

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

        # ---- Build a mapping {bad_location_id -> new_location_id} ----------
        # Compute all targets first, then execute the reassignments. This
        # avoids updating a row whose target has already been consumed by
        # an earlier orphan in the same loop.
        reassignments: Dict[Any, Any] = {}
        unresolved: List[Any] = []

        for record in orphaned_burden:
            (burden_id, bad_location_id, year, disease_code,
            incidence_rate, cases_mean) = record

            new_location_id = None
            if isinstance(bad_location_id, str):
                bad_str = bad_location_id.lower()
                for loc_id, info in valid_locations.items():
                    if info['name'] and (bad_str in info['name']
                                        or info['name'] in bad_str):
                        new_location_id = loc_id
                        break

            if new_location_id is None:
                new_location_id = fallback_location_id

            if new_location_id is None:
                unresolved.append(bad_location_id)
                continue

            reassignments[bad_location_id] = new_location_id

        if unresolved:
            logger.warning(
                f"  Could not resolve a target location for "
                f"{len(unresolved)} orphan values; leaving those rows as-is."
            )

        fixed_count = 0
        for bad_id, new_id in reassignments.items():
            if bad_id == new_id:
                continue
            try:
                self._aggregate_and_reassign(
                    "fact_disease_burden", "location_id", bad_id, new_id
                )
                fixed_count += 1
            except Exception as e:
                logger.warning(
                    f"  Could not reassign burden location {bad_id} -> "
                    f"{new_id}: {e}"
                )

        self.session.commit()
        logger.info(f"Repaired {fixed_count} orphan location groups")
        return fixed_count


    # ===================== RESISTANCE STATUS =====================

    def _compute_resistance_status(self):
        """
        Compute resistance_status from whichever counts are available.

        Four sequential passes, strongest signal first. Each pass only
        touches rows where resistance_status is still NULL, so passes do
        not overwrite each other and the method is idempotent.
        """
        logger.info("=" * 60)
        logger.info("COMPUTING RESISTANCE STATUS")
        logger.info("=" * 60)

        total = self.session.execute(
            text("SELECT COUNT(*) FROM fact_insecticide_resistance")
        ).scalar() or 0

        if total == 0:
            logger.info("No resistance records found")
            return

        # Pass 1: tested + dead
        r1 = self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET
                mortality = ROUND((n_insect_dead * 100.0) / n_insect_tested, 2),
                resistance_status = CASE
                    WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 98 THEN 'Susceptible'
                    WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 90 THEN 'Suspected Resistance'
                    ELSE 'Resistant'
                END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND n_insect_tested IS NOT NULL AND n_insect_tested > 0
              AND n_insect_dead   IS NOT NULL AND n_insect_dead  >= 0
        """)).rowcount or 0
        self.session.commit()
        logger.info(f"  Pass 1 (tested + dead):          {r1} rows updated")

        # Pass 2: tested + resistant -> derive dead
        r2 = self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET
                n_insect_dead = n_insect_tested - n_insect_resistant,
                mortality = ROUND(
                    ((n_insect_tested - n_insect_resistant) * 100.0)
                    / n_insect_tested, 2
                ),
                resistance_status = CASE
                    WHEN ((n_insect_tested - n_insect_resistant) * 100.0
                          / n_insect_tested) >= 98 THEN 'Susceptible'
                    WHEN ((n_insect_tested - n_insect_resistant) * 100.0
                          / n_insect_tested) >= 90 THEN 'Suspected Resistance'
                    ELSE 'Resistant'
                END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND n_insect_tested    IS NOT NULL AND n_insect_tested > 0
              AND n_insect_resistant IS NOT NULL
              AND n_insect_resistant <= n_insect_tested
        """)).rowcount or 0
        self.session.commit()
        logger.info(f"  Pass 2 (tested + resistant):     {r2} rows updated")

        # Pass 3: susceptible + resistant
        r3 = self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET
                n_insect_tested = n_insect_susceptible + n_insect_resistant,
                n_insect_dead   = n_insect_susceptible,
                mortality = ROUND(
                    (n_insect_susceptible * 100.0)
                    / (n_insect_susceptible + n_insect_resistant), 2
                ),
                resistance_status = CASE
                    WHEN (n_insect_susceptible * 100.0
                          / (n_insect_susceptible + n_insect_resistant)) >= 98
                        THEN 'Susceptible'
                    WHEN (n_insect_susceptible * 100.0
                          / (n_insect_susceptible + n_insect_resistant)) >= 90
                        THEN 'Suspected Resistance'
                    ELSE 'Resistant'
                END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND n_insect_susceptible IS NOT NULL
              AND n_insect_resistant   IS NOT NULL
              AND (n_insect_susceptible + n_insect_resistant) > 0
        """)).rowcount or 0
        self.session.commit()
        logger.info(f"  Pass 3 (susc + resistant):       {r3} rows updated")

        # Pass 4: pre-computed mortality only
        r4 = self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET resistance_status = CASE
                WHEN mortality >= 98 THEN 'Susceptible'
                WHEN mortality >= 90 THEN 'Suspected Resistance'
                ELSE 'Resistant'
            END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND mortality IS NOT NULL AND mortality BETWEEN 0 AND 100
        """)).rowcount or 0
        self.session.commit()
        logger.info(f"  Pass 4 (pre-computed mortality): {r4} rows updated")

        self._verify_resistance_status()

    def _verify_resistance_status(self):
        status_dist = self.session.execute(text("""
            SELECT resistance_status, COUNT(*) as count
            FROM fact_insecticide_resistance
            WHERE resistance_status IS NOT NULL AND resistance_status != ''
            GROUP BY resistance_status
        """)).fetchall()

        remaining = self.session.execute(text("""
            SELECT COUNT(*) FROM fact_insecticide_resistance
            WHERE resistance_status IS NULL OR resistance_status = ''
        """)).scalar() or 0

        if status_dist:
            logger.info("Resistance status distribution:")
            for status, count in status_dist:
                logger.info(f"  {status}: {count}")
        else:
            logger.warning("No resistance statuses found!")

        if remaining:
            logger.warning(
                f"  {remaining} rows still have no resistance_status "
                f"(no usable counts and no pre-computed mortality)."
            )

    # ===================== BURDEN EXPANSION =====================

    def expand_burden_to_daily(
        self,
        force: bool = False,
        seed: int = 42,
        base_variation: float = 0.25,
    ) -> Dict[str, int]:
        """
        Expand yearly disease-burden rows into daily case records.

        This is the single canonical implementation. Both consolidate_all()
        and the CLI wrapper in fix_cases.py call it.

        Parameters
        ----------
        force : bool
            If True, delete existing ESTIMATE rows for a burden record before
            re-inserting. If False (the default), skip any burden record that
            already has ESTIMATE rows for its (location, year, disease).
        seed : int
            Random seed. Combined with location_id and year so each (location,
            year) reproduces the same daily series across runs.
        base_variation : float
            Log-normal sigma for the per-day noise term, before the internal
            0.7 multiplier. 0.25 is a reasonable default for surveillance data.

        Returns
        -------
        dict
            Counts: inserted, already_present, skipped, errors.
        """
        logger.info("=" * 60)
        logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS")
        logger.info("=" * 60)

        # ---- 0. case_count column must exist --------------------------------
        # The Alembic migration owns this column. Fail loudly if the DB has
        # not been upgraded, rather than silently doing a no-op or ALTER.
        try:
            self.session.execute(text("SELECT case_count FROM fact_disease_case LIMIT 1"))
        except Exception as e:
            logger.error(
                "case_count column missing from fact_disease_case. Run "
                "`alembic upgrade head` before expanding burden data."
            )
            raise RuntimeError(
                "Missing case_count column; run migrations first."
            ) from e

        # ---- 1. Ensure the ESTIMATE method exists ---------------------------
        self.session.execute(text("""
            INSERT INTO dim_identification_method
                (method_code, method_name, method_type)
            VALUES
                ('ESTIMATE', 'Estimated', 'modeled')
            ON CONFLICT (method_code) DO NOTHING
        """))
        self.session.commit()

        # ---- 2. Population lookup -------------------------------------------
        total_burden = self.session.execute(
            text("SELECT COUNT(*) FROM fact_disease_burden")
        ).scalar() or 0
        if total_burden == 0:
            logger.info("No burden records found; nothing to expand.")
            return {"inserted": 0, "already_present": 0, "skipped": 0, "errors": 0}

        pop_rows = self.session.execute(text("""
            SELECT location_id, indicator_value
            FROM fact_socioeconomic_condition
            WHERE indicator_code = 'SP.POP.TOTL'
        """)).fetchall()
        population_map = {r[0]: r[1] for r in pop_rows}

        default_population = 1_000_000
        for loc_id_row in self.session.execute(
            text("SELECT location_id FROM dim_location")
        ).fetchall():
            loc_id = loc_id_row[0]
            if (loc_id not in population_map
                    or population_map[loc_id] is None
                    or population_map[loc_id] <= 0):
                population_map[loc_id] = default_population

        # ---- 3. Iterate burden rows -----------------------------------------
        burden_records = self.session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, source_code,
                incidence_rate, cases_mean
            FROM fact_disease_burden
            WHERE (incidence_rate IS NOT NULL AND incidence_rate > 0)
            OR (cases_mean      IS NOT NULL AND cases_mean      > 0)
        """)).fetchall()

        if not burden_records:
            logger.warning("No burden records with valid data found.")
            return {"inserted": 0, "already_present": 0, "skipped": 0, "errors": 0}

        is_sqlite = "sqlite" in str(self.session.bind.url).lower()
        stats = {"inserted": 0, "already_present": 0, "skipped": 0, "errors": 0}

        for record in burden_records:
            (burden_id, location_id, year, disease_code,
            source_code, incidence_rate, cases_mean) = record

            population = population_map.get(location_id, default_population)
            if incidence_rate is not None and incidence_rate > 0 and population > 0:
                annual_cases = (incidence_rate / 100_000) * population
            elif cases_mean is not None and cases_mean > 0:
                annual_cases = cases_mean
            else:
                stats["skipped"] += 1
                continue
            annual_cases = max(annual_cases, 1)

            start_str = f"{year}-01-01"
            end_str   = f"{year}-12-31"

            # ---- Idempotency check ------------------------------------------
            existing = self.session.execute(text("""
                SELECT COUNT(*) FROM fact_disease_case
                WHERE location_id = :loc_id
                AND date_id_onset >= :start_date
                AND date_id_onset <= :end_date
                AND disease_code = :disease
                AND identification_method_code = 'ESTIMATE'
            """), {
                "loc_id":     location_id,
                "start_date": start_str,
                "end_date":   end_str,
                "disease":    disease_code,
            }).scalar() or 0

            if existing > 0 and not force:
                logger.info(
                    f"  Skipping burden_id {burden_id} "
                    f"(location={location_id}, year={year}, disease={disease_code}): "
                    f"{existing} ESTIMATE rows already present. Use force=True to overwrite."
                )
                stats["already_present"] += 1
                continue

            # ---- Generate daily series -------------------------------------
            daily_df = _burden_generate_daily_case_counts(
                annual_cases=annual_cases,
                year=year,
                location_id=location_id,
                base_variation=base_variation,
                seed=seed,
            )
            logger.info(
                f"  burden_id {burden_id} (loc={location_id}, year={year}): "
                f"{len(daily_df)} days, mean={daily_df['case_count'].mean():.1f}, "
                f"min={daily_df['case_count'].min()}, "
                f"max={daily_df['case_count'].max()}, "
                f"std={daily_df['case_count'].std():.1f}"
            )

            # ---- Write ------------------------------------------------------
            try:
                if existing > 0:
                    deleted = self.session.execute(text("""
                        DELETE FROM fact_disease_case
                        WHERE location_id = :loc_id
                        AND date_id_onset >= :start_date
                        AND date_id_onset <= :end_date
                        AND disease_code = :disease
                        AND identification_method_code = 'ESTIMATE'
                    """), {
                        "loc_id":     location_id,
                        "start_date": start_str,
                        "end_date":   end_str,
                        "disease":    disease_code,
                    }).rowcount or 0
                    logger.info(f"    Deleted {deleted} existing ESTIMATE rows")

                batch_size = 5000
                records_to_insert = []
                for idx, row in daily_df.iterrows():
                    records_to_insert.append({
                        "loc_id":     location_id,
                        "date_id":    row["date_id"],
                        "disease":    disease_code,
                        "src_code":   source_code,
                        "case_count": int(row["case_count"]),
                    })
                    is_last = (idx == len(daily_df) - 1)
                    if len(records_to_insert) >= batch_size or is_last:
                        if is_sqlite:
                            for rec in records_to_insert:
                                self.session.execute(text("""
                                    INSERT INTO fact_disease_case (
                                        location_id, date_id_onset, date_id_reported,
                                        disease_code, identification_method_code,
                                        source_code, case_status, host_type,
                                        age_group, sex, outcome, case_count
                                    ) VALUES (
                                        :loc_id, :date_id, :date_id,
                                        :disease, 'ESTIMATE',
                                        :src_code, 'confirmed', 'human',
                                        'all', 'unknown', 'unknown', :case_count
                                    )
                                """), rec)
                        else:
                            self.session.execute(text("""
                                INSERT INTO fact_disease_case (
                                    location_id, date_id_onset, date_id_reported,
                                    disease_code, identification_method_code,
                                    source_code, case_status, host_type,
                                    age_group, sex, outcome, case_count
                                ) VALUES (
                                    :loc_id, :date_id, :date_id,
                                    :disease, 'ESTIMATE',
                                    :src_code, 'confirmed', 'human',
                                    'all', 'unknown', 'unknown', :case_count
                                )
                            """), records_to_insert)
                        records_to_insert = []

                self.session.commit()
                stats["inserted"] += len(daily_df)

            except Exception as e:
                logger.error(f"    Error expanding burden_id {burden_id}: {e}")
                self.session.rollback()
                stats["errors"] += 1
                continue

        logger.info("=" * 60)
        logger.info("BURDEN EXPANSION COMPLETE")
        logger.info(f"  Inserted:          {stats['inserted']} daily records")
        logger.info(f"  Already present:   {stats['already_present']} burden records skipped")
        logger.info(f"  Skipped (no data): {stats['skipped']}")
        logger.info(f"  Errors:            {stats['errors']}")
        logger.info("=" * 60)

        return stats

    # ===================== DUPLICATE RESOLUTION =====================

    def resolve_fact_table_duplicates(self):
        """
        Resolve duplicates across fact tables.

        Every rebuild MUST preserve every column of the target table.
        Any column not present in the SELECT list is silently NULLed (or
        reset to its column default) by the rebuild. This method was the
        source of two silent data losses in earlier revisions:
            - fact_insecticide_resistance lost n_insect_susceptible,
              n_insect_resistant, all vgsc_* columns, exposure_time,
              vgsc_generation, and resistance_status.
            - fact_disease_case lost case_count (reset to 1).
        Both are fixed here by enumerating every column explicitly.

        Additionally, every block now guards its DELETE-then-INSERT
        rebuild with an explicit duplicate-count check. On a clean
        database, no table is touched at all. This eliminates the
        failure mode where an empty or partially-populated temp table
        combined with an unconditional DELETE would silently wipe an
        entire fact table.
        """
        logger.info("Resolving fact table duplicates...")

        # ---- fact_environment -------------------------------------------
        # Unique constraint: (location_id, date_id, variable).
        try:
            dup_count = self.session.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT location_id, date_id, variable
                    FROM fact_environment
                    GROUP BY location_id, date_id, variable
                    HAVING COUNT(*) > 1
                )
            """)).scalar() or 0

            if dup_count > 0:
                self.session.execute(text("""
                    CREATE TEMP TABLE temp_env_dedup AS
                    SELECT
                        location_id, date_id, variable,
                        AVG(value) AS value,
                        MIN(source_code) AS source_code
                    FROM fact_environment
                    GROUP BY location_id, date_id, variable
                """))
                self.session.execute(text("DELETE FROM fact_environment"))
                self.session.execute(text("""
                    INSERT INTO fact_environment
                        (location_id, date_id, variable, value, source_code)
                    SELECT location_id, date_id, variable, value, source_code
                    FROM temp_env_dedup
                """))
                self.session.execute(text("DROP TABLE temp_env_dedup"))
                self.session.commit()
                logger.info(f"  Deduplicated {dup_count} environment-key groups")
            else:
                logger.info("  No environment duplicates; fact_environment untouched")
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to deduplicate fact_environment: {e}")

        # ---- fact_insecticide_resistance ----------------------------------
        # Unique constraint: (location_id, date_id, vector_code,
        # insecticide_code, test_type_code, source_code, concentration).
        # Full column list preserved. resistance_status deliberately left
        # NULL here; _compute_resistance_status() runs after this method.
        try:
            dup_count = self.session.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT location_id, date_id, vector_code, insecticide_code,
                           test_type_code, source_code, concentration
                    FROM fact_insecticide_resistance
                    GROUP BY location_id, date_id, vector_code, insecticide_code,
                             test_type_code, source_code, concentration
                    HAVING COUNT(*) > 1
                )
            """)).scalar() or 0

            if dup_count > 0:
                self.session.execute(text("""
                    CREATE TEMP TABLE temp_ir_dedup AS
                    SELECT
                        location_id, date_id, vector_code, insecticide_code,
                        test_type_code, source_code, concentration,
                        MIN(exposure_time)        AS exposure_time,
                        SUM(n_insect_tested)      AS n_insect_tested,
                        SUM(n_insect_susceptible) AS n_insect_susceptible,
                        SUM(n_insect_resistant)   AS n_insect_resistant,
                        SUM(n_insect_dead)        AS n_insect_dead,
                        SUM(vgsc_n_insect_tested) AS vgsc_n_insect_tested,
                        SUM(vgsc995l_vgsc995l_n)  AS vgsc995l_vgsc995l_n,
                        SUM(vgsc995l_vgsc995f_n)  AS vgsc995l_vgsc995f_n,
                        SUM(vgsc995f_vgsc995f_n)  AS vgsc995f_vgsc995f_n,
                        SUM(vgsc995l_vgsc995s_n)  AS vgsc995l_vgsc995s_n,
                        SUM(vgsc995s_vgsc995s_n)  AS vgsc995s_vgsc995s_n,
                        SUM(vgsc995f_vgsc995s_n)  AS vgsc995f_vgsc995s_n,
                        MIN(vgsc_generation)      AS vgsc_generation,
                        CASE
                            WHEN SUM(n_insect_tested) > 0
                            THEN ROUND(
                                (CAST(SUM(n_insect_dead) AS FLOAT)
                                 / SUM(n_insect_tested)) * 100, 2)
                            ELSE NULL
                        END AS mortality
                    FROM fact_insecticide_resistance
                    GROUP BY location_id, date_id, vector_code, insecticide_code,
                             test_type_code, source_code, concentration
                """))
                self.session.execute(text("DELETE FROM fact_insecticide_resistance"))
                self.session.execute(text("""
                    INSERT INTO fact_insecticide_resistance (
                        location_id, date_id, vector_code, insecticide_code,
                        test_type_code, source_code, concentration,
                        exposure_time,
                        n_insect_tested, n_insect_susceptible, n_insect_resistant,
                        n_insect_dead,
                        vgsc_n_insect_tested,
                        vgsc995l_vgsc995l_n, vgsc995l_vgsc995f_n,
                        vgsc995f_vgsc995f_n, vgsc995l_vgsc995s_n,
                        vgsc995s_vgsc995s_n, vgsc995f_vgsc995s_n,
                        vgsc_generation, mortality
                    )
                    SELECT
                        location_id, date_id, vector_code, insecticide_code,
                        test_type_code, source_code, concentration,
                        exposure_time,
                        n_insect_tested, n_insect_susceptible, n_insect_resistant,
                        n_insect_dead,
                        vgsc_n_insect_tested,
                        vgsc995l_vgsc995l_n, vgsc995l_vgsc995f_n,
                        vgsc995f_vgsc995f_n, vgsc995l_vgsc995s_n,
                        vgsc995s_vgsc995s_n, vgsc995f_vgsc995s_n,
                        vgsc_generation, mortality
                    FROM temp_ir_dedup
                """))
                self.session.execute(text("DROP TABLE temp_ir_dedup"))
                self.session.commit()
                logger.info(f"  Deduplicated {dup_count} resistance-key groups")
            else:
                logger.info("  No resistance duplicates; fact_insecticide_resistance untouched")
        except Exception as e:
            self.session.rollback()
            logger.error(f"Failed to deduplicate fact_insecticide_resistance: {e}")

        # ---- fact_disease_case -------------------------------------------
        # Groups on the FULL natural key (case_status, age_group, sex
        # included). SUMs case_count. Only rewrites the table if genuine
        # duplicates exist.
        try:
            dup_count = self.session.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT location_id, date_id_onset, disease_code,
                           identification_method_code, source_code,
                           case_status, age_group, sex
                    FROM fact_disease_case
                    GROUP BY 1,2,3,4,5,6,7,8
                    HAVING COUNT(*) > 1
                )
            """)).scalar() or 0

            if dup_count > 0:
                self.session.execute(text("""
                    CREATE TEMP TABLE temp_case_dedup AS
                    SELECT
                        location_id, date_id_onset, disease_code,
                        identification_method_code, source_code,
                        case_status, age_group, sex,
                        MIN(date_id_reported) AS date_id_reported,
                        MIN(host_type)        AS host_type,
                        MIN(age_years)        AS age_years,
                        MIN(outcome)          AS outcome,
                        SUM(case_count)       AS case_count
                    FROM fact_disease_case
                    GROUP BY location_id, date_id_onset, disease_code,
                             identification_method_code, source_code,
                             case_status, age_group, sex
                """))
                self.session.execute(text("DELETE FROM fact_disease_case"))
                self.session.execute(text("""
                    INSERT INTO fact_disease_case (
                        location_id, date_id_onset, date_id_reported,
                        disease_code, identification_method_code,
                        source_code, case_status, host_type,
                        age_years, age_group, sex, outcome, case_count
                    )
                    SELECT
                        location_id, date_id_onset, date_id_reported,
                        disease_code, identification_method_code,
                        source_code, case_status, host_type,
                        age_years, age_group, sex, outcome, case_count
                    FROM temp_case_dedup
                """))
                self.session.execute(text("DROP TABLE temp_case_dedup"))
                self.session.commit()
                logger.info(f"  Deduplicated {dup_count} case-key groups "
                            f"(case_count and age_years preserved)")
            else:
                logger.info("  No case duplicates found; fact_disease_case untouched")
        except Exception as e:
            self.session.rollback()
            logger.warning(f"Could not deduplicate case records: {e}")

    # ===================== MAIN CONSOLIDATION =====================

    def consolidate_all(
        self,
        gadm_gpkg: Optional[str] = None,
        country_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Run the full consolidation pipeline.
        """
        from utilities.admin_alignment import (
            load_admin_hierarchy,
            resolve_parent_locations,
            attach_admin_concepts,
        )

        logger.info("=" * 60)
        logger.info("STARTING FULL DATA CONSOLIDATION")
        logger.info("=" * 60)

        # The rebuild runs first, unconditionally, so any prior
        # cross-level merge damage is corrected before the rest of the
        # pipeline reads the hierarchy.
        logger.info("Step 0a: Rebuilding admin hierarchy from standard_code...")
        rebuild_results = self._rebuild_admin_hierarchy_from_standard_code()

        if gadm_gpkg:
            logger.info("Step 0b: Loading GADM admin hierarchy...")
            load_admin_hierarchy(self.session, gadm_gpkg, country_codes)

            logger.info("Step 0c: Resolving parent locations by coordinates...")
            resolve_parent_locations(
                self.session, gadm_gpkg, country_codes
            )

            logger.info("Step 0d: Reassigning grid-sited facts to admin units...")
            grid_reassign_results = self.reassign_grid_facts_to_admin()
        else:
            grid_reassign_results = {}

        logger.info("Step 1: Aligning foreign keys...")
        fk_results = self.align_all_foreign_keys()

        logger.info("Step 2: Repairing dangling parent references...")
        dangling_fixed = self._repair_dangling_parents()

        logger.info("Step 3: Standardizing vocabularies...")
        vocab_std_results = self.standardize_vocabularies()

        if gadm_gpkg:
            logger.info("Step 3b: Attaching GADM concepts to locations...")
            attach_admin_concepts(self.session)

        logger.info("Step 4: Standardizing country codes...")
        self._standardize_country_codes()

        logger.info("Step 5: Fixing orphaned burden records...")
        burden_fixed = self.fix_orphaned_burden_records()

        logger.info("Step 6: Expanding burden to daily records...")
        self.expand_burden_to_daily(force=False)

        logger.info("Step 7: Consolidating locations (name-based merge)...")
        loc_results = self.consolidate_locations()

        logger.info("Step 7b: Re-checking dangling references after merge...")
        dangling_after_merge = self._repair_dangling_parents()

        logger.info("Step 8: Consolidating vocabulary...")
        vocab_cons_results = self.consolidate_vocabulary()

        logger.info("Step 9: Resolving duplicates...")
        dup_results = self.detect_duplicates()
        self.resolve_fact_table_duplicates()

        logger.info("Step 10: Computing resistance status...")
        self._compute_resistance_status()

        logger.info("Step 11: Final verification...")
        validation = self.validate_country_codes()
        final_orphan_check = self._check_remaining_orphans()
        hierarchy_check = self._check_hierarchy()

        results = {
            "foreign_key_alignment":  fk_results,
            "locations":              loc_results,
            "vocabulary_standard":    vocab_std_results,
            "vocabulary_consolidate": vocab_cons_results,
            "duplicates":             dup_results,
            "validation":             validation,
            "burden_fixed":           burden_fixed,
            "grid_reassignment":      grid_reassign_results,
            "hierarchy_rebuild":      rebuild_results,
            "dangling_parent_fixes":  dangling_fixed + dangling_after_merge,
            "remaining_orphans":      final_orphan_check,
            "hierarchy":              hierarchy_check,
        }

        logger.info("=" * 60)
        logger.info("CONSOLIDATION COMPLETE")
        logger.info(f"  Total locations:       {validation.get('total', 0)}")
        logger.info(f"  Valid country codes:   {validation.get('valid', 0)}")
        logger.info(f"  Burden records fixed:  {burden_fixed}")
        logger.info(f"  Grid-to-admin reassigned:")
        for tbl, n in (grid_reassign_results or {}).items():
            logger.info(f"    {tbl}: {n} rows")
        logger.info(f"  Hierarchy levels fixed:  {rebuild_results['level_fixed']}")
        logger.info(f"  Hierarchy parents fixed: {rebuild_results['parent_fixed']}")
        logger.info(f"  Dangling parents fixed:  {dangling_fixed + dangling_after_merge}")
        logger.info(f"  Remaining orphans:       {final_orphan_check}")
        logger.info(
            f"  Hierarchy: "
            f"{hierarchy_check['placed']} placed / "
            f"{hierarchy_check['total_non_admin']} non-admin"
        )
        logger.info("=" * 60)

        return results


    def _check_hierarchy(self) -> Dict[str, int]:
        """Count how many non-admin locations have a parent."""
        total_non_admin = self.session.execute(text("""
            SELECT COUNT(*) FROM dim_location
            WHERE (origin IS NULL OR origin <> 'gadm')
            AND (admin_level IS NULL OR admin_level NOT LIKE 'admin%')
        """)).scalar() or 0

        placed = self.session.execute(text("""
            SELECT COUNT(*) FROM dim_location
            WHERE (origin IS NULL OR origin <> 'gadm')
            AND parent_location_id IS NOT NULL
        """)).scalar() or 0

        return {"total_non_admin": total_non_admin, "placed": placed}


    def _check_remaining_orphans(self) -> Dict[str, int]:
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
                pk_cols = inspector.get_pk_constraint(ref_table).get('constrained_columns', [])
                if not pk_cols:
                    continue
                pk_col = pk_cols[0]
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

    
    def _rebuild_admin_hierarchy_from_standard_code(self) -> Dict[str, int]:
        """
        Rebuild admin_level and parent_location_id for every location
        whose standard_code is a GADM GID.

        Cross-level merges in earlier revisions collapsed admin units of
        different levels into each other, leaving chains like
        admin3 -> admin3 -> admin3 that violate the GADM hierarchy. This
        method rebuilds the chain from the authoritative source — the
        GID stored in standard_code — so the resulting hierarchy is
        correct regardless of prior damage.

        The GID encodes both level and ancestry:

            KEN           level 0 (admin0)
            KEN.1_1       level 1 (admin1)
            KEN.1.1_1     level 2 (admin2)
            KEN.1.1.1_1   level 3 (admin3)

        The `_<version>` suffix is stripped before parsing. Dot count
        determines level; the parent is the GID with the last segment
        removed.

        Returns
        -------
        dict
            {'level_fixed': int, 'parent_fixed': int, 'orphaned': int}
        """
        logger.info("=" * 60)
        logger.info("REBUILDING ADMIN HIERARCHY FROM STANDARD_CODE")
        logger.info("=" * 60)

        rows = self.session.execute(text("""
            SELECT location_id, standard_code, admin_level, parent_location_id
            FROM dim_location
            WHERE standard_code IS NOT NULL
              AND standard_code != ''
        """)).fetchall()

        if not rows:
            logger.info("  No locations have a standard_code; skipping.")
            return {"level_fixed": 0, "parent_fixed": 0, "orphaned": 0}

        # Map stripped GID -> location_id. First writer wins if the same
        # GID is stored on multiple rows (which would itself be a bug,
        # but we do not want to crash on it).
        gid_to_id: Dict[str, int] = {}
        parsed = []
        for loc_id, sc, cur_level, cur_parent in rows:
            stripped = str(sc).split("_")[0]
            if not stripped:
                continue
            parsed.append((int(loc_id), stripped, cur_level, cur_parent))
            if stripped not in gid_to_id:
                gid_to_id[stripped] = int(loc_id)

        level_fixed = 0
        parent_fixed = 0
        orphaned = 0

        for loc_id, gid, cur_level, cur_parent in parsed:
            dot_count = gid.count(".")
            if dot_count >= 3:
                new_level = "admin3"
            elif dot_count == 2:
                new_level = "admin2"
            elif dot_count == 1:
                new_level = "admin1"
            else:
                new_level = "admin0"

            if "." in gid:
                parent_prefix = gid.rsplit(".", 1)[0]
                new_parent = gid_to_id.get(parent_prefix)
            else:
                new_parent = None

            needs_level = (cur_level != new_level)
            needs_parent = (cur_parent != new_parent)

            if needs_level or needs_parent:
                self.session.execute(text("""
                    UPDATE dim_location
                    SET admin_level = :level, parent_location_id = :parent
                    WHERE location_id = :lid
                """), {"level": new_level, "parent": new_parent, "lid": loc_id})
                if needs_level:
                    level_fixed += 1
                if needs_parent:
                    parent_fixed += 1

            if new_parent is None and dot_count > 0:
                # A child whose parent GID is not present in the map.
                orphaned += 1

        self.session.commit()

        logger.info(f"  Admin levels corrected:  {level_fixed}")
        logger.info(f"  Parent links corrected:  {parent_fixed}")
        if orphaned:
            logger.warning(
                f"  {orphaned} rows reference a parent GID that is not "
                f"present in dim_location; they remain unparented."
            )
        logger.info("=" * 60)

        return {
            "level_fixed":  level_fixed,
            "parent_fixed": parent_fixed,
            "orphaned":     orphaned,
        }


    def _repair_dangling_parents(self) -> int:
        """
        Repair any dim_location row whose parent_location_id points at a
        row that no longer exists.

        Dangling parent references arise when consolidate_locations()
        merges two locations and deletes the non-canonical one without
        updating the rows that pointed at it. The reparenting fix in
        `_merge_by_name` and `_merge_location_clusters` prevents new
        occurrences; this method cleans up any that existed before that
        fix was applied, or that arise from any other code path that
        deletes a parent row.

        Repairs admin3 -> admin2, admin2 -> admin1, admin1 -> admin0 by
        matching on the GADM GID prefix. GADM GIDs are hierarchical by
        dot segment: `KEN.1.1` is an admin2, `KEN.1.1.1` is its admin3
        child. The parent's stripped GID is the child's stripped GID
        with the last segment removed.

        Runs before `align_all_foreign_keys` in `consolidate_all` so
        the FK sweep sees a coherent hierarchy. Idempotent: after the
        first run, the dangling sets are empty and the method is a
        no-op.

        Returns
        -------
        int
            Total number of rows repaired across all levels.
        """
        logger.info("=" * 60)
        logger.info("REPAIRING DANGLING PARENT REFERENCES")
        logger.info("=" * 60)

        total = 0

        for child_level, parent_level in (
            ("admin3", "admin2"),
            ("admin2", "admin1"),
            ("admin1", "admin0"),
        ):
            # Find every child at this level whose parent pointer is
            # broken — either it points at a row that has been deleted,
            # or it was left pointing at a merged-away row.
            dangling = self.session.execute(text("""
                SELECT l.location_id, l.standard_code
                FROM dim_location l
                WHERE l.admin_level = :child
                  AND l.parent_location_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM dim_location x
                      WHERE x.location_id = l.parent_location_id
                  )
            """), {"child": child_level}).fetchall()

            if not dangling:
                continue

            # Build a {stripped_gid: location_id} map for the parent level.
            # `split("_")[0]` strips GADM's `_<version>` suffix, e.g.
            # `KEN.1.1_1` -> `KEN.1.1`.
            parents = self.session.execute(text("""
                SELECT standard_code, location_id
                FROM dim_location
                WHERE admin_level = :parent
                  AND standard_code IS NOT NULL
            """), {"parent": parent_level}).fetchall()

            parent_map: Dict[str, int] = {}
            for sc, lid in parents:
                stripped = str(sc).split("_")[0]
                if stripped not in parent_map:
                    parent_map[stripped] = int(lid)

            fixed = 0
            for loc_id, std_code in dangling:
                if not std_code:
                    continue
                child_stripped = str(std_code).split("_")[0]
                if "." not in child_stripped:
                    continue
                parent_prefix = child_stripped.rsplit(".", 1)[0]
                new_parent = parent_map.get(parent_prefix)
                if new_parent is None:
                    continue
                self.session.execute(
                    text("""
                        UPDATE dim_location
                        SET parent_location_id = :new_parent
                        WHERE location_id = :loc_id
                    """),
                    {"new_parent": new_parent, "loc_id": int(loc_id)},
                )
                fixed += 1

            if fixed:
                logger.info(
                    f"  {child_level} -> {parent_level}: {fixed} repaired"
                )
            total += fixed

        self.session.commit()
        logger.info(f"  Total dangling references repaired: {total}")
        logger.info("=" * 60)
        return total


    # ===================== LOCATION CONSOLIDATION =====================

    def _standardize_country_codes(self, dry_run: bool = False) -> Dict[str, Any]:
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
                        results['fixed_missing'] += 1
                        needs_update = True
                    else:
                        results['unresolved'] += 1
                else:
                    results['unresolved'] += 1

            elif current_code:
                normalized_code = get_iso_country_code(current_code)
                if normalized_code and normalized_code != current_code:
                    if not dry_run:
                        loc.country_code = normalized_code
                    results['normalized_codes'] += 1
                    needs_update = True

            if loc.country_code and loc.country_code in ISO_COUNTRY_CODES:
                correct_name = ISO_TO_COUNTRY_NAME.get(loc.country_code)
                if correct_name and loc.country_name and loc.country_name != correct_name:
                    if not dry_run:
                        loc.country_name = correct_name
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
        """
        Assign a region to climate grid rows that have no country yet.

        Only rows whose coordinates fall inside the study region's
        bounding box are relabelled. The previous version blindly
        reassigned EVERY row with a NULL country_code to Kenya, which is
        how locations in Benin, Nigeria, and Liberia silently disappeared
        into KE on an earlier run.
        """
        region_code = get_iso_country_code(study_region) or 'KE'
        region_name = get_country_name(region_code) or study_region

        # Bounding boxes per supported study region.
        # (lat_min, lat_max, lon_min, lon_max)
        _BBOX = {
            'KE': (-5.0, 5.5, 33.5, 42.0),
        }
        bbox = _BBOX.get(region_code)
        if bbox is None:
            logger.warning(
                f"No bounding box defined for study region {region_code}; "
                f"skipping climate-coordinate binding."
            )
            return

        lat_min, lat_max, lon_min, lon_max = bbox

        result = self.session.execute(text("""
            UPDATE dim_location
            SET country_name = :region_name,
                country_code = :region_code
            WHERE (country_name IS NULL
                   OR country_name = ''
                   OR country_name = 'None'
                   OR country_name = 'UNKNOWN'
                   OR country_code IS NULL
                   OR country_code = ''
                   OR country_code = 'UNKNOWN')
              AND centroid_lat BETWEEN :lat_min AND :lat_max
              AND centroid_lon BETWEEN :lon_min AND :lon_max
        """), {
            "region_name": region_name,
            "region_code": region_code,
            "lat_min": lat_min, "lat_max": lat_max,
            "lon_min": lon_min, "lon_max": lon_max,
        })
        if result.rowcount:
            logger.info(
                f"  Assigned {result.rowcount} NULL-country locations to "
                f"{region_name} (inside bbox)"
            )

        if region_code == 'KE':
            self.session.execute(text("""
                UPDATE dim_location
                SET name = 'Siaya County (Climate Grid)',
                    country_name = :region_name,
                    country_code = :region_code
                WHERE (name LIKE '%/%' OR name IS NULL OR name = '' OR name = 'Siaya')
                  AND centroid_lat BETWEEN :lat_min AND :lat_max
                  AND centroid_lon BETWEEN :lon_min AND :lon_max
            """), {
                "region_name": region_name,
                "region_code": region_code,
                "lat_min": lat_min, "lat_max": lat_max,
                "lon_min": lon_min, "lon_max": lon_max,
            })

        self.session.commit()

    def consolidate_locations(self, distance_threshold_km: float = 5.0,
                              min_samples: int = 1, dry_run: bool = False):
        """Consolidate locations with country code standardization."""
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

        if not loc_non_grid.empty:
            loc_non_grid['name_normalized'] = loc_non_grid['name'].apply(
                self._normalize_location_name
            )

        name_results = (
            self._merge_by_name(loc_non_grid, dry_run)
            if not loc_non_grid.empty else []
        )

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
            'code_standardization':  code_results,
            'original_count':        len(loc_data),
            'grid_count':            len(loc_grid),
            'non_grid_count':        len(loc_non_grid),
            'name_duplicate_groups': name_results,
            'cluster_results':       cluster_results,
            'final_country_fixes':   final_fix,
        }

        if not dry_run:
            self.session.commit()

        return results

    def _is_grid_location(self, name) -> bool:
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
        # Group by admin_level as well as name. Without it, a site named
        # "Siaya" and an admin2 county named "Siaya" are grouped together
        # and one is deleted, collapsing a GADM unit into a non-GADM site.
        group_cols = ['name_normalized', 'admin_level']
        if 'country_code' in loc_data.columns:
            group_cols.append('country_code')

        for (name, admin_level, country_code), group in loc_data.groupby(group_cols):
            if len(group) <= 1:
                continue

            group = group.copy()
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

                        # Reparent children before the delete. The
                        # `location_id != canonical_id` guard prevents a
                        # self-loop when the canonical was itself a child
                        # of the source (which happens when a container
                        # and its contained location are merged).
                        self.session.execute(
                            text("""
                                UPDATE dim_location
                                SET parent_location_id = :new_parent
                                WHERE parent_location_id = :old_parent
                                  AND location_id != :new_parent
                            """),
                            {"new_parent": canonical_id, "old_parent": source_id},
                        )

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

        # Cluster only within the same admin_level. Merging across levels
        # collapses an admin3 into its containing admin2 (or vice versa)
        # when their centroids coincide, producing the "admin3 -> admin3"
        # chains that break the GADM hierarchy.
        for (country, admin_level), group in loc_df.groupby(
            ['country_code', 'admin_level']
        ):
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

            cluster_counter += len(set(clusters)) 

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

            group = group.copy()
            group['has_country'] = group['country_code'].notna()
            group['has_coords'] = group['centroid_lat'].notna() & group['centroid_lon'].notna()
            group_sorted = group.sort_values(
                by=['has_country', 'has_coords', 'name'],
                ascending=[False, False, True],
                na_position='last'
            )

            canonical = group_sorted.iloc[0]
            canonical_id = int(canonical['location_id'])

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

                        # See _merge_by_name for the rationale on the
                        # canonical_id guard.
                        self.session.execute(
                            text("""
                                UPDATE dim_location
                                SET parent_location_id = :new_parent
                                WHERE parent_location_id = :old_parent
                                  AND location_id != :new_parent
                            """),
                            {"new_parent": canonical_id, "old_parent": source_id},
                        )

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
                    # admin_level is NOT overwritten. It was set at insert
                    # time from the GADM layer or the mapping CSV; the
                    # canonical should already carry the correct value.
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
        """
        Reassign fact rows from source location to target location.

        Uses _aggregate_and_reassign, which SUMs counts and AVGs rates
        on natural-key collisions. The previous implementation deleted
        colliding rows; on the current dataset that discarded over
        1,200 vector-observation rows in a single consolidation run.
        """
        fact_tables_with_location = [
            'fact_vector_observation', 'fact_disease_case', 'fact_insecticide_resistance',
            'fact_bionomics', 'fact_intervention', 'fact_socioeconomic_condition',
            'fact_health_facility', 'fact_disease_burden', 'fact_environment',
        ]
        for table in fact_tables_with_location:
            self._aggregate_and_reassign(table, 'location_id', source_id, target_id)

    def fix_location_country_codes(self) -> int:
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

    def _normalize_species_name(self, name: str) -> str:
        """
        Fallback grouping key for vector rows that the vocabulary aligner
        did not stamp with a concept_id.

        Mirrors utilities.vocabulary.loader._norm_species so the fallback
        key and the vocabulary index key agree — two variants that both
        reduce to 'gambiae' will merge together here as well.
        """
        if not name or not isinstance(name, str):
            return ""
        s = name.strip().lower()
        s = re.sub(r"[_\-]", " ", s)
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"^anopheles\s+", "", s)
        s = _SPECIES_QUALIFIER_RE.sub("", s)
        s = re.sub(r"\s+s\.?\s*l\.?$", "", s)
        s = re.sub(r"\s+s\.?\s*s\.?$", "", s)
        s = _SPECIES_STRAIN_RE.sub("", s)
        return s.strip()

    def consolidate_vocabulary(self) -> Dict[str, Any]:
        logger.info("Consolidating vocabulary...")

        results = {}
        results["diseases"] = self._consolidate_dimension(
            DimDisease, "disease_name", "disease_code",
            fallback_key_fn=lambda s: s.strip().lower() if isinstance(s, str) else None,
        )
        results["vectors"] = self._consolidate_dimension(
            DimVector, "species_name", "vector_code",
            fallback_key_fn=self._normalize_species_name,
        )
        results["insecticides"] = self._consolidate_dimension(
            DimInsecticide, "insecticide_name", "insecticide_code",
            fallback_key_fn=lambda s: s.strip().lower() if isinstance(s, str) else None,
        )
        results["sources"] = self._consolidate_dimension(
            DimSource, "source_name", "source_code",
            fallback_key_fn=lambda s: s.strip().lower() if isinstance(s, str) else None,
        )

        self.session.commit()
        return results

    def _pick_canonical_vector(self, records: List[DimVector]) -> DimVector:
        usage: Dict[str, int] = {}
        for rec in records:
            try:
                cnt = self.session.execute(
                    text("SELECT COUNT(*) FROM fact_vector_observation WHERE vector_code = :c"),
                    {"c": rec.vector_code},
                ).scalar() or 0
            except Exception:
                cnt = 0
            usage[rec.vector_code] = cnt

        def score(rec: DimVector) -> int:
            s = 0
            if getattr(rec, "concept_id", None) is not None:
                s += 1_000_000
            if getattr(rec, "standard_code", None):
                s += 100_000
            name = (rec.species_name or "").strip()
            if name.lower().startswith("anopheles"):
                s += 10_000
            s += len(name)
            s += min(usage.get(rec.vector_code, 0), 9_999)
            return s

        return max(records, key=score)

    def _consolidate_dimension(self, model_class, name_col, code_col,
                               fallback_key_fn=None) -> Dict[str, Any]:
        records = self.session.query(model_class).all()
        if not records:
            return {"status": "no_data"}

        def key_of(rec):
            cid = getattr(rec, "concept_id", None)
            if cid is not None:
                return f"concept:{cid}"
            raw = getattr(rec, name_col)
            if not raw:
                return None
            if fallback_key_fn is not None:
                k = fallback_key_fn(raw)
                return f"name:{k}" if k else None
            return f"name:{raw}"

        groups: Dict[str, List] = {}
        for rec in records:
            k = key_of(rec)
            if k:
                groups.setdefault(k, []).append(rec)

        duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not duplicate_groups:
            return {"status": "ok", "duplicates_found": 0, "merged": []}

        is_vector = (model_class is DimVector)

        merged = []
        for key, members in duplicate_groups.items():
            if is_vector:
                canonical = self._pick_canonical_vector(members)
            else:
                canonical = sorted(members, key=lambda r: str(getattr(r, code_col)))[0]

            canonical_code = getattr(canonical, code_col)
            canonical_name = getattr(canonical, name_col)

            duplicate_codes = []
            for dup in members:
                if dup is canonical:
                    continue
                dup_code = getattr(dup, code_col)
                self._update_fact_references(model_class, dup_code, canonical_code)
                self.session.delete(dup)
                duplicate_codes.append(dup_code)

            merged.append({
                "group_key":       key,
                "canonical_code":  canonical_code,
                "canonical_name":  canonical_name,
                "merged_count":    len(members),
                "duplicate_codes": duplicate_codes,
                "member_names":    [getattr(m, name_col) for m in members],
            })

        self.session.commit()
        return {"status": "ok", "duplicates_found": len(duplicate_groups), "merged": merged}

    def _update_fact_references(self, dimension_model, old_code: str, new_code: str):
        """
        Reassign a dimension FK across every fact table that references it.

        Uses _aggregate_and_reassign so that natural-key collisions after
        the reassignment are resolved by aggregation, not deletion.
        """
        mapping = _DIMENSION_FACT_MAP.get(dimension_model)
        if not mapping:
            return
        for table_name, fk_col in zip(mapping['fact_tables'], mapping['fk_columns']):
            self._aggregate_and_reassign(table_name, fk_col, old_code, new_code)

    

    def _aggregate_and_reassign(self, table_name: str, fk_col: str,
                        old_value, new_value) -> None:
        """
        Reassign `table_name.fk_col` from old_value to new_value.

        Where the reassignment would produce two rows with the same natural
        key, the two rows are aggregated (SUM counts, AVG rates) rather than
        one being deleted.

        Columns listed under `derived` in the spec are NULLed out by the
        aggregation and recomputed afterwards by whatever pass owns them
        (currently _compute_resistance_status for fact_insecticide_resistance).

        Autoincrement primary keys are deliberately omitted from the INSERT
        column list — after aggregation, the old IDs are meaningless, and
        the DB assigns fresh ones.
        """
        if old_value == new_value:
            return

        spec = _FACT_AGGREGATION_SPEC.get(table_name)
        if spec is None:
            # No aggregation rules for this table. Plain UPDATE, catching
            # unique-constraint collisions.
            try:
                result = self.session.execute(
                    text(f"UPDATE {table_name} SET {fk_col} = :new WHERE {fk_col} = :old"),
                    {"new": new_value, "old": old_value},
                )
                self.session.commit()
                if result.rowcount:
                    logger.info(
                        f"    Plain reassign {table_name}.{fk_col}: "
                        f"{old_value} -> {new_value} ({result.rowcount} rows)"
                    )
            except IntegrityError:
                self.session.rollback()
                logger.error(
                    f"    Unique-constraint collision on {table_name}.{fk_col} "
                    f"when reassigning {old_value} -> {new_value}. No aggregation "
                    f"spec defined; manual review required. Rolling back this "
                    f"reassign; rows left untouched."
                )
                raise
            return

        # ---- Spec coverage check (fails loudly, not silently) ----------------
        self._validate_aggregation_spec(table_name, spec)

        nk      = spec["natural_key"]
        sums    = spec["sums"]
        means   = spec["means"]
        first   = spec["first"]
        derived = spec.get("derived", [])

        # Build the SELECT list and track the exact column order of the temp
        # table so the INSERT column list matches it 1:1.
        select_parts = [f"'{new_value}' AS {fk_col}"]
        temp_cols = [fk_col]
        for col in nk:
            if col == fk_col:
                continue
            select_parts.append(col)
            temp_cols.append(col)
        for col in sums:
            select_parts.append(f"SUM({col}) AS {col}")
            temp_cols.append(col)
        for col in means:
            select_parts.append(f"AVG({col}) AS {col}")
            temp_cols.append(col)
        for col in first:
            select_parts.append(f"MIN({col}) AS {col}")
            temp_cols.append(col)
        # `derived` columns are deliberately NOT selected. They will be
        # inserted as NULL (their column default) and recomputed by the
        # owning pass.
        select_sql = ", ".join(select_parts)

        group_cols = [c for c in nk if c != fk_col]
        group_by_sql = ("GROUP BY " + ", ".join(group_cols)) if group_cols else ""

        inspector = inspect(self.session.bind)

        # Exclude autoincrement / primary-key columns from the INSERT.
        pk_cols = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns", []) or []
        )
        insert_cols = [c for c in temp_cols if c not in pk_cols]

        try:
            self.session.execute(text(f"""
                CREATE TEMP TABLE _agg_{table_name} AS
                SELECT {select_sql}
                FROM {table_name}
                WHERE {fk_col} IN (:old_code, :new_code)
                {group_by_sql}
            """), {"old_code": old_value, "new_code": new_value})

            self.session.execute(text(
                f"DELETE FROM {table_name} WHERE {fk_col} IN (:old_code, :new_code)"
            ), {"old_code": old_value, "new_code": new_value})

            self.session.execute(text(f"""
                INSERT INTO {table_name} ({', '.join(insert_cols)})
                SELECT {', '.join(insert_cols)} FROM _agg_{table_name}
            """))

            self.session.execute(text(f"DROP TABLE _agg_{table_name}"))
            self.session.commit()
            logger.info(
                f"    Aggregated + reassigned {table_name}.{fk_col}: "
                f"{old_value} -> {new_value} "
                f"({len(insert_cols)} columns preserved"
                + (f", {len(derived)} derived column(s) reset for recomputation)"
                if derived else ")")
            )
        except Exception as e:
            self.session.rollback()
            logger.error(
                f"    Aggregation failed for {table_name}.{fk_col} "
                f"({old_value} -> {new_value}): {e}"
            )
            raise

        # If this table owns derived columns, refresh them now so callers
        # that invoke _aggregate_and_reassign outside of consolidate_all()
        # still get correct values without a manual second pass.
        if table_name == "fact_insecticide_resistance" and derived:
            try:
                self._compute_resistance_status()
            except Exception as e:
                logger.warning(
                    f"    Recompute of derived columns for "
                    f"{table_name} failed: {e}"
                )


    def _validate_aggregation_spec(self, table_name: str, spec: dict) -> None:
        """
        Warn (once per process, per table) if the aggregation spec does not
        account for every non-PK column of the table.

        Covered columns are the union of:
            natural_key, sums, means, first, derived
        Autoincrement PKs are always excluded (the DB assigns fresh ones).

        Any other column is a bug in the spec — it will be silently dropped
        by the aggregation INSERT. This guard was added after two such bugs
        (mortality/resistance_status on fact_insecticide_resistance,
        age_years on fact_disease_case) shipped and lost data on collision.
        """
        if not hasattr(self, "_validated_specs"):
            self._validated_specs: set = set()
        if table_name in self._validated_specs:
            return
        self._validated_specs.add(table_name)

        inspector = inspect(self.session.bind)
        try:
            table_cols = [c["name"] for c in inspector.get_columns(table_name)]
        except Exception:
            return

        pk_cols = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns", []) or []
        )

        covered = (
            set(spec.get("natural_key", []))
            | set(spec.get("sums",  []))
            | set(spec.get("means", []))
            | set(spec.get("first", []))
            | set(spec.get("derived", []))
        )

        uncovered = [c for c in table_cols if c not in covered and c not in pk_cols]
        if uncovered:
            logger.error(
                f"    AGGREGATION SPEC BUG: {table_name} columns {uncovered} "
                f"are not covered by _FACT_AGGREGATION_SPEC and will be "
                f"silently dropped on natural-key collision. Add each to "
                f"sums / means / first / derived."
            )


    # ===================== DUPLICATE DETECTION =====================

    def detect_duplicates(self) -> Dict[str, Any]:
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

    # ===================== VOCABULARY STANDARDIZATION =====================

    def standardize_vocabularies(self, vocab_dir: str = "vocabularies",
                                fuzzy_threshold: float = 0.85,
                                dry_run: bool = False) -> Dict[str, Any]:
        logger.info("=" * 60)
        logger.info("STANDARDIZING VOCABULARIES")
        logger.info("=" * 60)

        loader = VocabularyLoader(self.session)

        vocab_files = {
            "GADM":   (f"{vocab_dir}/gadm_locations.csv", "location"),
            "IDOMAL": (f"{vocab_dir}/idomal_diseases.csv", "disease"),
            "NCBI":   (f"{vocab_dir}/ncbi_vectors.csv",   "vector"),
        }
        for vocab_id, (path, domain) in vocab_files.items():
            if Path(path).exists():
                loader.load_csv(vocab_id, path, domain)
                logger.info(f"  Loaded {vocab_id} vocabulary from {path}")

        match_targets = [
            ("dim_location", "name",         "location", "GADM"),
            ("dim_vector",   "species_name", "vector",   "NCBI"),
            ("dim_disease",  "disease_name", "disease",  "IDOMAL"),
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
                        f"({v['exact']} exact, {v['normalized']} normalized, "
                        f"{v['fuzzy']} fuzzy, {v['unmatched']} unmatched)")
        logger.info("=" * 60)

        return results

    def _match_column(self, table: str, column: str, domain: str,
                      vocab_id: str, loader, threshold: float,
                      dry_run: bool) -> Dict[str, int]:
        try:
            rows = self.session.execute(
                text(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL")
            ).fetchall()
        except Exception as e:
            logger.error(f"  Could not read distinct values from {table}.{column}: {e}")
            return {"total": 0, "matched": 0, "exact": 0, "normalized": 0,
                    "fuzzy": 0, "unmatched": 0, "errors": 1, "concept_ids_set": 0}

        raw_values = [r[0] for r in rows]

        stats: Dict[str, int] = {
            "total": len(raw_values), "matched": 0, "exact": 0, "normalized": 0,
            "fuzzy": 0, "unmatched": 0, "errors": 0, "concept_ids_set": 0,
        }

        if not raw_values:
            return stats

        for value in raw_values:
            try:
                concept, method, score = loader.match(
                    value, vocab_id, domain, threshold=threshold
                )
            except Exception as e:
                logger.warning(f"  Match failed for '{value}' against {vocab_id}: {e}")
                stats["errors"] += 1
                continue

            concept_code = concept.get("concept_code") if concept else None
            confident = method in ("exact", "normalized")

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

            try:
                with self.session.begin_nested():
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
                            "t": table, "c": column, "v": value,
                            "vocab": vocab_id if concept else None,
                            "code": concept_code,
                            "method": method,
                            "score": score,
                            "approved": confident,
                        },
                    )

                    if concept and confident:
                        cid_row = self.session.execute(
                            text("SELECT concept_id FROM dim_concept "
                                 "WHERE vocabulary_id = :v AND concept_code = :c"),
                            {"v": vocab_id, "c": concept_code},
                        ).first()
                        concept_id = cid_row[0] if cid_row else None

                        self.session.execute(
                            text(f"UPDATE {table} "
                                 f"SET standard_code = :code, "
                                 f"    concept_id    = COALESCE(:cid, concept_id) "
                                 f"WHERE {column} = :value"),
                            {"code": concept_code, "cid": concept_id, "value": value},
                        )
                        if concept_id is not None:
                            stats["concept_ids_set"] += 1
            except Exception as e:
                logger.warning(
                    f"  Could not record match for {table}.{column} = '{value}': {e}"
                )
                stats["errors"] += 1

        if not dry_run:
            try:
                self.session.commit()
            except Exception as e:
                logger.error(f"  Commit failed for {table}.{column}: {e}")
                self.session.rollback()
                stats["errors"] += 1

        return stats

    
    def _aggregate_and_reassign_bulk(
        self,
        table_name: str,
        fk_col: str,
        old_values: List[Any],
        new_value: Any,
    ) -> int:
        """
        Reassign `table_name.fk_col` from a set of old values to a single
        new_value, aggregating natural-key collisions.

        This is the bulk variant of `_aggregate_and_reassign`. It exists
        because rolling up hundreds of grid cells into a single admin
        unit is common, and one transaction per pair is prohibitively
        slow on a table with millions of rows.

        Semantics match `_aggregate_and_reassign`:
            - SUM on columns listed under `sums`.
            - AVG on columns listed under `means`.
            - MIN on columns listed under `first`.
            - Columns listed under `derived` are set to NULL and must be
              recomputed by whatever pass owns them (currently
              _compute_resistance_status for fact_insecticide_resistance).
            - Autoincrement primary keys are omitted from the INSERT.

        Tables without a `_FACT_AGGREGATION_SPEC` entry fall back to a
        plain UPDATE with a unique-constraint guard.

        Returns the number of rows in the post-aggregation result.
        """
        if not old_values:
            return 0

        old_values = [v for v in old_values if v != new_value]
        if not old_values:
            return 0

        spec = _FACT_AGGREGATION_SPEC.get(table_name)

        # ---- Plain UPDATE for tables without aggregation rules. ----
        if spec is None:
            try:
                stmt = text(
                    f"UPDATE {table_name} SET {fk_col} = :new "
                    f"WHERE {fk_col} IN :olds"
                ).bindparams(bindparam("olds", expanding=True))
                result = self.session.execute(
                    stmt, {"new": new_value, "olds": old_values}
                )
                self.session.commit()
                return result.rowcount or 0
            except IntegrityError:
                self.session.rollback()
                logger.error(
                    f"    Unique-constraint collision on {table_name}.{fk_col} "
                    f"reassigning {len(old_values)} values -> {new_value}. "
                    f"No aggregation spec; manual review required."
                )
                raise

        # ---- Aggregation-aware rebuild. ----
        self._validate_aggregation_spec(table_name, spec)

        nk      = spec["natural_key"]
        sums    = spec["sums"]
        means   = spec["means"]
        first   = spec["first"]
        derived = spec.get("derived", [])

        # The new value is an integer surrogate key; embed it as a literal
        # so the SELECT is a simple projection and the expanding bindparam
        # only carries the WHERE-list.
        new_literal = int(new_value)

        select_parts = [f"{new_literal} AS {fk_col}"]
        temp_cols    = [fk_col]
        for col in nk:
            if col == fk_col:
                continue
            select_parts.append(col)
            temp_cols.append(col)
        for col in sums:
            select_parts.append(f"SUM({col}) AS {col}")
            temp_cols.append(col)
        for col in means:
            select_parts.append(f"AVG({col}) AS {col}")
            temp_cols.append(col)
        for col in first:
            select_parts.append(f"MIN({col}) AS {col}")
            temp_cols.append(col)

        select_sql   = ", ".join(select_parts)
        group_cols   = [c for c in nk if c != fk_col]
        group_by_sql = ("GROUP BY " + ", ".join(group_cols)) if group_cols else ""

        inspector = inspect(self.session.bind)
        pk_cols = set(
            inspector.get_pk_constraint(table_name)
                     .get("constrained_columns", []) or []
        )
        insert_cols = [c for c in temp_cols if c not in pk_cols]

        touched  = list(old_values) + [new_value]
        tmp_name = f"_bulk_{table_name}"

        try:
            create_stmt = text(f"""
                CREATE TEMP TABLE {tmp_name} AS
                SELECT {select_sql}
                FROM {table_name}
                WHERE {fk_col} IN :olds
                {group_by_sql}
            """).bindparams(bindparam("olds", expanding=True))
            self.session.execute(create_stmt, {"olds": touched})

            staged = self.session.execute(
                text(f"SELECT COUNT(*) FROM {tmp_name}")
            ).scalar() or 0

            delete_stmt = text(
                f"DELETE FROM {table_name} WHERE {fk_col} IN :olds"
            ).bindparams(bindparam("olds", expanding=True))
            self.session.execute(delete_stmt, {"olds": touched})

            self.session.execute(text(f"""
                INSERT INTO {table_name} ({', '.join(insert_cols)})
                SELECT {', '.join(insert_cols)} FROM {tmp_name}
            """))

            self.session.execute(text(f"DROP TABLE {tmp_name}"))
            self.session.commit()

            logger.info(
                f"    Bulk-aggregated {table_name}.{fk_col}: "
                f"{len(old_values)} old -> {new_value} "
                f"({staged} rows"
                + (f", {len(derived)} derived col(s) reset)" if derived else ")")
            )
            return staged

        except Exception as e:
            self.session.rollback()
            logger.error(
                f"    Bulk aggregation failed for {table_name}.{fk_col} "
                f"({len(old_values)} old -> {new_value}): {e}"
            )
            raise


    def reassign_grid_facts_to_admin(self) -> Dict[str, int]:
        """
        Reassign fact rows from environment grid cells to their nearest
        non-grid administrative ancestor.

        Runs after `resolve_parent_locations()` has populated the grid
        cells' parent_location_id, and before `expand_burden_to_daily()`
        so that newly generated daily case rows inherit an admin-sited
        location from their burden row rather than a grid cell.

        Only tables whose semantic meaning is "an administrative
        observation" are reassigned:

            fact_disease_case    cases are reported per admin unit
            fact_disease_burden  same
            fact_intervention    MAP ITN rasters are per-pixel; the study
                                 area is admin2, so the intervention
                                 coverage must be rolled up

        Deliberately NOT reassigned:

            fact_environment       per-pixel by design; rolling up to
                                   admin loses the spatial detail the
                                   vector-climate analysis depends on
            fact_vector_observation, fact_insecticide_resistance,
            fact_bionomics         already filed at `site`, not grid

        The parent chain can be multi-hop if a grid cell's parent is
        itself a grid cell, though in practice a single hop suffices.
        SQLite's recursive CTE support is assumed.

        Returns
        -------
        dict
            {table_name: rows_in_aggregated_result}
        """
        logger.info("=" * 60)
        logger.info("REASSIGNING GRID-SITED FACTS TO ADMIN UNITS")
        logger.info("=" * 60)

        # ---- Build the {grid_id: nearest_non_grid_ancestor_id} map. ----
        try:
            rows = self.session.execute(text("""
                WITH RECURSIVE ancestry(grid_id, ancestor_id) AS (
                    SELECT g.location_id, g.parent_location_id
                    FROM dim_location g
                    WHERE g.admin_level = 'grid'
                      AND g.parent_location_id IS NOT NULL
                    UNION ALL
                    SELECT a.grid_id, p.parent_location_id
                    FROM ancestry a
                    JOIN dim_location p ON a.ancestor_id = p.location_id
                    WHERE p.admin_level = 'grid'
                      AND p.parent_location_id IS NOT NULL
                )
                SELECT a.grid_id, a.ancestor_id
                FROM ancestry a
                JOIN dim_location p ON a.ancestor_id = p.location_id
                WHERE p.admin_level IS NULL
                   OR p.admin_level <> 'grid'
            """)).fetchall()
        except Exception as e:
            logger.error(f"  Could not build grid-to-admin map: {e}")
            return {}

        grid_to_admin = {int(r[0]): int(r[1]) for r in rows}

        if not grid_to_admin:
            logger.info("  No grid cells have a non-grid ancestor; skipping.")
            return {}

        logger.info(
            f"  {len(grid_to_admin)} grid cells have a non-grid ancestor."
        )

        target_tables = [
            "fact_disease_case",
            "fact_disease_burden",
            "fact_intervention",
        ]

        results: Dict[str, int] = {}

        for table_name in target_tables:
            # Which of these grid cells actually appear in the table?
            try:
                existing = {
                    int(r[0]) for r in self.session.execute(
                        text(f"SELECT DISTINCT location_id FROM {table_name} "
                             f"WHERE location_id IS NOT NULL")
                    ).fetchall()
                }
            except Exception as e:
                logger.warning(f"  Could not scan {table_name}: {e}")
                results[table_name] = 0
                continue

            applicable = {g: a for g, a in grid_to_admin.items() if g in existing}
            if not applicable:
                logger.info(f"  {table_name}: no grid-sited rows to reassign")
                results[table_name] = 0
                continue

            # Group grid cells by their target admin unit.
            by_admin: Dict[int, List[int]] = {}
            for grid_id, admin_id in applicable.items():
                by_admin.setdefault(admin_id, []).append(grid_id)

            logger.info(
                f"  {table_name}: {len(applicable)} grid cells -> "
                f"{len(by_admin)} admin units"
            )

            total = 0
            failures = 0
            for admin_id, grid_ids in by_admin.items():
                try:
                    written = self._aggregate_and_reassign_bulk(
                        table_name, "location_id", grid_ids, admin_id
                    )
                    total += written
                except Exception as e:
                    failures += 1
                    logger.error(
                        f"    Failed to reassign {len(grid_ids)} grid cells "
                        f"-> admin {admin_id} in {table_name}: {e}"
                    )
                    continue

            results[table_name] = total
            if failures:
                logger.warning(
                    f"  {table_name}: {total} rows in aggregated result, "
                    f"{failures} admin-unit groups failed"
                )
            else:
                logger.info(
                    f"  {table_name}: {total} rows in aggregated result"
                )

        logger.info("=" * 60)
        return results

# ========================= CONVENIENCE FUNCTIONS =========================

def run_consolidation(dry_run: bool = False):
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
    if lat_col not in df.columns or lon_col not in df.columns:
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