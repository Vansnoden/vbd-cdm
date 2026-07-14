# run_analysis.py
"""
Script to run the standardized analysis pipeline for a specific study site.

Usage:
    python run_analysis.py --study "<location_code/name>" --country <country name> --start 2019 --end 2024
    python run_analysis.py --config config.json
    python run_analysis.py --help
"""

import argparse
import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
import traceback

# Add project root to path
project_root = Path(__file__).parent.absolute()
sys.path.insert(0, str(project_root))

from analysis.pipeline import run_analysis
from analysis.base_analysis import AnalysisConfig

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(project_root / 'logs' / 'analysis.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run CDM analysis pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage
  python run_analysis.py --study "Siaya_County" --location "Siaya" --country "KE"
  
  # With specific year range
  python run_analysis.py --study "Siaya_County" --location "Siaya" --country "KE" --start 2020 --end 2023
  
  # Using a config file
  python run_analysis.py --config config.json
  
  # Run all analyses with custom output
  python run_analysis.py --study "National" --location "ALL" --country "ALL" --output "outputs/analysis/national"
  
  # Run only specific analyses
  python run_analysis.py --study "Siaya" --location "Siaya" --country "KE" --no-risk --no-spatial
        """
    )
    
    # Study parameters
    parser.add_argument(
        "--study", 
        required=True,
        help="Study name (used for output file naming)"
    )
    parser.add_argument(
        "--location", 
        default="ALL", 
        help="Location name (use 'ALL' for all locations)"
    )
    parser.add_argument(
        "--country", 
        default="ALL", 
        help="Country code (use 'ALL' for all countries)"
    )
    parser.add_argument(
        "--start", 
        type=int, 
        help="Start year (default: auto-detect from data)"
    )
    parser.add_argument(
        "--end", 
        type=int, 
        help="End year (default: auto-detect from data)"
    )
    
    # Output options
    parser.add_argument(
        "--output", 
        default="outputs/analysis", 
        help="Output directory (default: outputs/analysis)"
    )
    parser.add_argument(
        "--config", 
        help="JSON config file path (overrides other arguments)"
    )
    parser.add_argument(
        "--no-save-config",
        action="store_true",
        help="Don't save configuration to output directory"
    )
    
    # Analysis options
    parser.add_argument(
        "--disease",
        default=None,
        help="Filter by disease code (e.g., 'malaria')"
    )
    parser.add_argument(
        "--no-seasonal",
        action="store_false",
        dest="run_seasonal",
        help="Skip seasonal pattern analysis"
    )
    parser.add_argument(
        "--no-climate",
        action="store_false",
        dest="run_climate_correlation",
        help="Skip climate correlation analysis"
    )
    parser.add_argument(
        "--no-lag",
        action="store_false",
        dest="run_lag_analysis",
        help="Skip lag analysis"
    )
    parser.add_argument(
        "--no-intervention",
        action="store_false",
        dest="run_intervention_impact",
        help="Skip intervention impact analysis"
    )
    parser.add_argument(
        "--no-resistance",
        action="store_false",
        dest="run_resistance_trends",
        help="Skip resistance trend analysis"
    )
    parser.add_argument(
        "--no-spatial",
        action="store_false",
        dest="run_spatial_analysis",
        help="Skip spatial analysis"
    )
    parser.add_argument(
        "--no-risk",
        action="store_false",
        dest="run_risk_modeling",
        help="Skip risk modeling"
    )
    
    # Advanced options
    parser.add_argument(
        "--max-lags",
        type=int,
        default=6,
        help="Maximum number of lags to test (default: 6)"
    )
    parser.add_argument(
        "--variables",
        nargs="+",
        default=["t2m", "tp"],
        help="Environmental variables to include (default: t2m tp)"
    )
    parser.add_argument(
        "--vectors",
        nargs="+",
        help="Vector species to include"
    )
    
    # Debug options
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print configuration and exit without running"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode with full traceback"
    )
    
    return parser.parse_args()


def load_config_file(config_path: str) -> Dict[str, Any]:
    """
    Load configuration from a JSON file.
    
    Args:
        config_path: Path to JSON config file
        
    Returns:
        Dictionary with configuration parameters
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"Loaded configuration from {config_path}")
        return config
    except FileNotFoundError:
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config file: {e}")
        sys.exit(1)


