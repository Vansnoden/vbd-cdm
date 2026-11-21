# -*- coding: utf-8 -*-

import datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database.session import Base


# ========================= DIMENSIONS =========================

class DimVocabulary(Base):
    """Registry of external vocabularies used for standardization.

    One row per (pack, release). ``vocabulary_id`` is version-qualified
    (``'gadm@4.1'``) so two releases coexist without a schema change;
    ``pack_id`` is the unversioned grouping key the registry selects on.

    ``checksum`` is written by two different writers with two different
    artefacts — the registry hashes a pack *manifest*, the loader hashes a
    vocabulary *CSV*. ``extra`` carries
    ``{"checksum_artefact": "manifest" | "csv"}`` so a reader can tell which
    (contract Amendment §5.4). ``extra`` is NULL for rows written before
    revision ``d5e2a8c71b93``, which reads as "artefact unknown".
    """
    __tablename__ = "dim_vocabulary"

    vocabulary_id   = Column(String(100), primary_key=True)   # 'gadm@4.1', legacy 'GADM'
    vocabulary_name = Column(String(1000), nullable=False)
    source_url      = Column(String(1000))
    version         = Column(String(100))                     # '4.1'; 'unknown' if never recorded
    description     = Column(String(1000))
    loaded_at       = Column(DateTime, default=datetime.datetime.utcnow)

    # Appended after `loaded_at` to match the migration's ADD COLUMN order.
    pack_id         = Column(String(100))                      # manifest key, e.g. 'gadm'
    checksum        = Column(String(64))                       # SHA-256, hex
    released_with   = Column(String(20))                       # tool release that shipped this pack
    #: JSON: ``{"checksum_artefact": "manifest" | "csv"}``. Nullable, because a
    #: digest written before the discriminator existed has an unknown artefact.
    extra           = Column(Text)


class DimConcept(Base):
    """Standard concepts extracted from an external vocabulary."""
    __tablename__ = "dim_concept"

    concept_id    = Column(Integer, primary_key=True, autoincrement=True)
    vocabulary_id = Column(String(100), ForeignKey("dim_vocabulary.vocabulary_id"), nullable=False)
    concept_code  = Column(String(100), nullable=False)        # 'GID_2_KEN.3_1', 'IDOMAL:0000657'
    concept_name  = Column(String(300), nullable=False)         # preferred label
    concept_class = Column(String(100))                        # 'admin2', 'species', 'disease'
    domain        = Column(String(100))                        # 'location', 'vector', 'disease'
    parent_code   = Column(String(100))                        # hierarchy
    extra         = Column(Text)                               # JSON: synonyms, definition, centroid, rank

    # Appended after `extra` to match the migration's ADD COLUMN order.
    # Populated by the vocabulary loader using
    # utilities.vocabulary.normalizers.compose(pack.normalizer_chain), so the
    # L2 (normalised) rung of the ladder is reproducible from the stored key.
    concept_norm  = Column(String(300))

    __table_args__ = (
        UniqueConstraint("vocabulary_id", "concept_code", name="uq_concept_vocab_code"),
        Index("idx_concept_vocab_class", "vocabulary_id", "concept_class"),
        Index("idx_concept_domain", "domain"),
        Index("idx_concept_norm", "concept_norm"),
    )


class ResolutionLog(Base):
    """Append-only ledger of every resolution decision, per run.

    One row per (source_table, source_column, source_value, run_id). Writing is
    insert-or-ignore against that key, so re-running a run id is a no-op while a
    new run adds a new witness. Nothing in the codebase updates or deletes from
    this table: the current decision lives in FactConceptMap, the history lives
    here.

    Fields mirror ``Resolution`` in interface_contract.md one-for-one, plus a
    surrogate primary key and ``resolved_at``.
    """

    __tablename__ = "fact_resolution_log"

    log_id             = Column(Integer, primary_key=True, autoincrement=True)
    source_table       = Column(String(100), nullable=False)
    source_column      = Column(String(100), nullable=False)
    source_value       = Column(String(300), nullable=False)
    vocabulary_id      = Column(String(100))    # 'gadm@4.1'; no FK, see migration comment
    vocabulary_version = Column(String(100))
    concept_code       = Column(String(100))
    match_method       = Column(String(30))     # exact|normalized|alias|fuzzy|ambiguous|unresolved
    match_score        = Column(Float)
    decision           = Column(String(20), nullable=False)
    candidates         = Column(Text)           # JSON list, rank order
    resolver_version   = Column(String(50))
    run_id             = Column(String(50), nullable=False)
    resolved_at        = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "source_table", "source_column", "source_value", "run_id",
            name="uq_resolution_log_source",
        ),
        Index("idx_resolution_log_decision", "decision"),
        Index("idx_resolution_log_run", "run_id"),
    )


