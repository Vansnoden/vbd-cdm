# -*- coding: utf-8 -*-
"""Pydantic schemas for the CDM."""

from __future__ import annotations
import json
from datetime import date, datetime
from typing import Any, FrozenSet, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_FROM_ATTRS = ConfigDict(from_attributes=True)
_DECISIONS_CACHE: dict[str, FrozenSet[str]] = {}
SUPPORTED_ENV_VARIABLES = frozenset({
    "2t", "t2m", "tp", "d2m", "rh", "ndvi", "evi", "lst_day", "lst_night",
    "temperature_mean", "temperature_min", "temperature_max",
    "rainfall_total", "relative_humidity", "aridity_index",
    "potential_breeding_sites",
})


def _canonical_decisions() -> FrozenSet[str]:
    """The four decision values, from their single definition."""

    cached = _DECISIONS_CACHE.get("v1")
    if cached is not None:
        return cached

    from etl.transformers.concept_resolver import (
        AUTO_ACCEPTED,
        NEEDS_REVIEW,
        REJECTED,
        UNRESOLVED,
    )

    try:
        from etl.transformers.concept_resolver import DECISIONS as _DECISIONS

        values = frozenset(_DECISIONS)
    except ImportError:
        values = frozenset({AUTO_ACCEPTED, NEEDS_REVIEW, REJECTED, UNRESOLVED})

    _DECISIONS_CACHE["v1"] = values
    return values


def _normalise_candidates(value: Any) -> Optional[str]:
    """Accept backend text or a Python list; store canonical JSON text."""

    if value is None:
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("candidates must be valid JSON text") from exc
        return value
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("candidates must be JSON-serialisable") from exc