def build_config_from_args(args) -> Dict[str, Any]:
    """
    Build configuration dictionary from command line arguments.
    
    Args:
        args: Parsed command line arguments
        
    Returns:
        Dictionary with configuration parameters
    """
    config = {
        'study_name': args.study,
        'location_name': args.location,
        'country_code': args.country,
        'output_dir': args.output,
        'run_seasonal': args.run_seasonal if hasattr(args, 'run_seasonal') else True,
        'run_climate_correlation': args.run_climate_correlation if hasattr(args, 'run_climate_correlation') else True,
        'run_lag_analysis': args.run_lag_analysis if hasattr(args, 'run_lag_analysis') else True,
        'run_intervention_impact': args.run_intervention_impact if hasattr(args, 'run_intervention_impact') else True,
        'run_resistance_trends': args.run_resistance_trends if hasattr(args, 'run_resistance_trends') else True,
        'run_spatial_analysis': args.run_spatial_analysis if hasattr(args, 'run_spatial_analysis') else True,
        'run_risk_modeling': args.run_risk_modeling if hasattr(args, 'run_risk_modeling') else True,
        'max_lag_months': args.max_lags,
        'environmental_variables': args.variables,
    }
    
    # Add optional parameters
    if args.start:
        config['start_year'] = args.start
    if args.end:
        config['end_year'] = args.end
    if args.disease:
        config['disease_code'] = args.disease
    if args.vectors:
        config['vector_codes'] = args.vectors
    
    return config


def validate_config(config: Dict[str, Any]) -> bool:
    """
    Validate configuration parameters.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        True if valid, False otherwise
    """
    errors = []
    warnings = []
    
    # Check required fields
    required = ['study_name', 'location_name', 'country_code']
    for field in required:
        if field not in config or not config[field]:
            errors.append(f"Missing required field: {field}")
    
    # Validate years
    if 'start_year' in config and config['start_year']:
        if config['start_year'] < 1900 or config['start_year'] > 2100:
            errors.append(f"Invalid start_year: {config['start_year']}")
    
    if 'end_year' in config and config['end_year']:
        if config['end_year'] < 1900 or config['end_year'] > 2100:
            errors.append(f"Invalid end_year: {config['end_year']}")
    
    if 'start_year' in config and 'end_year' in config:
        if config['start_year'] and config['end_year']:
            if config['start_year'] > config['end_year']:
                errors.append(f"start_year ({config['start_year']}) > end_year ({config['end_year']})")
    
    # Validate output directory
    if 'output_dir' in config:
        output_path = Path(config['output_dir'])
        if output_path.exists() and not output_path.is_dir():
            errors.append(f"Output path exists but is not a directory: {output_path}")
    
    # Check if data might be filtered out
    if config.get('location_name') == 'ALL' and config.get('country_code') == 'ALL':
        warnings.append("Using ALL locations and ALL countries - this may be slow")
    
    if errors:
        logger.error("Configuration validation failed:")
        for error in errors:
            logger.error(f"  ✗ {error}")
        return False
    
    if warnings:
        logger.info("Configuration warnings:")
        for warning in warnings:
            logger.info(f"  ⚠ {warning}")
    
    return True


def print_config_summary(config: Dict[str, Any]):
    """Print a summary of the configuration."""
    logger.info("=" * 60)
    logger.info("ANALYSIS CONFIGURATION")
    logger.info("=" * 60)
    logger.info(f"Study name:         {config.get('study_name', 'N/A')}")
    logger.info(f"Location:           {config.get('location_name', 'N/A')}")
    logger.info(f"Country:            {config.get('country_code', 'N/A')}")
    logger.info(f"Period:             {config.get('start_year', 'auto')} - {config.get('end_year', 'auto')}")
    logger.info(f"Output directory:   {config.get('output_dir', 'outputs/analysis')}")
    logger.info("-" * 60)
    logger.info("Analysis modules:")
    
    status_map = {
        True: '✓',
        False: '✗'
    }
    
    modules = [
        ('Seasonal', 'run_seasonal'),
        ('Climate correlation', 'run_climate_correlation'),
        ('Lag analysis', 'run_lag_analysis'),
        ('Intervention impact', 'run_intervention_impact'),
        ('Resistance trends', 'run_resistance_trends'),
        ('Spatial analysis', 'run_spatial_analysis'),
        ('Risk modeling', 'run_risk_modeling'),
    ]
    
    for label, key in modules:
        status = config.get(key, True)
        logger.info(f"  {status_map[status]} {label}: {status}")
    
    logger.info("-" * 60)
    logger.info(f"Max lags:           {config.get('max_lag_months', 6)}")
    logger.info(f"Env variables:      {config.get('environmental_variables', ['t2m', 'tp'])}")
    
    if config.get('disease_code'):
        logger.info(f"Disease filter:     {config['disease_code']}")
    if config.get('vector_codes'):
        logger.info(f"Vector filter:      {config['vector_codes']}")
    logger.info("=" * 60)


