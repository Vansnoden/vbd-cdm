#!/usr/bin/env python

# /home/void/Documents/codes/vbd-cdm/load_environmental_data.py

import sys
import re
from pathlib import Path
from datetime import datetime, timedelta

# Adjust path to project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.session import SessionLocal
from database.utils import ensure_dates_exist
from etl.loaders import DatabaseLoader


def parse_filename(filepath):
    """
    Extract year, location, and day-of-year from filename.
    Expected pattern: {year}_data_{location}_{doy}.nc
    Example: 2024_data_Mombasa_141.nc
    Returns (year, location, doy) or None if no match.
    """
    pattern = r'(?P<year>\d{4})_data_(?P<location>[A-Za-z]+)_(?P<doy>\d{1,3})\.nc$'
    match = re.search(pattern, filepath.name)
    if not match:
        return None
    year = int(match.group('year'))
    location = match.group('location').lower()   # e.g. 'mombasa'
    doy = int(match.group('doy'))
    return year, location, doy


def doy_to_date(year, doy):
    """Convert year and day-of-year to a date object."""
    return datetime(year, 1, 1) + timedelta(days=doy - 1)


def load_netcdf_files(data_root):
    """
    Find all .nc files in data_root (recursively), parse metadata,
    and load each using the DatabaseLoader.
    """
    # Collect all .nc files
    nc_files = list(Path(data_root).rglob("*.nc"))
    print(f"Found {len(nc_files)} NetCDF files.")

    # We'll collect all unique dates first to ensure dim_time is populated
    all_dates = set()
    file_info = []  # list of (file_path, location, date)

    for nc_file in nc_files:
        parsed = parse_filename(nc_file)
        if not parsed:
            print(f"Skipping file with unexpected name: {nc_file.name}")
            continue
        year, location, doy = parsed
        file_date = doy_to_date(year, doy)
        all_dates.add(file_date)
        file_info.append((nc_file, location, file_date))

    if not file_info:
        print("No valid files to load.")
        return

    # Ensure all required dates exist in dim_time
    min_date = min(all_dates).strftime('%Y-%m-%d')
    max_date = max(all_dates).strftime('%Y-%m-%d')
    ensure_dates_exist(min_date, max_date)
    print(f"Ensured dates from {min_date} to {max_date} exist.")

    # Now load each file
    db = SessionLocal()
    try:
        loader = DatabaseLoader(db)
        for nc_file, location, file_date in file_info:
            date_str = file_date.strftime('%Y-%m-%d')
            print(f"Loading {nc_file.name} for {location} on {date_str}...")
            try:
                # Adjust method name if necessary
                loader.load_netcdf_file(
                    file_path=nc_file,
                    location_code=location,
                    date=date_str
                )
                print(f"  ✅ Loaded")
            except Exception as e:
                print(f"  ❌ Failed: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    data_root = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results"
    load_netcdf_files(data_root)