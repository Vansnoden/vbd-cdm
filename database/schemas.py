# -*- coding: utf-8 -*-
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import date, datetime


# ========================= DIMENSIONS =========================

class DimTimeCreate(BaseModel):
    """Schema for creating time dimension records."""
    date_id: str = Field(..., description="Date in YYYY-MM-DD format")
    date: date
    year: int
    month: int
    week: Optional[int] = None
    day_of_year: Optional[int] = None
    season: Optional[str] = None
    is_holiday: Optional[bool] = False

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


class DimLocationCreate(BaseModel):
    """Schema for creating location records."""
    location_code: str = Field(..., description="Unique identifier for the location")
    name: str
    admin_level: Optional[str] = None
    parent_location_code: Optional[str] = None
    geometry_wkt: Optional[str] = None
    centroid_lat: Optional[float] = None
    centroid_lon: Optional[float] = None
    country_name: Optional[str] = None
    country_code: Optional[str] = None

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


class DimDiseaseCreate(BaseModel):
    """Schema for disease vocabulary with standard alignment."""
    disease_code: str = Field(..., max_length=20)
    disease_name: str
    description: Optional[str] = None
    source_standard: Optional[str] = None
    pathogen: Optional[str] = None
    icd_code: Optional[str] = None


class DimVectorCreate(BaseModel):
    """Schema for vector species vocabulary."""
    vector_code: str = Field(..., max_length=20)
    species_name: str
    subspecies: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    genus: Optional[str] = None
    family: Optional[str] = None


class DimInsecticideCreate(BaseModel):
    """Schema for insecticide vocabulary."""
    insecticide_code: str = Field(..., max_length=20)
    insecticide_name: str
    chemical_class: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None
    irac_group: Optional[str] = None


class DimIdentificationMethodCreate(BaseModel):
    """Schema for diagnostic method vocabulary."""
    method_code: str = Field(..., max_length=20)
    method_name: str
    method_type: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None


class DimResistanceTestTypeCreate(BaseModel):
    """Schema for resistance test type vocabulary."""
    test_type_code: str = Field(..., max_length=20)
    test_type_name: str
    exposure_time_unit: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None


class DimSocioeconomicIndicatorCreate(BaseModel):
    """Schema for socioeconomic indicator vocabulary."""
    indicator_code: str = Field(..., max_length=50)
    indicator_name: str
    unit: Optional[str] = None
    source: Optional[str] = None
    description: Optional[str] = None
    source_standard: Optional[str] = None


class DimSourceCreate(BaseModel):
    """Schema for data source provenance."""
    source_code: str = Field(..., max_length=20)
    source_name: str
    source_description: Optional[str] = None
    source_standard: Optional[str] = None
    source_url: Optional[str] = None
    version: Optional[str] = None


# ========================= FACT TABLES =========================

class FactVectorObservationCreate(BaseModel):
    """Schema for vector observation records."""
    location_code: str
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


class FactDiseaseCaseCreate(BaseModel):
    """Schema for disease case records."""
    location_code: str
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


class FactInsecticideResistanceCreate(BaseModel):
    """Schema for insecticide resistance test records."""
    vector_observation_id: int
    insecticide_code: str
    test_type_code: str
    source_code: str
    concentration: Optional[float] = None
    exposure_time: Optional[int] = None
    mortality: Optional[float] = None
    resistance_status: Optional[str] = None

    @field_validator('mortality')
    def validate_mortality(cls, v):
        if v is not None and (v < 0 or v > 1):
            raise ValueError("mortality must be between 0 and 1")
        return v


class FactBionomicsCreate(BaseModel):
    """Schema for bionomics records."""
    vector_observation_id: int
    source_code: str
    biting_rate: Optional[float] = None
    feeding_preference: Optional[str] = None      # anthropophagic, zoophagic, mixed
    biting_location: Optional[str] = None         # endophagic, exophagic, mixed
    resting_behavior: Optional[str] = None
    larval_site_type: Optional[str] = None


class FactInterventionCreate(BaseModel):
    """Schema for intervention records."""
    location_code: str
    date_start: date
    date_end: Optional[date] = None
    target_disease_code: str
    source_code: str
    intervention_type: Optional[str] = None
    coverage: Optional[float] = None

    @field_validator('coverage')
    def validate_coverage(cls, v):
        if v is not None and (v < 0 or v > 100):
            raise ValueError("coverage should be between 0 and 100 (percentage)")
        return v