class RunManifest(Base):
    """One row per pipeline invocation: what produced this database.

    The CDM records vocabulary provenance per resolution. This table records
    *run* provenance: the tool version, the git commit, the registry digests, the
    hashes of the configuration and the mapping files, the installed versions of
    every package that can change a stored number, and a JSON ``parameters``
    object that each phase extends (most importantly
    ``{"expansion": {...}}``, which names the policy behind every modelled case
    row).

    It is upserted rather than append-only: a run learns about itself as it
    proceeds, so ``record_run`` writes identity at start-up and later phases
    merge their parameters in. ``finished_at`` is NULL for a run that did not
    complete, which distinguishes an interrupted run from a finished one.

    No foreign keys, deliberately: a manifest must survive the database being
    partially rebuilt. A researcher who drops and re-ingests a fact table has not
    invalidated the record of the run that first populated it.
    """

    __tablename__ = "run_manifest"

    run_id                    = Column(String(50), primary_key=True)
    started_at                = Column(DateTime, nullable=False,
                                       default=datetime.datetime.utcnow)
    finished_at               = Column(DateTime)
    tool_version              = Column(String(20))
    git_sha                   = Column(String(64))
    registry_digest           = Column(String(64))
    registry_digest_with_kind = Column(String(64))
    countries                 = Column(Text)      # JSON list
    config_path               = Column(String(1000))
    config_sha256             = Column(String(64))
    parameters                = Column(Text)      # JSON object, merged per phase
    package_versions          = Column(Text)      # JSON object
    row_counts                = Column(Text)      # JSON object
    notes                     = Column(Text)

    __table_args__ = (
        Index("idx_run_manifest_started", "started_at"),
    )


class DimTime(Base):
    """Time dimension table"""
    __tablename__ = "dim_time"

    date_id = Column(String(10), primary_key=True)  # YYYY-MM-DD
    date = Column(Date, nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    week = Column(Integer)
    day_of_year = Column(Integer)
    season = Column(String(100))
    is_holiday = Column(Boolean, default=False)


class DimLocation(Base):
    """Location hierarchy (country, region, district, site)"""
    __tablename__ = "dim_location"

    location_id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(1000), nullable=False)
    #: Nullable *and* unique. See ``DBLoader.NATURAL_KEY_CONFIG`` for the
    #: sentinel this requires: SQL treats NULLs as distinct, so an upsert keyed
    #: on this column never conflicts for a row whose code is NULL.
    location_code = Column(String, unique=True)
    centroid_lat = Column(Float)
    centroid_lon = Column(Float)
    country_code = Column(String(10))
    country_name = Column(String(100))
    admin_level = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))
    parent_location_id = Column(
        Integer, ForeignKey("dim_location.location_id"), nullable=True
    )
    origin = Column(String(30), default="source")

    __table_args__ = (
        Index("idx_dim_location_parent", "parent_location_id"),
        Index("idx_dim_location_admin", "admin_level"),
        Index("idx_dim_location_origin", "origin"),
    )


class DimDisease(Base):
    """Disease vocabulary with standard alignment"""
    __tablename__ = "dim_disease"

    disease_code = Column(String(100), primary_key=True)
    disease_name = Column(String(100), nullable=False)
    description = Column(String(1000))
    source_standard = Column(String(100))
    pathogen = Column(String(100))
    icd_code = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))


class DimVector(Base):
    """Vector species/subspecies vocabulary"""
    __tablename__ = "dim_vector"

    vector_code = Column(String(100), primary_key=True)
    species_name = Column(String(100), nullable=False)
    subspecies = Column(String(100))
    description = Column(String(1000))
    source_standard = Column(String(100))
    genus = Column(String(100))
    family = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))


