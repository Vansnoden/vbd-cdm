#-*- coding:utf-8 -*-

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import date, datetime


# -------------------- DimTime --------------------
class DimTimeCreate(BaseModel):
    """Schema for creating time dimension records."""
    date_id: str = Field(..., description="Date in YYYY-MM-DD format")
    date: date
    year: int
    month: int
    week: Optional[int] = None
    day_of_year: Optional[int] = None
    season: Optional[str] = None
    is_holiday: Optional[str] = "no"

    @field_validator('date_id')
    def validate_date_id(cls, v):
        try:
            datetime.strptime(v, '%Y-%m-%d')
        except ValueError:
            raise ValueError("date_id must be in YYYY-MM-DD format")
        return v

    @field_validator('month')
    def validate_month(cls, v):
        if not 1 <= v <= 12:
            raise ValueError("month must be between 1 and 12")
        return v


# -------------------- Location --------------------
class LocationCreate(BaseModel):
    """Schema for creating/updating locations."""
    location_code: str = Field(..., description="Unique identifier for the location")
    name: str
    admin_level: str = Field(..., description="e.g., country, state, district, village")
    parent_location_code: Optional[str] = None
    geometry_wkt: Optional[str] = None
    centroid_lat: Optional[float] = None
    centroid_lon: Optional[float] = None

    @field_validator('centroid_lat')
    def validate_lat(cls, v):
        if v is not None and (v < -90 or v > 90):
            raise ValueError("latitude must be between -90 and 90")
        return v

    @field_validator('centroid_lon')
    def validate_lon(cls, v):
        if v is not None and (v < -180 or v > 180):
            raise ValueError("longitude must be between -180 and 180")
        return v


# -------------------- HostCase --------------------
class HostCaseCreate(BaseModel):
    """Schema for creating disease case records."""
    case_identifier: str = Field(..., description="External identifier for the case")
    location_code: str
    date_id_onset: str = Field(..., description="Date of symptom onset (YYYY-MM-DD)")
    date_id_reported: str = Field(..., description="Date case was reported (YYYY-MM-DD)")
    disease: str
    pathogen: Optional[str] = None
    case_status: str = "suspected"
    confirmation_method: Optional[str] = None
    host_type: str = "human"
    age_years: Optional[int] = None
    age_group: Optional[str] = None
    sex: Optional[str] = None
    outcome: Optional[str] = "unknown"

    @field_validator('date_id_onset', 'date_id_reported')
    def validate_date_format(cls, v):
        try:
            datetime.strptime(v, '%Y-%m-%d')
        except ValueError:
            raise ValueError(f"Invalid date format: {v}. Use YYYY-MM-DD")
        return v

    @field_validator('age_years')
    def validate_age(cls, v):
        if v is not None and v < 0:
            raise ValueError("age_years cannot be negative")
        return v

    @field_validator('sex')
    def validate_sex(cls, v):
        if v is not None and v.upper() not in {'M', 'F', 'OTHER', 'UNKNOWN'}:
            raise ValueError("sex must be one of: M, F, OTHER, UNKNOWN")
        return v


# -------------------- VectorObservation --------------------
class VectorObservationCreate(BaseModel):
    """Schema for creating vector surveillance records."""
    location_code: str
    date_id: str = Field(..., description="Date of observation (YYYY-MM-DD)")
    vector_species: str
    vector_subspecies: Optional[str] = None
    life_stage: Optional[str] = None  # egg, larva, pupa, adult
    sex: Optional[str] = None          # male, female, mixed
    collection_method: Optional[str] = None
    trap_id: Optional[str] = None
    count: Optional[int] = None
    density_per_trap: Optional[float] = None
    tested_for_pathogen: Optional[str] = None
    infection_status: Optional[str] = None  # positive, negative
    infection_rate: Optional[float] = None

    @field_validator('date_id')
    def validate_date_id(cls, v):
        try:
            datetime.strptime(v, '%Y-%m-%d')
        except ValueError:
            raise ValueError("date_id must be in YYYY-MM-DD format")
        return v

    @field_validator('infection_rate')
    def validate_rate(cls, v):
        if v is not None and (v < 0 or v > 1):
            raise ValueError("infection_rate must be between 0 and 1")
        return v


