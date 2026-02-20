#!/usr/bin/env python

# /home/void/Documents/codes/vbd-cdm/load_environmental_data.py

import sys
import re
from pathlib import Path
from datetime import datetime, timedelta

from database.models import Location
from database.session import SessionLocal
from database.utils import ensure_dates_exist
from etl.loaders import DatabaseLoader
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import xarray as xr
import geopandas as gpd
import pandas as pd
from shapely import wkt
from datetime import datetime

from database.session import SessionLocal
from database.models import Location, EnvironmentalFactor



# ---------- NetCDF utilities (simplified) ----------
def open_netcdf(filepath):
    return xr.open_dataset(filepath)

def extract_point(da, lat, lon):
    """Extract value at nearest grid point from a DataArray."""
    if 'longitude' in da.dims:
        lon_vals = da.longitude.values
        if lon_vals.min() >= 0 and lon < 0:
            lon += 360
        elif lon_vals.max() <= 180 and lon > 180:
            lon -= 360
    point = da.sel(latitude=lat, longitude=lon, method='nearest')
    if 'time' in point.dims and point.sizes['time'] > 1:
        point = point.mean(dim='time')
    return point.values.item() if point.values.size == 1 else float(point.values.mean())

def extract_polygon(da, polygon):
    """Average over polygon using rioxarray."""
    da = da.rio.write_crs("EPSG:4326")
    try:
        clipped = da.rio.clip([polygon], crs="EPSG:4326")
        mean_val = clipped.mean(dim=['latitude', 'longitude']).values
        if 'time' in clipped.dims:
            mean_val = mean_val.mean()
        return mean_val.item() if mean_val.size == 1 else float(mean_val)
    except Exception:
        return None

def aggregate_to_daily(ds, variable, method='mean'):
    if 'time' not in ds.dims or ds.sizes['time'] <= 1:
        return ds[variable]
    daily = ds[variable].resample(time='1D').reduce(method)
    return daily



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



def load_environmental_factor(start_date, end_date, data_dir, file_pattern_func=None):
    """
    Load environmental data from NetCDF files for all locations.
    :param start_date: string 'YYYY-MM-DD'
    :param end_date: string 'YYYY-MM-DD'
    :param data_dir: base directory containing the NetCDF files
    :param file_pattern_func: optional function(location_code, date) -> full file path.
                              If None, a default pattern is used:
                              f"{data_dir}/{location_code}_{date.strftime('%Y%m%d')}.nc"
    """
    session = SessionLocal()
    try:
        # 1. Get all locations with geometries
        locations = session.query(Location).all()
        if not locations:
            print("No locations found. Run load_location_data first.")
            return

        # Convert to a list of dicts with geometry
        loc_list = []
        for loc in locations:
            geom = wkt.loads(loc.geometry_wkt) if loc.geometry_wkt else None
            loc_list.append({
                'code': loc.location_code,
                'geometry': geom,
                'centroid_lat': loc.centroid_lat,
                'centroid_lon': loc.centroid_lon
            })

        # 2. Define default file pattern if not provided
        if file_pattern_func is None:
            def default_pattern(loc_code, date):
                return Path(data_dir) / f"{loc_code}_{date.strftime('%Y%m%d')}.nc"
            file_pattern_func = default_pattern

        # 3. Generate date range
        date_range = pd.date_range(start_date, end_date)

        # 4. For each date and location, process t2m
        for date in date_range:
            date_str = date.strftime('%Y-%m-%d')
            date_id = date_str

            for loc in loc_list:
                loc_code = loc['code']
                file_path = file_pattern_func(loc_code, date)

                if not Path(file_path).exists():
                    # Silently skip missing files (or print debug)
                    continue

                try:
                    ds = open_netcdf(file_path)
                except Exception as e:
                    print(f"Error opening {file_path}: {e}")
                    continue

                # We only have t2m in the mapping (others may be added later)
                # Hardcode t2m for now – you can move this to a mapping module later
                if 't2m' not in ds:
                    print(f"Variable 't2m' not found in {file_path}")
                    continue

                # Aggregate to daily if needed
                if 'time' in ds.dims and ds.sizes['time'] > 1:
                    da = aggregate_to_daily(ds, 't2m', method='mean')
                else:
                    da = ds['t2m']

                # Extract value
                geom = loc['geometry']
                if geom is None or geom.is_empty:
                    lat, lon = loc['centroid_lat'], loc['centroid_lon']
                    if lat is None or lon is None:
                        continue
                    val = extract_point(da, lat, lon)
                else:
                    val = extract_polygon(da, geom)

                if val is None:
                    continue

                # Convert Kelvin to Celsius
                val_celsius = val - 273.15

                # Upsert into environmental_factor
                existing = session.query(EnvironmentalFactor).filter_by(
                    location_code=loc_code,
                    date_id=date_id
                ).first()
                if existing:
                    existing.temperature_mean = val_celsius
                else:
                    env = EnvironmentalFactor(
                        location_code=loc_code,
                        date_id=date_id,
                        temperature_mean=val_celsius
                    )
                    session.add(env)

            # Commit after each date to avoid large transactions
            session.commit()
            print(f"Processed environmental data for {date_str}")

    finally:
        session.close()





if __name__ == "__main__":
    data_root = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results"
    load_netcdf_files(data_root)