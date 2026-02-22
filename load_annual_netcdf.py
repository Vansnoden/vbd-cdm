#!/usr/bin/env python

import sys
import re
from pathlib import Path
from datetime import datetime
import numpy as np

import xarray as xr
import pandas as pd
from shapely import wkt

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from database.session import SessionLocal
from database.models import Location, EnvironmentalFactor
from database.utils import ensure_dates_exist
from etl import netcdf_utils
from etl.mappings import NC_VARIABLE_MAP


def parse_filename(filepath):
    """
    Extract year and location from filename.
    Expected patterns:
        {year}_data_{location}_{id}.nc   (e.g., 2024_data_Mombasa_141.nc)
        {year}_data_{location}.nc        (e.g., 2024_data_Greater_Accra.nc)
    Returns (year, location) or (None, None) if no match.
    """
    # Pattern with ID
    pattern1 = r'(?P<year>\d{4})_data_(?P<location>[A-Za-z_]+)_\d+\.nc$'
    match = re.search(pattern1, filepath.name)
    if match:
        year = int(match.group('year'))
        location = match.group('location').lower()
        return year, location
    # Pattern without ID
    pattern2 = r'(?P<year>\d{4})_data_(?P<location>[A-Za-z_]+)\.nc$'
    match = re.search(pattern2, filepath.name)
    if match:
        year = int(match.group('year'))
        location = match.group('location').lower()
        return year, location
    return None, None


def load_annual_netcdf_files(data_root):
    """
    Process all annual NetCDF files in data_root and load into environmental_factor.
    """
    # Get database session
    db = SessionLocal()

    # Load all locations with their geometries and centroids
    locations = db.query(Location).all()
    loc_dict = {}
    for loc in locations:
        geom = wkt.loads(loc.geometry_wkt) if loc.geometry_wkt else None
        loc_dict[loc.location_code] = {
            'geometry': geom,
            'centroid_lat': loc.centroid_lat,
            'centroid_lon': loc.centroid_lon
        }

    if not loc_dict:
        print("No locations found in database. Run load_location_data.py first.")
        db.close()
        return

    # Find all NetCDF files recursively
    nc_files = list(Path(data_root).rglob("*.nc"))
    print(f"Found {len(nc_files)} NetCDF files.")

    for nc_file in nc_files:
        year, loc_code = parse_filename(nc_file)
        if loc_code is None:
            print(f"Skipping unrecognized file: {nc_file.name}")
            continue
        if loc_code not in loc_dict:
            print(f"Location '{loc_code}' not found in database. Skipping {nc_file.name}")
            continue

        print(f"\nProcessing {nc_file.name} (location: {loc_code}, year: {year})")

        # Open the dataset
        try:
            ds = xr.open_dataset(nc_file)
        except Exception as e:
            print(f"  Error opening file: {e}")
            continue

        # Get all unique dates from the time coordinate
        times = pd.to_datetime(ds.time.values)
        dates = sorted(set(t.date() for t in times))
        if not dates:
            print("  No time data found.")
            ds.close()
            continue

        # Ensure all dates exist in dim_time
        min_date = dates[0].strftime('%Y-%m-%d')
        max_date = dates[-1].strftime('%Y-%m-%d')
        ensure_dates_exist(min_date, max_date)

        # Get geometry info for this location
        geom_info = loc_dict[loc_code]
        geometry = geom_info['geometry']
        lat, lon = geom_info['centroid_lat'], geom_info['centroid_lon']

        # Process each variable defined in the mapping
        for nc_var, mapping in NC_VARIABLE_MAP.items():
            if nc_var not in ds:
                continue

            print(f"  Processing variable: {nc_var}")

            # Get the data array
            data = ds[nc_var]

            # If the variable has a 'step' dimension, sum over it to get daily totals
            if 'step' in data.dims:
                print(f"    Summing over 'step' dimension for {nc_var}...")
                data = data.sum(dim='step')

            # Get aggregation method name from mapping (default 'mean')
            method_name = mapping.get('agg_method', 'mean')
            
            # Convert method name to a callable
            method_func = {
                'mean': np.mean,
                'sum': np.sum,
                'max': np.max,
                'min': np.min,
            }.get(method_name)
            
            if method_func is None:
                print(f"    Warning: Unknown method '{method_name}', using np.mean")
                method_func = np.mean

            # Aggregate sub-daily data to daily using the callable
            try:
                # Create a temporary dataset containing just this variable
                # (to keep time coordinate)
                temp_ds = data.to_dataset()
                daily_da = netcdf_utils.aggregate_to_daily(
                    temp_ds, nc_var, method=method_func
                )
            except Exception as e:
                print(f"    Error aggregating: {e}")
                continue

            # Now iterate over each day
            for time_idx in range(len(daily_da.time)):
                day_slice = daily_da.isel(time=time_idx)
                current_date = pd.to_datetime(daily_da.time.values[time_idx]).date()
                date_id = current_date.strftime('%Y-%m-%d')

                # Extract value for this location
                try:
                    if geometry is None or geometry.is_empty:
                        if lat is None or lon is None:
                            continue
                        value = netcdf_utils.extract_point(day_slice, lat, lon)
                    else:
                        value = netcdf_utils.extract_polygon(day_slice, geometry)

                    if value is None:
                        continue

                    # Apply unit conversion
                    if mapping.get('convert'):
                        value = mapping['convert'](value)

                    # Prepare record for upsert
                    record = {
                        'location_code': loc_code,
                        'date_id': date_id,
                        mapping['column']: value
                    }

                    # Upsert into database
                    existing = db.query(EnvironmentalFactor).filter_by(
                        location_code=loc_code,
                        date_id=date_id
                    ).first()

                    if existing:
                        setattr(existing, mapping['column'], value)
                    else:
                        # Create full record with all columns (others will be NULL)
                        full_record = {
                            'location_code': loc_code,
                            'date_id': date_id,
                            mapping['column']: value
                        }
                        env_factor = EnvironmentalFactor(**full_record)
                        db.add(env_factor)

                except Exception as e:
                    print(f"    Error extracting value for {date_id}: {e}")
                    continue

        # Commit after each file to avoid large transactions
        db.commit()
        ds.close()
        print(f"  Finished processing {nc_file.name}")

    db.close()
    print("\nAll files processed.")


if __name__ == "__main__":
    data_root = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results"
    load_annual_netcdf_files(data_root)