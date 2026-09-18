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
    DimInsecticide, DimLocation, DimResistanceTestType, DimSocioeconomicIndicator, 
    DimTime, DimDisease, DimVector, DimSource,
    FactDiseaseCase, FactVectorObservation, FactEnvironment,
    FactIntervention, FactInsecticideResistance, FactDiseaseBurden, 
    FactSocioeconomicCondition, FactBionomics, FactHealthFacility
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
    run_vector_climate: bool = True 
    run_vector_disease: bool = False
    run_vector_occurrence: bool = True
    run_vector_clustering: bool = True
    run_lag_analysis: bool = True
    run_intervention_impact: bool = True
    run_resistance_trends: bool = True
    run_spatial_analysis: bool = False
    run_risk_modeling: bool = False
    
    # Output settings
    output_dir: str = "outputs/analysis"
    figure_format: str = "png"
    dpi: int = 300
    save_data: bool = True
    
    # Analysis parameters
    max_lag_months: int = 6
    seasonal_period: int = 12
    risk_threshold_percentile: float = 0.75

    # Custom parameters for integrated_risk_model
    features: Optional[List[str]] = None
    target: Optional[str] = None
    
    # Data filters
    disease_code: Optional[str] = None
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
        location_query = self.session.query(DimLocation)
        
        # If country_code is provided, filter by it
        if self.config.country_code and self.config.country_code != "ALL":
            location_query = location_query.filter(
                DimLocation.country_code == self.config.country_code
            )
        
        # If location_name is provided, try to match it
        if self.config.location_name and self.config.location_name != "ALL":
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
        
        # If no specific filters, get all locations in country (or all if no country filter)
        if self.config.country_code and self.config.country_code != "ALL":
            all_locations = location_query.all()
            if all_locations:
                self.location_ids = [loc.location_id for loc in all_locations]
                logger.info(f"Using all {len(self.location_ids)} location(s) in {self.config.country_code}")
                return
        else:
            # Get all locations
            all_locations = self.session.query(DimLocation).all()
            if all_locations:
                self.location_ids = [loc.location_id for loc in all_locations]
                logger.info(f"Using all {len(self.location_ids)} location(s) in the database")
                return
        
        # If no locations found, try to get all locations as fallback
        all_locations = self.session.query(DimLocation).all()
        if all_locations:
            self.location_ids = [loc.location_id for loc in all_locations]
            logger.warning(f"No specific locations found. Using all {len(self.location_ids)} locations in the database")
            return
        
        raise ValueError(f"No locations found matching filters")
    
    def query_data(self) -> Dict[str, pd.DataFrame]:
        """
        Query all relevant data from the CDM.
        
        Returns:
            Dictionary of DataFrames for each data type
        """
        logger.info("Querying data from CDM...")
        
        data = {}
        
        # 1. Disease cases (with fallbacks)
        data['cases'] = self._query_cases()
        logger.info(f"  - Cases: {len(data['cases'])} records")
        
        # 2. Vector observations
        data['vectors'] = self._query_vectors()
        logger.info(f"  - Vectors: {len(data['vectors'])} records")
        
        # 3. Environmental data (with debugging)
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
        
        # 7. Socioeconomic data
        data['socioeconomic'] = self._query_socioeconomic()
        logger.info(f"  - Socioeconomic: {len(data['socioeconomic'])} records")
        
        # 8. Bionomics data
        data['bionomics'] = self._query_bionomics()
        logger.info(f"  - Bionomics: {len(data['bionomics'])} records")
        
        return data
    
    
    def _query_cases(self) -> pd.DataFrame:
        try:
            query = self.session.query(
                DimTime.date.label("date"),
                DimTime.year.label("year"),
                DimTime.month.label("month"),
                DimTime.week.label("week"),
                DimTime.season.label("season"),
                func.sum(
                    func.coalesce(FactDiseaseCase.case_count, 1)
                ).label("case_count"),
            ).select_from(FactDiseaseCase).join(
                DimTime, FactDiseaseCase.date_id_reported == DimTime.date_id
            )

            if self.location_ids:
                query = query.filter(FactDiseaseCase.location_id.in_(self.location_ids))

            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    DimTime.year.between(self.config.start_year, self.config.end_year)
                )

            if self.config.disease_code:
                query = query.filter(FactDiseaseCase.disease_code == self.config.disease_code)

            query = query.group_by(
                DimTime.date, DimTime.year, DimTime.month, DimTime.week, DimTime.season
            ).order_by(DimTime.date)

            df = pd.read_sql(query.statement, self.session.bind)

            if not df.empty:
                logger.info(f"  Found case data: {len(df)} days, "
                            f"{int(df['case_count'].sum())} total cases")
                logger.info(f"  Date range: {df['date'].min()} to {df['date'].max()}")
                return df

            # Fallback chain unchanged ...
            logger.info("  No case data found, trying burden data as fallback...")
            burden_df = self._query_burden_as_cases()
            if not burden_df.empty:
                return burden_df
            logger.info("  No burden data found, trying vector abundance as proxy...")
            vector_df = self._query_vector_as_cases()
            if not vector_df.empty:
                return vector_df
            logger.warning("  No case, burden, or vector data available")
            return pd.DataFrame(columns=['date','year','month','week','season','case_count'])

        except Exception as e:
            logger.error(f"Error querying cases: {e}")
            import traceback; traceback.print_exc()
            return pd.DataFrame(columns=['date','year','month','week','season','case_count'])


    def _query_burden_as_cases(self) -> pd.DataFrame:
        """Use disease burden data as a proxy for cases."""
        try:
            query = self.session.query(
                FactDiseaseBurden.year,
                FactDiseaseBurden.cases_mean,
                FactDiseaseBurden.incidence_rate,
                FactDiseaseBurden.disease_code,
                DimLocation.name.label('location_name')
            ).select_from(
                FactDiseaseBurden
            ).join(
                DimLocation, FactDiseaseBurden.location_id == DimLocation.location_id
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactDiseaseBurden.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    FactDiseaseBurden.year.between(self.config.start_year, self.config.end_year)
                )
            
            if self.config.disease_code:
                query = query.filter(FactDiseaseBurden.disease_code == self.config.disease_code)
            
            df = pd.read_sql(query.statement, self.session.bind)
            
            if df.empty:
                return pd.DataFrame()
            
            # Use cases_mean as case_count
            df['case_count'] = df['cases_mean']
            df = df.dropna(subset=['case_count'])
            
            if df.empty:
                return pd.DataFrame()
            
            # Create monthly data by interpolating annual data
            monthly_dfs = []
            
            for _, row in df.iterrows():
                year = int(row['year'])
                case_count = row['case_count']
                
                # Create 12 months of data with slight variation
                for month in range(1, 13):
                    date = datetime(year, month, 1)
                    # Add seasonal variation (peak in wet season)
                    seasonal_factor = 1.0 + 0.3 * np.sin((month - 4) * 2 * np.pi / 12)
                    monthly_dfs.append({
                        'date': date,
                        'year': year,
                        'month': month,
                        'week': date.isocalendar()[1],
                        'season': 'wet' if month in [4, 5, 6, 7, 8, 9, 10] else 'dry',
                        'case_count': case_count * seasonal_factor
                    })
            
            result_df = pd.DataFrame(monthly_dfs)
            result_df['case_count'] = result_df['case_count'].round(0)
            result_df['date'] = pd.to_datetime(result_df['date'])
            
            return result_df
            
        except Exception as e:
            logger.error(f"Error querying burden as cases: {e}")
            return pd.DataFrame()
    
    def _query_vector_as_cases(self) -> pd.DataFrame:
        """Use vector abundance data as a proxy for disease risk."""
        try:
            query = self.session.query(
                DimTime.date,
                DimTime.year,
                DimTime.month,
                DimTime.week,
                DimTime.season,
                FactVectorObservation.count,
                FactVectorObservation.density_per_trap,
                DimVector.species_name
            ).select_from(
                FactVectorObservation
            ).join(
                DimTime, FactVectorObservation.date_id == DimTime.date_id
            ).join(
                DimVector, FactVectorObservation.vector_code == DimVector.vector_code
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactVectorObservation.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    DimTime.year.between(self.config.start_year, self.config.end_year)
                )
            
            df = pd.read_sql(query.statement, self.session.bind)
            
            if df.empty:
                return pd.DataFrame()
            
            # Aggregate by date
            if 'count' in df.columns:
                df = df.groupby(['date', 'year', 'month', 'week', 'season']).agg({
                    'count': 'sum',
                    'density_per_trap': 'mean'
                }).reset_index()
                
                # Use count as the response variable
                df = df.rename(columns={'count': 'case_count'})
                df['case_count'] = df['case_count'].fillna(0)
                return df[['date', 'year', 'month', 'week', 'season', 'case_count']]
            else:
                return pd.DataFrame()
            
        except Exception as e:
            logger.error(f"Error querying vector as cases: {e}")
            return pd.DataFrame()
    
    def _query_vectors(self) -> pd.DataFrame:
        """Query vector observation data."""
        try:
            query = self.session.query(
                DimTime.date,
                DimTime.year,
                DimTime.month,
                DimVector.species_name,
                DimVector.genus,
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
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactVectorObservation.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    DimTime.year.between(self.config.start_year, self.config.end_year)
                )
            
            if self.config.vector_codes:
                query = query.filter(DimVector.species_name.in_(self.config.vector_codes))
            
            df = pd.read_sql(query.statement, self.session.bind)
            
            # Aggregate by date if needed
            if not df.empty and 'count' in df.columns:
                # Group by date and species
                agg_cols = ['date', 'year', 'month', 'species_name']
                agg_dict = {
                    'count': 'sum',
                    'density_per_trap': 'mean',
                    'infection_rate': 'mean'
                }
                # Only include columns that exist
                existing_agg = {k: v for k, v in agg_dict.items() if k in df.columns}
                if existing_agg:
                    df = df.groupby(agg_cols).agg(existing_agg).reset_index()
            
            return df
            
        except Exception as e:
            logger.error(f"Error querying vectors: {e}")
            return pd.DataFrame()
    
    def _query_environment(self) -> pd.DataFrame:
        """Query environmental data with debugging."""
        try:
            # ===== DEBUGGING: Check what's in the database =====
            logger.info("  DEBUG: Checking environment table...")
            
            # Check total records in fact_environment
            total_env = self.session.query(FactEnvironment).count()
            logger.info(f"  DEBUG: Total records in fact_environment: {total_env}")
            
            if total_env > 0:
                # Check what location_ids exist in fact_environment
                env_loc_ids_result = self.session.query(FactEnvironment.location_id).distinct().all()
                env_loc_ids = [r[0] for r in env_loc_ids_result if r[0] is not None]
                logger.info(f"  DEBUG: location_ids in fact_environment: {env_loc_ids[:10]}... (total {len(env_loc_ids)})")
                
                # Check what variables exist
                env_vars_result = self.session.query(FactEnvironment.variable).distinct().all()
                env_vars = [r[0] for r in env_vars_result]
                logger.info(f"  DEBUG: variables in fact_environment: {env_vars}")
                
                # Check if location_ids match
                if self.location_ids:
                    matching = set(env_loc_ids) & set(self.location_ids)
                    logger.info(f"  DEBUG: Overlapping location_ids: {list(matching)[:10]}... (total {len(matching)})")
                    
                    # If no matching locations, show what's missing
                    if not matching:
                        logger.warning("  WARNING: No matching location_ids found!")
                        logger.warning(f"  WARNING: Environment location_ids (first 10): {env_loc_ids[:10]}")
                        logger.warning(f"  WARNING: Query location_ids (first 10): {self.location_ids[:10]}")
                        
                        # Get details about environment records
                        sample_env = self.session.query(
                            FactEnvironment.location_id,
                            FactEnvironment.variable,
                            FactEnvironment.date_id,
                            FactEnvironment.value
                        ).limit(5).all()
                        
                        for loc_id, var, date_id, val in sample_env:
                            logger.info(f"  DEBUG: Sample env: location_id={loc_id}, variable={var}, date={date_id}, value={val}")
                        
                        # Check if there are records with NULL location_id
                        null_count = self.session.query(FactEnvironment).filter(
                            FactEnvironment.location_id.is_(None)
                        ).count()
                        if null_count > 0:
                            logger.warning(f"  WARNING: {null_count} environment records have NULL location_id!")
            # ===== END DEBUGGING =====
            
            # Build the query
            query = self.session.query(
                DimTime.date,
                DimTime.year,
                DimTime.month,
                FactEnvironment.variable,
                FactEnvironment.value
            ).select_from(
                FactEnvironment
            ).join(
                DimTime, FactEnvironment.date_id == DimTime.date_id
            ).filter(
                FactEnvironment.variable.in_(self.config.environmental_variables)
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactEnvironment.location_id.in_(self.location_ids))
                logger.info(f"  Filtering environment by location_ids: {self.location_ids[:5]}... (total {len(self.location_ids)})")
            else:
                logger.warning("  No location_ids available for filtering")
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    DimTime.year.between(self.config.start_year, self.config.end_year)
                )
            
            df = pd.read_sql(query.statement, self.session.bind)
            logger.info(f"  DEBUG: Query returned {len(df)} rows")
            
            # Pivot to wide format
            if not df.empty:
                # Ensure we have unique combinations for pivoting
                df = df.drop_duplicates(subset=['date', 'variable'])
                
                # Pivot to wide format
                try:
                    df = df.pivot_table(
                        index=['date', 'year', 'month'],
                        columns='variable',
                        values='value',
                        aggfunc='mean'
                    ).reset_index()
                    logger.info(f"  Pivoted environment data: {len(df)} rows, {len(df.columns)} columns")
                except Exception as e:
                    logger.warning(f"Could not pivot environmental data: {e}")
                    # Return as is if pivoting fails
            
            return df
            
        except Exception as e:
            logger.error(f"Error querying environment: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()
    
    def _query_interventions(self) -> pd.DataFrame:
        """Query intervention data with proper date_id joins."""
        try:
            query = self.session.query(
                FactIntervention.intervention_id,
                FactIntervention.intervention_type,
                FactIntervention.coverage,
                FactIntervention.date_start,
                FactIntervention.date_end,
                DimTime.date.label('start_date'),
                DimTime.year.label('start_year'),
                DimTime.month.label('start_month'),
                DimDisease.disease_name,
                DimDisease.disease_code
            ).select_from(
                FactIntervention
            ).join(
                DimTime, FactIntervention.date_start == DimTime.date_id
            ).join(
                DimDisease, FactIntervention.target_disease_code == DimDisease.disease_code
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactIntervention.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    DimTime.year.between(self.config.start_year, self.config.end_year)
                )
            
            if self.config.disease_code:
                query = query.filter(FactIntervention.target_disease_code == self.config.disease_code)
            
            df = pd.read_sql(query.statement, self.session.bind)
            
            # Ensure we have a consistent 'date' column
            if not df.empty:
                # Use start_date as the intervention date
                if 'start_date' in df.columns:
                    df['date'] = pd.to_datetime(df['start_date'])
                elif 'date_start' in df.columns:
                    df['date'] = pd.to_datetime(df['date_start'])
            
            return df
            
        except Exception as e:
            logger.error(f"Error querying interventions: {e}")
            return pd.DataFrame()
    
    def _query_resistance(self) -> pd.DataFrame:
        """Query insecticide resistance data."""
        try:
            query = self.session.query(
                DimLocation.name.label('location_name'),
                DimLocation.country_code,
                DimTime.date,
                DimTime.year,
                DimTime.month,
                DimVector.species_name,
                DimInsecticide.insecticide_name,
                DimInsecticide.chemical_class,
                DimResistanceTestType.test_type_name,
                FactInsecticideResistance.resistance_id,
                FactInsecticideResistance.location_id,
                FactInsecticideResistance.insecticide_code,
                FactInsecticideResistance.test_type_code,
                FactInsecticideResistance.concentration,
                FactInsecticideResistance.exposure_time,
                FactInsecticideResistance.mortality,
                FactInsecticideResistance.resistance_status,
                FactInsecticideResistance.n_insect_tested,
                FactInsecticideResistance.n_insect_resistant,
                FactInsecticideResistance.n_insect_susceptible,
                FactInsecticideResistance.n_insect_dead
            ).select_from(
                FactInsecticideResistance
            ).join(
                DimLocation, FactInsecticideResistance.location_id == DimLocation.location_id
            ).join(
                DimTime, FactInsecticideResistance.date_id == DimTime.date_id
            ).join(
                DimVector, FactInsecticideResistance.vector_code == DimVector.vector_code
            ).join(
                DimInsecticide, FactInsecticideResistance.insecticide_code == DimInsecticide.insecticide_code
            ).join(
                DimResistanceTestType, FactInsecticideResistance.test_type_code == DimResistanceTestType.test_type_code
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactInsecticideResistance.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    DimTime.year.between(self.config.start_year, self.config.end_year)
                )
            
            df = pd.read_sql(query.statement, self.session.bind)
            
            # Log resistance status distribution for debugging
            if not df.empty and 'resistance_status' in df.columns:
                status_counts = df['resistance_status'].value_counts()
                if not status_counts.empty:
                    logger.info(f"  Resistance status distribution: {dict(status_counts)}")
                else:
                    logger.info("  Resistance status distribution: (no statuses found)")
            
            return df
            
        except Exception as e:
            logger.error(f"Error querying resistance: {e}")
            return pd.DataFrame()
    
    def _query_burden(self) -> pd.DataFrame:
        """Query disease burden data."""
        try:
            query = self.session.query(
                FactDiseaseBurden.year,
                FactDiseaseBurden.incidence_rate,
                FactDiseaseBurden.prevalence_rate,
                FactDiseaseBurden.mortality_rate,
                FactDiseaseBurden.cases_mean,
                DimDisease.disease_name,
                DimDisease.disease_code,
                DimLocation.name.label('location_name')
            ).select_from(
                FactDiseaseBurden
            ).join(
                DimDisease, FactDiseaseBurden.disease_code == DimDisease.disease_code
            ).join(
                DimLocation, FactDiseaseBurden.location_id == DimLocation.location_id
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactDiseaseBurden.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    FactDiseaseBurden.year.between(self.config.start_year, self.config.end_year)
                )
            
            if self.config.disease_code:
                query = query.filter(FactDiseaseBurden.disease_code == self.config.disease_code)
            
            return pd.read_sql(query.statement, self.session.bind)
            
        except Exception as e:
            logger.error(f"Error querying burden: {e}")
            return pd.DataFrame()
    
    def _query_socioeconomic(self) -> pd.DataFrame:
        """Query socioeconomic data."""
        try:
            query = self.session.query(
                FactSocioeconomicCondition.year,
                FactSocioeconomicCondition.indicator_value,
                DimSocioeconomicIndicator.indicator_name,
                DimSocioeconomicIndicator.indicator_code,
                DimSocioeconomicIndicator.unit,
                DimLocation.name.label('location_name')
            ).select_from(
                FactSocioeconomicCondition
            ).join(
                DimSocioeconomicIndicator, 
                FactSocioeconomicCondition.indicator_code == DimSocioeconomicIndicator.indicator_code
            ).join(
                DimLocation, FactSocioeconomicCondition.location_id == DimLocation.location_id
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactSocioeconomicCondition.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    FactSocioeconomicCondition.year.between(self.config.start_year, self.config.end_year)
                )
            
            return pd.read_sql(query.statement, self.session.bind)
            
        except Exception as e:
            logger.error(f"Error querying socioeconomic: {e}")
            return pd.DataFrame()
    
    def _query_bionomics(self) -> pd.DataFrame:
        """Query bionomics data."""
        try:
            query = self.session.query(
                DimLocation.name.label('location_name'),
                DimTime.date,
                DimTime.year,
                DimTime.month,
                DimVector.species_name,
                FactBionomics.biting_rate,
                FactBionomics.feeding_preference,
                FactBionomics.biting_location,
                FactBionomics.resting_behavior,
                FactBionomics.larval_site_type,
                DimSource.source_name
            ).select_from(
                FactBionomics
            ).join(
                DimLocation, FactBionomics.location_id == DimLocation.location_id
            ).join(
                DimTime, FactBionomics.date_id == DimTime.date_id
            ).join(
                DimVector, FactBionomics.vector_code == DimVector.vector_code
            ).join(
                DimSource, FactBionomics.source_code == DimSource.source_code
            )
            
            # Apply location filter
            if self.location_ids:
                query = query.filter(FactBionomics.location_id.in_(self.location_ids))
            
            if self.config.start_year and self.config.end_year:
                query = query.filter(
                    DimTime.year.between(self.config.start_year, self.config.end_year)
                )
            
            return pd.read_sql(query.statement, self.session.bind)
            
        except Exception as e:
            logger.error(f"Error querying bionomics: {e}")
            return pd.DataFrame()
    
    def get_data_summary(self) -> Dict[str, Any]:
        """
        Get a summary of the available data.
        
        Returns:
            Dictionary with data summary information
        """
        summary = {
            'location_ids': self.location_ids,
            'num_locations': len(self.location_ids),
            'start_year': self.config.start_year,
            'end_year': self.config.end_year,
            'country_code': self.config.country_code,
            'location_name': self.config.location_name,
            'disease_code': self.config.disease_code,
        }
        
        # Get counts from each table
        try:
            # Count cases
            case_count = self.session.query(FactDiseaseCase).filter(
                FactDiseaseCase.location_id.in_(self.location_ids)
            ).count()
            summary['case_count'] = case_count
            
            # Count vector observations
            vector_count = self.session.query(FactVectorObservation).filter(
                FactVectorObservation.location_id.in_(self.location_ids)
            ).count()
            summary['vector_count'] = vector_count
            
            # Count environment records
            env_count = self.session.query(FactEnvironment).filter(
                FactEnvironment.location_id.in_(self.location_ids)
            ).count()
            summary['environment_count'] = env_count
            
            # Count resistance records
            resistance_count = self.session.query(FactInsecticideResistance).filter(
                FactInsecticideResistance.location_id.in_(self.location_ids)
            ).count()
            summary['resistance_count'] = resistance_count
            
            # Count interventions
            intervention_count = self.session.query(FactIntervention).filter(
                FactIntervention.location_id.in_(self.location_ids)
            ).count()
            summary['intervention_count'] = intervention_count
            
            # Count burden records
            burden_count = self.session.query(FactDiseaseBurden).filter(
                FactDiseaseBurden.location_id.in_(self.location_ids)
            ).count()
            summary['burden_count'] = burden_count
            
        except Exception as e:
            logger.warning(f"Could not get data counts: {e}")
        
        return summary
    
    def close(self):
        """Close the database session."""
        if hasattr(self, 'session') and self.session:
            self.session.close()