#-*- coding:utf-8 -*-

from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, Text, UniqueConstraint
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


class Location(Base):
    """Geographic locations hierarchy"""
    __tablename__ = "location"
    
    id = Column(Integer, primary_key=True, index=True)
    location_code = Column(String(50), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    admin_level = Column(String(50))  # country, state, district, village
    parent_location_code = Column(String(50), ForeignKey("location.location_code"))
    # For spatial: use Geometry column for PostGIS, Text for SQLite
    geometry_wkt = Column(Text)  # WKT representation
    centroid_lat = Column(Float)
    centroid_lon = Column(Float)

    # Self-referential relationship
    parent = relationship("Location", remote_side=[location_code], backref="children")


class HostCase(Base):
    """Human or animal disease cases"""
    __tablename__ = "host_case"
    
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
    location = relationship("Location")
    onset_date = relationship("DimTime", foreign_keys=[date_id_onset])
    reported_date = relationship("DimTime", foreign_keys=[date_id_reported])


class VectorObservation(Base):
    """Vector surveillance data"""
    __tablename__ = "vector_observation"
    
    id = Column(Integer, primary_key=True, index=True)
    location_code = Column(String(50), ForeignKey("location.location_code"))
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
    location = relationship("Location")
    date = relationship("DimTime")


class EnvironmentalFactor(Base):
    """Climate and environmental data"""
    __tablename__ = "environmental_factor"
    
    id = Column(Integer, primary_key=True, index=True)
    location_code = Column(String(50), ForeignKey("location.location_code"))
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
    location = relationship("Location")
    date = relationship("DimTime")