def _normalise_json_object(value: Any) -> Optional[str]:
    """Accept a JSON object as text or a mapping; store canonical JSON text."""

    if value is None:
        return None
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        try:
            parsed = json.loads(text_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("extra must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("extra must be a JSON object")
        return value
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    raise ValueError("extra must be a JSON object (text or mapping)")


def _validate_yyyy_mm_dd(value: str) -> str:
    """Require a ``YYYY-MM-DD`` string."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError("must be a date in YYYY-MM-DD format") from None
    return value


def _require_decision(value: Optional[str]) -> Optional[str]:
    """Validate a decision value against the canonical set."""
    if value is None:
        return value
    allowed = _canonical_decisions()
    if value not in allowed:
        raise ValueError(f"decision must be one of {sorted(allowed)}, got {value!r}")
    return value


class DimTimeCreate(BaseModel):
    """A ``dim_time`` row. ``season`` is NULL until a climatology exists."""

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
    """A ``dim_location`` row."""

    location_id: Optional[int] = None
    location_code: Optional[str] = None
    name: str
    country_name: Optional[str] = None
    country_code: Optional[str] = None
    admin_level: Optional[str] = None
    centroid_lat: Optional[float] = None
    centroid_lon: Optional[float] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None
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
    """A ``dim_disease`` row. Every length matches ``String(…)`` in the DDL."""

    disease_code: str = Field(..., max_length=100)
    disease_name: str
    description: Optional[str] = None
    source_standard: Optional[str] = None
    pathogen: Optional[str] = None
    icd_code: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimVectorCreate(BaseModel):
    """A ``dim_vector`` row."""

    vector_code: str = Field(..., max_length=100)
    species_name: str
    subspecies: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    genus: Optional[str] = None
    family: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimInsecticideCreate(BaseModel):
    """A ``dim_insecticide`` row."""

    insecticide_code: str = Field(..., max_length=100)
    insecticide_name: str
    chemical_class: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    irac_group: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimIdentificationMethodCreate(BaseModel):
    """A ``dim_identification_method`` row."""

    method_code: str = Field(..., max_length=100)
    method_name: str
    method_type: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimResistanceTestTypeCreate(BaseModel):
    """A ``dim_resistance_test_type`` row."""

    test_type_code: str = Field(..., max_length=100)
    test_type_name: str
    exposure_time_unit: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimSocioeconomicIndicatorCreate(BaseModel):
    """A ``dim_socioeconomic_indicator`` row."""

    indicator_code: str = Field(..., max_length=100)
    indicator_name: str
    unit: Optional[str] = None
    source: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimSourceCreate(BaseModel):
    """A data source's provenance row."""

    source_code: str = Field(..., max_length=100)
    source_name: str
    source_description: Optional[str] = None
    source_standard: Optional[str] = None
    source_url: Optional[str] = None
    version: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimVocabularyCreate(BaseModel):
    """A vocabulary release. ``vocabulary_id`` is version-qualified."""

    vocabulary_id: str = Field(..., max_length=100, description="e.g. 'gadm@4.1'")
    vocabulary_name: str = Field(..., max_length=1000)
    source_url: Optional[str] = Field(None, max_length=1000)
    version: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=1000)
    pack_id: Optional[str] = Field(None, max_length=100)
    checksum: Optional[str] = Field(None, max_length=64)
    released_with: Optional[str] = Field(None, max_length=20)
    extra: Optional[str] = None

    @field_validator("checksum")
    @classmethod
    def _check_checksum(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        candidate = v.strip().lower()
        if len(candidate) != 64 or any(
            character not in "0123456789abcdef" for character in candidate
        ):
            raise ValueError("checksum must be 64 hexadecimal characters (SHA-256)")
        return candidate

    @field_validator("extra", mode="before")
    @classmethod
    def _check_extra(cls, v):
        return _normalise_json_object(v)

    @field_validator("extra")
    @classmethod
    def _check_checksum_artefact(cls, v: Optional[str]) -> Optional[str]:
        """Require the discriminator whenever a checksum is accompanied by it."""

        if v is None:
            return v
        payload = json.loads(v)
        artefact = payload.get("checksum_artefact")
        if artefact is not None and artefact not in ("manifest", "csv"):
            raise ValueError(
                "extra.checksum_artefact must be 'manifest' or 'csv', "
                f"got {artefact!r}"
            )
        return v


class DimConceptCreate(BaseModel):
    """A single standard concept inside one vocabulary release."""

    vocabulary_id: str = Field(..., max_length=100)
    concept_code: str = Field(..., max_length=100)
    concept_name: str = Field(..., max_length=300)
    concept_class: Optional[str] = Field(None, max_length=100)
    domain: Optional[str] = Field(None, max_length=100)
    parent_code: Optional[str] = Field(None, max_length=100)
    extra: Optional[str] = None
    concept_norm: Optional[str] = Field(None, max_length=300)

    @field_validator("extra", mode="before")
    @classmethod
    def _check_extra(cls, v):
        return _normalise_json_object(v)


class DimVocabularyResponse(DimVocabularyCreate):
    loaded_at: Optional[datetime] = None
    model_config = _FROM_ATTRS


class DimConceptResponse(DimConceptCreate):
    concept_id: int
    model_config = _FROM_ATTRS


class FactVectorObservationCreate(BaseModel):
    """A ``fact_vector_observation`` row."""

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
    """A ``fact_disease_case`` row."""

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
        upper = v.upper()
        if upper not in {"M", "F", "OTHER", "UNKNOWN"}:
            raise ValueError("sex must be one of: M, F, OTHER, UNKNOWN")
        return upper


class FactInsecticideResistanceCreate(BaseModel):
    """A ``fact_insecticide_resistance`` row.

    ``concentration`` is genuinely NULL-able; NULL is its own key, not a sentinel.
    """

    location_id: int
    date_id: str
    vector_code: str
    insecticide_code: str
    test_type_code: str
    source_code: str
    concentration: Optional[float] = None
    exposure_time: Optional[int] = None
    n_insect_tested: Optional[int] = None
    n_insect_susceptible: Optional[int] = None
    n_insect_resistant: Optional[int] = None
    n_insect_dead: Optional[int] = None
    vgsc_n_insect_tested: Optional[int] = None
    vgsc995l_vgsc995l_n: Optional[int] = None
    vgsc995l_vgsc995f_n: Optional[int] = None
    vgsc995f_vgsc995f_n: Optional[int] = None
    vgsc995l_vgsc995s_n: Optional[int] = None
    vgsc995s_vgsc995s_n: Optional[int] = None
    vgsc995f_vgsc995s_n: Optional[int] = None
    vgsc_generation: Optional[str] = None
    mortality: Optional[float] = Field(
        None, description="Mortality percentage (0-100)"
    )
    resistance_status: Optional[str] = None

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)

    @field_validator("concentration")
    @classmethod
    def _check_concentration(cls, v: Optional[float]) -> Optional[float]:
        """A negative concentration is not a measurement; use NULL."""
        if v is not None and v < 0:
            raise ValueError(
                "concentration cannot be negative; use None to record that the "
                "assay had no concentration")
        return v

    @field_validator("mortality")
    @classmethod
    def _check_mortality(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 100.0):
            raise ValueError("mortality must be a percentage between 0 and 100")
        return v

    @field_validator(
        "n_insect_tested", "n_insect_susceptible", "n_insect_resistant",
        "n_insect_dead", "vgsc_n_insect_tested",
    )
    @classmethod
    def _check_counts(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("insect counts cannot be negative")
        return v


class FactBionomicsCreate(BaseModel):
    """A ``fact_bionomics`` row."""

    location_id: int
    date_id: str
    vector_code: str
    source_code: str
    biting_rate: Optional[float] = None
    feeding_preference: Optional[str] = None   
    biting_location: Optional[str] = None      
    resting_behavior: Optional[str] = None
    larval_site_type: Optional[str] = None

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)


class FactInterventionCreate(BaseModel):
    """A ``fact_intervention`` row."""

    location_id: int
    date_start: str
    date_end: Optional[str] = None
    target_disease_code: str
    source_code: str
    intervention_type: Optional[str] = None
    coverage: Optional[float] = Field(
        None, description="Coverage percentage (0-100)"
    )

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
    """A ``fact_socioeconomic_condition`` row. ``source_code`` is part of the key."""

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
    """A ``fact_health_facility`` row."""

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
    """A ``fact_disease_burden`` row. ``pathogen_species`` is part of the key."""

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
    """A ``fact_environment`` row. ``source_code`` is part of the key."""

    location_id: int
    date_id: str
    variable: str = Field(..., max_length=100)
    value: float
    source_code: str

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)


