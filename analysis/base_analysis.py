# analysis/base_analysis.py
"""
Standardized Analysis Pipeline for CDM-based Vector-Borne Disease Studies

This module provides a generic framework for analyzing integrated data from the
CDM database. It supports multiple study sites, analysis types, and output formats.
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from sqlalchemy import text
from sqlalchemy.orm import Session
from pathlib import Path
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Union, Any, Tuple
from dataclasses import dataclass, field
import warnings
warnings.filterwarnings('ignore')

from database.session import SessionLocal
from database.models import (
    DimInsecticide, DimLocation, DimResistanceTestType, DimTime, DimDisease, DimVector,
    FactDiseaseCase, FactVectorObservation, FactEnvironment,
    FactIntervention, FactInsecticideResistance, FactDiseaseBurden
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class AnalysisConfig:
    """Configuration for a standardized analysis pipeline."""
    
    # Study parameters
    study_name: str
    location_name: str
    country_code: str
    start_year: int
    end_year: int
    
    # Analysis types to run
    run_seasonal: bool = True
    run_climate_correlation: bool = True
    run_lag_analysis: bool = True
    run_intervention_impact: bool = True
    run_resistance_trends: bool = True
    run_spatial_analysis: bool = False
    run_risk_modeling: bool = False
    
    # Output settings
    output_dir: str = "analysis/outputs"
    figure_format: str = "png"
    dpi: int = 300
    save_data: bool = True
    
    # Analysis parameters
    max_lag_months: int = 6
    seasonal_period: int = 12
    risk_threshold_percentile: float = 0.75
    
    # Data filters
    disease_code: Optional[str] = "malaria"
    vector_codes: Optional[List[str]] = None
    environmental_variables: List[str] = field(
        default_factory=lambda: ["t2m", "tp", "d2m"]
    )
    
    def __post_init__(self):
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Create subdirectories
        (self.output_dir / "figures").mkdir(exist_ok=True)
        (self.output_dir / "data").mkdir(exist_ok=True)
        (self.output_dir / "reports").mkdir(exist_ok=True)


class BaseAnalysis:
    """
    Base class for CDM-based analyses.
    
    This class provides core functionality for querying, analyzing, and
    visualizing data from the CDM database. It is designed to be extended
    for specific study sites or research questions.
    """
    
    def __init__(self, config: AnalysisConfig):
        """
        Initialize the analysis with a configuration.
        
        Args:
            config: AnalysisConfig object with study parameters
        """
        self.config = config
        self.session = SessionLocal()
        self.results = {}
        self.location_ids = []
        
        logger.info(f"Initializing analysis for {config.study_name}")
        self._initialize_location()
        
    def _initialize_location(self):
        """Identify location IDs for the study site."""
        location_query = self.session.query(DimLocation).filter(
            DimLocation.country_code == self.config.country_code
        )
        
        # Try exact name match first
        exact_match = location_query.filter(
            DimLocation.name == self.config.location_name
        ).all()
        
        if exact_match:
            self.location_ids = [loc.location_id for loc in exact_match]
            logger.info(f"Found {len(self.location_ids)} location(s) matching '{self.config.location_name}'")
            return
        
        # Try partial match
        partial_match = location_query.filter(
            DimLocation.name.like(f'%{self.config.location_name}%')
        ).all()
        
        if partial_match:
            self.location_ids = [loc.location_id for loc in partial_match]
            logger.info(f"Found {len(self.location_ids)} location(s) containing '{self.config.location_name}'")
            return
        
        # Fallback: get all locations in country
        all_locations = location_query.all()
        if all_locations:
            self.location_ids = [loc.location_id for loc in all_locations]
            logger.warning(
                f"No match for '{self.config.location_name}'. "
                f"Using {len(self.location_ids)} location(s) in {self.config.country_code}"
            )
        else:
            raise ValueError(f"No locations found for {self.config.country_code}")
    
    def query_data(self) -> Dict[str, pd.DataFrame]:
        """
        Query all relevant data from the CDM.
        
        Returns:
            Dictionary of DataFrames for each data type
        """
        logger.info("Querying data from CDM...")
        
        location_filter = FactDiseaseCase.location_id.in_(self.location_ids)
        
        data = {}
        
        # 1. Disease cases
        data['cases'] = self._query_cases()
        logger.info(f"  - Cases: {len(data['cases'])} records")
        
        # 2. Vector observations
        data['vectors'] = self._query_vectors()
        logger.info(f"  - Vectors: {len(data['vectors'])} records")
        
        # 3. Environmental data
        data['environment'] = self._query_environment()
        logger.info(f"  - Environment: {len(data['environment'])} records")
        
        # 4. Interventions
        data['interventions'] = self._query_interventions()
        logger.info(f"  - Interventions: {len(data['interventions'])} records")
        
        # 5. Resistance data
        data['resistance'] = self._query_resistance()
        logger.info(f"  - Resistance: {len(data['resistance'])} records")
        
        # 6. Disease burden
        data['burden'] = self._query_burden()
        logger.info(f"  - Burden: {len(data['burden'])} records")
        
        return data
    
    def _query_cases(self) -> pd.DataFrame:
        """Query disease case data."""
        query = self.session.query(
            DimTime.date,
            DimTime.year,
            DimTime.month,
            DimTime.week,
            DimTime.season,
            FactDiseaseCase.disease_code,
            FactDiseaseCase.case_status,
            FactDiseaseCase.host_type,
            FactDiseaseCase.age_group,
            FactDiseaseCase.sex,
            FactDiseaseCase.outcome,
            FactDiseaseCase.case_id
        ).select_from(
            FactDiseaseCase
        ).join(
            DimTime, FactDiseaseCase.date_id_reported == DimTime.date_id
        ).filter(
            FactDiseaseCase.location_id.in_(self.location_ids),
            DimTime.year.between(self.config.start_year, self.config.end_year)
        )
        
        if self.config.disease_code:
            query = query.filter(FactDiseaseCase.disease_code == self.config.disease_code)
        
        df = pd.read_sql(query.statement, self.session.bind)
        
        # Aggregate counts
        if not df.empty:
            df = df.groupby([
                'date', 'year', 'month', 'week', 'season'
            ]).size().reset_index(name='case_count')
        
        return df
    
    def _query_vectors(self) -> pd.DataFrame:
        """Query vector observation data."""
        query = self.session.query(
            DimTime.date,
            DimTime.year,
            DimTime.month,
            DimVector.species_name,
            FactVectorObservation.life_stage,
            FactVectorObservation.sex,
            FactVectorObservation.count,
            FactVectorObservation.density_per_trap,
            FactVectorObservation.infection_status,
            FactVectorObservation.infection_rate
        ).select_from(
            FactVectorObservation
        ).join(
            DimTime, FactVectorObservation.date_id == DimTime.date_id
        ).join(
            DimVector, FactVectorObservation.vector_code == DimVector.vector_code
        ).filter(
            FactVectorObservation.location_id.in_(self.location_ids),
            DimTime.year.between(self.config.start_year, self.config.end_year)
        )
        
        if self.config.vector_codes:
            query = query.filter(DimVector.species_name.in_(self.config.vector_codes))
        
        return pd.read_sql(query.statement, self.session.bind)
    
    def _query_environment(self) -> pd.DataFrame:
        """Query environmental data."""
        query = self.session.query(
            DimTime.date,
            DimTime.year,
            DimTime.month,
            FactEnvironment.variable,
            FactEnvironment.value
        ).join(
            FactEnvironment, DimTime.date_id == FactEnvironment.date_id
        ).filter(
            FactEnvironment.location_id.in_(self.location_ids),
            DimTime.year.between(self.config.start_year, self.config.end_year),
            FactEnvironment.variable.in_(self.config.environmental_variables)
        )
        
        df = pd.read_sql(query.statement, self.session.bind)
        
        # Pivot to wide format
        if not df.empty:
            df = df.pivot_table(
                index=['date', 'year', 'month'],
                columns='variable',
                values='value',
                aggfunc='mean'
            ).reset_index()
        
        return df
    
    def _query_interventions(self) -> pd.DataFrame:
        """Query intervention data."""
        query = self.session.query(
            FactIntervention.intervention_type,
            FactIntervention.coverage,
            FactIntervention.date_start,
            FactIntervention.date_end,
            DimDisease.disease_name
        ).join(
            DimDisease, FactIntervention.target_disease_code == DimDisease.disease_code
        ).filter(
            FactIntervention.location_id.in_(self.location_ids)
        )
        
        return pd.read_sql(query.statement, self.session.bind)
    
    def _query_resistance(self) -> pd.DataFrame:
        """Query insecticide resistance data."""
        query = self.session.query(
            DimTime.date,
            DimTime.year,
            DimTime.month,
            DimVector.species_name,
            DimInsecticide.insecticide_name,
            DimInsecticide.chemical_class,
            DimResistanceTestType.test_type_name,
            FactInsecticideResistance.resistance_id,
            FactInsecticideResistance.vector_observation_id,
            FactInsecticideResistance.insecticide_code,
            FactInsecticideResistance.test_type_code,
            FactInsecticideResistance.concentration,
            FactInsecticideResistance.exposure_time,
            FactInsecticideResistance.mortality,
            FactInsecticideResistance.resistance_status
        ).select_from(
            FactInsecticideResistance
        ).join(
            FactVectorObservation, 
            FactInsecticideResistance.vector_observation_id == FactVectorObservation.observation_id
        ).join(
            DimTime, FactVectorObservation.date_id == DimTime.date_id
        ).join(
            DimVector, FactVectorObservation.vector_code == DimVector.vector_code
        ).join(
            DimInsecticide, 
            FactInsecticideResistance.insecticide_code == DimInsecticide.insecticide_code
        ).join(
            DimResistanceTestType,
            FactInsecticideResistance.test_type_code == DimResistanceTestType.test_type_code
        ).filter(
            FactVectorObservation.location_id.in_(self.location_ids),
            DimTime.year.between(self.config.start_year, self.config.end_year)
        )
        
        return pd.read_sql(query.statement, self.session.bind)
    
    def _query_burden(self) -> pd.DataFrame:
        """Query disease burden data."""
        query = self.session.query(
            FactDiseaseBurden.year,
            FactDiseaseBurden.incidence_rate,
            FactDiseaseBurden.prevalence_rate,
            FactDiseaseBurden.mortality_rate,
            FactDiseaseBurden.cases_mean,
            DimDisease.disease_name
        ).join(
            DimDisease, FactDiseaseBurden.disease_code == DimDisease.disease_code
        ).filter(
            FactDiseaseBurden.location_id.in_(self.location_ids),
            FactDiseaseBurden.year.between(self.config.start_year, self.config.end_year)
        )
        
        if self.config.disease_code:
            query = query.filter(FactDiseaseBurden.disease_code == self.config.disease_code)
        
        return pd.read_sql(query.statement, self.session.bind)
    
    def close(self):
        """Close the database session."""
        self.session.close()