# -------------------- EnvironmentalFactor --------------------
class EnvironmentalFactorCreate(BaseModel):
    """Schema for creating environmental data records."""
    location_code: str
    date_id: str = Field(..., description="Date of observation (YYYY-MM-DD)")
    temperature_mean: Optional[float] = None
    temperature_min: Optional[float] = None
    temperature_max: Optional[float] = None
    rainfall_total: Optional[float] = None
    relative_humidity: Optional[float] = None
    ndvi: Optional[float] = None
    evi: Optional[float] = None
    lst_day: Optional[float] = None
    aridity_index: Optional[float] = None
    potential_breeding_sites: Optional[int] = None

    @field_validator('date_id')
    def validate_date_id(cls, v):
        try:
            datetime.strptime(v, '%Y-%m-%d')
        except ValueError:
            raise ValueError("date_id must be in YYYY-MM-DD format")
        return v

    @field_validator('temperature_mean', 'temperature_min', 'temperature_max')
    def validate_temperature(cls, v):
        if v is not None and v < -100:
            raise ValueError("unrealistically low temperature")
        return v

    @field_validator('rainfall_total')
    def validate_rainfall(cls, v):
        if v is not None and v < 0:
            raise ValueError("rainfall cannot be negative")
        return v

    @field_validator('relative_humidity')
    def validate_humidity(cls, v):
        if v is not None and (v < 0 or v > 100):
            raise ValueError("relative humidity must be between 0 and 100")
        return v

    @field_validator('ndvi', 'evi')
    def validate_vegetation_indices(cls, v):
        if v is not None and (v < -1 or v > 1):
            raise ValueError("NDVI and EVI must be between -1 and 1")
        return v


# -------------------- Optional: Response Schemas (if you ever need to return data via API) --------------------
class LocationResponse(LocationCreate):
    id: int
    class Config:
        from_attributes = True

class HostCaseResponse(HostCaseCreate):
    id: int
    class Config:
        from_attributes = True

class VectorObservationResponse(VectorObservationCreate):
    id: int
    class Config:
        from_attributes = True

class EnvironmentalFactorResponse(EnvironmentalFactorCreate):
    id: int
    class Config:
        from_attributes = True


# -------------------- DiseaseBurden --------------------
class DiseaseBurdenBase(BaseModel):
    """Base schema for disease burden data."""
    location_code: str
    year: int
    disease: str
    incidence_rate: Optional[float] = None
    prevalence_rate: Optional[float] = None
    mortality_rate: Optional[float] = None
    cases_lower: Optional[float] = None
    cases_upper: Optional[float] = None
    cases_mean: Optional[float] = None
    source: Optional[str] = None
    version: Optional[str] = None
    raster_file: Optional[str] = None
    extraction_method: Optional[str] = None


class DiseaseBurdenCreate(DiseaseBurdenBase):
    """Schema for creating disease burden records."""
    pass


class DiseaseBurdenUpdate(BaseModel):
    """Schema for updating disease burden records."""
    incidence_rate: Optional[float] = None
    prevalence_rate: Optional[float] = None
    mortality_rate: Optional[float] = None
    cases_lower: Optional[float] = None
    cases_upper: Optional[float] = None
    cases_mean: Optional[float] = None
    source: Optional[str] = None
    version: Optional[str] = None
    extraction_method: Optional[str] = None


class DiseaseBurdenResponse(DiseaseBurdenBase):
    """Schema for disease burden response."""
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class DiseaseBurdenInDB(DiseaseBurdenBase):
    """Schema for disease burden in database."""
    id: int
    
    class Config:
        from_attributes = True