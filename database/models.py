# -*- coding: utf-8 -*-

import datetime
import uuid
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, UniqueConstraint, Index, Text
from sqlalchemy.orm import relationship
from database.session import Base


# ========================= DIMENSIONS =========================

class DimTime(Base):
    """Time dimension table"""
    __tablename__ = "dim_time"

    date_id = Column(String(10), primary_key=True)  # YYYY-MM-DD
    date = Column(Date, nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    week = Column(Integer)
    day_of_year = Column(Integer)
    season = Column(String(20))
    is_holiday = Column(Boolean, default=False)


class DimLocation(Base):
    """Location hierarchy (country, region, district, site)"""
    __tablename__ = "dim_location"

    location_id = Column(Integer,  primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    location_code = Column(String, unique=True)
    centroid_lat = Column(Float)
    centroid_lon = Column(Float)
    country_code = Column(String(10))
    country_name = Column(String(100))
    admin_level = Column(String(50))


class DimDisease(Base):
    """Disease vocabulary with standard alignment"""
    __tablename__ = "dim_disease"

    disease_code = Column(String(20), primary_key=True)
    disease_name = Column(String(100), nullable=False)
    description = Column(String(500))
    source_standard = Column(String(100))
    pathogen = Column(String(100))
    icd_code = Column(String(20))


class DimVector(Base):
    """Vector species/subspecies vocabulary"""
    __tablename__ = "dim_vector"

    vector_code = Column(String(20), primary_key=True)
    species_name = Column(String(100), nullable=False)
    subspecies = Column(String(50))
    description = Column(String(500))
    source_standard = Column(String(100))
    genus = Column(String(50))
    family = Column(String(50))


class DimInsecticide(Base):
    """Insecticide vocabulary"""
    __tablename__ = "dim_insecticide"

    insecticide_code = Column(String(20), primary_key=True)
    insecticide_name = Column(String(100), nullable=False)
    chemical_class = Column(String(50))
    description = Column(String(500))
    source_standard = Column(String(100))
    irac_group = Column(String(20))


class DimIdentificationMethod(Base):
    """Diagnostic method vocabulary"""
    __tablename__ = "dim_identification_method"

    method_code = Column(String(20), primary_key=True)
    method_name = Column(String(100), nullable=False)
    method_type = Column(String(50))
    description = Column(String(500))
    source_standard = Column(String(100))


class DimResistanceTestType(Base):
    """Resistance bioassay type vocabulary"""
    __tablename__ = "dim_resistance_test_type"

    test_type_code = Column(String(20), primary_key=True)
    test_type_name = Column(String(100), nullable=False)
    exposure_time_unit = Column(String(20))
    description = Column(String(500))
    source_standard = Column(String(100))


class DimSocioeconomicIndicator(Base):
    """Socioeconomic indicator vocabulary"""
    __tablename__ = "dim_socioeconomic_indicator"

    indicator_code = Column(String(50), primary_key=True)
    indicator_name = Column(String(200), nullable=False)
    unit = Column(String(50))
    source = Column(String(200))
    description = Column(String(500))
    source_standard = Column(String(100))


class DimSource(Base):
    """Data source provenance"""
    __tablename__ = "dim_source"

    source_code = Column(String(20), primary_key=True)
    source_name = Column(String(200), nullable=False)
    source_description = Column(String(500))
    source_standard = Column(String(100))
    source_url = Column(String(500))
    version = Column(String(50))


# ========================= FACT TABLES =========================

class FactVectorObservation(Base):
    __tablename__ = "fact_vector_observation"

    observation_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_id = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    vector_code = Column(String(20), ForeignKey("dim_vector.vector_code"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    life_stage = Column(String(50))
    sex = Column(String(10))
    collection_method = Column(String(100))
    count = Column(Integer)
    density_per_trap = Column(Float)
    tested_for_pathogen = Column(String(100))
    infection_status = Column(String(20))
    infection_rate = Column(Float)

    # Relationships
    location = relationship("DimLocation")
    time = relationship("DimTime", foreign_keys=[date_id])
    vector = relationship("DimVector")
    source = relationship("DimSource")

    __table_args__ = (
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
    disease_code = Column(String(20), ForeignKey("dim_disease.disease_code"), nullable=False)
    identification_method_code = Column(String(20), ForeignKey("dim_identification_method.method_code"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    case_status = Column(String(20))
    host_type = Column(String(20))
    age_years = Column(Integer)
    age_group = Column(String(20))
    sex = Column(String(10))
    outcome = Column(String(20))

    # Relationships
    location = relationship("DimLocation")
    onset_date = relationship("DimTime", foreign_keys=[date_id_onset])
    reported_date = relationship("DimTime", foreign_keys=[date_id_reported])
    disease = relationship("DimDisease")
    identification_method = relationship("DimIdentificationMethod")
    source = relationship("DimSource")

    __table_args__ = (
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
    insecticide_code = Column(String(20), ForeignKey("dim_insecticide.insecticide_code"), nullable=False)
    test_type_code = Column(String(20), ForeignKey("dim_resistance_test_type.test_type_code"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

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
    vgsc995s_vgsc995s_n = Column(Integer)
    vgsc_generation = Column(String)
    vgsc_generation = Column(String)
    mortality = Column(Float)
    resistance_status = Column(String(30))

    # Relationships
    location = relationship("DimLocation")
    insecticide = relationship("DimInsecticide")
    test_type = relationship("DimResistanceTestType")
    source = relationship("DimSource")

    __table_args__ = (
        Index("idx_fact_resistance_insecticide", "insecticide_code"),
        Index("idx_fact_resistance_source", "source_code"),
    )


class FactBionomics(Base):
    __tablename__ = "fact_bionomics"

    bionomics_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_id = Column(String(10), ForeignKey("dim_time.date_id"), nullable=False)
    vector_code = Column(String(20), ForeignKey("dim_vector.vector_code"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    biting_rate = Column(Float)
    feeding_preference = Column(String(20))
    biting_location = Column(String(20))
    resting_behavior = Column(String(50))
    larval_site_type = Column(String(100))

    # Relationships
    location = relationship("DimLocation")
    source = relationship("DimSource")
    time = relationship("DimTime", foreign_keys=[date_id])
    vector = relationship("DimVector")

    __table_args__ = (
        Index("idx_fact_bionomics_source", "source_code"),
    )


class FactIntervention(Base):
    __tablename__ = "fact_intervention"

    intervention_id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    date_start = Column(Date, ForeignKey("dim_time.date_id"), nullable=False)
    date_end = Column(Date, ForeignKey("dim_time.date_id"))
    target_disease_code = Column(String(20), ForeignKey("dim_disease.disease_code"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    intervention_type = Column(String(50))
    coverage = Column(Float)

    # Relationships
    location = relationship("DimLocation")
    start_date = relationship("DimTime", foreign_keys=[date_start])
    end_date = relationship("DimTime", foreign_keys=[date_end])
    target_disease = relationship("DimDisease")
    source = relationship("DimSource")

    __table_args__ = (
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
    indicator_code = Column(String(50), ForeignKey("dim_socioeconomic_indicator.indicator_code"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    indicator_value = Column(Float)

    # Relationships
    location = relationship("DimLocation")
    indicator = relationship("DimSocioeconomicIndicator")
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint("location_id", "year", "indicator_code", name="uq_socio_loc_year_indicator"),
        Index("idx_fact_socio_location", "location_id"),
        Index("idx_fact_socio_year", "year"),
        Index("idx_fact_socio_indicator", "indicator_code"),
        Index("idx_fact_socio_source", "source_code"),
    )


class FactHealthFacility(Base):
    __tablename__ = "fact_health_facility"

    facility_id = Column(Integer, primary_key=True, autoincrement=True)
    facility_code = Column(String(50), unique=True, nullable=False)
    location_id = Column(Integer, ForeignKey("dim_location.location_id"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    name = Column(String(200))
    lat = Column(Float)
    lon = Column(Float)
    type = Column(String(50))
    catchment_population = Column(Integer)

    # Relationships
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
    disease_code = Column(String(20), ForeignKey("dim_disease.disease_code"), nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    incidence_rate = Column(Float)
    prevalence_rate = Column(Float)
    mortality_rate = Column(Float)
    cases_mean = Column(Float)

    # Relationships
    location = relationship("DimLocation")
    disease = relationship("DimDisease")
    source = relationship("DimSource")

    __table_args__ = (
        UniqueConstraint("location_id", "year", "disease_code", name="uq_burden_loc_year_disease"),
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
    variable = Column(String(50), nullable=False)   # e.g., 'temperature_mean', 'rainfall_total'
    value = Column(Float, nullable=False)
    source_code = Column(String(20), ForeignKey("dim_source.source_code"), nullable=False)

    # Relationships
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