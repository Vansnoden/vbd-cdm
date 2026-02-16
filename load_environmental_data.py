#!/usr/bin/env python

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.session import SessionLocal
from database.utils import ensure_dates_exist
from etl.loaders import DatabaseLoader


def load_environmental_factor(start_date, end_date, data_dir):
    # Ensure dim_time is populated
    ensure_dates_exist(start_date, end_date)
    # Define a function that returns the NetCDF file path for a given location and date
    # ADJUST THIS TO MATCH YOUR ACTUAL FILE NAMING CONVENTION
    def file_path_func(location_code, date):
        # Example: /data/location/YYYY/YYYYMMDD_location.nc
        # location_code might be 'mombasa' or 'siaya'
        year = date.strftime('%Y')
        date_str = date.strftime('%Y%m%d')
        # Assume files are stored in a directory per location
        base_dir = Path(data_dir)
        # File name pattern: 2024_data_{location}_{day}.nc? But day is not enough.
        # We'll assume the file is named like: {location}_{date_str}.nc
        # Update this to your actual convention!
        return base_dir / f"{location_code}_{date_str}.nc"
    
    db = SessionLocal()
    try:
        loader = DatabaseLoader(db)
        loader.load_from_netcdf(start_date, end_date, file_path_func)
    finally:
        db.close()

if __name__ == "__main__":

    load_environmental_factor(
        start_date = '2024-01-01', 
        end_date = '2025-12-31',
        data_dir = '/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results/2024_data'
    )