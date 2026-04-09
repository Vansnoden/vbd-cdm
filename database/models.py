#-*- coding:utf-8 -*-

import datetime

from sqlalchemy import Column, DateTime, Integer, String, Float, Date, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from geoalchemy2 import Geometry 
from database.session import Base


class DimTime(Base):
    """Time dimension table for easy temporal analysis"""
    __tablename__ = "dim_time"
    
    date_id = Column(String(10), primary_key=True)  # YYYY-MM-DD
    date = Column(Date, nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    week = Column(Integer)
    day_of_year = Column(Integer)
    season = Column(String(20))  # 'dry', 'wet', 'spring', etc.
    is_holiday = Column(String(3), default='no')  # 'yes' or 'no'


class DimLocation(Base):
    """Geographic locations hierarchy"""
    __tablename__ = "dim_location"
    
    id = Column(Integer, primary_key=True, index=True)
    location_code = Column(String(50), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    admin_level = Column(String(50))  # country, state, district, village
    parent_location_code = Column(String(50), ForeignKey("dim_location.location_code"))
    geometry_wkt = Column(Text)
    centroid_lat = Column(Float)
    centroid_lon = Column(Float)
    country_name = Column(String)
    country_code = Column(String)
    parent = relationship("DimLocation",
                          remote_side=[location_code], backref="children")

    def set_country_from_coords(self):
        """
        Automatically set country_name and country_code from centroid coordinates.
        """
        from database.geoutils import get_country_from_coords
        
        if self.centroid_lat and self.centroid_lon:
            country_name, country_code = get_country_from_coords(
                self.centroid_lat, self.centroid_lon
            )
            self.country_name = country_name
            self.country_code = country_code


class FactHostCase(Base):
    """Human or animal disease cases"""
    __tablename__ = "fact_host_case"
    
    id = Column(Integer, primary_key=True, index=True)
    case_identifier = Column(String(100), unique=True)  # External ID
    location_code = Column(String(50), ForeignKey("location.location_code"))
    date_id_onset = Column(String(10), ForeignKey("dim_time.date_id"))
    date_id_reported = Column(String(10), ForeignKey("dim_time.date_id"))
    
    # Disease classification
    disease = Column(String(100), nullable=False)  # 'malaria', 'dengue'
    pathogen = Column(String(100))  # 'Plasmodium falciparum'
    case_status = Column(String(50))  # 'suspected', 'probable', 'confirmed'
    confirmation_method = Column(String(100))  # 'PCR', 'RDT', 'clinical'
    
    # Host details
    host_type = Column(String(50), default='human')  # 'human', 'animal'
    age_years = Column(Integer)
    age_group = Column(String(50))
    sex = Column(String(10))
    outcome = Column(String(50))  # 'recovered', 'died', 'unknown'
    
    # Relationships
    location = relationship("DimLocation")
    onset_date = relationship("DimTime", foreign_keys=[date_id_onset])
    reported_date = relationship("DimTime", foreign_keys=[date_id_reported])


class FactDiseaseBurden(Base):
    """Disease burden data from sources like Malaria Atlas Project"""
    __tablename__ = "fact_disease_burden"
    
    id = Column(Integer, primary_key=True, index=True)
    location_code = Column(String(50), ForeignKey("location.location_code"), nullable=False)
    year = Column(Integer, nullable=False)
    disease = Column(String(50), nullable=False)  # 'malaria', 'dengue', etc.
    
    # Incidence and prevalence metrics
    incidence_rate = Column(Float)  # Cases per 1000 population per year
    prevalence_rate = Column(Float)  # Percentage of population infected
    mortality_rate = Column(Float)  # Deaths per 1000 population per year
    cases_lower = Column(Float)  # Lower confidence interval
    cases_upper = Column(Float)  # Upper confidence interval
    cases_mean = Column(Float)   # Mean estimate
    
    # Metadata
    source = Column(String(200))  # e.g., 'Malaria Atlas Project'
    version = Column(String(50))   # Dataset version
    raster_file = Column(String(500))  # Original raster file name
    extraction_method = Column(String(50))  # 'mean', 'median', etc.
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    
    # Relationships
    location = relationship("DimLocation")
    
    __table_args__ = (
        UniqueConstraint('location_code', 'year', 'disease', name='uq_disease_burden_loc_year_disease'),
    )


class FactVectorObservation(Base):
    """Vector surveillance data"""
    __tablename__ = "fact_vector_observation"
    
    id = Column(Integer, primary_key=True, index=True)
    location_code = Column(String(50), ForeignKey("dim_location.location_code"))
    date_id = Column(String(10), ForeignKey("dim_time.date_id"))
    
    # Vector details
    vector_species = Column(String(100), nullable=False)
    vector_subspecies = Column(String(100))
    life_stage = Column(String(50))  # 'egg', 'larva', 'pupa', 'adult'
    sex = Column(String(10))  # 'male', 'female', 'mixed'
    
    # Collection methods
    collection_method = Column(String(100))  # 'CDC_light_trap', 'BG_sentinel'
    count = Column(Integer)
    density_per_trap = Column(Float)  # Calculated field
    
    # Infection status
    tested_for_pathogen = Column(String(100))
    infection_status = Column(String(50))  # 'positive', 'negative'
    infection_rate = Column(Float)  # Proportion positive
    
    # Relationships
    location = relationship("DimLocation")
    date = relationship("DimTime")


class FactEnvironmentalFactor(Base):
    """Climate and environmental data"""
    __tablename__ = "fact_environmental_factor"
    
    id = Column(Integer, primary_key=True, index=True)
    location_code = Column(String(50), ForeignKey("dim_location.location_code"))
    date_id = Column(String(10), ForeignKey("dim_time.date_id"))
    
    # Climate variables
    temperature_mean = Column(Float)  # °C
    temperature_min = Column(Float)
    temperature_max = Column(Float)
    rainfall_total = Column(Float)  # mm
    relative_humidity = Column(Float)  # %
    
    # Remote sensing indices
    ndvi = Column(Float)  # Normalized Difference Vegetation Index
    evi = Column(Float)   # Enhanced Vegetation Index
    lst_day = Column(Float)  # Land Surface Temperature (day)
    
    # Derived indicators
    aridity_index = Column(Float)
    potential_breeding_sites = Column(Integer)  # Estimated from rainfall
    
    __table_args__ = (UniqueConstraint('location_code', 'date_id', name='uq_env_location_date'),)
    
    # Relationships
    location = relationship("DimLocation")
    date = relationship("DimTime")


class FactHealthFacility(Base):
    __tablename__ = "fact_health_facility"
    id = Column(Integer, primary_key=True)
    facility_code = Column(String(50), unique=True)
    name = Column(String(200))
    location_code = Column(String(50), ForeignKey("dim_location.location_code"))
    lat = Column(Float)
    lon = Column(Float)
    type = Column(String(50))  # hospital, clinic, dispensary
    catchment_population = Column(Integer)


class FactIntervention(Base):
    __tablename__ = "fact_intervention"
    id = Column(Integer, primary_key=True)
    intervention_type = Column(String(50))  # 'IRS', 'ITN', 'larviciding'
    date_start = Column(Date)
    date_end = Column(Date)
    location_code = Column(String(50), ForeignKey("dim_location.location_code"))
    coverage = Column(Float)  # percentage or number
    target_disease = Column(String(50))  # 'malaria', 'dengue', 'both'


class FactResistanceTest(Base):
    __tablename__ = "fact_resistance_test"
    id = Column(Integer, primary_key=True)
    vector_observation_id = Column(Integer, ForeignKey("fact_vector_observation.id"))
    test_type = Column(String(50))  # 'WHO tube', 'CDC bottle'
    insecticide = Column(String(100))
    concentration = Column(Float)
    exposure_time = Column(Integer)  # minutes
    mortality = Column(Float)        # proportion dead
    resistance_status = Column(String(50))  # 'susceptible', 'confirmed', 'possible'


class FactSocioEconomic(Base):
    __tablename__ = "fact_socioeconomic"
    id = Column(Integer, primary_key=True)
    location_code = Column(String(50), ForeignKey("location.location_code"))
    year = Column(Integer)
    indicator_name = Column(String(100))  # e.g., 'ITN_use', 'poverty_rate'
    indicator_value = Column(Float)
    source = Column(String(100))



# class Intervention(Base):
#     __tablename__ = "intervention"
    
#     id = Column(Integer, primary_key=True)
#     campaign_id = Column(String(100), nullable=True)
#     location_code = Column(String(50), ForeignKey("location.location_code"), nullable=False)
#     year = Column(Integer, nullable=False)
#     month = Column(Integer, nullable=False)
#     intervention_type = Column(String(50), nullable=False)
#     sub_type = Column(String(100))
#     coverage_numeric = Column(Float)
#     coverage_unit = Column(String(50))
#     coverage_percent = Column(Float)
#     target_population = Column(Integer)
#     source = Column(String(200))
#     notes = Column(Text)
#     last_updated = Column(DateTime, default=datetime.utcnow)
    
#     __table_args__ = (
#         UniqueConstraint('location_code', 'year', 'month', 'intervention_type', 
#                          name='uq_intervention_loc_date_type'),
#     )


# class CaseLine(Base):
#     __tablename__ = "case_line"

#     id = Column(Integer, primary_key=True)
#     case_identifier = Column(String(100), unique=True)
#     location_code = Column(String(50), ForeignKey("location.location_code"), nullable=False)
#     date_onset = Column(Date)
#     date_report = Column(Date)
#     date_confirmation = Column(Date)
#     disease = Column(String(50), nullable=False)
#     pathogen = Column(String(100))
#     case_status = Column(String(20), nullable=False)
#     confirmation_method = Column(String(50))
#     age_years = Column(Integer)
#     age_group = Column(String(20))
#     sex = Column(String(10))
#     pregnancy_status = Column(String(20))
#     severity = Column(String(20))
#     outcome = Column(String(20))
#     outcome_date = Column(Date)
#     treatment = Column(String(100))
#     source = Column(String(200))
#     last_updated = Column(DateTime, default=datetime.utcnow)


# class HealthFacility(Base):
#     __tablename__ = "health_facility"

#     id = Column(Integer, primary_key=True)
#     facility_code = Column(String(50), unique=True, nullable=False)
#     name = Column(String(200), nullable=False)
#     location_code = Column(String(50), ForeignKey("location.location_code"), nullable=False)
#     facility_type = Column(String(50))
#     ownership = Column(String(50))
#     operational_status = Column(String(20), default='functional')
#     latitude = Column(Float, nullable=False)
#     longitude = Column(Float, nullable=False)
#     geocoding_method = Column(String(50))
#     address = Column(Text)
#     catchment_population = Column(Integer)
#     catchment_area_km2 = Column(Float)
#     has_maternity = Column(Boolean, default=False)
#     has_lab = Column(Boolean, default=False)
#     has_microscopy = Column(Boolean, default=False)
#     has_rdt = Column(Boolean, default=False)
#     has_act = Column(Boolean, default=False)
#     inpatient_beds = Column(Integer)
#     outpatient_visits_2023 = Column(Integer)  # or use a separate year table for multiple years
#     source = Column(String(200))
#     last_updated = Column(DateTime)