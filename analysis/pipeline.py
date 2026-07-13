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
from typing import Dict, Optional, Union
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
        
        Returns:
            Dictionary with all analysis results
        """
        logger.info(f"Starting analysis for {self.config.study_name}")
        
        # 1. Query data
        self.data = self.base.query_data()
        
        # 2. Run analyses based on configuration
        if self.config.run_seasonal:
            logger.info("Running seasonal pattern analysis...")
            self.results['seasonal'] = self._analyze_seasonal()
        
        if self.config.run_climate_correlation:
            logger.info("Running climate correlation analysis...")
            self.results['climate_correlation'] = self._analyze_climate_correlation()
        
        if self.config.run_lag_analysis:
            logger.info("Running lag analysis...")
            self.results['lag_analysis'] = self._analyze_lags()
        
        if self.config.run_intervention_impact:
            logger.info("Running intervention impact analysis...")
            self.results['intervention_impact'] = self._analyze_interventions()
        
        if self.config.run_resistance_trends:
            logger.info("Running resistance trend analysis...")
            self.results['resistance_trends'] = self._analyze_resistance()
        
        if self.config.run_risk_modeling:
            logger.info("Building integrated risk model...")
            self.results['risk_model'] = self._analyze_risk()
        
        # 3. Generate visualizations
        logger.info("Generating visualizations...")
        self._generate_figures()
        
        # 4. Save results
        self._save_results()
        
        # 5. Generate report
        self._generate_report()
        
        logger.info("Analysis complete!")
        return self.results
    
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
        """Run climate correlation analysis."""
        env = self.data.get('environment', pd.DataFrame())
        cases = self.data.get('cases', pd.DataFrame())
        
        if env.empty or cases.empty:
            return {'error': 'Insufficient data for climate correlation'}
        
        # Merge data
        env['date'] = pd.to_datetime(env['date'])
        cases['date'] = pd.to_datetime(cases['date'])
        
        # Ensure date is set as index for proper merging
        env = env.set_index('date')
        cases = cases.set_index('date')
        
        # Merge on date index
        merged = cases.join(env, how='inner').reset_index()
        
        if merged.empty:
            return {'error': 'No overlapping data for correlation'}
        
        # Calculate correlations
        env_cols = [col for col in merged.columns 
                   if col in self.config.environmental_variables]
        
        correlations = {}
        for col in env_cols:
            if col in merged.columns and merged[col].notna().any():
                corr = merged['case_count'].corr(merged[col])
                correlations[col] = corr
        
        # Create correlation matrix
        corr_cols = ['case_count'] + [c for c in env_cols if c in merged.columns]
        corr_matrix = merged[corr_cols].corr()
        
        return {
            'correlations': correlations,
            'merged_data': merged,
            'correlation_matrix': corr_matrix
        }
    
    def _analyze_lags(self) -> Dict:
        """Run lag correlation analysis."""
        env = self.data.get('environment', pd.DataFrame())
        cases = self.data.get('cases', pd.DataFrame())
        
        if env.empty or cases.empty:
            return {'error': 'Insufficient data for lag analysis'}
        
        # Merge data
        env['date'] = pd.to_datetime(env['date'])
        cases['date'] = pd.to_datetime(cases['date'])
        
        env = env.set_index('date')
        cases = cases.set_index('date')
        merged = cases.join(env, how='inner').reset_index()
        
        if merged.empty:
            return {'error': 'No overlapping data for lag analysis'}
        
        predictor_cols = [col for col in merged.columns 
                         if col in self.config.environmental_variables]
        
        return self.modules.lag_correlation_analysis(
            merged,
            date_col='date',
            response_col='case_count',
            predictor_cols=predictor_cols,
            max_lags=self.config.max_lag_months
        )
    
    def _analyze_interventions(self) -> Dict:
        """Run intervention impact analysis."""
        cases = self.data.get('cases', pd.DataFrame())
        interventions = self.data.get('interventions', pd.DataFrame())
        
        if cases.empty or interventions.empty:
            return {'error': 'No intervention data available'}
        
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
        
        return self.modules.resistance_trend_analysis(
            resistance,
            date_col='date',
            status_col='resistance_status',
            group_cols=['species_name']
        )
    
    def _analyze_risk(self) -> Dict:
        """Build integrated risk model."""
        cases = self.data.get('cases', pd.DataFrame())
        env = self.data.get('environment', pd.DataFrame())
        vectors = self.data.get('vectors', pd.DataFrame())
        
        if cases.empty or env.empty:
            return {'error': 'Insufficient data for risk modeling'}
        
        # Merge cases and environment
        cases['date'] = pd.to_datetime(cases['date'])
        env['date'] = pd.to_datetime(env['date'])
        
        cases = cases.set_index('date')
        env = env.set_index('date')
        
        merged = cases.join(env, how='inner').reset_index()
        
        if vectors is not None and not vectors.empty:
            vectors['date'] = pd.to_datetime(vectors['date'])
            vectors = vectors.set_index('date')
            merged = merged.join(vectors[['count']], how='left')
            merged['count'] = merged['count'].fillna(0)
        
        predictor_cols = self.config.environmental_variables.copy()
        if 'count' in merged.columns:
            predictor_cols.append('count')
        
        return self.modules.integrated_risk_model(
            merged,
            date_col='date',
            response_col='case_count',
            predictor_cols=predictor_cols
        )
    
    def _generate_figures(self):
        """Generate all visualizations."""
        fig_dir = self.output_dir / "figures"
        fig_dir.mkdir(exist_ok=True)
        
        # Seasonal pattern figure
        if 'seasonal' in self.results and 'error' not in self.results['seasonal']:
            self.viz.plot_seasonal_patterns(
                self.results['seasonal'],
                self.config.study_name,
                fig_dir
            )
        
        # Lag analysis figure
        if 'lag_analysis' in self.results and 'error' not in self.results['lag_analysis']:
            self.viz.plot_lag_correlations(
                self.results['lag_analysis'],
                self.config.study_name,
                fig_dir
            )
        
        # Intervention impact figure
        if 'intervention_impact' in self.results and 'error' not in self.results['intervention_impact']:
            self.viz.plot_intervention_impact(
                self.results['intervention_impact'],
                self.config.study_name,
                fig_dir
            )
        
        # Resistance trends figure
        if 'resistance_trends' in self.results and 'error' not in self.results['resistance_trends']:
            self.viz.plot_resistance_trends(
                self.results['resistance_trends'],
                self.config.study_name,
                fig_dir
            )
        
        # Risk model figure
        if 'risk_model' in self.results and 'error' not in self.results['risk_model']:
            self.viz.plot_risk_model(
                self.results['risk_model'],
                self.config.study_name,
                fig_dir
            )
        
        # Overview dashboard
        self.viz.create_dashboard(
            self.results,
            self.config.study_name,
            fig_dir
        )
    
    def _save_results(self):
        """Save analysis results to files."""
        data_dir = self.output_dir / "data"
        data_dir.mkdir(exist_ok=True)
        
        for name, result in self.results.items():
            if isinstance(result, dict):
                # Save DataFrames
                for key, value in result.items():
                    if isinstance(value, pd.DataFrame):
                        value.to_csv(data_dir / f"{name}_{key}.csv", index=False)
                    elif key == 'predictions' and isinstance(value, dict):
                        # Save predictions as CSV
                        pred_df = pd.DataFrame(value)
                        pred_df.to_csv(data_dir / f"{name}_predictions.csv", index=False)
                
                # Save summary as JSON
                summary = {k: v for k, v in result.items() 
                          if not isinstance(v, (pd.DataFrame, np.ndarray)) and not isinstance(v, dict)}
                if summary:
                    with open(data_dir / f"{name}_summary.json", 'w') as f:
                        json.dump(summary, f, indent=2, default=str)
    
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
            'summary': self._create_summary()
        }
        
        with open(report_dir / "analysis_report.json", 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        # Generate markdown report
        self._generate_markdown_report(report, report_dir / "analysis_report.md")
    
    def _create_summary(self) -> Dict:
        """Create a summary of key findings."""
        summary = {}
        
        # Seasonal patterns
        if 'seasonal' in self.results:
            seasonal = self.results['seasonal']
            if 'peak_months' in seasonal and not seasonal['peak_months'].empty:
                summary['peak_months'] = seasonal['peak_months'][['month', 'mean']].to_dict('records')
        
        # Lag analysis
        if 'lag_analysis' in self.results:
            lag = self.results['lag_analysis']
            if 'optimal_lags' in lag and lag['optimal_lags']:
                summary['optimal_lags'] = lag['optimal_lags']
        
        # Intervention impact
        if 'intervention_impact' in self.results:
            impact = self.results['intervention_impact']
            if 'summary' in impact:
                summary['intervention_summary'] = impact['summary']
        
        # Resistance trends
        if 'resistance_trends' in self.results:
            resistance = self.results['resistance_trends']
            if 'overall_trend' in resistance:
                summary['resistance_trend'] = resistance['overall_trend']
        
        # Risk model
        if 'risk_model' in self.results:
            risk = self.results['risk_model']
            if 'r2_score' in risk:
                summary['model_performance'] = {
                    'r2_score': risk['r2_score'],
                    'rmse': risk['rmse']
                }
            if 'feature_importance' in risk:
                summary['top_features'] = sorted(
                    risk['feature_importance'].items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:5]
        
        return summary
    
    def _generate_markdown_report(self, report: Dict, output_path: Path):
        """Generate a markdown report from analysis results."""
        lines = []
        lines.append(f"# Analysis Report: {report['study_name']}")
        lines.append("")
        lines.append(f"**Location:** {report['location']}, {report['country']}")
        lines.append(f"**Period:** {report['period']}")
        lines.append(f"**Generated:** {report['generated']}")
        lines.append("")
        lines.append("## Summary of Findings")
        lines.append("")
        
        summary = report['summary']
        
        if 'peak_months' in summary:
            lines.append("### Seasonal Patterns")
            lines.append("Peak months for disease activity:")
            for month in summary['peak_months']:
                lines.append(f"- Month {month['month']}: {month['mean']:.2f} cases/month")
            lines.append("")
        
        if 'optimal_lags' in summary:
            lines.append("### Climate-Disease Lags")
            lines.append("Optimal lag periods (months):")
            for lag in summary['optimal_lags']:
                lines.append(f"- {lag['predictor']}: {lag['lag_months']} months (r={lag['correlation']:.3f})")
            lines.append("")
        
        if 'intervention_summary' in summary:
            lines.append("### Intervention Impact")
            s = summary['intervention_summary']
            lines.append(f"- Average change: {s['avg_absolute_change']:.2f} cases (±{s['avg_relative_change']:.1f}%)")
            lines.append(f"- Significant interventions: {s['significant_interventions']}")
            lines.append("")
        
        if 'resistance_trend' in summary:
            lines.append("### Insecticide Resistance Trends")
            t = summary['resistance_trend']
            lines.append(f"- Trend direction: {t['direction']} (slope={t['slope']:.3f}, r²={t['r_squared']:.3f})")
            lines.append(f"- Statistical significance: p={t['p_value']:.4f}")
            lines.append("")
        
        if 'model_performance' in summary:
            lines.append("### Integrated Risk Model")
            m = summary['model_performance']
            lines.append(f"- R² score: {m['r2_score']:.3f}")
            lines.append(f"- RMSE: {m['rmse']:.3f}")
            lines.append("")
        
        if 'top_features' in summary:
            lines.append("### Key Risk Factors")
            for feature, importance in summary['top_features']:
                lines.append(f"- {feature}: {importance:.3f}")
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
        
        with open(output_path, 'w') as f:
            f.write('\n'.join(lines))
    
    def close(self):
        """Close database connections."""
        self.base.close()


# Convenience function
def run_analysis(
    study_name: str,
    location: str,
    country: str = "KE",
    start_year: int = 2019,
    end_year: int = 2024,
    output_dir: str = "analysis/outputs",
    **kwargs
) -> Dict:
    """
    Convenience function to run the analysis pipeline.
    
    Args:
        study_name: Name of the study
        location: Location name to analyze
        country: Country code (default: "KE")
        start_year: Start year (default: 2019)
        end_year: End year (default: 2024)
        output_dir: Output directory (default: "analysis/outputs")
        **kwargs: Additional analysis parameters
    
    Returns:
        Dictionary with analysis results
    """
    config = AnalysisConfig(
        study_name=study_name,
        location_name=location,
        country_code=country,
        start_year=start_year,
        end_year=end_year,
        output_dir=output_dir,
        **kwargs
    )
    
    pipeline = AnalysisPipeline(config)
    try:
        results = pipeline.run()
    finally:
        pipeline.close()
    
    return results