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
from typing import Dict, List, Tuple, Optional
from scipy import stats
from scipy.ndimage import gaussian_filter1d
from statsmodels.tsa.seasonal import seasonal_decompose
from statsmodels.tsa.stattools import acf, pacf, ccf
from statsmodels.graphics.tsaplots import plot_acf, plot_pacf
import warnings
warnings.filterwarnings('ignore')


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
        
        # Ensure date is datetime
        df[date_col] = pd.to_datetime(df[date_col])
        
        # Group by month
        df['month'] = df[date_col].dt.month
        df['month_name'] = df['month'].apply(lambda x: pd.to_datetime(f'2000-{x:02d}-01').strftime('%b'))
        df['year'] = df[date_col].dt.year
        df['week'] = df[date_col].dt.isocalendar().week
        
        # Monthly averages
        if group_cols:
            monthly_agg = df.groupby(group_cols + ['month', 'month_name'])[value_col].agg(['mean', 'std', 'count']).reset_index()
            results['monthly_by_group'] = monthly_agg
        else:
            monthly_agg = df.groupby(['month', 'month_name'])[value_col].agg(['mean', 'std', 'count']).reset_index()
            results['monthly'] = monthly_agg
        
        # Year-over-year comparison
        yoy = df.groupby(['year', 'month'])[value_col].mean().reset_index()
        results['year_over_year'] = yoy
        
        # Seasonal decomposition if enough data
        if len(df) >= 24:  # At least 2 years of data
            try:
                ts = df.set_index(date_col)[value_col].sort_index()
                decomposition = seasonal_decompose(ts, model='multiplicative', period=12)
                results['decomposition'] = decomposition
            except Exception as e:
                print(f"Could not perform seasonal decomposition: {e}")
        
        # Peak identification
        peak_months = monthly_agg.nlargest(3, 'mean')
        results['peak_months'] = peak_months
        
        return results
    
    @staticmethod
    def lag_correlation_analysis(
        df: pd.DataFrame,
        date_col: str = 'date',
        response_col: str = 'case_count',
        predictor_cols: List[str] = None,
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
        
        if predictor_cols is None:
            # Automatically detect numeric columns
            predictor_cols = df.select_dtypes(include=[np.number]).columns.tolist()
            predictor_cols = [col for col in predictor_cols if col not in [date_col, response_col]]
        
        # Ensure date is datetime and set as index
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.sort_values(date_col)
        
        # Aggregate to monthly if needed
        df['month'] = df[date_col].dt.to_period('M')
        
        if group_cols:
            monthly = df.groupby(group_cols + ['month']).agg({
                response_col: 'sum',
                **{col: 'mean' for col in predictor_cols}
            }).reset_index()
        else:
            monthly = df.groupby('month').agg({
                response_col: 'sum',
                **{col: 'mean' for col in predictor_cols}
            }).reset_index()
        
        monthly['date'] = monthly['month'].dt.start_time
        
        lag_results = []
        
        for col in predictor_cols:
            for lag in range(0, max_lags + 1):
                # Shift predictor
                shifted = monthly[col].shift(lag)
                valid_idx = ~(shifted.isna() | monthly[response_col].isna())
                
                if valid_idx.sum() > 5:  # Need enough data points
                    correlation, p_value = stats.pearsonr(
                        shifted[valid_idx],
                        monthly[response_col][valid_idx]
                    )
                    
                    lag_results.append({
                        'predictor': col,
                        'lag_months': lag,
                        'correlation': correlation,
                        'p_value': p_value,
                        'sample_size': valid_idx.sum()
                    })
        
        results['correlations'] = pd.DataFrame(lag_results)
        
        # Find optimal lags
        if not lag_results:
            results['optimal_lags'] = {}
        else:
            optimal = results['correlations'].groupby('predictor').apply(
                lambda x: x.loc[x['correlation'].idxmax() if not x.empty else None]
            )
            results['optimal_lags'] = optimal.to_dict('records')
        
        # Cross-correlation function
        results['ccf'] = {}
        for col in predictor_cols:
            try:
                ccf_values = ccf(
                    monthly[response_col].fillna(0).values,
                    monthly[col].fillna(0).values,
                    adjusted=False
                )
                results['ccf'][col] = ccf_values[:max_lags+1]
            except:
                pass
        
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
        
        if cases_df.empty or interventions_df.empty:
            return {'error': 'No data for intervention analysis'}
        
        cases_df[date_col] = pd.to_datetime(cases_df[date_col])
        interventions_df[date_col] = pd.to_datetime(interventions_df[date_col])
        
        # Aggregate cases to monthly
        cases_df['month'] = cases_df[date_col].dt.to_period('M')
        monthly_cases = cases_df.groupby('month')[value_col].sum().reset_index()
        monthly_cases['date'] = monthly_cases['month'].dt.start_time
        
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
                
                # Statistical test
                try:
                    t_stat, p_value = stats.ttest_ind(
                        pre_cases[value_col].values,
                        post_cases[value_col].values
                    )
                except:
                    t_stat, p_value = None, None
                
                impact_results.append({
                    'intervention_type': intervention.get(intervention_col, 'Unknown'),
                    'date': start_date,
                    'pre_mean': pre_mean,
                    'post_mean': post_mean,
                    'absolute_change': post_mean - pre_mean,
                    'relative_change': ((post_mean - pre_mean) / pre_mean) * 100 if pre_mean > 0 else None,
                    'p_value': p_value,
                    'pre_months': len(pre_cases),
                    'post_months': len(post_cases)
                })
        
        results['impact'] = pd.DataFrame(impact_results)
        
        if not impact_results:
            return {'error': 'No intervention impact could be calculated'}
        
        # Summary statistics
        results['summary'] = {
            'avg_absolute_change': np.mean([r['absolute_change'] for r in impact_results]),
            'avg_relative_change': np.mean([r['relative_change'] for r in impact_results if r['relative_change'] is not None]),
            'significant_interventions': len([r for r in impact_results if r['p_value'] and r['p_value'] < 0.05])
        }
        
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
            group_cols: Optional columns to group by
            
        Returns:
            Dictionary with resistance trend results
        """
        results = {}
        
        if resistance_df.empty:
            return {'error': 'No resistance data available'}
        
        resistance_df[date_col] = pd.to_datetime(resistance_df[date_col])
        resistance_df['year'] = resistance_df[date_col].dt.year
        
        # Classify resistance status
        resistance_df['status_category'] = resistance_df[status_col].apply(
            lambda x: 'Resistant' if str(x).lower() in ['resistant', 'high', 'resistance'] 
            else ('Susceptible' if str(x).lower() in ['susceptible', 'low'] else 'Unknown')
        )
        
        # Group by year and optional group columns
        group_cols = group_cols or []
        yearly_counts = resistance_df.groupby(['year'] + group_cols).size().reset_index(name='count')
        
        # Add status categories for annual analysis
        annual_status = resistance_df.groupby(['year'] + group_cols + ['status_category']).size().reset_index(name='count')
        
        # Calculate percentages
        total_by_year = annual_status.groupby(['year'] + group_cols)['count'].sum().reset_index(name='total')
        annual_status = annual_status.merge(total_by_year, on=['year'] + group_cols)
        annual_status['percentage'] = (annual_status['count'] / annual_status['total']) * 100
        
        results['annual_status'] = annual_status
        
        # Trend analysis
        if group_cols:
            # We need to handle each group separately
            trends = []
            for group_name, group_data in annual_status.groupby(group_cols):
                # Pivot for this group
                pivot = group_data.pivot(
                    index='year', 
                    columns='status_category', 
                    values='percentage'
                ).fillna(0)
                
                # Calculate trend for resistance (assuming first column is resistance)
                if 'Resistant' in pivot.columns:
                    resistant_trend = pivot['Resistant'].values
                    years = pivot.index.values
                    if len(resistant_trend) > 2:
                        slope, intercept, r_value, p_value, std_err = stats.linregress(years, resistant_trend)
                        trends.append({
                            'group': group_name if isinstance(group_name, tuple) else [group_name],
                            'slope': slope,
                            'r_squared': r_value**2,
                            'p_value': p_value
                        })
            
            results['trends'] = pd.DataFrame(trends)
        else:
            # Overall trend
            pivot = annual_status.pivot(
                index='year', 
                columns='status_category', 
                values='percentage'
            ).fillna(0)
            
            if 'Resistant' in pivot.columns:
                resistant_trend = pivot['Resistant'].values
                years = pivot.index.values
                if len(resistant_trend) > 2:
                    slope, intercept, r_value, p_value, std_err = stats.linregress(years, resistant_trend)
                    results['overall_trend'] = {
                        'slope': slope,
                        'r_squared': r_value**2,
                        'p_value': p_value,
                        'direction': 'increasing' if slope > 0 else 'decreasing'
                    }
        
        results['pivot'] = pivot if 'pivot' in locals() else None
        
        return results
    
    @staticmethod
    def integrated_risk_model(
        cases_df: pd.DataFrame,
        env_df: pd.DataFrame,
        vector_df: Optional[pd.DataFrame] = None,
        date_col: str = 'date',
        response_col: str = 'case_count',
        predictor_cols: List[str] = None,
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
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import r2_score, mean_squared_error
        from sklearn.model_selection import train_test_split
        
        results = {}
        
        # Ensure date is datetime
        cases_df[date_col] = pd.to_datetime(cases_df[date_col])
        env_df[date_col] = pd.to_datetime(env_df[date_col])
        
        # Merge data
        merged = cases_df.merge(env_df, on=date_col, how='inner')
        
        if vector_df is not None:
            vector_df[date_col] = pd.to_datetime(vector_df[date_col])
            vector_agg = vector_df.groupby(date_col)['count'].sum().reset_index()
            merged = merged.merge(vector_agg, on=date_col, how='left')
            merged['count'] = merged['count'].fillna(0)
        
        if merged.empty:
            return {'error': 'No data for risk modeling'}
        
        # Prepare features
        if predictor_cols is None:
            predictor_cols = [col for col in merged.columns 
                            if col not in [date_col, response_col, 'year', 'month']]
        
        X = merged[predictor_cols].copy()
        y = merged[response_col].copy()
        
        # Handle missing values
        X = X.fillna(X.mean())
        
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
        results['feature_importance'] = dict(zip(predictor_cols, rf.feature_importances_))
        results['r2_score'] = r2
        results['rmse'] = rmse
        results['predictions'] = {
            'actual': y_test.values,
            'predicted': y_pred
        }
        
        # Risk threshold
        y_threshold = np.percentile(y, 75)
        y_test_binary = (y_test > y_threshold).astype(int)
        y_pred_binary = (y_pred > y_threshold).astype(int)
        
        from sklearn.metrics import accuracy_score, classification_report
        results['classification'] = {
            'accuracy': accuracy_score(y_test_binary, y_pred_binary),
            'threshold': y_threshold,
            'report': classification_report(y_test_binary, y_pred_binary, output_dict=True)
        }
        
        return results