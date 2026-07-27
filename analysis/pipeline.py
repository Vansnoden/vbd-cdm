# analysis/pipeline.py
"""
Standardized Analysis Pipeline Orchestrator

This module orchestrates the complete analysis workflow for a CDM-based study.
It integrates data querying, analysis modules, and visualization generation.
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
import json
from datetime import datetime
import logging
from typing import Dict, Optional, Union, List, Any
import warnings
warnings.filterwarnings('ignore')

from analysis.base_analysis import BaseAnalysis, AnalysisConfig
from analysis.analysis_modules import AnalysisModules
from analysis.visualization import VisualizationGenerator

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    """
    Orchestrates the complete analysis workflow.
    
    This class coordinates data extraction, analysis, and visualization
    for a standardized CDM-based study.
    """
    
    def __init__(self, config: Union[AnalysisConfig, Dict]):
        """
        Initialize the pipeline with a configuration.
        
        Args:
            config: AnalysisConfig object or dictionary
        """
        if isinstance(config, dict):
            self.config = AnalysisConfig(**config)
        else:
            self.config = config
        
        self.base = BaseAnalysis(self.config)
        self.modules = AnalysisModules()
        self.viz = VisualizationGenerator(self.config.output_dir)
        
        self.data = None
        self.results = {}
        self._setup_outputs()
    
    def _setup_outputs(self):
        """Create output directories and setup logging."""
        self.output_dir = self.config.output_dir
        (self.output_dir / "figures").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "data").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "reports").mkdir(parents=True, exist_ok=True)
    
    def run(self) -> Dict:
        """
        Execute the complete analysis pipeline.
        """
        logger.info(f"Starting analysis for {self.config.study_name}")
        
        try:
            # 1. Query data
            self.data = self.base.query_data()
            
            # Check if we have any data
            total_records = sum(len(df) for df in self.data.values())
            if total_records == 0:
                logger.warning("No data found in database!")
                return {'error': 'No data available for analysis'}
            
            # Log data availability
            data_summary = {k: len(v) for k, v in self.data.items()}
            logger.info(f"Data summary: {data_summary}")
            
            # 2. Run analyses based on configuration and data availability
            self._run_analyses()
            
            # 3. Generate visualizations
            logger.info("Generating visualizations...")
            self._generate_figures()
            
            # 4. Save results
            self._save_results()
            
            # 5. Generate report
            self._generate_report()
            
            logger.info("Analysis complete!")
            return self.results
            
        except Exception as e:
            logger.error(f"Pipeline execution failed: {e}", exc_info=True)
            return {'error': str(e)}
    
    def _run_analyses(self):
        """Run all configured analyses."""
        
        # Seasonal pattern analysis
        if self.config.run_seasonal:
            logger.info("Running seasonal pattern analysis...")
            if self.data.get('cases', pd.DataFrame()).empty:
                logger.warning("No case data available for seasonal analysis")
                self.results['seasonal'] = {'error': 'No case data available'}
            else:
                try:
                    self.results['seasonal'] = self._analyze_seasonal()
                except Exception as e:
                    logger.error(f"Seasonal analysis failed: {e}")
                    self.results['seasonal'] = {'error': str(e)}
        
        # Climate correlation analysis
        if self.config.run_climate_correlation:
            logger.info("Running climate correlation analysis...")
            if self.data.get('environment', pd.DataFrame()).empty:
                logger.warning("No environmental data available for climate correlation")
                self.results['climate_correlation'] = {'error': 'No environmental data available'}
            else:
                try:
                    self.results['climate_correlation'] = self._analyze_climate_correlation()
                except Exception as e:
                    logger.error(f"Climate correlation analysis failed: {e}")
                    self.results['climate_correlation'] = {'error': str(e)}

        # Vector-Climate Analysis
        if self.config.run_vector_climate:
            logger.info("Running vector-climate analysis...")
            if self.data.get('vectors', pd.DataFrame()).empty:
                logger.warning("No vector data available for vector-climate analysis")
                self.results['vector_climate'] = {'error': 'No vector data available'}
            else:
                try:
                    self.results['vector_climate'] = self._analyze_vector_climate()
                except Exception as e:
                    logger.error(f"Vector-climate analysis failed: {e}")
                    self.results['vector_climate'] = {'error': str(e)}
        
        # Vector-Disease Analysis
        if self.config.run_vector_disease:
            logger.info("Running vector-disease analysis...")
            if self.data.get('vectors', pd.DataFrame()).empty or self.data.get('cases', pd.DataFrame()).empty:
                logger.warning("No vector or case data available for vector-disease analysis")
                self.results['vector_disease'] = {'error': 'No vector or case data available'}
            else:
                try:
                    self.results['vector_disease'] = self._analyze_vector_disease()
                except Exception as e:
                    logger.error(f"Vector-disease analysis failed: {e}")
                    self.results['vector_disease'] = {'error': str(e)}
        
        # Vector Occurrence Analysis
        if self.config.run_vector_occurrence:
            logger.info("Running vector occurrence analysis...")
            if self.data.get('vectors', pd.DataFrame()).empty:
                logger.warning("No vector data available for occurrence analysis")
                self.results['vector_occurrence'] = {'error': 'No vector data available'}
            else:
                try:
                    self.results['vector_occurrence'] = self._analyze_vector_occurrence()
                except Exception as e:
                    logger.error(f"Vector occurrence analysis failed: {e}")
                    self.results['vector_occurrence'] = {'error': str(e)}
        
        # Vector Clustering
        if self.config.run_vector_clustering:
            logger.info("Running vector clustering analysis...")
            if self.data.get('vectors', pd.DataFrame()).empty:
                logger.warning("No vector data available for clustering")
                self.results['vector_clustering'] = {'error': 'No vector data available'}
            else:
                try:
                    self.results['vector_clustering'] = self._analyze_vector_clustering()
                except Exception as e:
                    logger.error(f"Vector clustering analysis failed: {e}")
                    self.results['vector_clustering'] = {'error': str(e)}
        
        # Lag analysis
        if self.config.run_lag_analysis:
            logger.info("Running lag analysis...")
            try:
                self.results['lag_analysis'] = self._analyze_lags()
            except Exception as e:
                logger.error(f"Lag analysis failed: {e}")
                self.results['lag_analysis'] = {'error': str(e)}
        
        # Intervention impact analysis
        if self.config.run_intervention_impact:
            logger.info("Running intervention impact analysis...")
            if self.data.get('interventions', pd.DataFrame()).empty:
                logger.warning("No intervention data available")
                self.results['intervention_impact'] = {'error': 'No intervention data available'}
            else:
                try:
                    self.results['intervention_impact'] = self._analyze_interventions()
                except Exception as e:
                    logger.error(f"Intervention impact analysis failed: {e}")
                    self.results['intervention_impact'] = {'error': str(e)}
        
        # Resistance trend analysis
        if self.config.run_resistance_trends:
            logger.info("Running resistance trend analysis...")
            if self.data.get('resistance', pd.DataFrame()).empty:
                logger.warning("No resistance data available for trend analysis")
                self.results['resistance_trends'] = {'error': 'No resistance data available'}
            else:
                try:
                    self.results['resistance_trends'] = self._analyze_resistance()
                except Exception as e:
                    logger.error(f"Resistance trend analysis failed: {e}")
                    self.results['resistance_trends'] = {'error': str(e)}
        
        # Spatial analysis
        if self.config.run_spatial_analysis:
            logger.info("Running spatial analysis...")
            try:
                self.results['spatial_analysis'] = self._analyze_spatial()
            except Exception as e:
                logger.error(f"Spatial analysis failed: {e}")
                self.results['spatial_analysis'] = {'error': str(e)}
        
        # Risk modeling
        if self.config.run_risk_modeling:
            logger.info("Building integrated risk model...")
            try:
                self.results['risk_model'] = self._analyze_risk()
            except Exception as e:
                logger.error(f"Risk modeling failed: {e}")
                self.results['risk_model'] = {'error': str(e)}
    
    def _analyze_seasonal(self) -> Dict:
        """Run seasonal pattern analysis."""
        cases = self.data.get('cases', pd.DataFrame())
        if cases.empty:
            return {'error': 'No case data available'}
        
        return self.modules.seasonal_patterns(
            cases,
            date_col='date',
            value_col='case_count'
        )
    

    def _analyze_climate_correlation(self) -> Dict:
        """
        Run climate correlation analysis with vector data integration.
        """
        env = self.data.get('environment', pd.DataFrame())
        cases = self.data.get('cases', pd.DataFrame())
        vectors = self.data.get('vectors', pd.DataFrame())
        
        if env.empty:
            return {'error': 'No environmental data available'}
        
        if cases.empty:
            return {'error': 'No case data available for correlation'}
        
        # Make copies
        env = env.copy()
        cases = cases.copy()
        
        # Ensure date is datetime
        env['date'] = pd.to_datetime(env['date'])
        cases['date'] = pd.to_datetime(cases['date'])
        
        # Log date ranges for debugging
        logger.info(f"  Cases date range: {cases['date'].min()} to {cases['date'].max()}")
        logger.info(f"  Environment date range: {env['date'].min()} to {env['date'].max()}")
        
        # Drop duplicate columns from env (keep only date as common column)
        env_cols_to_drop = ['year', 'month']
        for col in env_cols_to_drop:
            if col in env.columns:
                env = env.drop(columns=[col])
        
        # Merge on date
        merged = cases.merge(env, on='date', how='inner')
        
        if merged.empty:
            logger.warning("  No overlapping dates found between cases and environment")
            
            # Try to find closest date match by resampling
            logger.info("  Attempting to resample cases to daily frequency...")
            try:
                # Create daily time series from cases
                cases_daily = cases.set_index('date').resample('D').ffill().reset_index()
                cases_daily = cases_daily.rename(columns={'index': 'date'})
                merged = cases_daily.merge(env, on='date', how='inner')
                
                if not merged.empty:
                    logger.info(f"  Found {len(merged)} overlapping records after resampling")
                else:
                    return {'error': 'No overlapping data for correlation'}
            except Exception as e:
                logger.warning(f"  Resampling failed: {e}")
                return {'error': 'No overlapping data for correlation'}
        
        logger.info(f"  Merged data: {len(merged)} overlapping records")
        
        # Calculate correlations
        env_cols = [col for col in merged.columns 
                if col in self.config.environmental_variables]
        
        correlations = {}
        for col in env_cols:
            if col in merged.columns and merged[col].notna().any():
                try:
                    corr = merged['case_count'].corr(merged[col])
                    if not np.isnan(corr):
                        correlations[col] = corr
                    else:
                        correlations[col] = None
                except Exception as e:
                    logger.warning(f"Could not calculate correlation for {col}: {e}")
                    correlations[col] = None
        
        # NEW: Also calculate vector-climate correlations if vector data exists
        vector_climate_results = {}
        vector_disease_results = {}
        
        if not vectors.empty:
            logger.info("  Including vector data in correlation analysis...")
            
            # Vector-Climate
            try:
                vector_climate = self._analyze_vector_climate()
                if vector_climate and 'error' not in vector_climate:
                    vector_climate_results = vector_climate
            except Exception as e:
                logger.warning(f"Vector-climate analysis failed: {e}")
            
            # Vector-Disease
            try:
                vector_disease = self._analyze_vector_disease()
                if vector_disease and 'error' not in vector_disease:
                    vector_disease_results = vector_disease
            except Exception as e:
                logger.warning(f"Vector-disease analysis failed: {e}")
        
        # Create correlation matrix
        corr_cols = ['case_count'] + [c for c in env_cols if c in merged.columns and merged[c].notna().any()]
        try:
            if len(corr_cols) >= 2:
                corr_matrix = merged[corr_cols].corr()
            else:
                corr_matrix = pd.DataFrame()
        except Exception as e:
            logger.warning(f"Could not create correlation matrix: {e}")
            corr_matrix = pd.DataFrame()
        
        return {
            'correlations': correlations,
            'merged_data': merged,
            'correlation_matrix': corr_matrix,
            'vector_climate': vector_climate_results,
            'vector_disease': vector_disease_results
        }

    
    def _analyze_lags(self) -> Dict:
        """
        Run lag correlation analysis.
        
        Handles duplicate columns by dropping them before merging.
        """
        env = self.data.get('environment', pd.DataFrame())
        cases = self.data.get('cases', pd.DataFrame())
        
        if env.empty:
            return {'error': 'No environmental data available for lag analysis'}
        
        if cases.empty:
            return {'error': 'No case data available for lag analysis'}
        
        # Make copies
        env = env.copy()
        cases = cases.copy()
        
        # Ensure date is datetime
        env['date'] = pd.to_datetime(env['date'])
        cases['date'] = pd.to_datetime(cases['date'])
        
        # Log date ranges for debugging
        logger.info(f"  Cases date range: {cases['date'].min()} to {cases['date'].max()}")
        logger.info(f"  Environment date range: {env['date'].min()} to {env['date'].max()}")
        
        # Drop duplicate columns from env
        env_cols_to_drop = ['year', 'month']
        for col in env_cols_to_drop:
            if col in env.columns:
                env = env.drop(columns=[col])
        
        # Merge on date
        merged = cases.merge(env, on='date', how='inner')
        
        if merged.empty:
            logger.warning("  No overlapping dates found between cases and environment")
            
            # Try to find closest date match by resampling
            logger.info("  Attempting to resample cases to daily frequency...")
            try:
                cases_daily = cases.set_index('date').resample('D').ffill().reset_index()
                cases_daily = cases_daily.rename(columns={'index': 'date'})
                merged = cases_daily.merge(env, on='date', how='inner')
                
                if not merged.empty:
                    logger.info(f"  Found {len(merged)} overlapping records after resampling")
                else:
                    return {'error': 'No overlapping data for lag analysis'}
            except Exception as e:
                logger.warning(f"  Resampling failed: {e}")
                return {'error': 'No overlapping data for lag analysis'}
        
        logger.info(f"  Merged data: {len(merged)} overlapping records")
        
        # Ensure we have enough data
        if len(merged) < 5:
            return {'error': f'Not enough data for lag analysis. Need at least 5 records, got {len(merged)}'}
        
        # Create monthly aggregation
        merged['date'] = pd.to_datetime(merged['date'])
        merged['month'] = merged['date'].dt.to_period('M')
        
        # Aggregate to monthly
        agg_dict = {'case_count': 'sum'}
        env_cols = [col for col in merged.columns if col in self.config.environmental_variables]
        for col in env_cols:
            if col in merged.columns:
                agg_dict[col] = 'mean'
        
        monthly = merged.groupby('month').agg(agg_dict).reset_index()
        monthly['date'] = monthly['month'].dt.start_time
        
        if len(monthly) < 2:
            return {'error': f'Not enough monthly data for lag analysis. Need at least 2 months, got {len(monthly)}'}
        
        predictor_cols = [col for col in monthly.columns if col in self.config.environmental_variables]
        
        if not predictor_cols:
            return {'error': 'No valid predictor columns available'}
        
        return self.modules.lag_correlation_analysis(
            monthly,
            date_col='date',
            response_col='case_count',
            predictor_cols=predictor_cols,
            max_lags=min(self.config.max_lag_months, len(monthly) // 2)
        )
    
    def _analyze_interventions(self) -> Dict:
        """Run intervention impact analysis."""
        cases = self.data.get('cases', pd.DataFrame())
        interventions = self.data.get('interventions', pd.DataFrame())
        
        if cases.empty:
            return {'error': 'No case data available for intervention analysis'}
        
        if interventions.empty:
            return {'error': 'No intervention data available'}
        
        # Need at least 3 cases records for meaningful analysis
        if len(cases) < 3:
            return {'error': f'Not enough case data for intervention analysis. Need at least 3 records, got {len(cases)}'}
        
        # Check if interventions have date column
        if 'date' not in interventions.columns:
            return {'error': 'Intervention data missing date column'}
        
        return self.modules.intervention_impact_analysis(
            cases,
            interventions,
            date_col='date',
            value_col='case_count'
        )
    
    def _analyze_resistance(self) -> Dict:
        """Run resistance trend analysis."""
        resistance = self.data.get('resistance', pd.DataFrame())
        
        if resistance.empty:
            return {'error': 'No resistance data available'}
        
        # Ensure we have date column
        if 'date' not in resistance.columns:
            return {'error': 'Resistance data missing date column'}
        
        # Check if we have enough data
        if len(resistance) < 3:
            return {'error': f'Not enough resistance data. Need at least 3 records, got {len(resistance)}'}
        
        # Check if status column has data
        if 'resistance_status' in resistance.columns:
            status_counts = resistance['resistance_status'].value_counts()
            logger.info(f"  Resistance status distribution: {dict(status_counts)}")
        else:
            return {'error': 'Resistance data missing status column'}
        
        return self.modules.resistance_trend_analysis(
            resistance,
            date_col='date',
            status_col='resistance_status',
            group_cols=['species_name']
        )
    

    def _analyze_spatial(self) -> Dict:
        """Run spatial analysis with proper location handling."""
        cases = self.data.get('cases', pd.DataFrame())
        vectors = self.data.get('vectors', pd.DataFrame())
        locations = self.data.get('locations', pd.DataFrame())
        
        if cases.empty and vectors.empty:
            return {'error': 'No data available for spatial analysis'}
        
        # Handle cases data
        spatial_cases = pd.DataFrame()
        if not cases.empty:
            # Cases may not have location_id if aggregated
            if 'location_id' not in cases.columns:
                # Try to infer location_id from other columns or create a default
                logger.warning("No location_id column in cases data for spatial analysis")
                # Create a default location_id for all cases
                cases['location_id'] = 1  # Default location
            
            # Aggregate cases by location
            if 'location_id' in cases.columns:
                spatial_cases = cases.groupby(['location_id']).agg({
                    'case_count': ['sum', 'mean', 'count']
                }).reset_index()
                spatial_cases.columns = ['location_id', 'total_cases', 'mean_cases', 'num_months']
        
        # Handle vectors data
        spatial_vectors = pd.DataFrame()
        if not vectors.empty and 'count' in vectors.columns and 'location_id' in vectors.columns:
            spatial_vectors = vectors.groupby(['location_id']).agg({
                'count': 'sum',
                'density_per_trap': 'mean'
            }).reset_index()
            spatial_vectors.columns = ['location_id', 'total_vectors', 'mean_density']
        
        return {
            'spatial_cases': spatial_cases,
            'spatial_vectors': spatial_vectors,
            'num_locations': max(len(spatial_cases), len(spatial_vectors))
        }
    

    def _analyze_risk(self) -> Dict:
        """Build integrated risk model."""
        cases = self.data.get('cases', pd.DataFrame())
        env = self.data.get('environment', pd.DataFrame())
        vectors = self.data.get('vectors', pd.DataFrame())
        
        if cases.empty:
            return {'error': 'No case data available for risk modeling'}
        
        if env.empty:
            return {'error': 'No environmental data available for risk modeling'}
        
        # Need at least 5 data points
        if len(cases) < 5:
            return {'error': f'Not enough case data for risk modeling. Need at least 5 records, got {len(cases)}'}
        
        # Merge cases and environment
        cases = cases.copy()
        env = env.copy()
        
        cases['date'] = pd.to_datetime(cases['date'])
        env['date'] = pd.to_datetime(env['date'])
        
        # Drop duplicate columns from env
        env_cols_to_drop = ['year', 'month']
        for col in env_cols_to_drop:
            if col in env.columns:
                env = env.drop(columns=[col])
        
        # Merge
        merged = cases.merge(env, on='date', how='inner')
        
        if merged.empty:
            logger.warning("No overlapping dates found between cases and environment for risk modeling")
            return {'error': 'No overlapping data for risk modeling'}
        
        # Add vector data if available
        if vectors is not None and not vectors.empty:
            vectors = vectors.copy()
            vectors['date'] = pd.to_datetime(vectors['date'])
            if 'count' in vectors.columns:
                vector_agg = vectors.groupby('date')['count'].sum().reset_index()
                merged = merged.merge(vector_agg, on='date', how='left')
                merged['count'] = merged['count'].fillna(0)
            elif 'density_per_trap' in vectors.columns:
                vector_agg = vectors.groupby('date')['density_per_trap'].mean().reset_index()
                merged = merged.merge(vector_agg, on='date', how='left')
                merged['density_per_trap'] = merged['density_per_trap'].fillna(0)
        
        # Prepare predictor columns
        predictor_cols = self.config.environmental_variables.copy()
        if 'count' in merged.columns:
            predictor_cols.append('count')
        if 'density_per_trap' in merged.columns:
            predictor_cols.append('density_per_trap')
        
        # Use custom features if provided
        if self.config.features:
            predictor_cols = [col for col in self.config.features if col in merged.columns]
        
        # Filter to available columns
        predictor_cols = [col for col in predictor_cols if col in merged.columns]
        
        if not predictor_cols:
            return {'error': 'No valid predictor columns available for risk modeling'}
        
        # Check if we have enough data
        if len(merged) < 10:
            return {'error': f'Not enough data for risk modeling. Need at least 10 samples, got {len(merged)}'}
        
        # Call the module with correct argument order
        return self.modules.integrated_risk_model(
            cases_df=merged,  # First arg: cases_df
            env_df=merged,    # Second arg: env_df (use merged since we already combined)
            vector_df=None,   # Vector already merged
            date_col='date',
            response_col='case_count',
            predictor_cols=predictor_cols
        )


    def _generate_figures(self):
        """Generate all visualizations."""
        fig_dir = self.output_dir / "figures"
        fig_dir.mkdir(exist_ok=True)
        
        # Seasonal pattern figure
        if 'seasonal' in self.results:
            seasonal = self.results['seasonal']
            if seasonal and 'error' not in seasonal:
                try:
                    self.viz.plot_seasonal_patterns(
                        seasonal,
                        self.config.study_name,
                        fig_dir
                    )
                except Exception as e:
                    logger.warning(f"Could not generate seasonal plot: {e}")
        
        # Climate correlation figure
        if 'climate_correlation' in self.results:
            climate = self.results['climate_correlation']
            if climate and 'error' not in climate:
                try:
                    self.viz.plot_climate_correlation(
                        climate,
                        self.config.study_name,
                        fig_dir
                    )
                except Exception as e:
                    logger.warning(f"Could not generate climate correlation plot: {e}")
        
        # Lag analysis figure
        if 'lag_analysis' in self.results:
            lag = self.results['lag_analysis']
            if lag and 'error' not in lag:
                try:
                    self.viz.plot_lag_correlations(
                        lag,
                        self.config.study_name,
                        fig_dir
                    )
                except Exception as e:
                    logger.warning(f"Could not generate lag plot: {e}")
        
        # Intervention impact figure
        if 'intervention_impact' in self.results:
            impact = self.results['intervention_impact']
            if impact and 'error' not in impact:
                try:
                    self.viz.plot_intervention_impact(
                        impact,
                        self.config.study_name,
                        fig_dir
                    )
                except Exception as e:
                    logger.warning(f"Could not generate intervention plot: {e}")
        
        # Resistance trends figure
        if 'resistance_trends' in self.results:
            resistance = self.results['resistance_trends']
            if resistance and 'error' not in resistance:
                try:
                    self.viz.plot_resistance_trends(
                        resistance,
                        self.config.study_name,
                        fig_dir
                    )
                except Exception as e:
                    logger.warning(f"Could not generate resistance plot: {e}")
        
        # Risk model figure
        if 'risk_model' in self.results:
            risk = self.results['risk_model']
            if risk and 'error' not in risk:
                try:
                    self.viz.plot_risk_model(
                        risk,
                        self.config.study_name,
                        fig_dir
                    )
                except Exception as e:
                    logger.warning(f"Could not generate risk model plot: {e}")
        
        # Spatial analysis figure
        if 'spatial_analysis' in self.results:
            spatial = self.results['spatial_analysis']
            if spatial and 'error' not in spatial:
                try:
                    self.viz.plot_spatial_analysis(
                        spatial,
                        self.config.study_name,
                        fig_dir
                    )
                except Exception as e:
                    logger.warning(f"Could not generate spatial analysis plot: {e}")
        
        # Overview dashboard - always try to create
        try:
            self.viz.create_dashboard(
                self.results,
                self.config.study_name,
                fig_dir
            )
        except Exception as e:
            logger.warning(f"Could not generate dashboard: {e}")
    
    def _save_results(self):
        """Save analysis results to files."""
        data_dir = self.output_dir / "data"
        data_dir.mkdir(exist_ok=True)
        
        for name, result in self.results.items():
            if isinstance(result, dict):
                # Save DataFrames
                for key, value in result.items():
                    if isinstance(value, pd.DataFrame) and not value.empty:
                        try:
                            value.to_csv(data_dir / f"{name}_{key}.csv", index=False)
                        except Exception as e:
                            logger.warning(f"Could not save {name}_{key}.csv: {e}")
                    elif key == 'predictions' and isinstance(value, dict):
                        try:
                            pred_df = pd.DataFrame(value)
                            pred_df.to_csv(data_dir / f"{name}_predictions.csv", index=False)
                        except Exception as e:
                            logger.warning(f"Could not save {name}_predictions.csv: {e}")
                
                # Save summary as JSON (excluding large objects)
                summary = {}
                for k, v in result.items():
                    if k in ['model', 'scaler', 'decomposition', 'merged_data']:
                        continue  # Skip large/unsupported objects
                    if not isinstance(v, (pd.DataFrame, np.ndarray, dict)) or k == 'correlation_matrix':
                        if k == 'correlation_matrix' and isinstance(v, pd.DataFrame):
                            summary[k] = v.to_dict()
                        elif isinstance(v, (int, float, str, bool, type(None))):
                            summary[k] = v
                        elif k == 'ccf' and isinstance(v, dict):
                            summary[k] = {k2: v2.tolist() if isinstance(v2, np.ndarray) else v2 
                                         for k2, v2 in v.items()}
                
                if summary:
                    try:
                        with open(data_dir / f"{name}_summary.json", 'w') as f:
                            json.dump(summary, f, indent=2, default=str)
                    except Exception as e:
                        logger.warning(f"Could not save {name}_summary.json: {e}")
    
    def _generate_report(self):
        """Generate a summary report."""
        report_dir = self.output_dir / "reports"
        report_dir.mkdir(exist_ok=True)
        
        report = {
            'study_name': self.config.study_name,
            'location': self.config.location_name,
            'country': self.config.country_code,
            'period': f"{self.config.start_year}-{self.config.end_year}",
            'generated': datetime.now().isoformat(),
            'data_summary': {k: len(v) for k, v in self.data.items() if isinstance(v, pd.DataFrame)},
            'summary': self._create_summary(),
            'errors': self._collect_errors()
        }
        
        # Save JSON report
        try:
            with open(report_dir / "analysis_report.json", 'w') as f:
                json.dump(report, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Could not save JSON report: {e}")
        
        # Generate markdown report
        try:
            self._generate_markdown_report(report, report_dir / "analysis_report.md")
        except Exception as e:
            logger.warning(f"Could not generate markdown report: {e}")
    
    def _create_summary(self) -> Dict:
        """Create a summary of key findings."""
        summary = {}
        
        # Seasonal patterns
        if 'seasonal' in self.results:
            seasonal = self.results['seasonal']
            if seasonal and 'error' not in seasonal:
                if 'peak_months' in seasonal and not seasonal['peak_months'].empty:
                    try:
                        summary['peak_months'] = seasonal['peak_months'][['month', 'mean']].to_dict('records')
                    except:
                        pass
        
        # Climate correlation
        if 'climate_correlation' in self.results:
            climate = self.results['climate_correlation']
            if climate and 'error' not in climate:
                if 'correlations' in climate:
                    summary['climate_correlations'] = {k: v for k, v in climate['correlations'].items() 
                                                      if v is not None}
        
        # Lag analysis
        if 'lag_analysis' in self.results:
            lag = self.results['lag_analysis']
            if lag and 'error' not in lag:
                if 'optimal_lags' in lag and lag['optimal_lags']:
                    summary['optimal_lags'] = lag['optimal_lags']
        
        # Intervention impact
        if 'intervention_impact' in self.results:
            impact = self.results['intervention_impact']
            if impact and 'error' not in impact:
                if 'summary' in impact:
                    summary['intervention_summary'] = impact['summary']
        
        # Resistance trends
        if 'resistance_trends' in self.results:
            resistance = self.results['resistance_trends']
            if resistance and 'error' not in resistance:
                if 'overall_trend' in resistance:
                    summary['resistance_trend'] = resistance['overall_trend']
        
        # Risk model
        if 'risk_model' in self.results:
            risk = self.results['risk_model']
            if risk and 'error' not in risk:
                if 'r2_score' in risk:
                    summary['model_performance'] = {
                        'r2_score': risk['r2_score'],
                        'rmse': risk['rmse']
                    }
                if 'feature_importance' in risk:
                    try:
                        top_features = sorted(
                            risk['feature_importance'].items(),
                            key=lambda x: x[1],
                            reverse=True
                        )[:5]
                        summary['top_features'] = top_features
                    except:
                        pass
        
        return summary
    
    def _collect_errors(self) -> Dict:
        """Collect all errors from analysis results."""
        errors = {}
        for name, result in self.results.items():
            if isinstance(result, dict) and 'error' in result:
                errors[name] = result['error']
        return errors
    
    def _generate_markdown_report(self, report: Dict, output_path: Path):
        """Generate a markdown report from analysis results."""
        lines = []
        lines.append(f"# Analysis Report: {report['study_name']}")
        lines.append("")
        lines.append(f"**Location:** {report['location']}, {report['country']}")
        lines.append(f"**Period:** {report['period']}")
        lines.append(f"**Generated:** {report['generated']}")
        lines.append("")
        
        # Data availability
        lines.append("## Data Availability")
        lines.append("")
        data_summary = report.get('data_summary', {})
        if data_summary:
            lines.append("| Data Type | Records |")
            lines.append("|-----------|---------|")
            for key, count in data_summary.items():
                lines.append(f"| {key} | {count} |")
        else:
            lines.append("No data available.")
        lines.append("")
        
        # Summary of findings
        summary = report.get('summary', {})
        if summary:
            lines.append("## Summary of Findings")
            lines.append("")
            
            if 'peak_months' in summary:
                lines.append("### Seasonal Patterns")
                lines.append("Peak months for disease activity:")
                for month in summary['peak_months']:
                    lines.append(f"- Month {month.get('month', 'N/A')}: {month.get('mean', 0):.2f} cases/month")
                lines.append("")
            
            if 'climate_correlations' in summary:
                lines.append("### Climate Correlations")
                for var, corr in summary['climate_correlations'].items():
                    if corr is not None:
                        lines.append(f"- {var}: r = {corr:.3f}")
                lines.append("")
            
            if 'optimal_lags' in summary:
                lines.append("### Climate-Disease Lags")
                lines.append("Optimal lag periods (months):")
                for lag in summary['optimal_lags']:
                    if isinstance(lag, dict):
                        predictor = lag.get('predictor', 'N/A')
                        lag_months = lag.get('lag_months', 'N/A')
                        correlation = lag.get('correlation', 0)
                        lines.append(f"- {predictor}: {lag_months} months (r={correlation:.3f})")
                lines.append("")
            
            if 'intervention_summary' in summary:
                lines.append("### Intervention Impact")
                s = summary['intervention_summary']
                if isinstance(s, dict):
                    lines.append(f"- Average change: {s.get('avg_absolute_change', 0):.2f} cases")
                    avg_rel = s.get('avg_relative_change', 0)
                    if avg_rel is not None:
                        lines.append(f"- Average relative change: {avg_rel:.1f}%")
                    lines.append(f"- Significant interventions: {s.get('significant_interventions', 0)}")
                lines.append("")
            
            if 'resistance_trend' in summary:
                lines.append("### Insecticide Resistance Trends")
                t = summary['resistance_trend']
                if isinstance(t, dict):
                    lines.append(f"- Trend direction: {t.get('direction', 'stable')}")
                    slope = t.get('slope', 0)
                    if slope is not None:
                        lines.append(f"- Slope: {slope:.3f}")
                    r_squared = t.get('r_squared', 0)
                    if r_squared is not None:
                        lines.append(f"- R²: {r_squared:.3f}")
                    p_value = t.get('p_value', 0)
                    if p_value is not None:
                        lines.append(f"- p-value: {p_value:.4f}")
                lines.append("")
            
            if 'model_performance' in summary:
                lines.append("### Integrated Risk Model")
                m = summary['model_performance']
                if isinstance(m, dict):
                    r2 = m.get('r2_score', 0)
                    rmse = m.get('rmse', 0)
                    if r2 is not None:
                        lines.append(f"- R² score: {r2:.3f}")
                    if rmse is not None:
                        lines.append(f"- RMSE: {rmse:.3f}")
                lines.append("")
            
            if 'top_features' in summary:
                lines.append("### Top Risk Factors")
                for feature, importance in summary['top_features']:
                    lines.append(f"- {feature}: {importance:.3f}")
                lines.append("")
        
        # Errors
        errors = report.get('errors', {})
        if errors:
            lines.append("## Warnings/Errors")
            lines.append("")
            for analysis, error in errors.items():
                lines.append(f"- **{analysis}**: {error}")
            lines.append("")
        
        lines.append("---")
        lines.append("")
        lines.append("## Detailed Results")
        lines.append("")
        lines.append("See the following files for detailed results:")
        lines.append("")
        lines.append("- `data/`: CSV files with detailed analysis results")
        lines.append("- `figures/`: Visualizations and plots")
        lines.append("- `analysis_report.json`: Raw results in JSON format")
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            logger.info(f"Markdown report saved to {output_path}")
        except Exception as e:
            logger.warning(f"Could not write markdown report: {e}")
    
    def close(self):
        """Close database connections."""
        if hasattr(self, 'base'):
            self.base.close()


    def _analyze_vector_climate(self) -> Dict:
        vectors = self.data.get('vectors', pd.DataFrame())
        env = self.data.get('environment', pd.DataFrame())
        
        if vectors.empty:
            return {'error': 'No vector data available'}
        
        if env.empty:
            return {'error': 'No environmental data available'}
        
        vector_col = 'count' if 'count' in vectors.columns else 'density_per_trap'
        if vector_col not in vectors.columns:
            return {'error': 'No vector abundance column found'}
        
        climate_vars = self.config.environmental_variables
        
        # Only group by species if it exists and has >1 unique value
        group_cols = []
        if 'species_name' in vectors.columns:
            # Don't add species_name as a group column if it's already in the DataFrame
            # The analysis module will handle it
            if vectors['species_name'].nunique() > 1:
                group_cols.append('species_name')
        
        return self.modules.vector_climate_analysis(
            vector_df=vectors,
            climate_df=env,
            date_col='date',
            vector_col=vector_col,
            group_cols=group_cols if group_cols else None,
            climate_variables=climate_vars,
            aggregation='monthly',
            lag_months=self.config.max_lag_months
        )


    def _analyze_vector_disease(self) -> Dict:
        vectors = self.data.get('vectors', pd.DataFrame())
        cases = self.data.get('cases', pd.DataFrame())
        
        if vectors.empty:
            return {'error': 'No vector data available'}
        
        if cases.empty:
            return {'error': 'No case data available'}
        
        vector_col = 'count' if 'count' in vectors.columns else 'density_per_trap'
        if vector_col not in vectors.columns:
            return {'error': 'No vector abundance column found'}
        
        group_cols = []
        if 'species_name' in vectors.columns and vectors['species_name'].nunique() > 1:
            group_cols.append('species_name')
        
        return self.modules.vector_disease_analysis(
            vector_df=vectors,
            disease_df=cases,
            date_col='date',
            vector_col=vector_col,
            disease_col='case_count',
            group_cols=group_cols if group_cols else None,
            aggregation='monthly',
            lag_months=self.config.max_lag_months
        )


    def _analyze_vector_occurrence(self) -> Dict:
        """
        Run vector occurrence analysis.
        """
        vectors = self.data.get('vectors', pd.DataFrame())
        
        if vectors.empty:
            return {'error': 'No vector data available'}
        
        vector_col = 'count' if 'count' in vectors.columns else 'density_per_trap'
        if vector_col not in vectors.columns:
            return {'error': 'No vector abundance column found'}
        
        group_cols = []
        if 'species_name' in vectors.columns:
            group_cols.append('species_name')
        if 'location_id' in vectors.columns:
            group_cols.append('location_id')
        
        return self.modules.vector_occurrence_analysis(
            vector_df=vectors,
            date_col='date',
            vector_col=vector_col,
            group_cols=group_cols if group_cols else None
        )


    def _analyze_vector_clustering(self) -> Dict:
        """
        Run vector abundance clustering analysis.
        """
        vectors = self.data.get('vectors', pd.DataFrame())
        
        if vectors.empty:
            return {'error': 'No vector data available'}
        
        vector_col = 'count' if 'count' in vectors.columns else 'density_per_trap'
        if vector_col not in vectors.columns:
            return {'error': 'No vector abundance column found'}
        
        # Determine grouping columns
        group_cols = []
        if 'species_name' in vectors.columns:
            group_cols.append('species_name')
        if 'location_id' in vectors.columns:
            group_cols.append('location_id')
        
        if not group_cols:
            return {'error': 'No grouping columns available for clustering'}
        
        return self.modules.vector_abundance_clustering(
            vector_df=vectors,
            date_col='date',
            vector_col=vector_col,
            group_cols=group_cols,
            n_clusters=min(4, vectors[group_cols[0]].nunique())
        )


# Convenience function
def run_analysis(
    study_name: str,
    location: str = "ALL",
    country: str = "ALL",
    start_year: int = None,
    end_year: int = None,
    output_dir: str = "outputs/analysis",
    features: Optional[List[str]] = None,
    target: Optional[str] = None,
    **kwargs
) -> Dict:
    """
    Convenience function to run the analysis pipeline.
    
    Args:
        study_name: Name of the study
        location: Location name to analyze (use "ALL" for all locations)
        country: Country code (use "ALL" for all countries)
        start_year: Start year (default: get from data)
        end_year: End year (default: get from data)
        output_dir: Output directory (default: "outputs/analysis")
        features: List of feature columns for risk model
        target: Target column for risk model
        **kwargs: Additional analysis parameters
    
    Returns:
        Dictionary with analysis results
    """
    # If no years specified, get from data
    if start_year is None or end_year is None:
        try:
            from database.session import SessionLocal
            from database.models import DimTime
            
            session = SessionLocal()
            try:
                # Get min and max years from time dimension
                min_year = session.query(DimTime.year).order_by(DimTime.year.asc()).first()
                max_year = session.query(DimTime.year).order_by(DimTime.year.desc()).first()
                
                if min_year and max_year:
                    start_year = start_year or min_year[0]
                    end_year = end_year or max_year[0]
                else:
                    start_year = start_year or 2019
                    end_year = end_year or 2024
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"Could not get year range from database: {e}")
            start_year = start_year or 2019
            end_year = end_year or 2024
    
    config = AnalysisConfig(
        study_name=study_name,
        location_name=location,
        country_code=country,
        start_year=start_year,
        end_year=end_year,
        output_dir=output_dir,
        features=features,
        target=target,
        **kwargs
    )
    
    pipeline = AnalysisPipeline(config)
    try:
        results = pipeline.run()
    finally:
        pipeline.close()
    
    return results