class FactSocioeconomicConditionCreate(BaseModel):
    """Schema for socioeconomic condition records."""
    location_code: str
    year: int
    indicator_code: str
    source_code: str
    indicator_value: float

    @field_validator('year')
    def validate_year(cls, v):
        if v < 1900 or v > 2100:
            raise ValueError("year out of reasonable range")
        return v


class FactHealthFacilityCreate(BaseModel):
    """Schema for health facility records."""
    facility_code: str
    location_code: str
    source_code: str
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    type: Optional[str] = None
    catchment_population: Optional[int] = None

    @field_validator('lat')
    def validate_lat(cls, v):
        if v is not None and (v < -90 or v > 90):
            raise ValueError("latitude must be between -90 and 90")
        return v

    @field_validator('lon')
    def validate_lon(cls, v):
        if v is not None and (v < -180 or v > 180):
            raise ValueError("longitude must be between -180 and 180")
        return v


class FactDiseaseBurdenCreate(BaseModel):
    """Schema for disease burden estimates."""
    location_code: str
    year: int
    disease_code: str
    source_code: str
    incidence_rate: Optional[float] = None
    prevalence_rate: Optional[float] = None
    mortality_rate: Optional[float] = None
    cases_mean: Optional[float] = None


class FactEnvironmentalCreate(BaseModel):
    """Schema for environmental data (long format)."""
    location_code: str
    date_id: str
    variable: str = Field(..., description="e.g., 'temperature_mean', 'rainfall_total'")
    value: float
    source_code: str

    @field_validator('date_id')
    def validate_date_id(cls, v):
        try:
            datetime.strptime(v, '%Y-%m-%d')
        except ValueError:
            raise ValueError("date_id must be in YYYY-MM-DD format")
        return v

    @field_validator('variable')
    def validate_variable(cls, v):
        allowed = {'temperature_mean', 'temperature_min', 'temperature_max',
                   'rainfall_total', 'relative_humidity', 'ndvi', 'evi',
                   'lst_day', 'aridity_index', 'potential_breeding_sites'}
        if v not in allowed:
            # Allow any variable but warn? Or just accept.
            pass
        return v


# ========================= RESPONSE SCHEMAS =========================

class DimTimeResponse(DimTimeCreate):
    class Config:
        from_attributes = True


class DimLocationResponse(DimLocationCreate):
    class Config:
        from_attributes = True


class DimDiseaseResponse(DimDiseaseCreate):
    class Config:
        from_attributes = True


class DimVectorResponse(DimVectorCreate):
    class Config:
        from_attributes = True


class DimInsecticideResponse(DimInsecticideCreate):
    class Config:
        from_attributes = True


class DimIdentificationMethodResponse(DimIdentificationMethodCreate):
    class Config:
        from_attributes = True


class DimResistanceTestTypeResponse(DimResistanceTestTypeCreate):
    class Config:
        from_attributes = True


class DimSocioeconomicIndicatorResponse(DimSocioeconomicIndicatorCreate):
    class Config:
        from_attributes = True


class DimSourceResponse(DimSourceCreate):
    class Config:
        from_attributes = True


class FactVectorObservationResponse(FactVectorObservationCreate):
    observation_id: int
    class Config:
        from_attributes = True


class FactDiseaseCaseResponse(FactDiseaseCaseCreate):
    case_id: int
    class Config:
        from_attributes = True


class FactInsecticideResistanceResponse(FactInsecticideResistanceCreate):
    resistance_id: int
    class Config:
        from_attributes = True


class FactBionomicsResponse(FactBionomicsCreate):
    bionomics_id: int
    class Config:
        from_attributes = True


class FactInterventionResponse(FactInterventionCreate):
    intervention_id: int
    class Config:
        from_attributes = True


class FactSocioeconomicConditionResponse(FactSocioeconomicConditionCreate):
    socio_id: int
    class Config:
        from_attributes = True


class FactHealthFacilityResponse(FactHealthFacilityCreate):
    facility_id: int
    class Config:
        from_attributes = True


class FactDiseaseBurdenResponse(FactDiseaseBurdenCreate):
    burden_id: int
    class Config:
        from_attributes = True


class FactEnvironmentalResponse(FactEnvironmentalCreate):
    fact_env_id: int
    class Config:
        from_attributes = True