class _FactConceptMapFields(BaseModel):
    """Field and single-field-validator set shared by the concept-map schemas."""

    source_table: str = Field(..., max_length=100)
    source_column: str = Field(..., max_length=100)
    source_value: str = Field(..., max_length=300)

    vocabulary_id: Optional[str] = Field(None, max_length=100)
    concept_code: Optional[str] = Field(None, max_length=100)
    match_method: Optional[str] = Field(None, max_length=30)
    match_score: Optional[float] = None

    decision: Optional[str] = Field(None, max_length=20)
    vocabulary_version: Optional[str] = Field(None, max_length=100)
    resolver_version: Optional[str] = Field(None, max_length=50)
    last_run_id: Optional[str] = Field(None, max_length=50)

    reviewed_by: Optional[str] = Field(None, max_length=100)
    reviewed_at: Optional[datetime] = None

    is_approved: Optional[bool] = None

    @field_validator("decision")
    @classmethod
    def _check_decision(cls, v: Optional[str]) -> Optional[str]:
        return _require_decision(v)

    @field_validator("match_score")
    @classmethod
    def _check_match_score(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("match_score must be between 0 and 1")
        return v


class FactConceptMapCreate(_FactConceptMapFields):
    """The current decision for one raw value, keyed by the source triple."""

    @model_validator(mode="after")
    def _review_needs_an_author(self) -> "FactConceptMapCreate":
        if self.is_approved is not None and not (self.reviewed_by or "").strip():
            raise ValueError(
                "is_approved may only be set together with reviewed_by: "
                "True means 'a human approved this' and False means 'a human "
                "rejected this'. A verdict without a reviewer is "
                "indistinguishable from an automated one, which is exactly the "
                "conflation that made is_approved default to False."
            )
        return self


class FactConceptMapResponse(_FactConceptMapFields):
    """A stored concept-map row."""

    map_id: int
    mapped_at: Optional[datetime] = None
    model_config = _FROM_ATTRS


class ResolutionLogCreate(BaseModel):
    """One append-only resolution witness, scoped to a run id."""

    source_table: str = Field(..., max_length=100)
    source_column: str = Field(..., max_length=100)
    source_value: str = Field(..., max_length=300)
    vocabulary_id: Optional[str] = Field(None, max_length=100)
    vocabulary_version: Optional[str] = Field(None, max_length=100)
    concept_code: Optional[str] = Field(None, max_length=100)
    match_method: Optional[str] = Field(None, max_length=30)
    match_score: Optional[float] = None
    decision: str = Field(..., max_length=20)
    candidates: Optional[str] = None
    resolver_version: Optional[str] = Field(None, max_length=50)
    run_id: str = Field(..., max_length=50)
    resolved_at: Optional[datetime] = None

    @field_validator("decision")
    @classmethod
    def _check_decision(cls, v: str) -> str:
        validated = _require_decision(v)
        assert validated is not None
        return validated

    @field_validator("match_score")
    @classmethod
    def _check_match_score(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("match_score must be between 0 and 1")
        return v

    @field_validator("candidates", mode="before")
    @classmethod
    def _check_candidates(cls, v):
        return _normalise_candidates(v)

    @model_validator(mode="after")
    def _concept_code_only_when_accepted(self) -> "ResolutionLogCreate":
        """A withheld decision must not carry an applied concept."""

        if self.concept_code and self.decision != "auto_accepted":
            raise ValueError(
                f"decision {self.decision!r} cannot carry concept_code "
                f"{self.concept_code!r}: only 'auto_accepted' applies a concept. "
                f"For a withheld decision put the candidate in `candidates`."
            )
        return self


class ResolutionLogResponse(ResolutionLogCreate):
    """A stored ledger row."""

    log_id: int
    model_config = _FROM_ATTRS


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


__all__ = [
    "DimConceptCreate",
    "DimConceptResponse",
    "DimDiseaseCreate",
    "DimDiseaseResponse",
    "DimIdentificationMethodCreate",
    "DimIdentificationMethodResponse",
    "DimInsecticideCreate",
    "DimInsecticideResponse",
    "DimLocationCreate",
    "DimLocationResponse",
    "DimResistanceTestTypeCreate",
    "DimResistanceTestTypeResponse",
    "DimSocioeconomicIndicatorCreate",
    "DimSocioeconomicIndicatorResponse",
    "DimSourceCreate",
    "DimSourceResponse",
    "DimTimeCreate",
    "DimTimeResponse",
    "DimVectorCreate",
    "DimVectorResponse",
    "DimVocabularyCreate",
    "DimVocabularyResponse",
    "FactBionomicsCreate",
    "FactBionomicsResponse",
    "FactConceptMapCreate",
    "FactConceptMapResponse",
    "FactDiseaseBurdenCreate",
    "FactDiseaseBurdenResponse",
    "FactDiseaseCaseCreate",
    "FactDiseaseCaseResponse",
    "FactEnvironmentalCreate",
    "FactEnvironmentalResponse",
    "FactHealthFacilityCreate",
    "FactHealthFacilityResponse",
    "FactInsecticideResistanceCreate",
    "FactInsecticideResistanceResponse",
    "FactInterventionCreate",
    "FactInterventionResponse",
    "FactSocioeconomicConditionCreate",
    "FactSocioeconomicConditionResponse",
    "FactVectorObservationCreate",
    "FactVectorObservationResponse",
    "ResolutionLogCreate",
    "ResolutionLogResponse",
    "SUPPORTED_ENV_VARIABLES",
]