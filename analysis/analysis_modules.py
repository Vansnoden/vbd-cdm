# analysis/analysis_modules.py
"""
Modular analysis components for CDM-based studies.

This module provides reusable analysis functions that can be combined
in different pipelines. Includes comprehensive vector analysis:
- Vector occurrence vs climate factors
- Vector abundance vs climate factors
- Vector occurrence vs disease cases
- Vector abundance vs disease cases
- Vector-climate seasonal patterns
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from typing import Dict, List, Tuple, Optional, Union, Any
from scipy import stats
from scipy.ndimage import gaussian_filter1d
from statsmodels.tsa.seasonal import seasonal_decompose
from statsmodels.tsa.stattools import acf, pacf, ccf
from statsmodels.graphics.tsaplots import plot_acf, plot_pacf
from scipy.cluster.hierarchy import dendrogram, linkage
from scipy.spatial.distance import pdist
import warnings
warnings.filterwarnings('ignore')
import logging

logger = logging.getLogger(__name__)


class AnalysisModules:
    """
    Collection of analysis functions for CDM data.
    Includes comprehensive vector-climate and vector-disease analysis.
    """
    
    # ========================= EXISTING METHODS =========================
    
    @staticmethod
    def seasonal_patterns(
        df: pd.DataFrame,
        date_col: str = 'date',
        value_col: str = 'case_count',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """Analyze seasonal patterns in time series data."""
        results = {}
        
        if df is None or df.empty:
            return {'error': 'No data available for seasonal analysis'}
        
        if len(df) < 2:
            return {'error': f'Not enough data for seasonal analysis. Need at least 2 records, got {len(df)}'}
        
        try:
            df = df.copy()
            df[date_col] = pd.to_datetime(df[date_col])
            
            df['month'] = df[date_col].dt.month
            df['month_name'] = df['month'].apply(lambda x: pd.to_datetime(f'2000-{x:02d}-01').strftime('%b'))
            df['year'] = df[date_col].dt.year
            df['week'] = df[date_col].dt.isocalendar().week
            
            if len(df) >= 3:
                if group_cols:
                    monthly_agg = df.groupby(group_cols + ['month', 'month_name'])[value_col].agg(['mean', 'std', 'count']).reset_index()
                    results['monthly_by_group'] = monthly_agg
                else:
                    monthly_agg = df.groupby(['month', 'month_name'])[value_col].agg(['mean', 'std', 'count']).reset_index()
                    results['monthly'] = monthly_agg
            else:
                monthly_agg = df.groupby(['month', 'month_name'])[value_col].agg(['mean', 'count']).reset_index()
                monthly_agg['std'] = np.nan
                results['monthly'] = monthly_agg
            
            if df['year'].nunique() > 1:
                yoy = df.groupby(['year', 'month'])[value_col].mean().reset_index()
                results['year_over_year'] = yoy
            else:
                results['year_over_year'] = pd.DataFrame()
            
            if len(df) >= 24:
                try:
                    ts = df.set_index(date_col)[value_col].sort_index()
                    ts = ts.interpolate(method='time')
                    if len(ts) >= 24:
                        decomposition = seasonal_decompose(ts, model='additive', period=12)
                        results['decomposition'] = decomposition
                except Exception as e:
                    logger.warning(f"Could not perform seasonal decomposition: {e}")
            
            if not monthly_agg.empty and len(monthly_agg) >= 3:
                peak_months = monthly_agg.nlargest(3, 'mean')
                results['peak_months'] = peak_months
            elif not monthly_agg.empty:
                peak_months = monthly_agg.nlargest(1, 'mean')
                results['peak_months'] = peak_months
            else:
                results['peak_months'] = pd.DataFrame()
            
        except Exception as e:
            logger.error(f"Error in seasonal_patterns: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= VECTOR-CLIMATE ANALYSIS =========================
    
    @staticmethod
    def vector_climate_analysis(
        vector_df: pd.DataFrame,
        climate_df: pd.DataFrame,
        date_col: str = 'date',
        vector_col: str = 'count',
        group_cols: Optional[List[str]] = None,
        climate_variables: Optional[List[str]] = None,
        aggregation: str = 'monthly',
        lag_months: int = 3
    ) -> Dict:
        """
        Comprehensive analysis of vector occurrence/abundance vs climate factors.
        """
        results = {}
        
        if vector_df is None or vector_df.empty:
            return {'error': 'No vector data available'}
        
        if climate_df is None or climate_df.empty:
            return {'error': 'No climate data available'}
        
        try:
            # Make copies
            vector_df = vector_df.copy()
            climate_df = climate_df.copy()
            
            # Ensure date is datetime
            vector_df[date_col] = pd.to_datetime(vector_df[date_col])
            climate_df[date_col] = pd.to_datetime(climate_df[date_col])
            
            # Auto-detect climate variables
            if climate_variables is None:
                climate_variables = [col for col in climate_df.columns 
                                    if col not in ['date', 'year', 'month'] 
                                    and pd.api.types.is_numeric_dtype(climate_df[col])]
            
            # Aggregate to specified frequency
            if aggregation == 'monthly':
                vector_df['period'] = vector_df[date_col].dt.to_period('M')
                climate_df['period'] = climate_df[date_col].dt.to_period('M')
                
                # Aggregate vector data - only include columns that exist
                vector_agg_cols = [vector_col]  # Start with the vector column
                if group_cols:
                    # Only include group columns that exist in the DataFrame
                    valid_group_cols = [col for col in group_cols if col in vector_df.columns]
                    vector_agg_cols.extend(valid_group_cols)
                
                # Build aggregation dict
                agg_dict = {vector_col: 'sum'}
                for col in vector_agg_cols:
                    if col != vector_col and col in vector_df.columns and col != 'period' and col != date_col:
                        agg_dict[col] = 'first'
                
                # Perform aggregation with only valid columns
                vector_agg = vector_df.groupby(['period'] + [col for col in vector_agg_cols if col != vector_col and col in vector_df.columns]).agg(agg_dict).reset_index()
                vector_agg['date'] = vector_agg['period'].dt.start_time
                
                # Aggregate climate data
                climate_agg_dict = {var: 'mean' for var in climate_variables if var in climate_df.columns}
                if 'tp' in climate_agg_dict:
                    climate_agg_dict['tp'] = 'sum'
                if 'precipitation' in climate_agg_dict:
                    climate_agg_dict['precipitation'] = 'sum'
                
                climate_agg = climate_df.groupby('period').agg(climate_agg_dict).reset_index()
                climate_agg['date'] = climate_agg['period'].dt.start_time
                
            elif aggregation == 'weekly':
                vector_df['period'] = vector_df[date_col].dt.to_period('W')
                climate_df['period'] = climate_df[date_col].dt.to_period('W')
                
                agg_dict = {vector_col: 'sum'}
                if group_cols:
                    for col in group_cols:
                        if col in vector_df.columns:
                            agg_dict[col] = 'first'
                
                vector_agg = vector_df.groupby(['period'] + (group_cols or [])).agg(agg_dict).reset_index()
                vector_agg['date'] = vector_agg['period'].dt.start_time
                
                climate_agg_dict = {var: 'mean' for var in climate_variables if var in climate_df.columns}
                climate_agg = climate_df.groupby('period').agg(climate_agg_dict).reset_index()
                climate_agg['date'] = climate_agg['period'].dt.start_time
                
            else:  # daily
                vector_agg = vector_df
                climate_agg = climate_df
            
            # Drop period column
            for df in [vector_agg, climate_agg]:
                if 'period' in df.columns:
                    df.drop(columns=['period'], inplace=True)
            
            # Merge vector and climate data
            merged = vector_agg.merge(climate_agg, on='date', how='inner')
            
            if merged.empty:
                return {'error': 'No overlapping dates between vector and climate data'}
            
            logger.info(f"Vector-climate merged data: {len(merged)} records")
            
            results['merged_data'] = merged
            results['aggregation'] = aggregation
            
            # --- 1. Correlation Analysis ---
            correlations = {}
            for var in climate_variables:
                if var in merged.columns and merged[var].notna().any():
                    valid_mask = merged[vector_col].notna() & merged[var].notna()
                    if valid_mask.sum() > 5:
                        corr, p_value = stats.pearsonr(
                            merged.loc[valid_mask, vector_col],
                            merged.loc[valid_mask, var]
                        )
                        if not np.isnan(corr):
                            correlations[var] = {
                                'correlation': corr,
                                'p_value': p_value,
                                'n': valid_mask.sum()
                            }
            
            results['correlations'] = correlations
            
            # --- 2. Lag Correlation Analysis ---
            if lag_months > 0:
                lag_results = []
                for var in climate_variables:
                    if var not in merged.columns:
                        continue
                    
                    sorted_merged = merged.sort_values('date')
                    
                    for lag in range(0, lag_months + 1):
                        shifted = sorted_merged[var].shift(lag)
                        valid = ~(shifted.isna() | sorted_merged[vector_col].isna())
                        
                        if valid.sum() > 5:
                            corr, p_value = stats.pearsonr(
                                shifted[valid],
                                sorted_merged.loc[valid, vector_col]
                            )
                            if not np.isnan(corr):
                                lag_results.append({
                                    'climate_variable': var,
                                    'lag_months': lag,
                                    'correlation': corr,
                                    'p_value': p_value,
                                    'n': valid.sum()
                                })
                
                results['lag_correlations'] = pd.DataFrame(lag_results) if lag_results else pd.DataFrame()
                
                if lag_results:
                    optimal = pd.DataFrame(lag_results).groupby('climate_variable').apply(
                        lambda x: x.loc[x['correlation'].idxmax()] if not x.empty else None
                    )
                    results['optimal_lags'] = optimal.to_dict('records') if not optimal.empty else []
                else:
                    results['optimal_lags'] = []
            
            # --- 3. Seasonal Patterns by Climate ---
            if aggregation == 'monthly':
                merged['month'] = merged['date'].dt.month
                seasonal_patterns = {}
                
                for var in climate_variables:
                    if var in merged.columns:
                        try:
                            monthly = merged.groupby('month').agg({
                                vector_col: 'mean',
                                var: 'mean'
                            }).reset_index()
                            
                            seasonal_patterns[var] = {
                                'vector_abundance': monthly[vector_col].tolist(),
                                'climate_values': monthly[var].tolist(),
                                'months': monthly['month'].tolist()
                            }
                        except Exception as e:
                            logger.warning(f"Could not compute seasonal patterns for {var}: {e}")
                
                results['seasonal_patterns'] = seasonal_patterns
            
            # --- 4. Climate Envelope Analysis ---
            envelope = {}
            for var in climate_variables:
                if var in merged.columns:
                    bins = np.percentile(merged[var].dropna(), np.linspace(0, 100, 11))
                    merged['bin'] = pd.cut(merged[var], bins)
                    
                    bin_means = merged.groupby('bin')[vector_col].agg(['mean', 'std', 'count']).reset_index()
                    bin_means['variable'] = var
                    envelope[var] = bin_means.to_dict('records')
            
            results['climate_envelope'] = envelope
            
            # --- 5. Correlation Matrix ---
            corr_cols = [vector_col] + [v for v in climate_variables if v in merged.columns]
            if len(corr_cols) >= 2:
                corr_matrix = merged[corr_cols].corr()
                results['correlation_matrix'] = corr_matrix
            
            # --- 6. Summary Statistics ---
            results['summary'] = {
                'total_observations': len(merged),
                'date_range': {
                    'start': merged['date'].min().strftime('%Y-%m-%d'),
                    'end': merged['date'].max().strftime('%Y-%m-%d')
                },
                'mean_vector_abundance': merged[vector_col].mean(),
                'max_vector_abundance': merged[vector_col].max(),
                'climate_variables_analyzed': climate_variables
            }
            
            # --- 7. Group-specific analysis (only if group columns exist and are valid) ---
            if group_cols:
                # Filter to only valid group columns that exist
                valid_group_cols = [col for col in group_cols if col in merged.columns]
                if valid_group_cols:
                    group_results = {}
                    for group_name, group_data in merged.groupby(valid_group_cols):
                        if len(group_data) > 10:
                            group_corrs = {}
                            for var in climate_variables:
                                if var in group_data.columns:
                                    valid = group_data[vector_col].notna() & group_data[var].notna()
                                    if valid.sum() > 5:
                                        corr, p = stats.pearsonr(
                                            group_data.loc[valid, vector_col],
                                            group_data.loc[valid, var]
                                        )
                                        if not np.isnan(corr):
                                            group_corrs[var] = {'correlation': corr, 'p_value': p}
                            
                            group_results[str(group_name)] = {
                                'n_records': len(group_data),
                                'mean_abundance': group_data[vector_col].mean(),
                                'correlations': group_corrs
                            }
                    
                    results['group_analysis'] = group_results
                
        except Exception as e:
            logger.error(f"Error in vector_climate_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= VECTOR-DISEASE ANALYSIS =========================
    
    @staticmethod
    def vector_disease_analysis(
        vector_df: pd.DataFrame,
        disease_df: pd.DataFrame,
        date_col: str = 'date',
        vector_col: str = 'count',
        disease_col: str = 'case_count',
        group_cols: Optional[List[str]] = None,
        aggregation: str = 'monthly',
        lag_months: int = 3
    ) -> Dict:
        """
        Comprehensive analysis of vector occurrence/abundance vs disease cases.
        
        Analyzes:
        - Vector abundance vs disease cases
        - Vector occurrence vs disease cases
        - Lagged relationships between vectors and disease
        - Seasonal patterns of both vectors and disease
        
        Args:
            vector_df: DataFrame with vector observation data
            disease_df: DataFrame with disease case data
            date_col: Name of date column
            vector_col: Name of vector abundance column
            disease_col: Name of disease case column
            group_cols: Columns to group by (e.g., species)
            aggregation: Temporal aggregation ('daily', 'weekly', 'monthly')
            lag_months: Maximum lag to analyze
            
        Returns:
            Dictionary with comprehensive vector-disease analysis results
        """
        results = {}
        
        if vector_df is None or vector_df.empty:
            return {'error': 'No vector data available'}
        
        if disease_df is None or disease_df.empty:
            return {'error': 'No disease data available'}
        
        try:
            # Make copies
            vector_df = vector_df.copy()
            disease_df = disease_df.copy()
            
            # Ensure date is datetime
            vector_df[date_col] = pd.to_datetime(vector_df[date_col])
            disease_df[date_col] = pd.to_datetime(disease_df[date_col])
            
            # Aggregate to specified frequency
            if aggregation == 'monthly':
                vector_df['period'] = vector_df[date_col].dt.to_period('M')
                disease_df['period'] = disease_df[date_col].dt.to_period('M')
                
                # Aggregate vector data
                agg_dict = {vector_col: 'sum'}
                if group_cols:
                    for col in group_cols:
                        if col in vector_df.columns:
                            agg_dict[col] = 'first'
                
                vector_agg = vector_df.groupby(['period'] + (group_cols or [])).agg(agg_dict).reset_index()
                vector_agg['date'] = vector_agg['period'].dt.start_time
                
                # Aggregate disease data
                disease_agg = disease_df.groupby('period')[disease_col].sum().reset_index()
                disease_agg['date'] = disease_agg['period'].dt.start_time
                
            elif aggregation == 'weekly':
                vector_df['period'] = vector_df[date_col].dt.to_period('W')
                disease_df['period'] = disease_df[date_col].dt.to_period('W')
                
                agg_dict = {vector_col: 'sum'}
                if group_cols:
                    for col in group_cols:
                        if col in vector_df.columns:
                            agg_dict[col] = 'first'
                
                vector_agg = vector_df.groupby(['period'] + (group_cols or [])).agg(agg_dict).reset_index()
                vector_agg['date'] = vector_agg['period'].dt.start_time
                
                disease_agg = disease_df.groupby('period')[disease_col].sum().reset_index()
                disease_agg['date'] = disease_agg['period'].dt.start_time
                
            else:  # daily
                vector_agg = vector_df
                disease_agg = disease_df
            
            # Drop period column
            for df in [vector_agg, disease_agg]:
                if 'period' in df.columns:
                    df.drop(columns=['period'], inplace=True)
            
            # Merge vector and disease data
            merged = vector_agg.merge(disease_agg, on='date', how='inner')
            
            if merged.empty:
                return {'error': 'No overlapping dates between vector and disease data'}
            
            logger.info(f"Vector-disease merged data: {len(merged)} records")
            
            # Store merged data
            results['merged_data'] = merged
            results['aggregation'] = aggregation
            
            # --- 1. Correlation Analysis ---
            valid_mask = merged[vector_col].notna() & merged[disease_col].notna()
            if valid_mask.sum() > 5:
                corr, p_value = stats.pearsonr(
                    merged.loc[valid_mask, vector_col],
                    merged.loc[valid_mask, disease_col]
                )
                if not np.isnan(corr):
                    results['correlation'] = {
                        'pearson_r': corr,
                        'p_value': p_value,
                        'n': valid_mask.sum()
                    }
                
                # Also compute Spearman correlation
                corr_s, p_s = stats.spearmanr(
                    merged.loc[valid_mask, vector_col],
                    merged.loc[valid_mask, disease_col]
                )
                if not np.isnan(corr_s):
                    results['spearman_correlation'] = {
                        'rho': corr_s,
                        'p_value': p_s,
                        'n': valid_mask.sum()
                    }
            
            # --- 2. Lag Correlation Analysis ---
            if lag_months > 0:
                sorted_merged = merged.sort_values('date')
                lag_results = []
                
                for lag in range(0, lag_months + 1):
                    shifted_vector = sorted_merged[vector_col].shift(lag)
                    valid = ~(shifted_vector.isna() | sorted_merged[disease_col].isna())
                    
                    if valid.sum() > 5:
                        corr, p_value = stats.pearsonr(
                            shifted_vector[valid],
                            sorted_merged.loc[valid, disease_col]
                        )
                        if not np.isnan(corr):
                            lag_results.append({
                                'lag_months': lag,
                                'correlation': corr,
                                'p_value': p_value,
                                'n': valid.sum()
                            })
                
                results['lag_correlations'] = pd.DataFrame(lag_results) if lag_results else pd.DataFrame()
                
                if lag_results:
                    optimal = pd.DataFrame(lag_results).loc[
                        pd.DataFrame(lag_results)['correlation'].idxmax()
                    ]
                    results['optimal_lag'] = optimal.to_dict() if not optimal.empty else {}
            
            # --- 3. Cross-Correlation Function ---
            try:
                if len(merged) > 10:
                    # Prepare series
                    vector_series = merged.sort_values('date')[vector_col].fillna(0).values
                    disease_series = merged.sort_values('date')[disease_col].fillna(0).values
                    
                    # Compute CCF
                    ccf_values = ccf(disease_series, vector_series, adjusted=False)
                    max_lag = min(lag_months * 2, len(ccf_values) - 1)
                    results['ccf'] = {
                        'values': ccf_values[:max_lag + 1].tolist(),
                        'lags': list(range(0, max_lag + 1))
                    }
            except Exception as e:
                logger.warning(f"Could not compute CCF: {e}")
            
            # --- 4. Seasonal Pattern Comparison ---
            if aggregation == 'monthly':
                merged['month'] = merged['date'].dt.month
                seasonal_comparison = merged.groupby('month').agg({
                    vector_col: 'mean',
                    disease_col: 'mean'
                }).reset_index()
                
                # Normalize to compare patterns
                seasonal_comparison['vector_norm'] = (
                    seasonal_comparison[vector_col] / seasonal_comparison[vector_col].max()
                )
                seasonal_comparison['disease_norm'] = (
                    seasonal_comparison[disease_col] / seasonal_comparison[disease_col].max()
                )
                
                results['seasonal_comparison'] = seasonal_comparison.to_dict('records')
                
                # Find peak months
                vector_peak = seasonal_comparison.loc[
                    seasonal_comparison[vector_col].idxmax(), 'month'
                ]
                disease_peak = seasonal_comparison.loc[
                    seasonal_comparison[disease_col].idxmax(), 'month'
                ]
                
                results['peak_months'] = {
                    'vector_peak_month': int(vector_peak),
                    'disease_peak_month': int(disease_peak),
                    'lag_between_peaks': int(disease_peak - vector_peak)
                }
            
            # --- 5. Ratio Analysis (Vectors per Case) ---
            merged['vector_per_case'] = merged[vector_col] / (merged[disease_col] + 1)
            results['vector_per_case'] = {
                'mean': merged['vector_per_case'].mean(),
                'std': merged['vector_per_case'].std(),
                'min': merged['vector_per_case'].min(),
                'max': merged['vector_per_case'].max()
            }
            
            # --- 6. Group-specific analysis ---
            if group_cols:
                group_results = {}
                for group_name, group_data in merged.groupby(group_cols):
                    if len(group_data) > 10:
                        valid = group_data[vector_col].notna() & group_data[disease_col].notna()
                        if valid.sum() > 5:
                            corr, p = stats.pearsonr(
                                group_data.loc[valid, vector_col],
                                group_data.loc[valid, disease_col]
                            )
                            group_results[str(group_name)] = {
                                'n_records': len(group_data),
                                'mean_vector': group_data[vector_col].mean(),
                                'mean_cases': group_data[disease_col].mean(),
                                'correlation': corr if not np.isnan(corr) else None,
                                'p_value': p
                            }
                
                results['group_analysis'] = group_results
            
            # --- 7. Summary Statistics ---
            results['summary'] = {
                'total_observations': len(merged),
                'date_range': {
                    'start': merged['date'].min().strftime('%Y-%m-%d'),
                    'end': merged['date'].max().strftime('%Y-%m-%d')
                },
                'mean_vector_abundance': merged[vector_col].mean(),
                'mean_disease_cases': merged[disease_col].mean(),
                'max_vector_abundance': merged[vector_col].max(),
                'max_disease_cases': merged[disease_col].max()
            }
            
        except Exception as e:
            logger.error(f"Error in vector_disease_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= VECTOR OCCURRENCE ANALYSIS =========================
    
    @staticmethod
    def vector_occurrence_analysis(
        vector_df: pd.DataFrame,
        date_col: str = 'date',
        vector_col: str = 'count',
        group_cols: Optional[List[str]] = None,
        presence_threshold: float = 0.0
    ) -> Dict:
        """
        Analyze vector occurrence (presence/absence) patterns.
        
        Args:
            vector_df: DataFrame with vector observation data
            date_col: Name of date column
            vector_col: Name of vector abundance column
            group_cols: Columns to group by (e.g., species, location)
            presence_threshold: Minimum value to consider as present
            
        Returns:
            Dictionary with occurrence analysis results
        """
        results = {}
        
        if vector_df is None or vector_df.empty:
            return {'error': 'No vector data available'}
        
        try:
            vector_df = vector_df.copy()
            vector_df[date_col] = pd.to_datetime(vector_df[date_col])
            
            # Create presence/absence
            vector_df['present'] = vector_df[vector_col] > presence_threshold
            
            # Overall occurrence rate
            if group_cols:
                occurrence_by_group = vector_df.groupby(group_cols)['present'].agg(['mean', 'count']).reset_index()
                occurrence_by_group.columns = group_cols + ['occurrence_rate', 'total_observations']
                results['occurrence_by_group'] = occurrence_by_group.to_dict('records')
            else:
                results['overall_occurrence_rate'] = float(vector_df['present'].mean())
                results['total_observations'] = len(vector_df)
            
            # Seasonal occurrence
            vector_df['month'] = vector_df[date_col].dt.month
            vector_df['year'] = vector_df[date_col].dt.year
            
            monthly_occurrence = vector_df.groupby('month')['present'].mean().reset_index()
            results['monthly_occurrence'] = monthly_occurrence.to_dict('records')
            
            # Yearly occurrence trends
            if vector_df['year'].nunique() > 1:
                yearly_occurrence = vector_df.groupby('year')['present'].mean().reset_index()
                results['yearly_occurrence'] = yearly_occurrence.to_dict('records')
                
                # Trend analysis
                if len(yearly_occurrence) > 2:
                    slope, intercept, r_value, p_value, std_err = stats.linregress(
                        yearly_occurrence['year'],
                        yearly_occurrence['present']
                    )
                    results['occurrence_trend'] = {
                        'slope': slope,
                        'r_squared': r_value**2,
                        'p_value': p_value,
                        'direction': 'increasing' if slope > 0 else 'decreasing'
                    }
            
            # Occurrence by location
            if 'location_id' in vector_df.columns:
                location_occurrence = vector_df.groupby('location_id')['present'].agg(['mean', 'count']).reset_index()
                location_occurrence.columns = ['location_id', 'occurrence_rate', 'total_observations']
                results['location_occurrence'] = location_occurrence.to_dict('records')
            
            # Occurrence by species
            if 'species_name' in vector_df.columns:
                species_occurrence = vector_df.groupby('species_name')['present'].agg(['mean', 'count']).reset_index()
                species_occurrence.columns = ['species_name', 'occurrence_rate', 'total_observations']
                results['species_occurrence'] = species_occurrence.to_dict('records')
            
        except Exception as e:
            logger.error(f"Error in vector_occurrence_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= ABUNDANCE CLUSTERING =========================
    
    @staticmethod
    def vector_abundance_clustering(
        vector_df: pd.DataFrame,
        group_cols: List[str],
        date_col: str = 'date',
        vector_col: str = 'count',
        method: str = 'ward',
        n_clusters: int = 4
    ) -> Dict:
        """
        Cluster locations/species based on vector abundance patterns.
        
        Args:
            vector_df: DataFrame with vector data
            date_col: Name of date column
            vector_col: Name of vector abundance column
            group_cols: Columns to cluster by (e.g., ['location_id', 'species_name'])
            method: Clustering method ('ward', 'average', 'complete')
            n_clusters: Number of clusters to identify
            
        Returns:
            Dictionary with clustering results
        """
        results = {}
        
        if vector_df is None or vector_df.empty:
            return {'error': 'No vector data available'}
        
        if not group_cols:
            return {'error': 'Group columns required for clustering'}
        
        try:
            from sklearn.cluster import KMeans
            from sklearn.preprocessing import StandardScaler
            
            vector_df = vector_df.copy()
            vector_df[date_col] = pd.to_datetime(vector_df[date_col])
            vector_df['month'] = vector_df[date_col].dt.month
            
            # Create pivot table: groups as rows, months as columns
            pivot = vector_df.pivot_table(
                index=group_cols,
                columns='month',
                values=vector_col,
                aggfunc='mean',
                fillna=0
            )
            
            if pivot.empty:
                return {'error': 'Could not create pivot table for clustering'}
            
            # Cluster groups based on abundance patterns
            scaler = StandardScaler()
            scaled_data = scaler.fit_transform(pivot.values)
            
            # K-means clustering
            kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            clusters = kmeans.fit_predict(scaled_data)
            
            # Add cluster labels
            pivot['cluster'] = clusters
            results['cluster_assignments'] = pivot['cluster'].to_dict()
            results['cluster_centers'] = kmeans.cluster_centers_.tolist()
            
            # Cluster summary
            cluster_summary = pivot.groupby('cluster').size().reset_index(name='count')
            results['cluster_summary'] = cluster_summary.to_dict('records')
            
            # For each cluster, get the average monthly pattern
            cluster_patterns = pivot.groupby('cluster').mean().reset_index()
            results['cluster_patterns'] = cluster_patterns.to_dict('records')
            
            # Identify characteristic months for each cluster
            for cluster_id in range(n_clusters):
                pattern = cluster_patterns[cluster_patterns['cluster'] == cluster_id]
                if not pattern.empty:
                    monthly_values = pattern.iloc[0, 1:]  # Skip cluster column
                    peak_month = monthly_values.idxmax()
                    results[f'cluster_{cluster_id}_peak_month'] = int(peak_month)
            
        except Exception as e:
            logger.error(f"Error in vector_abundance_clustering: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= LAG CORRELATION =========================
    
    @staticmethod
    def lag_correlation_analysis(
        df: pd.DataFrame,
        date_col: str = 'date',
        response_col: str = 'case_count',
        predictor_cols: Optional[List[str]] = None,
        max_lags: int = 6,
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """Analyze lagged correlations between predictors and response."""
        results = {}
        
        if df is None or df.empty:
            return {'error': 'No data available for lag analysis'}
        
        if len(df) < 5:
            return {'error': f'Not enough data for lag analysis. Need at least 5 records, got {len(df)}'}
        
        try:
            if predictor_cols is None:
                predictor_cols = df.select_dtypes(include=[np.number]).columns.tolist()
                predictor_cols = [col for col in predictor_cols if col not in [date_col, response_col]]
            
            df = df.copy()
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.sort_values(date_col)
            
            df['month'] = df[date_col].dt.to_period('M')
            
            if df['month'].nunique() < 2:
                return {'error': f'Not enough months for lag analysis. Need at least 2 months, got {df["month"].nunique()}'}
            
            if group_cols:
                agg_dict = {response_col: 'sum'}
                for col in predictor_cols:
                    if col in df.columns:
                        agg_dict[col] = 'mean'
                monthly = df.groupby(group_cols + ['month']).agg(agg_dict).reset_index()
            else:
                agg_dict = {response_col: 'sum'}
                for col in predictor_cols:
                    if col in df.columns:
                        agg_dict[col] = 'mean'
                monthly = df.groupby('month').agg(agg_dict).reset_index()
            
            monthly['date'] = monthly['month'].dt.start_time
            
            lag_results = []
            
            for col in predictor_cols:
                if col not in monthly.columns:
                    continue
                if monthly[col].isna().all():
                    continue
                for lag in range(0, max_lags + 1):
                    shifted = monthly[col].shift(lag)
                    valid_idx = ~(shifted.isna() | monthly[response_col].isna())
                    
                    if valid_idx.sum() > 5:
                        try:
                            correlation, p_value = stats.pearsonr(
                                shifted[valid_idx],
                                monthly[response_col][valid_idx]
                            )
                            
                            if not np.isnan(correlation):
                                lag_results.append({
                                    'predictor': col,
                                    'lag_months': lag,
                                    'correlation': correlation,
                                    'p_value': p_value,
                                    'sample_size': valid_idx.sum()
                                })
                        except Exception as e:
                            logger.warning(f"Could not compute correlation for {col} at lag {lag}: {e}")
            
            results['correlations'] = pd.DataFrame(lag_results) if lag_results else pd.DataFrame()
            
            if not lag_results:
                results['optimal_lags'] = []
            else:
                try:
                    optimal = results['correlations'].groupby('predictor').apply(
                        lambda x: x.loc[x['correlation'].idxmax()] if not x.empty else None
                    )
                    if isinstance(optimal, pd.Series) and not optimal.empty:
                        results['optimal_lags'] = [optimal.to_dict()] if hasattr(optimal, 'to_dict') else []
                    elif isinstance(optimal, pd.DataFrame) and not optimal.empty:
                        results['optimal_lags'] = optimal.to_dict('records')
                    else:
                        results['optimal_lags'] = []
                except Exception as e:
                    logger.warning(f"Could not find optimal lags: {e}")
                    results['optimal_lags'] = []
            
            results['ccf'] = {}
            for col in predictor_cols:
                if col not in monthly.columns:
                    continue
                try:
                    response_series = monthly[response_col].fillna(0).values
                    predictor_series = monthly[col].fillna(0).values
                    if len(response_series) > max_lags + 1:
                        ccf_values = ccf(
                            response_series,
                            predictor_series,
                            adjusted=False
                        )
                        results['ccf'][col] = ccf_values[:max_lags+1]
                    else:
                        results['ccf'][col] = np.array([])
                except Exception as e:
                    logger.warning(f"Could not compute CCF for {col}: {e}")
                    results['ccf'][col] = np.array([])
            
        except Exception as e:
            logger.error(f"Error in lag_correlation_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= INTERVENTION IMPACT =========================
    
    @staticmethod
    def intervention_impact_analysis(
        cases_df: pd.DataFrame,
        interventions_df: pd.DataFrame,
        date_col: str = 'date',
        value_col: str = 'case_count',
        intervention_col: str = 'intervention_type',
        pre_period_months: int = 12,
        post_period_months: int = 12
    ) -> Dict:
        """Analyze intervention impact on disease outcomes."""
        results = {}
        
        if cases_df is None or cases_df.empty:
            return {'error': 'No case data for intervention analysis'}
        
        if interventions_df is None or interventions_df.empty:
            return {'error': 'No intervention data available'}
        
        if len(cases_df) < 3:
            return {'error': f'Not enough case data for intervention analysis. Need at least 3 records, got {len(cases_df)}'}
        
        try:
            cases_df = cases_df.copy()
            interventions_df = interventions_df.copy()
            
            cases_df[date_col] = pd.to_datetime(cases_df[date_col])
            interventions_df[date_col] = pd.to_datetime(interventions_df[date_col])
            
            cases_df['month'] = cases_df[date_col].dt.to_period('M')
            monthly_cases = cases_df.groupby('month')[value_col].sum().reset_index()
            monthly_cases['date'] = monthly_cases['month'].dt.start_time
            
            if len(monthly_cases) < 3:
                return {'error': f'Not enough monthly case data. Need at least 3 months, got {len(monthly_cases)}'}
            
            impact_results = []
            
            for idx, intervention in interventions_df.iterrows():
                start_date = intervention[date_col]
                
                pre_start = start_date - pd.DateOffset(months=pre_period_months)
                post_end = start_date + pd.DateOffset(months=post_period_months)
                
                pre_cases = monthly_cases[
                    (monthly_cases['date'] >= pre_start) &
                    (monthly_cases['date'] < start_date)
                ]
                post_cases = monthly_cases[
                    (monthly_cases['date'] >= start_date) &
                    (monthly_cases['date'] <= post_end)
                ]
                
                if len(pre_cases) > 0 and len(post_cases) > 0:
                    pre_mean = pre_cases[value_col].mean()
                    post_mean = post_cases[value_col].mean()
                    
                    t_stat, p_value = None, None
                    if len(pre_cases) > 2 and len(post_cases) > 2:
                        try:
                            t_stat, p_value = stats.ttest_ind(
                                pre_cases[value_col].values,
                                post_cases[value_col].values
                            )
                        except:
                            t_stat, p_value = None, None
                    
                    intervention_type = intervention.get(intervention_col, 'Unknown')
                    if pd.isna(intervention_type) or intervention_type == '':
                        intervention_type = f"Intervention_{idx}"
                    
                    impact_results.append({
                        'intervention_type': intervention_type,
                        'date': start_date,
                        'pre_mean': pre_mean,
                        'post_mean': post_mean,
                        'absolute_change': post_mean - pre_mean,
                        'relative_change': ((post_mean - pre_mean) / pre_mean) * 100 if pre_mean > 0 else None,
                        'p_value': p_value,
                        'pre_months': len(pre_cases),
                        'post_months': len(post_cases)
                    })
            
            results['impact'] = pd.DataFrame(impact_results) if impact_results else pd.DataFrame()
            
            if not impact_results:
                return {'error': 'No intervention impact could be calculated'}
            
            valid_abs_changes = [r['absolute_change'] for r in impact_results]
            valid_rel_changes = [r['relative_change'] for r in impact_results if r['relative_change'] is not None]
            
            results['summary'] = {
                'avg_absolute_change': np.mean(valid_abs_changes) if valid_abs_changes else 0,
                'avg_relative_change': np.mean(valid_rel_changes) if valid_rel_changes else 0,
                'significant_interventions': len([r for r in impact_results if r['p_value'] and r['p_value'] < 0.05])
            }
            
        except Exception as e:
            logger.error(f"Error in intervention_impact_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= RESISTANCE TRENDS =========================
    
    @staticmethod
    def resistance_trend_analysis(
        resistance_df: pd.DataFrame,
        date_col: str = 'date',
        status_col: str = 'resistance_status',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """Analyze trends in insecticide resistance."""
        results = {}
        
        if resistance_df is None or resistance_df.empty:
            return {'error': 'No resistance data available'}
        
        if len(resistance_df) < 3:
            return {'error': f'Not enough resistance data. Need at least 3 records, got {len(resistance_df)}'}
        
        try:
            resistance_df = resistance_df.copy()
            
            if date_col not in resistance_df.columns:
                return {'error': f"Date column '{date_col}' not found in resistance data"}
            
            if status_col not in resistance_df.columns:
                return {'error': f"Status column '{status_col}' not found in resistance data"}
            
            resistance_df[date_col] = pd.to_datetime(resistance_df[date_col])
            resistance_df['year'] = resistance_df[date_col].dt.year
            
            def classify_status(x):
                if pd.isna(x) or x is None:
                    return 'Unknown'
                x_str = str(x).lower()
                if x_str in ['resistant', 'high', 'resistance', 'r']:
                    return 'Resistant'
                elif x_str in ['susceptible', 'low', 's']:
                    return 'Susceptible'
                else:
                    return 'Unknown'
            
            resistance_df['status_category'] = resistance_df[status_col].apply(classify_status)
            
            group_cols = group_cols or []
            
            annual_status = resistance_df.groupby(['year'] + group_cols + ['status_category']).size().reset_index(name='count')
            
            if annual_status.empty:
                return {'error': 'No data after grouping'}
            
            total_by_year = annual_status.groupby(['year'] + group_cols)['count'].sum().reset_index(name='total')
            annual_status = annual_status.merge(total_by_year, on=['year'] + group_cols)
            annual_status['percentage'] = (annual_status['count'] / annual_status['total']) * 100
            
            annual_status = annual_status.drop_duplicates(subset=['year'] + group_cols + ['status_category'])
            
            results['annual_status'] = annual_status
            
            pivot = None
            
            if group_cols:
                trends = []
                for group_name, group_data in annual_status.groupby(group_cols):
                    pivot_data = group_data[['year', 'status_category', 'percentage']].copy()
                    pivot_data = pivot_data.drop_duplicates(subset=['year', 'status_category'])
                    
                    if pivot_data.empty:
                        continue
                    
                    try:
                        pivot_local = pivot_data.pivot(
                            index='year', 
                            columns='status_category', 
                            values='percentage'
                        ).fillna(0)
                        
                        if pivot_local.empty or pivot_local.isna().all().all():
                            continue
                        
                        if len(pivot_local) < 2:
                            continue
                        
                        if 'Resistant' in pivot_local.columns:
                            resistant_trend = pivot_local['Resistant'].values
                            years = pivot_local.index.values
                            if len(resistant_trend) > 2 and not np.all(resistant_trend == resistant_trend[0]):
                                try:
                                    slope, intercept, r_value, p_value, std_err = stats.linregress(years, resistant_trend)
                                    trends.append({
                                        'group': group_name if isinstance(group_name, tuple) else [group_name],
                                        'slope': slope,
                                        'r_squared': r_value**2,
                                        'p_value': p_value
                                    })
                                except Exception as e:
                                    logger.warning(f"Could not compute regression for group {group_name}: {e}")
                    except Exception as e:
                        logger.warning(f"Could not compute trend for group {group_name}: {e}")
                        continue
                
                results['trends'] = pd.DataFrame(trends) if trends else pd.DataFrame()
                
                if annual_status['year'].nunique() > 1:
                    try:
                        pivot_data = annual_status[['year', 'status_category', 'percentage']].copy()
                        pivot_data = pivot_data.drop_duplicates(subset=['year', 'status_category'])
                        
                        if not pivot_data.empty and pivot_data['year'].nunique() > 1:
                            pivot = pivot_data.pivot(
                                index='year', 
                                columns='status_category', 
                                values='percentage'
                            ).fillna(0)
                    except Exception as e:
                        logger.warning(f"Could not create combined pivot: {e}")
            
            else:
                pivot_data = annual_status[['year', 'status_category', 'percentage']].copy()
                pivot_data = pivot_data.drop_duplicates(subset=['year', 'status_category'])
                
                if not pivot_data.empty and pivot_data['year'].nunique() > 1:
                    pivot = pivot_data.pivot(
                        index='year', 
                        columns='status_category', 
                        values='percentage'
                    ).fillna(0)
                    
                    results['pivot'] = pivot
                    
                    if 'Resistant' in pivot.columns and len(pivot) > 1:
                        resistant_trend = pivot['Resistant'].values
                        years = pivot.index.values
                        if len(resistant_trend) > 2 and not np.all(resistant_trend == resistant_trend[0]):
                            try:
                                slope, intercept, r_value, p_value, std_err = stats.linregress(years, resistant_trend)
                                results['overall_trend'] = {
                                    'slope': slope,
                                    'r_squared': r_value**2,
                                    'p_value': p_value,
                                    'direction': 'increasing' if slope > 0 else 'decreasing'
                                }
                            except Exception as e:
                                logger.warning(f"Could not compute overall trend: {e}")
                else:
                    results['pivot'] = pd.DataFrame()
                    
        except Exception as e:
            logger.warning(f"Could not compute trend: {e}")
            results['trend_error'] = str(e)
        
        if pivot is None:
            pivot = pd.DataFrame()
        
        if 'pivot' not in results:
            results['pivot'] = pivot
        
        return results
    
    # ========================= INTEGRATED RISK MODEL =========================
    
    @staticmethod
    def integrated_risk_model(
        cases_df: pd.DataFrame,
        env_df: pd.DataFrame,
        vector_df: Optional[pd.DataFrame] = None,
        date_col: str = 'date',
        response_col: str = 'case_count',
        predictor_cols: Optional[List[str]] = None,
        test_size: float = 0.2
    ) -> Dict:
        """Build an integrated risk model combining multiple data sources."""
        try:
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.preprocessing import StandardScaler
            from sklearn.metrics import r2_score, mean_squared_error
            from sklearn.model_selection import train_test_split
        except ImportError:
            return {'error': 'scikit-learn not installed. Please install with: pip install scikit-learn'}
        
        results = {}
        
        if cases_df is None or cases_df.empty:
            return {'error': 'No case data available for risk modeling'}
        
        if env_df is None or env_df.empty:
            return {'error': 'No environmental data available for risk modeling'}
        
        if len(cases_df) < 10:
            return {'error': f'Not enough case data for risk modeling. Need at least 10 records, got {len(cases_df)}'}
        
        try:
            cases_df = cases_df.copy()
            env_df = env_df.copy()
            
            cases_df[date_col] = pd.to_datetime(cases_df[date_col])
            env_df[date_col] = pd.to_datetime(env_df[date_col])
            
            env_cols = [col for col in env_df.columns if col not in ['date', 'year', 'month']]
            case_cols = [col for col in cases_df.columns if col not in ['date']]
            
            env_clean = env_df[['date'] + env_cols].copy() if env_cols else env_df[['date']].copy()
            cases_clean = cases_df[['date'] + case_cols].copy() if case_cols else cases_df[['date']].copy()
            
            merged = cases_clean.merge(env_clean, on='date', how='inner')
            
            if vector_df is not None and not vector_df.empty:
                vector_df = vector_df.copy()
                vector_df[date_col] = pd.to_datetime(vector_df[date_col])
                if 'count' in vector_df.columns:
                    vector_agg = vector_df.groupby(date_col)['count'].sum().reset_index()
                elif 'density_per_trap' in vector_df.columns:
                    vector_agg = vector_df.groupby(date_col)['density_per_trap'].mean().reset_index()
                else:
                    vector_agg = vector_df.groupby(date_col).size().reset_index(name='vector_count')
                merged = merged.merge(vector_agg, on=date_col, how='left')
                for col in vector_agg.columns:
                    if col != date_col and col in merged.columns:
                        merged[col] = merged[col].fillna(0)
            
            if merged.empty:
                return {'error': 'No overlapping data for risk modeling'}
            
            if predictor_cols is None:
                predictor_cols = [col for col in merged.columns 
                                if col not in [date_col, response_col, 'year', 'month']]
                predictor_cols = [col for col in predictor_cols 
                                if col in merged.columns and pd.api.types.is_numeric_dtype(merged[col])]
            
            available_predictors = [col for col in predictor_cols if col in merged.columns]
            
            if not available_predictors:
                return {'error': 'No valid predictor columns available'}
            
            X = merged[available_predictors].copy()
            y = merged[response_col].copy()
            
            X = X.fillna(X.mean())
            y = y.fillna(y.mean())
            
            if len(X) < 10:
                return {'error': f'Not enough data for modeling. Need at least 10 samples, got {len(X)}'}
            
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_size, random_state=42
            )
            
            scaler = StandardScaler()
            X_train_scaled = scaler.fit_transform(X_train)
            X_test_scaled = scaler.transform(X_test)
            
            rf = RandomForestRegressor(
                n_estimators=100,
                max_depth=10,
                random_state=42,
                n_jobs=-1
            )
            rf.fit(X_train_scaled, y_train)
            
            y_pred = rf.predict(X_test_scaled)
            r2 = r2_score(y_test, y_pred)
            rmse = np.sqrt(mean_squared_error(y_test, y_pred))
            
            results['model'] = rf
            results['scaler'] = scaler
            results['feature_importance'] = dict(zip(available_predictors, rf.feature_importances_))
            results['r2_score'] = r2
            results['rmse'] = rmse
            results['predictions'] = {
                'actual': y_test.values,
                'predicted': y_pred
            }
            
            try:
                y_threshold = np.percentile(y, 75)
                y_test_binary = (y_test > y_threshold).astype(int)
                y_pred_binary = (y_pred > y_threshold).astype(int)
                
                from sklearn.metrics import accuracy_score, classification_report
                results['classification'] = {
                    'accuracy': accuracy_score(y_test_binary, y_pred_binary),
                    'threshold': y_threshold,
                    'report': classification_report(y_test_binary, y_pred_binary, output_dict=True)
                }
            except Exception as e:
                logger.warning(f"Could not compute classification metrics: {e}")
                results['classification'] = {
                    'accuracy': 0,
                    'threshold': 0,
                    'report': {}
                }
            
        except Exception as e:
            logger.error(f"Error in integrated_risk_model: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= SPATIOTEMPORAL ANALYSIS =========================
    
    @staticmethod
    def spatiotemporal_analysis(
        df: pd.DataFrame,
        location_col: str = 'location_id',
        date_col: str = 'date',
        value_col: str = 'case_count',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """Analyze spatiotemporal patterns in the data."""
        results = {}
        
        if df is None or df.empty:
            return {'error': 'No data available for spatiotemporal analysis'}
        
        if df[location_col].nunique() < 2:
            return {'error': f'Not enough locations for spatiotemporal analysis. Need at least 2 locations, got {df[location_col].nunique()}'}
        
        if df[date_col].nunique() < 2:
            return {'error': f'Not enough time points for spatiotemporal analysis. Need at least 2 dates, got {df[date_col].nunique()}'}
        
        try:
            df = df.copy()
            
            df[date_col] = pd.to_datetime(df[date_col])
            df['year'] = df[date_col].dt.year
            df['month'] = df[date_col].dt.month
            df['season'] = df['month'].apply(lambda x: 'Dry' if x in [11, 12, 1, 2, 3] else 'Wet')
            
            location_agg = df.groupby([location_col])[value_col].agg(['mean', 'std', 'sum', 'count']).reset_index()
            location_agg.columns = [location_col, 'mean_value', 'std_value', 'sum_value', 'count_value']
            
            location_agg = location_agg.sort_values('mean_value', ascending=False)
            results['location_summary'] = location_agg
            
            heatmap_data = df.groupby([location_col, 'month'])[value_col].mean().reset_index()
            heatmap_pivot = heatmap_data.pivot(index=location_col, columns='month', values=value_col).fillna(0)
            results['heatmap_data'] = heatmap_pivot
            
            seasonal_by_location = df.groupby([location_col, 'season'])[value_col].mean().reset_index()
            seasonal_pivot = seasonal_by_location.pivot(index=location_col, columns='season', values=value_col).fillna(0)
            results['seasonal_by_location'] = seasonal_pivot
            
            yoy_by_location = df.groupby([location_col, 'year'])[value_col].sum().reset_index()
            yoy_pivot = yoy_by_location.pivot(index=location_col, columns='year', values=value_col).fillna(0)
            results['yoy_by_location'] = yoy_pivot
            
        except Exception as e:
            logger.error(f"Error in spatiotemporal_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= CORRELATION MATRIX =========================
    
    @staticmethod
    def correlation_matrix_analysis(
        df: pd.DataFrame,
        columns: Optional[List[str]] = None,
        method: str = 'pearson'
    ) -> Dict:
        """Compute and analyze correlation matrix."""
        results = {}
        
        if df is None or df.empty:
            return {'error': 'No data available for correlation analysis'}
        
        if len(df) < 3:
            return {'error': f'Not enough data for correlation analysis. Need at least 3 records, got {len(df)}'}
        
        try:
            df = df.copy()
            
            if columns is None:
                columns = df.select_dtypes(include=[np.number]).columns.tolist()
            else:
                columns = [col for col in columns if col in df.columns]
            
            if len(columns) < 2:
                return {'error': 'Need at least 2 columns for correlation analysis'}
            
            corr_matrix = df[columns].corr(method=method)
            results['correlation_matrix'] = corr_matrix
            
            corr_unstacked = corr_matrix.unstack()
            corr_unstacked = corr_unstacked[corr_unstacked < 1]
            corr_unstacked = corr_unstacked.sort_values(ascending=False)
            
            top_positive = corr_unstacked.head(10)
            results['top_positive_correlations'] = top_positive.to_dict()
            
            top_negative = corr_unstacked.tail(10)
            results['top_negative_correlations'] = top_negative.to_dict()
            
            results['summary'] = {
                'num_columns': len(columns),
                'num_pairs': len(corr_unstacked),
                'mean_correlation': corr_unstacked.mean() if not corr_unstacked.empty else 0,
                'std_correlation': corr_unstacked.std() if not corr_unstacked.empty else 0
            }
            
        except Exception as e:
            logger.error(f"Error in correlation_matrix_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    # ========================= DATA SUMMARY =========================
    
    @staticmethod
    def summarize_data(
        df: pd.DataFrame,
        date_col: str = 'date',
        value_col: str = 'case_count',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """Generate a summary of the data."""
        results = {}
        
        if df is None or df.empty:
            return {'error': 'No data available for summary'}
        
        try:
            df = df.copy()
            df[date_col] = pd.to_datetime(df[date_col])
            
            results['total_records'] = len(df)
            results['date_range'] = {
                'start': df[date_col].min().strftime('%Y-%m-%d'),
                'end': df[date_col].max().strftime('%Y-%m-%d')
            }
            
            if value_col in df.columns:
                results['value_summary'] = {
                    'mean': df[value_col].mean(),
                    'std': df[value_col].std(),
                    'min': df[value_col].min(),
                    'max': df[value_col].max(),
                    'sum': df[value_col].sum()
                }
            
            if group_cols:
                for col in group_cols:
                    if col in df.columns:
                        results[f'{col}_distribution'] = df[col].value_counts().to_dict()
            
        except Exception as e:
            logger.error(f"Error in summarize_data: {e}")
            return {'error': str(e)}
        
        return results