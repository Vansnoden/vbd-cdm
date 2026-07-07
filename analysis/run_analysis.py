# run_analysis.py
"""
Script to run the standardized analysis pipeline for a specific study site.

Usage:
    python run_analysis.py --study "Siaya County" --country KE --start 2019 --end 2024
"""

import argparse
import sys
from pathlib import Path
import logging

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from analysis.pipeline import run_analysis

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run CDM analysis pipeline")
    parser.add_argument("--study", required=True, help="Study name")
    parser.add_argument("--location", required=True, help="Location name")
    parser.add_argument("--country", default="KE", help="Country code")
    parser.add_argument("--start", type=int, default=2019, help="Start year")
    parser.add_argument("--end", type=int, default=2024, help="End year")
    parser.add_argument("--output", default="analysis/outputs", help="Output directory")
    parser.add_argument("--config", help="JSON config file path")
    
    args = parser.parse_args()
    
    if args.config:
        import json
        with open(args.config, 'r') as f:
            config = json.load(f)
        study_name = config.get('study_name', args.study)
        location = config.get('location_name', args.location)
        country = config.get('country_code', args.country)
        start_year = config.get('start_year', args.start)
        end_year = config.get('end_year', args.end)
        output_dir = config.get('output_dir', args.output)
    else:
        study_name = args.study
        location = args.location
        country = args.country
        start_year = args.start
        end_year = args.end
        output_dir = args.output
    
    logger.info(f"Running analysis for {study_name} ({location}, {country})")
    
    results = run_analysis(
        study_name=study_name,
        location=location,
        country=country,
        start_year=start_year,
        end_year=end_year,
        output_dir=output_dir,
        run_seasonal=True,
        run_climate_correlation=True,
        run_lag_analysis=True,
        run_intervention_impact=True,
        run_resistance_trends=True,
        run_risk_modeling=True
    )
    
    logger.info("Analysis completed successfully!")
    
    # Print summary
    if 'summary' in results:
        from pprint import pprint
        print("\n=== Analysis Summary ===\n")
        pprint(results.get('summary', {}))


if __name__ == "__main__":
    main()