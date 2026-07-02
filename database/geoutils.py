
import reverse_geocode as rg
import numpy as np
from typing import Tuple, Optional
import pandas as pd
from functools import lru_cache

from database.constants import COUNTRY_LOOKUP


@lru_cache(maxsize=1000)
def get_country_from_coords(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Get country name and country code from latitude and longitude coordinates.
    
    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        
    Returns:
        Tuple of (country_name, country_code) or (None, None) if not found
    """
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
        return None, None
    
    try:
        # Try reverse_geocode first (requires internet for first lookup, caches results)
        result = rg.search([(lat, lon)])
        if result and len(result) > 0:
            country_name = result[0].get('country')
            country_code = result[0].get('country_code')
            return country_name, country_code
    except Exception as e:
        print(f"Reverse geocode failed for ({lat}, {lon}): {e}")
    
    # Fallback to nearest known point lookup
    return _get_country_from_nearest(lat, lon)


def _get_country_from_nearest(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Fallback method: find nearest known coordinate in lookup table.
    Useful for offline operation or when reverse_geocode fails.
    """
    if not COUNTRY_LOOKUP:
        return None, None
    
    # Find nearest known coordinate
    min_dist = float('inf')
    best_match = None
    
    for (ref_lat, ref_lon), country_info in COUNTRY_LOOKUP.items():
        # Simple Euclidean distance (not accurate for large distances but works for rough matching)
        dist = np.sqrt((lat - ref_lat)**2 + (lon - ref_lon)**2)
        if dist < min_dist and dist < 5.0:  # Within ~5 degrees (~550km at equator)
            min_dist = dist
            best_match = country_info
    
    return best_match if best_match else (None, None)


def bulk_get_countries(coords_list):
    """
    Get countries for multiple coordinates in batch.
    
    Args:
        coords_list: List of (lat, lon) tuples
        
    Returns:
        List of (country_name, country_code) tuples
    """
    results = []
    for lat, lon in coords_list:
        results.append(get_country_from_coords(lat, lon))
    return results


def add_country_to_dataframe(df, lat_col='centroid_lat', lon_col='centroid_lon'):
    """
    Add country columns to a DataFrame containing coordinates.
    
    Args:
        df: DataFrame with lat/lon columns
        lat_col: Name of latitude column
        lon_col: Name of longitude column
        
    Returns:
        DataFrame with added 'country_name' and 'country_code' columns
    """
    df = df.copy()
    countries = []
    codes = []
    
    for idx, row in df.iterrows():
        country, code = get_country_from_coords(row[lat_col], row[lon_col])
        countries.append(country)
        codes.append(code)
    
    df['country_name'] = countries
    df['country_code'] = codes
    return df


def enrich_location_dataframe(
    df: pd.DataFrame,
    lat_col: str = 'centroid_lat',
    lon_col: str = 'centroid_lon',
    country_code_col: str = 'country_code',
    country_name_col: str = 'country_name'
) -> pd.DataFrame:
    """
    Fill missing country_code and country_name from centroid_lat/centroid_lon.
    Adds the columns if they are missing.
    """
    if df.empty:
        return df

    df = df.copy()

    # Ensure both columns exist
    if country_code_col not in df.columns:
        df[country_code_col] = None
    if country_name_col not in df.columns:
        df[country_name_col] = None

    # Identify rows that need enrichment: null or empty string
    missing_mask = (
        df[country_code_col].isna() | 
        (df[country_code_col] == '')
    )

    if not missing_mask.any():
        return df

    # Build list of (lat, lon) for rows needing enrichment and having valid coords
    coords_list = []
    indices = []
    for idx in df.index[missing_mask]:
        lat = df.loc[idx, lat_col]
        lon = df.loc[idx, lon_col]
        if pd.notna(lat) and pd.notna(lon):
            coords_list.append((lat, lon))
            indices.append(idx)

    if not coords_list:
        return df

    # Bulk geocode (cached + fallback to local lookup)
    results = bulk_get_countries(coords_list)   # list of (country_name, country_code)

    # Assign results back
    for idx, (name, code) in zip(indices, results):
        if code is not None:
            df.loc[idx, country_code_col] = code
            df.loc[idx, country_name_col] = name

    return df