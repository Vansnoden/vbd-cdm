#-*- coding: utf-8 -*-

from sqlalchemy.orm import Session
from database import models, schemas
from typing import List, Dict
import pandas as pd


class DatabaseLoader:
    def __init__(self, db: Session):
        self.db = db
    
    def bulk_load_cases(self, cases_data: List[Dict]):
        """Load multiple cases efficiently"""
        for case_dict in cases_data:
            # Validate with Pydantic
            case = schemas.HostCaseCreate(**case_dict)
            
            # Convert to ORM model
            db_case = models.HostCase(**case.dict())
            
            self.db.add(db_case)
        
        self.db.commit()
    

    def upsert_location(self, location_data: Dict):
        """Insert or update location"""
        location = self.db.query(models.Location)\
            .filter(models.Location.location_code == location_data['location_code'])\
            .first()
        
        if location:
            # Update existing
            for key, value in location_data.items():
                setattr(location, key, value)
        else:
            # Create new
            location = models.Location(**location_data)
            self.db.add(location)
        
        self.db.commit()
        return location
    
    
    def load_from_dataframe(self, df: pd.DataFrame, model_class):
        """Generic method to load pandas DataFrame to any model"""
        records = df.to_dict('records')
        for record in records:
            # Handle missing columns
            filtered_record = {k: v for k, v in record.items() 
                             if hasattr(model_class, k)}
            db_obj = model_class(**filtered_record)
            self.db.add(db_obj)
        
        self.db.commit()