class DimInsecticide(Base):
    """Insecticide vocabulary"""
    __tablename__ = "dim_insecticide"

    insecticide_code = Column(String(100), primary_key=True)
    insecticide_name = Column(String(100), nullable=False)
    chemical_class = Column(String(100))
    description = Column(String(1000))
    source_standard = Column(String(100))
    irac_group = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))


class DimIdentificationMethod(Base):
    """Diagnostic method vocabulary"""
    __tablename__ = "dim_identification_method"

    method_code = Column(String(100), primary_key=True)
    method_name = Column(String(100), nullable=False)
    method_type = Column(String(100))
    description = Column(String(1000))
    source_standard = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))


class DimResistanceTestType(Base):
    """Resistance bioassay type vocabulary"""
    __tablename__ = "dim_resistance_test_type"

    test_type_code = Column(String(100), primary_key=True)
    test_type_name = Column(String(100), nullable=False)
    exposure_time_unit = Column(String(100))
    description = Column(String(1000))
    source_standard = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))


class DimSocioeconomicIndicator(Base):
    """Socioeconomic indicator vocabulary"""
    __tablename__ = "dim_socioeconomic_indicator"

    indicator_code = Column(String(100), primary_key=True)
    indicator_name = Column(String(1000), nullable=False)
    unit = Column(String(100))
    source = Column(String(1000))
    description = Column(String(1000))
    source_standard = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))


class DimSource(Base):
    """Data source provenance"""
    __tablename__ = "dim_source"

    source_code = Column(String(100), primary_key=True)
    source_name = Column(String(1000), nullable=False)
    source_description = Column(String(1000))
    source_standard = Column(String(100))
    source_url = Column(String(1000))
    version = Column(String(100))
    standard_code = Column(String(100))
    concept_id    = Column(Integer, ForeignKey("dim_concept.concept_id"))


# ========================= FACT TABLES =========================

class FactConceptMap(Base):
    """The **current** decision for one raw value, keyed on the source triple.

    One row per ``(source_table, source_column, source_value)``, holding whatever
    decision is in force now — ``auto_accepted``, ``needs_review``, ``rejected``
    or ``unresolved``. ``concept_code`` is non-NULL only for ``auto_accepted``
    (contract Amendment §5.6), so a reader must key the decision, never the mere
    presence of a code. ``fact_resolution_log`` holds the same values plus one
    row per run, which is the history; this table is the state.

    ``is_approved`` is the **human** verdict and is three-state: ``None`` means
    nobody has reviewed it, ``True`` a human approved, ``False`` a human
    rejected. No automated writer touches it, and the projection's upsert refuses
    to change ``concept_code`` on a row whose ``is_approved`` is ``True``.
    """
    __tablename__ = "fact_concept_map"

    map_id         = Column(Integer, primary_key=True, autoincrement=True)
    source_table   = Column(String(100), nullable=False)
    source_column  = Column(String(100), nullable=False)
    source_value   = Column(String(300), nullable=False)
    vocabulary_id  = Column(String(100), ForeignKey("dim_vocabulary.vocabulary_id"))
    concept_code   = Column(String(100))
    match_method   = Column(String(30))
    match_score    = Column(Float)

    decision           = Column(String(20))
    vocabulary_version = Column(String(100))
    resolver_version   = Column(String(50))
    last_run_id        = Column(String(50))

    is_approved    = Column(Boolean, nullable=True)
    reviewed_by    = Column(String(100))
    reviewed_at    = Column(DateTime)
    mapped_at      = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("source_table", "source_column", "source_value",
                         name="uq_concept_map_source"),
        Index("idx_concept_map_source", "source_table", "source_column"),
        Index("idx_concept_map_approved", "is_approved"),
        Index("idx_concept_map_decision", "decision"),
    )


class FactVectorObservation(Base):
    __tablename__ = "fact_vector_observation"

    observation_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_id = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    vector_code = Column(String(100), ForeignKey("dim_vector.vector_code"), nullable=False)
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    life_stage = Column(String(100))
    sex = Column(String(10))
    collection_method = Column(String(100))
    count = Column(Integer)
    density_per_trap = Column(Float)
    tested_for_pathogen = Column(String(100))
    infection_status = Column(String(100))
    infection_rate = Column(Float)

    location = relationship("DimLocation")
    time = relationship("DimTime", foreign_keys=[date_id])
    vector = relationship("DimVector")
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint(
            "location_id", "date_id", "vector_code", "source_code",
            "life_stage", "sex", "collection_method",
            name="uq_fact_vec_obs_natural",
        ),
        Index("idx_fact_vector_location", "location_id"),
        Index("idx_fact_vector_date", "date_id"),
        Index("idx_fact_vector_vector", "vector_code"),
        Index("idx_fact_vector_source", "source_code"),
    )