def get_available_years() -> tuple:
    """
    Get the available year range from the database.
    
    Returns:
        Tuple of (min_year, max_year) or (None, None) if not available
    """
    try:
        from database.session import SessionLocal
        from database.models import DimTime
        
        session = SessionLocal()
        try:
            min_year = session.query(DimTime.year).order_by(DimTime.year.asc()).first()
            max_year = session.query(DimTime.year).order_by(DimTime.year.desc()).first()
            
            if min_year and max_year:
                return min_year[0], max_year[0]
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Could not get year range from database: {e}")
    
    return None, None


def setup_logging(verbose: bool = False, debug: bool = False):
    """Setup logging configuration."""
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger('analysis').setLevel(logging.DEBUG)
        logging.getLogger('sqlalchemy').setLevel(logging.DEBUG)
    elif verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger('analysis').setLevel(logging.DEBUG)
        logging.getLogger('sqlalchemy').setLevel(logging.WARNING)
    else:
        logging.getLogger('analysis').setLevel(logging.INFO)
    
    # Create logs directory
    log_dir = project_root / 'logs'
    log_dir.mkdir(exist_ok=True)
    
    # Add file handler
    file_handler = logging.FileHandler(log_dir / 'analysis.log', mode='a')
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    logging.getLogger().addHandler(file_handler)


def save_configuration(config: Dict[str, Any], output_dir: Path, args: argparse.Namespace):
    """
    Save configuration for reproducibility.
    
    Args:
        config: Configuration dictionary
        output_dir: Output directory
        args: Parsed command line arguments
    """
    try:
        config_path = output_dir / 'analysis_config.json'
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'config': config,
                'timestamp': datetime.now().isoformat(),
                'command': ' '.join(sys.argv),
                'python_version': sys.version,
                'working_directory': str(project_root)
            }, f, indent=2, default=str)
        logger.info(f"Configuration saved to {config_path}")
    except Exception as e:
        logger.warning(f"Could not save configuration: {e}")


def print_results_summary(results: Dict[str, Any], output_dir: Path):
    """
    Print a summary of the analysis results.
    
    Args:
        results: Analysis results dictionary
        output_dir: Output directory
    """
    logger.info("=" * 60)
    logger.info("ANALYSIS COMPLETE")
    logger.info("=" * 60)
    
    # Check each analysis for errors
    failed_analyses = []
    completed_analyses = []
    skipped_analyses = []
    
    for name, result in results.items():
        if name in ['error', 'summary', 'data_summary']:
            continue
        if isinstance(result, dict):
            if 'error' in result:
                failed_analyses.append(name)
            else:
                completed_analyses.append(name)
    
    # Print data summary
    if 'data_summary' in results:
        logger.info("Data Availability:")
        for key, count in results['data_summary'].items():
            logger.info(f"  {key}: {count:,} records")
    
    logger.info("-" * 60)
    
    if completed_analyses:
        logger.info(f"✓ Completed analyses ({len(completed_analyses)}):")
        for name in completed_analyses:
            logger.info(f"  - {name}")
    
    if failed_analyses:
        logger.info(f"✗ Failed analyses ({len(failed_analyses)}):")
        for name in failed_analyses:
            error_msg = results.get(name, {}).get('error', 'Unknown error')
            logger.info(f"  - {name}: {error_msg}")
    
    # Print key findings if available
    if 'summary' in results:
        summary = results['summary']
        if summary:
            logger.info("-" * 60)
            logger.info("Key Findings:")
            
            if 'peak_months' in summary:
                peaks = summary['peak_months']
                if peaks:
                    peak_str = ", ".join([f"Month {p.get('month', 'N/A')}" for p in peaks[:3]])
                    logger.info(f"  Peak months: {peak_str}")
            
            if 'climate_correlations' in summary:
                corrs = summary['climate_correlations']
                if corrs:
                    for var, corr in corrs.items():
                        if corr is not None:
                            logger.info(f"  {var}: r = {corr:.3f}")
            
            if 'resistance_trend' in summary:
                trend = summary['resistance_trend']
                if trend:
                    direction = trend.get('direction', 'stable')
                    slope = trend.get('slope', 0)
                    logger.info(f"  Resistance trend: {direction} (slope={slope:.3f})")
            
            if 'model_performance' in summary:
                perf = summary['model_performance']
                if perf:
                    r2 = perf.get('r2_score', 0)
                    logger.info(f"  Risk model R²: {r2:.3f}")
    
    # Print output location
    logger.info("-" * 60)
    logger.info(f"Output directory: {output_dir.absolute()}")
    logger.info("=" * 60)


