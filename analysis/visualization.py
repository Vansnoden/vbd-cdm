# analysis/visualization.py
"""
Visualization generation for CDM analysis results.

This module provides functions to create publication-quality figures
from analysis results.
"""

import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Optional, List, Tuple
import logging
import warnings
warnings.filterwarnings('ignore')

logger = logging.getLogger(__name__)


class VisualizationGenerator:
    """
    Generate publication-ready visualizations from analysis results.
    """
    
    def __init__(self, output_dir: Path):
        """
        Initialize with output directory.
        
        Args:
            output_dir: Directory to save figures
        """
        self.output_dir = Path(output_dir)
        self.figures_dir = self.output_dir / "figures"
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        
        # Set style
        plt.style.use('seaborn-v0_8-whitegrid')
        sns.set_palette("husl")
    
    def plot_seasonal_patterns(
        self,
        seasonal_results: Dict,
        study_name: str,
        output_dir: Optional[Path] = None
    ):
        """Plot seasonal patterns."""
        if 'error' in seasonal_results:
            logger.warning(f"Skipping seasonal plot: {seasonal_results['error']}")
            return
        
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        # Monthly pattern
        ax = axes[0]
        if 'monthly' in seasonal_results:
            monthly = seasonal_results['monthly']
            bars = ax.bar(
                monthly['month'],
                monthly['mean'],
                yerr=monthly['std'],
                capsize=3,
                alpha=0.7
            )
            ax.set_xlabel('Month')
            ax.set_ylabel('Average Cases')
            ax.set_title(f'Seasonal Pattern: {study_name}')
            ax.set_xticks(range(1, 13))
            ax.set_xticklabels(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])
            ax.grid(True, alpha=0.3)
        
        # Year-over-year comparison
        ax = axes[1]
        if 'year_over_year' in seasonal_results:
            yoy = seasonal_results['year_over_year']
            # Pivot for line plot
            yoy_pivot = yoy.pivot(index='month', columns='year', values='mean')
            yoy_pivot.plot(marker='o', ax=ax)
            ax.set_xlabel('Month')
            ax.set_ylabel('Cases')
            ax.set_title('Year-over-Year Comparison')
            ax.legend(title='Year')
            ax.grid(True, alpha=0.3)
            ax.set_xticks(range(1, 13))
            ax.set_xticklabels(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])
        
        plt.tight_layout()
        output_path = (output_dir or self.figures_dir) / f"{study_name}_seasonal_patterns.png"
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Saved seasonal patterns to {output_path}")
    
    def plot_lag_correlations(
        self,
        lag_results: Dict,
        study_name: str,
        output_dir: Optional[Path] = None
    ):
        """Plot lag correlation results."""
        if 'error' in lag_results:
            logger.warning(f"Skipping lag plot: {lag_results['error']}")
            return
        
        if 'correlations' not in lag_results:
            return
        
        corr_df = lag_results['correlations']
        
        if corr_df.empty:
            return
        
        # Plot correlations by lag
        fig, ax = plt.subplots(figsize=(10, 6))
        
        for predictor in corr_df['predictor'].unique():
            subset = corr_df[corr_df['predictor'] == predictor]
            ax.plot(
                subset['lag_months'],
                subset['correlation'],
                marker='o',
                label=predictor,
                linewidth=2
            )
        
        ax.axhline(y=0, color='black', linestyle='--', alpha=0.5)
        ax.set_xlabel('Lag (months)')
        ax.set_ylabel('Pearson Correlation')
        ax.set_title(f'Climate-Disease Lag Correlations: {study_name}')
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = (output_dir or self.figures_dir) / f"{study_name}_lag_correlations.png"
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Saved lag correlations to {output_path}")
    
    def plot_intervention_impact(
        self,
        impact_results: Dict,
        study_name: str,
        output_dir: Optional[Path] = None
    ):
        """Plot intervention impact analysis."""
        if 'error' in impact_results:
            logger.warning(f"Skipping intervention plot: {impact_results['error']}")
            return
        
        if 'impact' not in impact_results:
            return
        
        impact_df = impact_results['impact']
        
        if impact_df.empty:
            return
        
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        # Absolute change
        ax = axes[0]
        colors = ['red' if x < 0 else 'green' for x in impact_df['absolute_change']]
        bars = ax.bar(
            range(len(impact_df)),
            impact_df['absolute_change'],
            color=colors,
            alpha=0.7
        )
        ax.axhline(y=0, color='black', linestyle='-', alpha=0.5)
        ax.set_xlabel('Intervention')
        ax.set_ylabel('Absolute Change in Cases')
        ax.set_title(f'Intervention Impact: {study_name}')
        ax.set_xticks(range(len(impact_df)))
        ax.set_xticklabels(impact_df['intervention_type'], rotation=45, ha='right')
        ax.grid(True, alpha=0.3)
        
        # Relative change
        ax = axes[1]
        valid = impact_df['relative_change'].notna()
        if valid.any():
            ax.bar(
                range(valid.sum()),
                impact_df.loc[valid, 'relative_change'],
                color='steelblue',
                alpha=0.7
            )
            ax.axhline(y=0, color='black', linestyle='-', alpha=0.5)
            ax.set_xlabel('Intervention')
            ax.set_ylabel('Relative Change (%)')
            ax.set_title('Relative Change in Cases')
            ax.set_xticks(range(valid.sum()))
            ax.set_xticklabels(impact_df.loc[valid, 'intervention_type'], rotation=45, ha='right')
            ax.grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = (output_dir or self.figures_dir) / f"{study_name}_intervention_impact.png"
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Saved intervention impact to {output_path}")
    
    
    def plot_resistance_trends(
        self,
        resistance_results: Dict,
        study_name: str,
        output_dir: Optional[Path] = None
    ):
        """Plot insecticide resistance trends."""
        if 'error' in resistance_results:
            logger.warning(f"Skipping resistance plot: {resistance_results['error']}")
            return
        
        if 'annual_status' not in resistance_results:
            return
        
        annual_status = resistance_results['annual_status']
        
        if annual_status.empty:
            return
        
        # Check if we have pivot data
        pivot = resistance_results.get('pivot')
        
        if pivot is not None and not pivot.empty:
            fig, axes = plt.subplots(1, 2, figsize=(14, 5))
            
            # Stacked bar chart
            ax = axes[0]
            pivot.plot(kind='bar', stacked=True, ax=ax)
            ax.set_xlabel('Year')
            ax.set_ylabel('Percentage of Tests')
            ax.set_title(f'Resistance Status Trends: {study_name}')
            ax.legend(title='Status', bbox_to_anchor=(1.05, 1), loc='upper left')
            ax.set_xticklabels(ax.get_xticklabels(), rotation=45)
            ax.grid(True, alpha=0.3)
            
            # Resistance trend line
            ax = axes[1]
            if 'Resistant' in pivot.columns:
                resistant_trend = pivot['Resistant']
                years = pivot.index.values
                
                ax.plot(years, resistant_trend, 'o-', linewidth=2, color='red')
                
                # Add trendline if in results
                if 'overall_trend' in resistance_results:
                    trend = resistance_results['overall_trend']
                    # Calculate trendline values
                    if len(years) > 1:
                        slope = trend.get('slope', 0)
                        intercept = trend.get('intercept', 0)
                        trendline = slope * years + intercept
                        ax.plot(years, trendline, '--', color='darkred', 
                            label=f"Slope: {slope:.3f}")
                
                ax.set_xlabel('Year')
                ax.set_ylabel('Resistance (%)')
                ax.set_title('Resistance Trend')
                ax.grid(True, alpha=0.3)
                ax.legend()
            
            plt.tight_layout()
            output_path = (output_dir or self.figures_dir) / f"{study_name}_resistance_trends.png"
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"Saved resistance trends to {output_path}")
        else:
            # Try to create a simpler plot using annual_status directly
            self._plot_simple_resistance_trends(annual_status, study_name, output_dir)


    def _plot_simple_resistance_trends(self, annual_status, study_name, output_dir):
        """Fallback: simpler resistance trend plot."""
        fig, ax = plt.subplots(figsize=(10, 6))
        
        # Group by year and status category to calculate percentages
        status_pivot = annual_status.pivot_table(
            index='year',
            columns='status_category',
            values='percentage',
            aggfunc='mean'
        ).fillna(0)
        
        if 'Resistant' in status_pivot.columns:
            ax.plot(status_pivot.index, status_pivot['Resistant'], 
                'o-', linewidth=2, color='red', label='Resistant')
        
        if 'Susceptible' in status_pivot.columns:
            ax.plot(status_pivot.index, status_pivot['Susceptible'], 
                'o-', linewidth=2, color='green', label='Susceptible')
        
        ax.set_xlabel('Year')
        ax.set_ylabel('Percentage')
        ax.set_title(f'Resistance Status Trends: {study_name}')
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = (output_dir or self.figures_dir) / f"{study_name}_resistance_trends.png"
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Saved simple resistance trends to {output_path}")
    
    def plot_risk_model(
        self,
        risk_results: Dict,
        study_name: str,
        output_dir: Optional[Path] = None
    ):
        """Plot integrated risk model results."""
        if 'error' in risk_results:
            logger.warning(f"Skipping risk model plot: {risk_results['error']}")
            return
        
        fig, axes = plt.subplots(2, 2, figsize=(14, 12))
        
        # Feature importance
        ax = axes[0, 0]
        if 'feature_importance' in risk_results:
            importance = risk_results['feature_importance']
            features = list(importance.keys())
            values = list(importance.values())
            idx = np.argsort(values)[::-1][:10]  # Top 10
            
            ax.barh([features[i] for i in idx], [values[i] for i in idx])
            ax.set_xlabel('Feature Importance')
            ax.set_title('Top Risk Factors')
            ax.grid(True, alpha=0.3)
        
        # Actual vs predicted
        ax = axes[0, 1]
        if 'predictions' in risk_results:
            preds = risk_results['predictions']
            ax.scatter(preds['actual'], preds['predicted'], alpha=0.5)
            ax.plot(
                [preds['actual'].min(), preds['actual'].max()],
                [preds['actual'].min(), preds['actual'].max()],
                'r--',
                alpha=0.5,
                label='Perfect Prediction'
            )
            ax.set_xlabel('Actual Cases')
            ax.set_ylabel('Predicted Cases')
            ax.set_title(f'Model Performance (R² = {risk_results.get("r2_score", 0):.3f})')
            ax.legend()
            ax.grid(True, alpha=0.3)
        
        # Residuals
        ax = axes[1, 0]
        if 'predictions' in risk_results:
            preds = risk_results['predictions']
            residuals = preds['actual'] - preds['predicted']
            ax.hist(residuals, bins=20, edgecolor='black', alpha=0.7)
            ax.axvline(x=0, color='red', linestyle='--', alpha=0.5)
            ax.set_xlabel('Residuals')
            ax.set_ylabel('Frequency')
            ax.set_title('Residual Distribution')
            ax.grid(True, alpha=0.3)
        
        # Classification performance
        ax = axes[1, 1]
        if 'classification' in risk_results:
            clf = risk_results['classification']
            if 'report' in clf:
                report = clf['report']
                classes = ['0 (Low)', '1 (High)']
                metrics = ['precision', 'recall', 'f1-score']
                
                data = []
                for cls in ['0', '1']:
                    if cls in report:
                        data.append([report[cls].get(m, 0) for m in metrics])
                
                if data:
                    x = np.arange(len(metrics))
                    width = 0.35
                    
                    for i, (cls_name, values) in enumerate(zip(classes, data)):
                        ax.bar(x + i*width, values, width, label=cls_name)
                    
                    ax.set_xlabel('Metric')
                    ax.set_ylabel('Score')
                    ax.set_title(f'Classification Performance (Acc = {clf.get("accuracy", 0):.3f})')
                    ax.set_xticks(x + width/2)
                    ax.set_xticklabels(['Precision', 'Recall', 'F1-Score'])
                    ax.legend()
                    ax.grid(True, alpha=0.3)
                    ax.set_ylim(0, 1)
        
        plt.tight_layout()
        output_path = (output_dir or self.figures_dir) / f"{study_name}_risk_model.png"
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Saved risk model to {output_path}")
    
    def create_dashboard(
        self,
        results: Dict,
        study_name: str,
        output_dir: Optional[Path] = None
    ):
        """
        Create a comprehensive dashboard combining all plots.
        """
        fig = plt.figure(figsize=(16, 12))
        gs = fig.add_gridspec(3, 3, hspace=0.3, wspace=0.3)
        
        # 1. Seasonal pattern (top left)
        ax1 = fig.add_subplot(gs[0, 0])
        if 'seasonal' in results and 'monthly' in results['seasonal']:
            monthly = results['seasonal']['monthly']
            ax1.bar(monthly['month'], monthly['mean'], alpha=0.7)
            ax1.set_title('Seasonal Pattern')
            ax1.set_xlabel('Month')
            ax1.set_ylabel('Cases')
            ax1.set_xticks(range(1, 13))
            ax1.set_xticklabels(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])
            ax1.grid(True, alpha=0.3)
        
        # 2. Lag correlations (top center)
        ax2 = fig.add_subplot(gs[0, 1])
        if 'lag_analysis' in results and 'correlations' in results['lag_analysis']:
            corr_df = results['lag_analysis']['correlations']
            if not corr_df.empty:
                for predictor in corr_df['predictor'].unique():
                    subset = corr_df[corr_df['predictor'] == predictor]
                    ax2.plot(subset['lag_months'], subset['correlation'], 
                            marker='o', label=predictor)
                ax2.axhline(y=0, color='black', linestyle='--', alpha=0.5)
                ax2.set_title('Lag Correlations')
                ax2.set_xlabel('Lag (months)')
                ax2.set_ylabel('Correlation')
                ax2.legend()
                ax2.grid(True, alpha=0.3)
        
        # 3. Intervention impact (top right)
        ax3 = fig.add_subplot(gs[0, 2])
        if 'intervention_impact' in results and 'impact' in results['intervention_impact']:
            impact_df = results['intervention_impact']['impact']
            if not impact_df.empty:
                valid = impact_df['relative_change'].notna()
                if valid.any():
                    ax3.bar(range(valid.sum()), 
                           impact_df.loc[valid, 'relative_change'], 
                           color='steelblue')
                    ax3.axhline(y=0, color='black', linestyle='-', alpha=0.5)
                    ax3.set_title('Intervention Impact')
                    ax3.set_xlabel('Intervention')
                    ax3.set_ylabel('Relative Change (%)')
                    ax3.set_xticks(range(valid.sum()))
                    ax3.set_xticklabels(impact_df.loc[valid, 'intervention_type'], 
                                       rotation=45, ha='right')
                    ax3.grid(True, alpha=0.3)
        
        # 4. Resistance trends (middle left)
        ax4 = fig.add_subplot(gs[1, 0])
        if 'resistance_trends' in results and 'annual_status' in results['resistance_trends']:
            annual = results['resistance_trends']['annual_status']
            if not annual.empty:
                pivot = annual.pivot(index='year', columns='status_category', values='percentage').fillna(0)
                pivot.plot(kind='bar', stacked=True, ax=ax4)
                ax4.set_title('Resistance Status')
                ax4.set_xlabel('Year')
                ax4.set_ylabel('Percentage')
                ax4.legend(loc='upper left', bbox_to_anchor=(1, 1))
                ax4.grid(True, alpha=0.3)
        
        # 5. Risk model performance (middle center)
        ax5 = fig.add_subplot(gs[1, 1])
        if 'risk_model' in results and 'predictions' in results['risk_model']:
            preds = results['risk_model']['predictions']
            ax5.scatter(preds['actual'], preds['predicted'], alpha=0.5)
            ax5.plot([preds['actual'].min(), preds['actual'].max()],
                    [preds['actual'].min(), preds['actual'].max()],
                    'r--', alpha=0.5)
            ax5.set_title(f'Risk Model (R² = {results["risk_model"].get("r2_score", 0):.3f})')
            ax5.set_xlabel('Actual Cases')
            ax5.set_ylabel('Predicted Cases')
            ax5.grid(True, alpha=0.3)
        
        # 6. Feature importance (middle right)
        ax6 = fig.add_subplot(gs[1, 2])
        if 'risk_model' in results and 'feature_importance' in results['risk_model']:
            importance = results['risk_model']['feature_importance']
            features = list(importance.keys())
            values = list(importance.values())
            idx = np.argsort(values)[::-1][:8]
            
            ax6.barh([features[i] for i in idx], [values[i] for i in idx])
            ax6.set_title('Top Risk Factors')
            ax6.set_xlabel('Importance')
            ax6.grid(True, alpha=0.3)
        
        # 7. Summary statistics (bottom, spans 3 columns)
        ax7 = fig.add_subplot(gs[2, :])
        ax7.axis('off')
        
        # Create summary text
        summary_text = f"=== {study_name} Analysis Dashboard ===\n\n"
        
        if 'seasonal' in results and 'peak_months' in results['seasonal']:
            peaks = results['seasonal']['peak_months']
            if not peaks.empty:
                summary_text += f"Peak months: {', '.join([str(int(p)) for p in peaks['month'].values[:3]])}\n"
        
        if 'lag_analysis' in results and 'optimal_lags' in results['lag_analysis']:
            lags = results['lag_analysis']['optimal_lags']
            if lags:
                summary_text += "Optimal lags: " + ", ".join([f"{l['predictor']}: {l['lag_months']}m" for l in lags[:3]]) + "\n"
        
        if 'intervention_impact' in results and 'summary' in results['intervention_impact']:
            s = results['intervention_impact']['summary']
            summary_text += f"Intervention impact: {s.get('avg_relative_change', 0):.1f}% change\n"
        
        if 'resistance_trends' in results and 'overall_trend' in results['resistance_trends']:
            t = results['resistance_trends']['overall_trend']
            summary_text += f"Resistance trend: {t.get('direction', 'stable')} (slope={t.get('slope', 0):.3f})\n"
        
        if 'risk_model' in results and 'r2_score' in results['risk_model']:
            summary_text += f"Risk model performance: R²={results['risk_model']['r2_score']:.3f}"
        
        ax7.text(0.05, 0.5, summary_text, fontsize=12, fontfamily='monospace',
                verticalalignment='center', transform=ax7.transAxes)
        
        plt.suptitle(f'Integrated Analysis: {study_name}', fontsize=16, y=0.98)
        
        plt.tight_layout()
        output_path = (output_dir or self.figures_dir) / f"{study_name}_dashboard.png"
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Saved dashboard to {output_path}")