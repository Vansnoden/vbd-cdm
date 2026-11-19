# -*- coding: utf-8 -*-
"""
Pydantic schemas for the CDM.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, FrozenSet, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


#: Shared config for every Response schema: build from ORM objects.
#:
#: Defined before any class uses it. That ordering is the whole point of this
#: file's repair (review §5.12); a name used before assignment here raised
#: ``NameError`` at import and took ``import database`` down with it.
_FROM_ATTRS = ConfigDict(from_attributes=True)


_DECISIONS_CACHE: dict[str, FrozenSet[str]] = {}


def _canonical_decisions() -> FrozenSet[str]:
    """The four decision values, from their single definition.

    See the module docstring for why the import is deferred. Raises
    ``ImportError`` rather than falling back to a permissive answer: a schema
    that accepts any decision string is a schema that validates nothing.
    """
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
        # `DECISIONS` is not in concept_resolver yet (batch 6B adds it). These
        # are the module's own constants, imported above — not a second copy.
        values = frozenset({AUTO_ACCEPTED, NEEDS_REVIEW, REJECTED, UNRESOLVED})

    _DECISIONS_CACHE["v1"] = values
    return values


def _normalise_candidates(value: Any) -> Optional[str]:
    """Accept backend text or a Python list; store canonical JSON text.

    Defined before the validators that reference it (review §5.12). The column
    is ``Text``, so:

    * ``None`` stays ``None``;
    * a ``str`` is validated as JSON and returned **unchanged** — a reader must
      get back exactly the text the resolver wrote, not a re-serialisation;
    * a list/tuple/dict is serialised with ``sort_keys`` and fixed separators so
      two runs produce byte-identical evidence.
    """
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
    """Accept a JSON object as text or a mapping; store canonical JSON text.

    For ``dim_concept.extra`` and ``dim_vocabulary.extra``. A JSON *list* is
    rejected: both columns are objects by contract (synonyms/definition/
    centroid, and the checksum artefact discriminator), and accepting a bare
    list would let a legacy pipe-delimited synonym list be written as the whole
    payload.
    """
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
    # Optional on create because the database autoincrements it.
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
    disease_code: str = Field(..., max_length=20)
    disease_name: str
    description: Optional[str] = None
    source_standard: Optional[str] = None
    pathogen: Optional[str] = None
    icd_code: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimVectorCreate(BaseModel):
    vector_code: str = Field(..., max_length=20)
    species_name: str
    subspecies: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    genus: Optional[str] = None
    family: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimInsecticideCreate(BaseModel):
    insecticide_code: str = Field(..., max_length=20)
    insecticide_name: str
    chemical_class: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    irac_group: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimIdentificationMethodCreate(BaseModel):
    method_code: str = Field(..., max_length=20)
    method_name: str
    method_type: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimResistanceTestTypeCreate(BaseModel):
    test_type_code: str = Field(..., max_length=20)
    test_type_name: str
    exposure_time_unit: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimSocioeconomicIndicatorCreate(BaseModel):
    indicator_code: str = Field(..., max_length=50)
    indicator_name: str
    unit: Optional[str] = None
    source: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimSourceCreate(BaseModel):
    """A data source's provenance row.

    Deliberately carries **no** resolution fields. ``decision``,
    ``candidates``, ``resolver_version``, ``run_id`` and ``resolved_at`` were
    pasted in here by an earlier batch (review §3.4), which made
    ``run_id``/``decision`` required to insert a source and put two unrelated
    concepts in one class. They belong to :class:`ResolutionLogCreate`, where
    they now live.

    ``standard_code`` and ``concept_id`` *do* belong here: ``DimSource`` carries
    both, and the registry writes them through a source-domain pack.
    """

    source_code: str = Field(..., max_length=20)
    source_name: str
    source_description: Optional[str] = None
    source_standard: Optional[str] = None
    source_url: Optional[str] = None
    version: Optional[str] = None
    standard_code: Optional[str] = Field(None, max_length=100)
    concept_id: Optional[int] = None


class DimVocabularyCreate(BaseModel):
    """A vocabulary release. ``vocabulary_id`` is version-qualified.

    ``checksum`` is the SHA-256 of *an* artefact, and ``extra`` must say which:
    the registry hashes a pack manifest, the loader hashes a CSV, and both write
    this column (contract Amendment §5.4). A reader that does not consult
    ``extra["checksum_artefact"]`` cannot tell them apart.
    """

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
        """Require the discriminator whenever a checksum is accompanied by it.

        The check is one-directional on purpose: ``extra`` is optional (a
        pre-registry row has no artefact to declare), but when it carries a
        ``checksum_artefact`` the value must be one this codebase writes. A
        typo here would make a reader's lookup miss and silently treat a CSV
        digest as a manifest digest.
        """
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
    # Number of cases represented by this row: 1 for an individual record,
    # >1 for an aggregated daily surveillance / burden-expanded row.
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
    """Aligned with ``FactInsecticideResistance``.

    A resistance test is a first-class entity, not a child of a vector
    observation: it carries its own location, date, vector code, insecticide,
    test type and source.
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
    mortality: Optional[float] = Field(
        None, description="Mortality percentage (0-100)"
    )
    resistance_status: Optional[str] = None

    @field_validator("date_id")
    @classmethod
    def _check_date_id(cls, v: str) -> str:
        return _validate_yyyy_mm_dd(v)

    @field_validator("mortality")
    @classmethod
    def _check_mortality(cls, v: Optional[float]) -> Optional[float]:
        # Percentage, matching the WHO bioassay convention and the
        # consolidation.py / fix_resistance_status.py pipeline.
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
    """Aligned with ``FactBionomics`` — no ``vector_observation_id``."""

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
    """Aligned with ``FactIntervention``.

    ``date_start`` / ``date_end`` are ``dim_time.date_id`` strings, not Python
    dates.
    """

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
    """Aligned with ``FactEnvironment`` — long format.

    ``variable`` is free text, but the ETL and analysis pipelines expect the set
    in :data:`SUPPORTED_ENV_VARIABLES`. An unexpected variable is allowed; the
    caller logs it.
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


#: Environmental variable names the analysis pipeline understands.
SUPPORTED_ENV_VARIABLES = frozenset({
    "2t", "t2m", "tp", "d2m", "rh", "ndvi", "evi", "lst_day", "lst_night",
    "temperature_mean", "temperature_min", "temperature_max",
    "rainfall_total", "relative_humidity", "aridity_index",
    "potential_breeding_sites",
})


# =============================================================================
# CONCEPT MAPPING AND THE RESOLUTION LEDGER
# =============================================================================

class _FactConceptMapFields(BaseModel):
    """Field and single-field-validator set shared by the concept-map schemas.

    Extracted so :class:`FactConceptMapCreate` can carry the reviewer rule and
    :class:`FactConceptMapResponse` can inherit the *fields* without it. See the
    Response class for why that split is deliberate.
    """

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

    # Human review only: None = unreviewed, True = approved, False = rejected.
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
    """The current decision for one raw value, keyed by the source triple.

    ``is_approved`` is the *human* verdict and is three-state; writing one is a
    review, and a review has an author. The validator below makes that
    structural rather than conventional — a row claiming a human approved it,
    with no ``reviewed_by``, is a provenance hole the database cannot detect
    (review C4, contract Amendment).
    """

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
    """A stored concept-map row.

    Derives from the field-only base rather than from ``FactConceptMapCreate``
    on purpose: the reviewer rule is a *write-side* constraint. A pre-refactor
    row can legitimately carry ``is_approved = TRUE`` with ``reviewed_by =
    NULL`` — the batch-1 migration deliberately does not backfill a reviewer
    nobody can name — and validating that on read would make the API unable to
    return its own legacy data.
    """

    map_id: int
    mapped_at: Optional[datetime] = None
    model_config = _FROM_ATTRS


class ResolutionLogCreate(BaseModel):
    """One append-only resolution witness, scoped to a run id.

    ``source_table``, ``source_column``, ``decision`` and ``run_id`` are all
    required because all four are ``NOT NULL`` in ``fact_resolution_log``, and
    ``run_id`` is part of that table's unique key — SQL treats NULLs as
    distinct, so a NULL run id would silently allow duplicate witnesses of one
    decision.
    """

    source_table: str = Field(..., max_length=100)
    source_column: str = Field(..., max_length=100)
    source_value: str = Field(..., max_length=300)

    vocabulary_id: Optional[str] = Field(None, max_length=100)
    vocabulary_version: Optional[str] = Field(None, max_length=100)
    concept_code: Optional[str] = Field(None, max_length=100)
    match_method: Optional[str] = Field(None, max_length=30)
    match_score: Optional[float] = None

    decision: str = Field(..., max_length=20)

    # JSON text, or a list which is serialised deterministically.
    candidates: Optional[str] = None

    resolver_version: Optional[str] = Field(None, max_length=50)
    run_id: str = Field(..., max_length=50)

    # NOT NULL in the table; the model supplies a Python-side default when this
    # is omitted on an ORM insert.
    resolved_at: Optional[datetime] = None

    @field_validator("decision")
    @classmethod
    def _check_decision(cls, v: str) -> str:
        validated = _require_decision(v)
        # `_require_decision` is Optional-tolerant; here the column is NOT NULL.
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
        """A withheld decision must not carry an applied concept.

        Contract Amendment: ``concept_code`` is not ``None`` only for
        ``auto_accepted``. The resolver enforces it, ``DimensionEntry`` enforces
        it structurally, and this is the transport-level check — a client
        posting ``decision='needs_review'`` with a code would otherwise write a
        row that the review queue reads as applied.
        """
        if self.concept_code and self.decision != "auto_accepted":
            raise ValueError(
                f"decision {self.decision!r} cannot carry concept_code "
                f"{self.concept_code!r}: only 'auto_accepted' applies a concept. "
                f"For a withheld decision put the candidate in `candidates`."
            )
        return self


class ResolutionLogResponse(ResolutionLogCreate):
    """A stored ledger row.

    ``ResolutionLogCreate`` is fully defined above — that ordering is the
    finding §5.12 repair: the pre-batch file inherited from a class that did not
    exist yet and the module could not be imported.
    """

    log_id: int
    model_config = _FROM_ATTRS


# =============================================================================
# RESPONSE SCHEMAS
# =============================================================================

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