def main():
    """Main entry point."""
    # Parse arguments
    args = parse_args()
    
    # Setup logging
    setup_logging(args.verbose, args.debug)
    
    # Load configuration
    if args.config:
        config = load_config_file(args.config)
        # Override with command line arguments if provided
        if args.study:
            config['study_name'] = args.study
        if args.location:
            config['location_name'] = args.location
        if args.country:
            config['country_code'] = args.country
        if args.start:
            config['start_year'] = args.start
        if args.end:
            config['end_year'] = args.end
        if args.output:
            config['output_dir'] = args.output
        if args.disease:
            config['disease_code'] = args.disease
    else:
        config = build_config_from_args(args)
    
    # Auto-detect years if not specified
    if 'start_year' not in config or not config['start_year']:
        min_year, _ = get_available_years()
        if min_year:
            config['start_year'] = min_year
            logger.info(f"Auto-detected start year: {min_year}")
        else:
            config['start_year'] = 2019
            logger.info(f"Using default start year: 2019")
    
    if 'end_year' not in config or not config['end_year']:
        _, max_year = get_available_years()
        if max_year:
            config['end_year'] = max_year
            logger.info(f"Auto-detected end year: {max_year}")
        else:
            config['end_year'] = 2024
            logger.info(f"Using default end year: 2024")
    
    # Validate configuration
    if not validate_config(config):
        sys.exit(1)
    
    # Print configuration summary
    print_config_summary(config)
    
    # Dry run - exit after printing config
    if args.dry_run:
        logger.info("Dry run completed. Exiting.")
        sys.exit(0)
    
    # Create output directory
    output_dir = Path(config['output_dir'])
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save configuration for reproducibility
    if not args.no_save_config:
        save_configuration(config, output_dir, args)
    
    # Run the analysis
    try:
        logger.info("=" * 60)
        logger.info("STARTING ANALYSIS")
        logger.info("=" * 60)
        
        # Create AnalysisConfig object
        analysis_config = AnalysisConfig(**config)
        
        # Run analysis
        results = run_analysis(
            study_name=analysis_config.study_name,
            location=analysis_config.location_name,
            country=analysis_config.country_code,
            start_year=analysis_config.start_year,
            end_year=analysis_config.end_year,
            output_dir=str(analysis_config.output_dir),
            run_seasonal=analysis_config.run_seasonal,
            run_climate_correlation=analysis_config.run_climate_correlation,
            run_lag_analysis=analysis_config.run_lag_analysis,
            run_intervention_impact=analysis_config.run_intervention_impact,
            run_resistance_trends=analysis_config.run_resistance_trends,
            run_spatial_analysis=analysis_config.run_spatial_analysis,
            run_risk_modeling=analysis_config.run_risk_modeling,
            max_lag_months=analysis_config.max_lag_months,
            seasonal_period=analysis_config.seasonal_period,
            risk_threshold_percentile=analysis_config.risk_threshold_percentile,
            disease_code=analysis_config.disease_code,
            vector_codes=analysis_config.vector_codes,
            environmental_variables=analysis_config.environmental_variables
        )
        
        # Check for errors
        if results and isinstance(results, dict):
            if 'error' in results:
                logger.error(f"Analysis failed: {results['error']}")
                if args.debug:
                    traceback.print_exc()
                sys.exit(1)
            
            # Print results summary
            print_results_summary(results, output_dir)
        else:
            logger.warning("Analysis returned no results")
        
    except KeyboardInterrupt:
        logger.info("Analysis interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        if args.debug:
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()