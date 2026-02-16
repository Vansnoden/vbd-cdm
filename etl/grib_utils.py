# etl/grib_utils.py

import xarray as xr
import pandas as pd
import numpy as np
import rioxarray
from shapely.geometry import Point, Polygon



def open_grib(filepath, variable=None):
    """Open a GRIB file with xarray + cfgrib engine."""
    # If variable is specified, try to filter at open time (if file contains multiple)
    kwargs = {'engine': 'cfgrib'}
    if variable:
        kwargs['filter_by_keys'] = {'shortName': variable}
    return xr.open_dataset(filepath, **kwargs)



def extract_point(ds, lat, lon, variable):
    """Extract value at the nearest grid point to (lat, lon)."""
    # Handle longitude wrapping if needed (assume ds uses 0-360 or -180-180)
    # Adjust lon to match dataset's coordinate system
    if hasattr(ds, 'longitude'):
        lon_vals = ds.longitude.values
        if lon_vals.min() >= 0 and lon < 0:
            lon += 360
        elif lon_vals.max() <= 180 and lon > 180:
            lon -= 360
    point = ds[variable].sel(latitude=lat, longitude=lon, method='nearest')
    return point.values.item() if point.values.size == 1 else float(point.values.mean())


def extract_polygon(ds, polygon, variable):
    """
    Average variable over a polygon (shapely Polygon) using rioxarray.
    Returns mean value (float) or None if no overlap.
    """
    # Ensure dataset has a proper CRS (usually WGS84)
    ds = ds.rio.write_crs("EPSG:4326")
    # Clip to polygon
    try:
        clipped = ds[variable].rio.clip([polygon], crs="EPSG:4326")
        # Compute mean over all remaining grid cells
        mean_val = clipped.mean(dim=['latitude', 'longitude']).values
        return mean_val.item() if mean_val.size == 1 else float(mean_val)
    except Exception:
        # No overlap or error
        return None


def aggregate_to_daily(ds, variable, method='mean'):
    """Resample sub-daily data to daily using method (mean, sum, min, max)."""
    # Ensure time is a datetime coordinate
    if 'time' not in ds.dims:
        return ds  # already daily?
    daily = ds[variable].resample(time='1D').reduce(method)
    return daily