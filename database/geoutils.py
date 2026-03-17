
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