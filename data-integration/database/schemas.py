# -*- coding: utf-8 -*-
"""
Pydantic schemas for the CDM.

These mirror the SQLAlchemy models in database/models.py. Each table has a
`*Create` schema (client-facing input, excludes the autoincrement PK) and a
`*Response` schema (full record, includes the PK).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# =============================================================================
# Common validators
# =============================================================================

def _validate_yyyy_mm_dd(v: str) -> str:
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError("must be a date in YYYY-MM-DD format")
    return v


# =============================================================================
# DIMENSIONS
# =============================================================================

class DimTimeCreate(BaseModel):
    date_id: str = Field(..., description="Date in YYYY-MM-DD format")
    date: date
    year: int
    month: int
    week: Optional[int] = None
    day_of_year: Optional[int] = None
    season: Optional[str] = None
    is_holiday: Optional[bool] = False

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)

    @field_validator("month")
    @classmethod
    def _check_month(cls, v: int) -> int:
        if not 1 <= v <= 12:
            raise ValueError("month must be between 1 and 12")
        return v


class DimLocationCreate(BaseModel):
    # PK is optional on create because it is autoincremented by the database.
    location_id: Optional[int] = None
    location_code: Optional[str] = None
    name: str
    country_name: Optional[str] = None
    country_code: Optional[str] = None
    admin_level: Optional[str] = None
    centroid_lat: Optional[float] = None
    centroid_lon: Optional[float] = None
    parent_location_id: Optional[int] = None
    origin: Optional[str] = "source"

    @field_validator("centroid_lat")
    @classmethod
    def _check_lat(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-90.0 <= v <= 90.0):
            raise ValueError("latitude must be between -90 and 90")
        return v

    @field_validator("centroid_lon")
    @classmethod
    def _check_lon(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-180.0 <= v <= 180.0):
            raise ValueError("longitude must be between -180 and 180")
        return v


class DimDiseaseCreate(BaseModel):
    disease_code: str = Field(..., max_length=20)
    disease_name: str
    description: Optional[str] = None
    source_standard: Optional[str] = None
    pathogen: Optional[str] = None
    icd_code: Optional[str] = None


class DimVectorCreate(BaseModel):
    vector_code: str = Field(..., max_length=20)
    species_name: str
    subspecies: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    genus: Optional[str] = None
    family: Optional[str] = None


class DimInsecticideCreate(BaseModel):
    insecticide_code: str = Field(..., max_length=20)
    insecticide_name: str
    chemical_class: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    irac_group: Optional[str] = None


class DimIdentificationMethodCreate(BaseModel):
    method_code: str = Field(..., max_length=20)
    method_name: str
    method_type: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None


class DimResistanceTestTypeCreate(BaseModel):
    test_type_code: str = Field(..., max_length=20)
    test_type_name: str
    exposure_time_unit: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None


class DimSocioeconomicIndicatorCreate(BaseModel):
    indicator_code: str = Field(..., max_length=50)
    indicator_name: str
    unit: Optional[str] = None
    source: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None


class DimSourceCreate(BaseModel):
    source_code: str = Field(..., max_length=20)
    source_name: str
    source_description: Optional[str] = None
    source_standard: Optional[str] = None
    source_url: Optional[str] = None
    version: Optional[str] = None


# =============================================================================
# FACT TABLES
# =============================================================================

class FactVectorObservationCreate(BaseModel):
    location_id: int
    date_id: str
    vector_code: str
    source_code: str
    life_stage: Optional[str] = None
    sex: Optional[str] = None
    collection_method: Optional[str] = None
    count: Optional[int] = None
    density_per_trap: Optional[float] = None
    tested_for_pathogen: Optional[str] = None
    infection_status: Optional[str] = None
    infection_rate: Optional[float] = None

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)

    @field_validator("infection_rate")
    @classmethod
    def _check_rate(cls, v: Optional[float]) -> Optional[float]:
        # Stored as a proportion in [0, 1].
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("infection_rate must be between 0 and 1")
        return v


class FactDiseaseCaseCreate(BaseModel):
    location_id: int
    date_id_onset: str
    date_id_reported: str
    disease_code: str
    identification_method_code: str
    source_code: str
    case_status: Optional[str] = "suspected"
    host_type: Optional[str] = "human"
    age_years: Optional[int] = None
    age_group: Optional[str] = None
    sex: Optional[str] = None
    outcome: Optional[str] = "unknown"
    # Number of cases represented by this row. 1 for individual case records;
    # >1 for aggregated daily surveillance / burden-expanded rows.
    case_count: Optional[int] = Field(1, ge=1)

    @field_validator("date_id_onset", "date_id_reported")
    @classmethod
    def _check_dates(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)

    @field_validator("age_years")
    @classmethod
    def _check_age(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("age_years cannot be negative")
        return v

    @field_validator("sex")
    @classmethod
    def _check_sex(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v_upper = v.upper()
        if v_upper not in {"M", "F", "OTHER", "UNKNOWN"}:
            raise ValueError("sex must be one of: M, F, OTHER, UNKNOWN")
        return v_upper


class FactInsecticideResistanceCreate(BaseModel):
    """
    Aligned with FactInsecticideResistance in database/models.py.

    A resistance test is a first-class entity, not a child of a vector
    observation. It carries its own location, date, vector code, insecticide,
    test type, and source.
    """
    location_id: int
    date_id: str
    vector_code: str
    insecticide_code: str
    test_type_code: str
    source_code: str

    # Test conditions
    concentration: Optional[float] = None
    exposure_time: Optional[int] = None

    # Raw counts
    n_insect_tested: Optional[int] = None
    n_insect_susceptible: Optional[int] = None
    n_insect_resistant: Optional[int] = None
    n_insect_dead: Optional[int] = None

    # VGSC knockdown-resistance genotype counts
    vgsc_n_insect_tested: Optional[int] = None
    vgsc995l_vgsc995l_n: Optional[int] = None
    vgsc995l_vgsc995f_n: Optional[int] = None
    vgsc995f_vgsc995f_n: Optional[int] = None
    vgsc995l_vgsc995s_n: Optional[int] = None
    vgsc995s_vgsc995s_n: Optional[int] = None
    vgsc995f_vgsc995s_n: Optional[int] = None
    vgsc_generation: Optional[str] = None

    # Outcome
    mortality: Optional[float] = Field(None, description="Mortality percentage (0-100)")
    resistance_status: Optional[str] = None

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)

    @field_validator("mortality")
    @classmethod
    def _check_mortality(cls, v: Optional[float]) -> Optional[float]:
        # Percentage, matching WHO bioassay convention and the
        # consolidation.py / fix_resistance_status.py pipeline.
        if v is not None and not (0.0 <= v <= 100.0):
            raise ValueError("mortality must be a percentage between 0 and 100")
        return v

    @field_validator("n_insect_tested", "n_insect_susceptible",
                     "n_insect_resistant", "n_insect_dead",
                     "vgsc_n_insect_tested")
    @classmethod
    def _check_counts(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("insect counts cannot be negative")
        return v


class FactBionomicsCreate(BaseModel):
    """Aligned with FactBionomics — no vector_observation_id."""
    location_id: int
    date_id: str
    vector_code: str
    source_code: str
    biting_rate: Optional[float] = None
    feeding_preference: Optional[str] = None   # anthropophagic | zoophagic | mixed
    biting_location: Optional[str] = None      # endophagic | exophagic | mixed
    resting_behavior: Optional[str] = None
    larval_site_type: Optional[str] = None

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)


class FactInterventionCreate(BaseModel):
    """
    Aligned with FactIntervention — date_start and date_end are FK string ids
    (dim_time.date_id, YYYY-MM-DD) not Python date objects.
    """
    location_id: int
    date_start: str
    date_end: Optional[str] = None
    target_disease_code: str
    source_code: str
    intervention_type: Optional[str] = None
    coverage: Optional[float] = Field(None, description="Coverage percentage (0-100)")

    @field_validator("date_start")
    @classmethod
    def _check_start(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)

    @field_validator("date_end")
    @classmethod
    def _check_end(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_yyyy_mm_dd(v)

    @field_validator("coverage")
    @classmethod
    def _check_coverage(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 100.0):
            raise ValueError("coverage must be between 0 and 100 (percentage)")
        return v


class FactSocioeconomicConditionCreate(BaseModel):
    location_id: int
    year: int
    indicator_code: str
    source_code: str
    indicator_value: float

    @field_validator("year")
    @classmethod
    def _check_year(cls, v: int) -> int:
        if v < 1900 or v > 2100:
            raise ValueError("year out of reasonable range")
        return v


class FactHealthFacilityCreate(BaseModel):
    facility_code: str
    location_id: int
    source_code: str
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    type: Optional[str] = None
    catchment_population: Optional[int] = None

    @field_validator("lat")
    @classmethod
    def _check_lat(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-90.0 <= v <= 90.0):
            raise ValueError("latitude must be between -90 and 90")
        return v

    @field_validator("lon")
    @classmethod
    def _check_lon(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-180.0 <= v <= 180.0):
            raise ValueError("longitude must be between -180 and 180")
        return v


class FactDiseaseBurdenCreate(BaseModel):
    location_id: int
    year: int
    disease_code: str
    pathogen_species: str
    source_code: str
    incidence_rate: Optional[float] = None
    prevalence_rate: Optional[float] = None
    mortality_rate: Optional[float] = None
    cases_mean: Optional[float] = None

    @field_validator("year")
    @classmethod
    def _check_year(cls, v: int) -> int:
        if v < 1900 or v > 2100:
            raise ValueError("year out of reasonable range")
        return v


class FactEnvironmentalCreate(BaseModel):
    """
    Aligned with FactEnvironment — long format (location, date, variable, value).

    `variable` is free text but the ETL and analysis pipelines expect the set
    listed in `SUPPORTED_ENV_VARIABLES` below. Passing an unexpected variable
    is allowed but logged by the caller.
    """
    location_id: int
    date_id: str
    variable: str = Field(..., max_length=50)
    value: float
    source_code: str

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)


# Known environmental variable names used by the analysis pipeline.
SUPPORTED_ENV_VARIABLES = frozenset({
    "2t", "t2m", "tp", "d2m", "rh", "ndvi", "evi", "lst_day", "lst_night",
    "temperature_mean", "temperature_min", "temperature_max",
    "rainfall_total", "relative_humidity", "aridity_index",
    "potential_breeding_sites",
})


# =============================================================================
# RESPONSE SCHEMAS
# =============================================================================

# Base config helper so we don't repeat `model_config` everywhere.
_FROM_ATTRS = ConfigDict(from_attributes=True)


class DimTimeResponse(DimTimeCreate):
    model_config = _FROM_ATTRS


class DimLocationResponse(DimLocationCreate):
    location_id: int  # required on read
    model_config = _FROM_ATTRS


class DimDiseaseResponse(DimDiseaseCreate):
    model_config = _FROM_ATTRS


class DimVectorResponse(DimVectorCreate):
    model_config = _FROM_ATTRS


class DimInsecticideResponse(DimInsecticideCreate):
    model_config = _FROM_ATTRS


class DimIdentificationMethodResponse(DimIdentificationMethodCreate):
    model_config = _FROM_ATTRS


class DimResistanceTestTypeResponse(DimResistanceTestTypeCreate):
    model_config = _FROM_ATTRS


class DimSocioeconomicIndicatorResponse(DimSocioeconomicIndicatorCreate):
    model_config = _FROM_ATTRS


class DimSourceResponse(DimSourceCreate):
    model_config = _FROM_ATTRS


class FactVectorObservationResponse(FactVectorObservationCreate):
    observation_id: int
    model_config = _FROM_ATTRS


class FactDiseaseCaseResponse(FactDiseaseCaseCreate):
    case_id: int
    model_config = _FROM_ATTRS


class FactInsecticideResistanceResponse(FactInsecticideResistanceCreate):
    resistance_id: int
    model_config = _FROM_ATTRS


class FactBionomicsResponse(FactBionomicsCreate):
    bionomics_id: int
    model_config = _FROM_ATTRS


class FactInterventionResponse(FactInterventionCreate):
    intervention_id: int
    model_config = _FROM_ATTRS


class FactSocioeconomicConditionResponse(FactSocioeconomicConditionCreate):
    socio_id: int
    model_config = _FROM_ATTRS


class FactHealthFacilityResponse(FactHealthFacilityCreate):
    facility_id: int
    model_config = _FROM_ATTRS


class FactDiseaseBurdenResponse(FactDiseaseBurdenCreate):
    burden_id: int
    model_config = _FROM_ATTRS


class FactEnvironmentalResponse(FactEnvironmentalCreate):
    fact_env_id: int
    model_config = _FROM_ATTRS