class FactDiseaseCase(Base):
    __tablename__ = "fact_disease_case"

    case_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_id_onset = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    date_id_reported = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    disease_code = Column(String(100), ForeignKey("dim_disease.disease_code"), nullable=False)
    identification_method_code = Column(
        String(100), ForeignKey("dim_identification_method.method_code"), nullable=False
    )
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    case_status = Column(String(100))
    host_type = Column(String(100))
    age_years = Column(Integer)
    age_group = Column(String(100))
    sex = Column(String(10))
    outcome = Column(String(100))
    case_count = Column(Integer,
        nullable=False,
        default=1,
        server_default="1")

    location = relationship("DimLocation")
    onset_date = relationship("DimTime", foreign_keys=[date_id_onset])
    reported_date = relationship("DimTime", foreign_keys=[date_id_reported])
    disease = relationship("DimDisease")
    identification_method = relationship("DimIdentificationMethod")
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint(
            "location_id", "date_id_onset", "disease_code",
            "identification_method_code", "source_code",
            "case_status", "age_group", "sex",
            name="uq_fact_case_natural",
        ),
        Index("idx_fact_case_location", "location_id"),
        Index("idx_fact_case_onset", "date_id_onset"),
        Index("idx_fact_case_reported", "date_id_reported"),
        Index("idx_fact_case_disease", "disease_code"),
        Index("idx_fact_case_source", "source_code"),
    )


class FactInsecticideResistance(Base):
    __tablename__ = "fact_insecticide_resistance"

    resistance_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_id = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    vector_code = Column(String(100), ForeignKey("dim_vector.vector_code"), nullable=False)
    insecticide_code = Column(
        String(100), ForeignKey("dim_insecticide.insecticide_code"), nullable=False
    )
    test_type_code = Column(
        String(100), ForeignKey("dim_resistance_test_type.test_type_code"), nullable=False
    )
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    concentration = Column(Float)
    exposure_time = Column(Integer)
    n_insect_tested = Column(Integer)
    n_insect_susceptible = Column(Integer)
    n_insect_resistant = Column(Integer)
    n_insect_dead = Column(Integer)
    vgsc_n_insect_tested = Column(Integer)
    vgsc995l_vgsc995l_n = Column(Integer)
    vgsc995l_vgsc995f_n = Column(Integer)
    vgsc995f_vgsc995f_n = Column(Integer)
    vgsc995l_vgsc995s_n = Column(Integer)
    vgsc995s_vgsc995s_n = Column(Integer)
    vgsc995f_vgsc995s_n = Column(Integer)
    vgsc_generation = Column(String)
    mortality = Column(Float)
    resistance_status = Column(String(30))

    location = relationship("DimLocation")
    insecticide = relationship("DimInsecticide")
    test_type = relationship("DimResistanceTestType")
    source = relationship("DimSource")
    time = relationship("DimTime", foreign_keys=[date_id])
    vector = relationship("DimVector")

    __table_args__ = (
        UniqueConstraint(
            "location_id", "date_id", "vector_code", "insecticide_code",
            "test_type_code", "source_code", "concentration",
            name="uq_fact_ir_natural",
        ),
        Index("idx_fact_resistance_insecticide", "insecticide_code"),
        Index("idx_fact_resistance_source", "source_code"),
        Index("idx_fact_resistance_date", "date_id"),
        Index("idx_fact_resistance_vector", "vector_code"),
    )


class FactBionomics(Base):
    __tablename__ = "fact_bionomics"

    bionomics_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_id = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    vector_code = Column(String(100), ForeignKey("dim_vector.vector_code"), nullable=False)
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    biting_rate = Column(Float)
    feeding_preference = Column(String(100))
    biting_location = Column(String(100))
    resting_behavior = Column(String(100))
    larval_site_type = Column(String(100))

    location = relationship("DimLocation")
    source = relationship("DimSource")
    time = relationship("DimTime", foreign_keys=[date_id])
    vector = relationship("DimVector")

    __table_args__ = (
        UniqueConstraint(
            "location_id", "date_id", "vector_code", "source_code",
            name="uq_fact_bionomics_natural",
        ),
        Index("idx_fact_bionomics_source", "source_code"),
        Index("idx_fact_bionomics_date", "date_id"),
        Index("idx_fact_bionomics_vector", "vector_code"),
    )


