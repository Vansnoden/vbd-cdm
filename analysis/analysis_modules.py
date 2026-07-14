# analysis/analysis_modules.py
"""
Modular analysis components for CDM-based studies.

This module provides reusable analysis functions that can be combined
in different pipelines.
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
import warnings
warnings.filterwarnings('ignore')
import logging


logger = logging.getLogger(__name__)


class AnalysisModules:
    """
    Collection of analysis functions for CDM data.
    """
    
    @staticmethod
    def seasonal_patterns(
        df: pd.DataFrame,
        date_col: str = 'date',
        value_col: str = 'case_count',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """
        Analyze seasonal patterns in time series data.
        
        Args:
            df: DataFrame with date and value columns
            date_col: Name of date column
            value_col: Name of value column
            group_cols: Optional columns to group by
            
        Returns:
            Dictionary with seasonal analysis results
        """
        results = {}
        
        # Check if dataframe is empty
        if df is None or df.empty:
            return {'error': 'No data available for seasonal analysis'}
        
        # Need at least 2 data points for meaningful analysis
        if len(df) < 2:
            return {'error': f'Not enough data for seasonal analysis. Need at least 2 records, got {len(df)}'}
        
        try:
            # Ensure date is datetime
            df = df.copy()
            df[date_col] = pd.to_datetime(df[date_col])
            
            # Group by month
            df['month'] = df[date_col].dt.month
            df['month_name'] = df['month'].apply(lambda x: pd.to_datetime(f'2000-{x:02d}-01').strftime('%b'))
            df['year'] = df[date_col].dt.year
            df['week'] = df[date_col].dt.isocalendar().week
            
            # Monthly averages - only if we have enough data
            if len(df) >= 3:
                if group_cols:
                    monthly_agg = df.groupby(group_cols + ['month', 'month_name'])[value_col].agg(['mean', 'std', 'count']).reset_index()
                    results['monthly_by_group'] = monthly_agg
                else:
                    monthly_agg = df.groupby(['month', 'month_name'])[value_col].agg(['mean', 'std', 'count']).reset_index()
                    results['monthly'] = monthly_agg
            else:
                # With very few points, just show raw values
                monthly_agg = df.groupby(['month', 'month_name'])[value_col].agg(['mean', 'count']).reset_index()
                monthly_agg['std'] = np.nan
                results['monthly'] = monthly_agg
            
            # Year-over-year comparison - only if we have multiple years
            if df['year'].nunique() > 1:
                yoy = df.groupby(['year', 'month'])[value_col].mean().reset_index()
                results['year_over_year'] = yoy
            else:
                results['year_over_year'] = pd.DataFrame()
            
            # Seasonal decomposition if enough data (at least 24 months)
            if len(df) >= 24:
                try:
                    ts = df.set_index(date_col)[value_col].sort_index()
                    # Handle missing values by interpolation
                    ts = ts.interpolate(method='time')
                    if len(ts) >= 24:
                        decomposition = seasonal_decompose(ts, model='additive', period=12)
                        results['decomposition'] = decomposition
                except Exception as e:
                    logger.warning(f"Could not perform seasonal decomposition: {e}")
            
            # Peak identification - only if we have enough data
            if not monthly_agg.empty and len(monthly_agg) >= 3:
                peak_months = monthly_agg.nlargest(3, 'mean')
                results['peak_months'] = peak_months
            elif not monthly_agg.empty:
                # With few data points, just return the top month
                peak_months = monthly_agg.nlargest(1, 'mean')
                results['peak_months'] = peak_months
            else:
                results['peak_months'] = pd.DataFrame()
            
        except Exception as e:
            logger.error(f"Error in seasonal_patterns: {e}")
            return {'error': str(e)}
        
        return results
    
    @staticmethod
    def lag_correlation_analysis(
        df: pd.DataFrame,
        date_col: str = 'date',
        response_col: str = 'case_count',
        predictor_cols: Optional[List[str]] = None,
        max_lags: int = 6,
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """
        Analyze lagged correlations between predictors and response.
        
        Args:
            df: DataFrame with time series data
            date_col: Name of date column
            response_col: Name of response variable
            predictor_cols: List of predictor variable names
            max_lags: Maximum number of lags to test
            group_cols: Optional columns to group by
            
        Returns:
            Dictionary with lag analysis results
        """
        results = {}
        
        # Check if dataframe is empty
        if df is None or df.empty:
            return {'error': 'No data available for lag analysis'}
        
        # Need at least 5 data points for meaningful correlation
        if len(df) < 5:
            return {'error': f'Not enough data for lag analysis. Need at least 5 records, got {len(df)}'}
        
        try:
            if predictor_cols is None:
                # Automatically detect numeric columns
                predictor_cols = df.select_dtypes(include=[np.number]).columns.tolist()
                predictor_cols = [col for col in predictor_cols if col not in [date_col, response_col]]
            
            # Ensure date is datetime and set as index
            df = df.copy()
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.sort_values(date_col)
            
            # Aggregate to monthly if needed
            df['month'] = df[date_col].dt.to_period('M')
            
            # Ensure we have at least 2 months for aggregation
            if df['month'].nunique() < 2:
                return {'error': f'Not enough months for lag analysis. Need at least 2 months, got {df["month"].nunique()}'}
            
            if group_cols:
                # Build aggregation dictionary
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
                # Ensure we have non-null data
                if monthly[col].isna().all():
                    continue
                for lag in range(0, max_lags + 1):
                    # Shift predictor
                    shifted = monthly[col].shift(lag)
                    valid_idx = ~(shifted.isna() | monthly[response_col].isna())
                    
                    if valid_idx.sum() > 5:  # Need enough data points
                        try:
                            correlation, p_value = stats.pearsonr(
                                shifted[valid_idx],
                                monthly[response_col][valid_idx]
                            )
                            
                            # Check for valid correlation (not NaN)
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
            
            # Find optimal lags
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
            
            # Cross-correlation function
            results['ccf'] = {}
            for col in predictor_cols:
                if col not in monthly.columns:
                    continue
                try:
                    # Fill NaN values for CCF
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
        """
        Analyze the impact of interventions on disease outcomes.
        
        Args:
            cases_df: DataFrame with case data
            interventions_df: DataFrame with intervention data
            date_col: Name of date column
            value_col: Name of value column
            intervention_col: Name of intervention column
            pre_period_months: Months before intervention to include
            post_period_months: Months after intervention to include
            
        Returns:
            Dictionary with intervention impact results
        """
        results = {}
        
        if cases_df is None or cases_df.empty:
            return {'error': 'No case data for intervention analysis'}
        
        if interventions_df is None or interventions_df.empty:
            return {'error': 'No intervention data available'}
        
        # Need at least 3 cases records for meaningful analysis
        if len(cases_df) < 3:
            return {'error': f'Not enough case data for intervention analysis. Need at least 3 records, got {len(cases_df)}'}
        
        try:
            # Make copies to avoid modifying originals
            cases_df = cases_df.copy()
            interventions_df = interventions_df.copy()
            
            cases_df[date_col] = pd.to_datetime(cases_df[date_col])
            interventions_df[date_col] = pd.to_datetime(interventions_df[date_col])
            
            # Aggregate cases to monthly
            cases_df['month'] = cases_df[date_col].dt.to_period('M')
            monthly_cases = cases_df.groupby('month')[value_col].sum().reset_index()
            monthly_cases['date'] = monthly_cases['month'].dt.start_time
            
            # Ensure we have enough monthly data
            if len(monthly_cases) < 3:
                return {'error': f'Not enough monthly case data. Need at least 3 months, got {len(monthly_cases)}'}
            
            impact_results = []
            
            for idx, intervention in interventions_df.iterrows():
                start_date = intervention[date_col]
                
                # Define pre and post periods
                pre_start = start_date - pd.DateOffset(months=pre_period_months)
                post_end = start_date + pd.DateOffset(months=post_period_months)
                
                # Filter cases
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
                    
                    # Statistical test - only if we have enough data points
                    t_stat, p_value = None, None
                    if len(pre_cases) > 2 and len(post_cases) > 2:
                        try:
                            t_stat, p_value = stats.ttest_ind(
                                pre_cases[value_col].values,
                                post_cases[value_col].values
                            )
                        except:
                            t_stat, p_value = None, None
                    
                    # Get intervention type with fallback
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
            
            # Summary statistics
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
    
    @staticmethod
    def resistance_trend_analysis(
        resistance_df: pd.DataFrame,
        date_col: str = 'date',
        status_col: str = 'resistance_status',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """
        Analyze trends in insecticide resistance.
        
        Args:
            resistance_df: DataFrame with resistance data
            date_col: Name of date column
            status_col: Name of resistance status column
            group_cols: Optional columns to group by (e.g., ['species_name'])
            
        Returns:
            Dictionary with resistance trend analysis results
        """
        results = {}
        
        if resistance_df is None or resistance_df.empty:
            return {'error': 'No resistance data available'}
        
        # Need at least 3 records for meaningful analysis
        if len(resistance_df) < 3:
            return {'error': f'Not enough resistance data. Need at least 3 records, got {len(resistance_df)}'}
        
        try:
            # Make a copy to avoid modifying original
            resistance_df = resistance_df.copy()
            
            # Ensure date column exists
            if date_col not in resistance_df.columns:
                return {'error': f"Date column '{date_col}' not found in resistance data"}
            
            # Ensure status column exists
            if status_col not in resistance_df.columns:
                return {'error': f"Status column '{status_col}' not found in resistance data"}
            
            resistance_df[date_col] = pd.to_datetime(resistance_df[date_col])
            resistance_df['year'] = resistance_df[date_col].dt.year
            
            # Classify resistance status
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
            
            # Group by year and optional group columns
            group_cols = group_cols or []
            
            # Add status categories for annual analysis
            annual_status = resistance_df.groupby(['year'] + group_cols + ['status_category']).size().reset_index(name='count')
            
            # Handle case where we have group columns but no data
            if annual_status.empty:
                return {'error': 'No data after grouping'}
            
            # Calculate percentages
            total_by_year = annual_status.groupby(['year'] + group_cols)['count'].sum().reset_index(name='total')
            annual_status = annual_status.merge(total_by_year, on=['year'] + group_cols)
            annual_status['percentage'] = (annual_status['count'] / annual_status['total']) * 100
            
            # Remove duplicates to ensure clean data for pivoting
            annual_status = annual_status.drop_duplicates(subset=['year'] + group_cols + ['status_category'])
            
            results['annual_status'] = annual_status
            
            # Trend analysis - handle duplicates in pivot
            pivot = None
            
            if group_cols:
                # Handle each group separately
                trends = []
                for group_name, group_data in annual_status.groupby(group_cols):
                    # Remove duplicate year-status combinations
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
                        
                        # Skip if all values are 0
                        if pivot_local.empty or pivot_local.isna().all().all():
                            continue
                        
                        # Skip if only one year of data
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
                
                # Also create a combined pivot for visualization
                if annual_status['year'].nunique() > 1:
                    try:
                        # Create a simple combined pivot
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
                # Overall trend - remove duplicates
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
        
        # If pivot is None, create empty DataFrame
        if pivot is None:
            pivot = pd.DataFrame()
        
        # Store pivot for visualization
        if 'pivot' not in results:
            results['pivot'] = pivot
        
        return results
    
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
        """
        Build an integrated risk model combining multiple data sources.
        
        Args:
            cases_df: DataFrame with case data
            env_df: DataFrame with environmental data
            vector_df: Optional DataFrame with vector data
            date_col: Name of date column
            response_col: Name of response variable
            predictor_cols: List of predictor variable names
            test_size: Proportion of data to use for testing
            
        Returns:
            Dictionary with risk model results
        """
        try:
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.preprocessing import StandardScaler
            from sklearn.metrics import r2_score, mean_squared_error
            from sklearn.model_selection import train_test_split
        except ImportError:
            return {'error': 'scikit-learn not installed. Please install with: pip install scikit-learn'}
        
        results = {}
        
        # Check if data is available
        if cases_df is None or cases_df.empty:
            return {'error': 'No case data available for risk modeling'}
        
        if env_df is None or env_df.empty:
            return {'error': 'No environmental data available for risk modeling'}
        
        # Need at least 10 data points for modeling
        if len(cases_df) < 10:
            return {'error': f'Not enough case data for risk modeling. Need at least 10 records, got {len(cases_df)}'}
        
        try:
            # Make copies to avoid modifying originals
            cases_df = cases_df.copy()
            env_df = env_df.copy()
            
            # Ensure date is datetime
            cases_df[date_col] = pd.to_datetime(cases_df[date_col])
            env_df[date_col] = pd.to_datetime(env_df[date_col])
            
            # Drop duplicate columns before merging
            # Keep only date as common column
            env_cols = [col for col in env_df.columns if col not in ['date', 'year', 'month']]
            case_cols = [col for col in cases_df.columns if col not in ['date']]
            
            env_clean = env_df[['date'] + env_cols].copy() if env_cols else env_df[['date']].copy()
            cases_clean = cases_df[['date'] + case_cols].copy() if case_cols else cases_df[['date']].copy()
            
            # Merge data
            merged = cases_clean.merge(env_clean, on='date', how='inner')
            
            if vector_df is not None and not vector_df.empty:
                vector_df = vector_df.copy()
                vector_df[date_col] = pd.to_datetime(vector_df[date_col])
                # Aggregate vector data by date
                if 'count' in vector_df.columns:
                    vector_agg = vector_df.groupby(date_col)['count'].sum().reset_index()
                elif 'density_per_trap' in vector_df.columns:
                    vector_agg = vector_df.groupby(date_col)['density_per_trap'].mean().reset_index()
                else:
                    vector_agg = vector_df.groupby(date_col).size().reset_index(name='vector_count')
                merged = merged.merge(vector_agg, on=date_col, how='left')
                # Fill missing values
                for col in vector_agg.columns:
                    if col != date_col and col in merged.columns:
                        merged[col] = merged[col].fillna(0)
            
            if merged.empty:
                return {'error': 'No overlapping data for risk modeling'}
            
            # Prepare features
            if predictor_cols is None:
                predictor_cols = [col for col in merged.columns 
                                if col not in [date_col, response_col, 'year', 'month']]
                # Filter to only numeric columns
                predictor_cols = [col for col in predictor_cols 
                                if col in merged.columns and pd.api.types.is_numeric_dtype(merged[col])]
            
            # Filter to only available columns
            available_predictors = [col for col in predictor_cols if col in merged.columns]
            
            if not available_predictors:
                return {'error': 'No valid predictor columns available'}
            
            X = merged[available_predictors].copy()
            y = merged[response_col].copy()
            
            # Handle missing values
            X = X.fillna(X.mean())
            y = y.fillna(y.mean())
            
            # Check if we have enough data
            if len(X) < 10:
                return {'error': f'Not enough data for modeling. Need at least 10 samples, got {len(X)}'}
            
            # Split data
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_size, random_state=42
            )
            
            # Scale features
            scaler = StandardScaler()
            X_train_scaled = scaler.fit_transform(X_train)
            X_test_scaled = scaler.transform(X_test)
            
            # Train Random Forest
            rf = RandomForestRegressor(
                n_estimators=100,
                max_depth=10,
                random_state=42,
                n_jobs=-1
            )
            rf.fit(X_train_scaled, y_train)
            
            # Evaluate
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
            
            # Risk threshold
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
    
    @staticmethod
    def spatiotemporal_analysis(
        df: pd.DataFrame,
        location_col: str = 'location_id',
        date_col: str = 'date',
        value_col: str = 'case_count',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """
        Analyze spatiotemporal patterns in the data.
        
        Args:
            df: DataFrame with location, date, and value columns
            location_col: Name of location column
            date_col: Name of date column
            value_col: Name of value column
            group_cols: Optional columns to group by
            
        Returns:
            Dictionary with spatiotemporal analysis results
        """
        results = {}
        
        if df is None or df.empty:
            return {'error': 'No data available for spatiotemporal analysis'}
        
        # Need at least 2 locations and 2 time points for meaningful analysis
        if df[location_col].nunique() < 2:
            return {'error': f'Not enough locations for spatiotemporal analysis. Need at least 2 locations, got {df[location_col].nunique()}'}
        
        if df[date_col].nunique() < 2:
            return {'error': f'Not enough time points for spatiotemporal analysis. Need at least 2 dates, got {df[date_col].nunique()}'}
        
        try:
            # Make a copy to avoid modifying original
            df = df.copy()
            
            df[date_col] = pd.to_datetime(df[date_col])
            df['year'] = df[date_col].dt.year
            df['month'] = df[date_col].dt.month
            df['season'] = df['month'].apply(lambda x: 'Dry' if x in [11, 12, 1, 2, 3] else 'Wet')
            
            # Spatial patterns by location
            location_agg = df.groupby([location_col])[value_col].agg(['mean', 'std', 'sum', 'count']).reset_index()
            location_agg.columns = [location_col, 'mean_value', 'std_value', 'sum_value', 'count_value']
            
            # Sort by mean value
            location_agg = location_agg.sort_values('mean_value', ascending=False)
            results['location_summary'] = location_agg
            
            # Spatiotemporal heatmap data
            heatmap_data = df.groupby([location_col, 'month'])[value_col].mean().reset_index()
            heatmap_pivot = heatmap_data.pivot(index=location_col, columns='month', values=value_col).fillna(0)
            results['heatmap_data'] = heatmap_pivot
            
            # Seasonal patterns by location
            seasonal_by_location = df.groupby([location_col, 'season'])[value_col].mean().reset_index()
            seasonal_pivot = seasonal_by_location.pivot(index=location_col, columns='season', values=value_col).fillna(0)
            results['seasonal_by_location'] = seasonal_pivot
            
            # Year-over-year trends by location
            yoy_by_location = df.groupby([location_col, 'year'])[value_col].sum().reset_index()
            yoy_pivot = yoy_by_location.pivot(index=location_col, columns='year', values=value_col).fillna(0)
            results['yoy_by_location'] = yoy_pivot
            
        except Exception as e:
            logger.error(f"Error in spatiotemporal_analysis: {e}")
            return {'error': str(e)}
        
        return results
    
    @staticmethod
    def correlation_matrix_analysis(
        df: pd.DataFrame,
        columns: Optional[List[str]] = None,
        method: str = 'pearson'
    ) -> Dict:
        """
        Compute and analyze correlation matrix.
        
        Args:
            df: DataFrame with numeric columns
            columns: Optional list of columns to include
            method: Correlation method ('pearson', 'spearman', 'kendall')
            
        Returns:
            Dictionary with correlation matrix and summary
        """
        results = {}
        
        if df is None or df.empty:
            return {'error': 'No data available for correlation analysis'}
        
        # Need at least 3 rows for meaningful correlation
        if len(df) < 3:
            return {'error': f'Not enough data for correlation analysis. Need at least 3 records, got {len(df)}'}
        
        try:
            # Make a copy to avoid modifying original
            df = df.copy()
            
            # Select columns
            if columns is None:
                columns = df.select_dtypes(include=[np.number]).columns.tolist()
            else:
                columns = [col for col in columns if col in df.columns]
            
            if len(columns) < 2:
                return {'error': 'Need at least 2 columns for correlation analysis'}
            
            # Compute correlation matrix
            corr_matrix = df[columns].corr(method=method)
            results['correlation_matrix'] = corr_matrix
            
            # Get top correlations
            # Remove self-correlations
            corr_unstacked = corr_matrix.unstack()
            corr_unstacked = corr_unstacked[corr_unstacked < 1]  # Remove self-correlations
            corr_unstacked = corr_unstacked.sort_values(ascending=False)
            
            # Top positive correlations
            top_positive = corr_unstacked.head(10)
            results['top_positive_correlations'] = top_positive.to_dict()
            
            # Top negative correlations
            top_negative = corr_unstacked.tail(10)
            results['top_negative_correlations'] = top_negative.to_dict()
            
            # Summary statistics
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
    
    @staticmethod
    def summarize_data(
        df: pd.DataFrame,
        date_col: str = 'date',
        value_col: str = 'case_count',
        group_cols: Optional[List[str]] = None
    ) -> Dict:
        """
        Generate a summary of the data.
        
        Args:
            df: DataFrame with data
            date_col: Name of date column
            value_col: Name of value column
            group_cols: Optional columns to group by
            
        Returns:
            Dictionary with data summary
        """
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