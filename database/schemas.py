#-*- coding:utf-8 -*-

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import date


class LocationCreate(BaseModel):
    """Schema for creating/updating locations"""
    location_code: str
    name: str
    admin_level: str
    parent_location_code: Optional[str] = None
    geometry_wkt: Optional[str] = None
    centroid_lat: Optional[float] = None
    centroid_lon: Optional[float] = None


class HostCaseCreate(BaseModel):
    """Schema for creating/updating cases"""
    case_identifier: str
    location_code: str
    date_id_onset: str  # YYYY-MM-DD format
    date_id_reported: str
    disease: str
    pathogen: Optional[str] = None
    case_status: str = "suspected"
    host_type: str = "human"
    age_years: Optional[int] = None
    outcome: Optional[str] = "unknown"
    
    @field_validator('date_id_onset', 'date_id_reported')
    def validate_date_format(cls, v):
        # Basic validation for date format
        try:
            year, month, day = map(int, v.split('-'))
            date(year, month, day)
        except:
            raise ValueError(f"Invalid date format: {v}. Use YYYY-MM-DD")
        return v