class FactIntervention(Base):
    __tablename__ = "fact_intervention"

    intervention_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_start = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    date_end = Column(String(10), ForeignKey("dim_time.date_id"))
    target_disease_code = Column(
        String(100), ForeignKey("dim_disease.disease_code"), nullable=False
    )
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    intervention_type = Column(String(100))
    coverage = Column(Float)

    location = relationship("DimLocation")
    start_date = relationship("DimTime", foreign_keys=[date_start])
    end_date = relationship("DimTime", foreign_keys=[date_end])
    target_disease = relationship("DimDisease")
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint(
            "location_id", "date_start", "target_disease_code",
            "source_code", "intervention_type",
            name="uq_fact_intervention_natural",
        ),
        Index("idx_fact_intervention_location", "location_id"),
        Index("idx_fact_intervention_start", "date_start"),
        Index("idx_fact_intervention_disease", "target_disease_code"),
        Index("idx_fact_intervention_source", "source_code"),
    )


class FactSocioeconomicCondition(Base):
    __tablename__ = "fact_socioeconomic_condition"

    socio_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    year = Column(Integer, nullable=False)
    indicator_code = Column(
        String(100),
        ForeignKey("dim_socioeconomic_indicator.indicator_code"),
        nullable=False,
    )
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    indicator_value = Column(Float)

    location = relationship("DimLocation")
    indicator = relationship("DimSocioeconomicIndicator")
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint(
            "location_id", "year", "indicator_code",
            name="uq_socio_loc_year_indicator",
        ),
        Index("idx_fact_socio_location", "location_id"),
        Index("idx_fact_socio_year", "year"),
        Index("idx_fact_socio_indicator", "indicator_code"),
        Index("idx_fact_socio_source", "source_code"),
    )


class FactHealthFacility(Base):
    __tablename__ = "fact_health_facility"

    facility_id = Column(Integer, primary_key=True, autoincrement=True)
    facility_code = Column(String(100), unique=True, nullable=False)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    name = Column(String(1000))
    lat = Column(Float)
    lon = Column(Float)
    type = Column(String(100))
    catchment_population = Column(Integer)

    location = relationship("DimLocation")
    source = relationship("DimSource")

    __table_args__ = (
        Index("idx_fact_facility_location", "location_id"),
        Index("idx_fact_facility_source", "source_code"),
    )


class FactDiseaseBurden(Base):
    __tablename__ = "fact_disease_burden"

    burden_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    year = Column(Integer, nullable=False)
    disease_code = Column(String(100), ForeignKey("dim_disease.disease_code"), nullable=False)
    pathogen_species = Column(String(50), nullable=True)
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    incidence_rate = Column(Float)
    prevalence_rate = Column(Float)
    mortality_rate = Column(Float)
    cases_mean = Column(Float)

    location = relationship("DimLocation")
    disease = relationship("DimDisease")
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint(
            "location_id", "year", "disease_code", "pathogen_species",
            name="uq_burden_loc_year_disease_pathogen",
        ),
        Index("idx_fact_burden_location", "location_id"),
        Index("idx_fact_burden_year", "year"),
        Index("idx_fact_burden_disease", "disease_code"),
        Index("idx_fact_burden_source", "source_code"),
    )


class FactEnvironment(Base):
    """Environmental data (temperature, rainfall, etc.) stored in long format"""
    __tablename__ = "fact_environment"

    fact_env_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_id = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    variable = Column(String(100), nullable=False)   # 'temperature_mean', 'rainfall_total'
    value = Column(Float, nullable=False)
    source_code = Column(String(100), ForeignKey("dim_source.source_code"), nullable=False)

    location = relationship("DimLocation")
    time = relationship("DimTime", foreign_keys=[date_id])
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint("location_id", "date_id", "variable", name="uq_env_loc_date_var"),
        Index("idx_fact_env_location", "location_id"),
        Index("idx_fact_env_date", "date_id"),
        Index("idx_fact_env_variable", "variable"),
        Index("idx_fact_env_source", "source_code"),
    )