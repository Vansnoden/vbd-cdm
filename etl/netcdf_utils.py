# etl/netcdf_utils.py
import xarray as xr
import pandas as pd
import numpy as np
import rioxarray
from shapely.geometry import Point, Polygon


def open_netcdf(filepath):
    """Open a NetCDF file with xarray."""
    return xr.open_dataset(filepath)


def extract_point(da, lat, lon):
    """
    Extract value from a DataArray at the nearest grid point.
    da: DataArray with dimensions latitude, longitude (and possibly time).
    """
    # Align longitude system if needed
    if 'longitude' in da.dims:
        lon_vals = da.longitude.values
        if lon_vals.min() >= 0 and lon < 0:
            lon += 360
        elif lon_vals.max() <= 180 and lon > 180:
            lon -= 360
    # Select nearest point – if time dimension exists, take first time?
    # For daily aggregated data, time should be a single step.
    point = da.sel(latitude=lat, longitude=lon, method='nearest')
    # If time still present, average over time (should be single after aggregation)
    if 'time' in point.dims and point.sizes['time'] > 1:
        point = point.mean(dim='time')
    return point.values.item() if point.values.size == 1 else float(point.values.mean())


def extract_polygon(da, polygon):
    """
    Average variable over a polygon using rioxarray.
    da: DataArray with latitude/longitude dimensions.
    """
    # Ensure CRS
    da = da.rio.write_crs("EPSG:4326")
    try:
        clipped = da.rio.clip([polygon], crs="EPSG:4326")
        mean_val = clipped.mean(dim=['latitude', 'longitude']).values
        # If time still present, average over time
        if 'time' in clipped.dims:
            mean_val = mean_val.mean()
        return mean_val.item() if mean_val.size == 1 else float(mean_val)
    except Exception:
        return None


def aggregate_to_daily(ds, variable, method='mean'):
    """
    Resample sub-daily data to daily using method.
    Returns a DataArray with a single time step per day.
    """
    # Ensure time is datetime
    if not np.issubdtype(ds.time.dtype, np.datetime64):
        ds['time'] = pd.to_datetime(ds.time.values)
    daily = ds[variable].resample(time='1D').reduce(method)
    return daily
