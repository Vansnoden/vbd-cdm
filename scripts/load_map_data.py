#!/usr/bin/env python
# scripts/load_map_data.py

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.session import SessionLocal
from etl.loaders import DatabaseLoader
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_map_incidence_data():
    """
    Example: Load MAP malaria incidence rasters for multiple years.
    """
    db = SessionLocal()
    loader = DatabaseLoader(db)
    
    try:
        # Example: Load incidence rasters for 2020-2024
        raster_files = [
            "/path/to/MAP/2020_incidence.tif",
            "/path/to/MAP/2021_incidence.tif", 
            "/path/to/MAP/2022_incidence.tif",
            "/path/to/MAP/2023_incidence.tif",
            "/path/to/MAP/2024_incidence.tif"
        ]
        
        years = [2020, 2021, 2022, 2023, 2024]
        
        for raster_file, year in zip(raster_files, years):
            if Path(raster_file).exists():
                stats = loader.load_map_raster_to_disease_burden(
                    raster_path=raster_file,
                    year=year,
                    disease='malaria',
                    source='Malaria Atlas Project',
                    version='2025',
                    metric_type='incidence_rate',
                    stat='mean'
                )
                
                print(f"\nYear {year} Results:")
                print(f"  Total locations: {stats['total_locations']}")
                print(f"  Successfully loaded: {stats['loaded']}")
                print(f"  Failed: {stats['failed']}")
                
                if stats['errors']:
                    print(f"\nErrors (first 5):")
                    for error in stats['errors'][:5]:
                        print(f"  - {error}")
            else:
                print(f"File not found: {raster_file}")
    
    finally:
        db.close()

def load_map_prevalence_data():
    """
    Example: Load MAP prevalence rasters.
    """
    db = SessionLocal()
    loader = DatabaseLoader(db)
    
    try:
        raster_files = [
            "/path/to/MAP/2020_prevalence.tif",
            "/path/to/MAP/2021_prevalence.tif"
        ]
        years = [2020, 2021]
        
        stats_multi = loader.load_multiple_map_rasters(
            raster_files=raster_files,
            years=years,
            disease='malaria',
            source='Malaria Atlas Project',
            metric_mapping={
                0: 'prevalence_rate',
                1: 'prevalence_rate'
            }
        )
        
        for i, stats in enumerate(stats_multi):
            print(f"\nYear {years[i]} Results:")
            print(f"  Loaded: {stats['loaded']} locations")
    
    finally:
        db.close()

if __name__ == "__main__":
    print("Loading MAP incidence data...")
    load_map_incidence_data()