import os
import gc
import pandas as pd
import numpy as np
from functools import wraps
from typing import Generator, Optional, List, Dict, Any, Union
import warnings
import logging
from rasterio.windows import Window

import rasterio

logger = logging.getLogger(__name__)

# ========================= Helper Decorators =========================

def ensure_file_exists():
    """Decorator to ensure the input file exists before calling the function."""
    def decorator(func):
        @wraps(func)
        def wrapper(fp, *args, **kwargs):
            if not os.path.exists(fp):
                raise FileNotFoundError(f"File not found: {fp}")
            return func(fp, *args, **kwargs)
        return wrapper
    return decorator


def validate_extension(*valid_extensions: str):
    """
    Decorator to validate file extension before calling the function.
    Also checks file existence.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(fp: str, *args, **kwargs):
            if not os.path.exists(fp):
                raise FileNotFoundError(f"File not found: {fp}")
            ext = os.path.splitext(fp)[1].lower().lstrip('.')
            if ext not in valid_extensions:
                raise ValueError(
                    f"Invalid file extension. Expected {valid_extensions}, got .{ext}"
                )
            return func(fp, *args, **kwargs)
        return wrapper
    return decorator


# ========================= CSV Utilities =========================

def optimized_df_to_csv(df: pd.DataFrame, output_path: str, chunksize: int = 100000) -> bool:
    """
    Write a DataFrame to CSV in chunks to save memory.
    Returns True if successful, False if df is empty or None.
    """
    if df is None or df.empty:
        return False
    if chunksize >= len(df):
        df.to_csv(output_path, index=False)
    else:
        with open(output_path, 'w', encoding='utf-8') as f:
            for i in range(0, len(df), chunksize):
                chunk = df.iloc[i:i+chunksize]
                chunk.to_csv(f, index=False, header=(i == 0), mode='a')
    return True


# ========================= Raster / GeoTIFF / COG =========================

def _raster_to_df(
    fp: str,
    band: Optional[Union[int, List[int]]] = None,
) -> pd.DataFrame:
    """
    Internal: open any raster supported by rioxarray and return as DataFrame.

    Args:
        fp:   Path to the raster file.
        band: Band selection, using 1-based indexing (matching GDAL).
              None    -> all bands (one row per pixel per band).
              int     -> a single band, e.g. band=1 for the mean of a
                         Mean/LCI/UCI triple.
              list    -> multiple bands.

    Returns:
        Long-format DataFrame with at least x, y, value columns. When
        `band` is None and the raster has more than one band, a `band`
        column is also present.
    """
    import rioxarray
    raster = rioxarray.open_rasterio(fp, chunks='auto')

    n_bands = raster.sizes.get('band', 1)

    if band is not None:
        if isinstance(band, int):
            if band < 1 or band > n_bands:
                raise ValueError(
                    f"band={band} out of range; file has {n_bands} band(s)"
                )
            # drop=True removes the scalar band coordinate so the resulting
            # DataFrame has no `band` column.
            raster = raster.sel(band=band, drop=True)
        elif isinstance(band, (list, tuple)):
            b = list(band)
            if not all(isinstance(x, int) and 1 <= x <= n_bands for x in b):
                raise ValueError(
                    f"band list {b} out of range; file has {n_bands} band(s)"
                )
            raster = raster.sel(band=b)
        else:
            raise ValueError(
                f"band must be int, list, or None; got {type(band).__name__}"
            )

    df = raster.to_dataframe(name='value').reset_index()
    if hasattr(raster, '_file_obj') and raster._file_obj:
        raster._file_obj.close()
    del raster
    gc.collect()
    return df


@validate_extension('cog', 'tif', 'tiff')
def cog_to_df(
    fp: str,
    band: Optional[Union[int, List[int]]] = None,
) -> pd.DataFrame:
    """Convert Cloud Optimized GeoTIFF (COG) or regular GeoTIFF to DataFrame."""
    return _raster_to_df(fp, band=band)


@validate_extension('cog', 'tif', 'tiff')
def cog_to_csv(
    fp: str,
    csv_path: str,
    chunksize: int = 50000,
    band: Optional[Union[int, List[int]]] = None,
) -> bool:
    """Convert COG/TIFF directly to CSV."""
    df = cog_to_df(fp, band=band)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('tif', 'tiff')
def tif_to_df_chunked(
    fp: str,
    window_size: int = 512,
    nodata_threshold: float = -9999.0,
    band: int = 1,
) -> Generator[pd.DataFrame, None, None]:
    """
    Generator that yields DataFrames for each block of a GeoTIFF.
    Filters out NoData values (<= nodata_threshold).
    Yields DataFrames with columns: 'x', 'y', 'value' (single band).

    Args:
        fp:               Path to the GeoTIFF.
        window_size:      Window size in pixels.
        nodata_threshold: Values <= this are dropped. Default -9999.
        band:             1-based band index to read. Default 1.
    """
    import rasterio
    from rasterio.windows import Window
    import numpy as np
    import pandas as pd

    with rasterio.open(fp) as src:
        if band < 1 or band > src.count:
            raise ValueError(
                f"band={band} out of range; file has {src.count} band(s)"
            )

        height, width = src.height, src.width
        nodata = src.nodata if src.nodata is not None else nodata_threshold

        for row_off in range(0, height, window_size):
            for col_off in range(0, width, window_size):
                win = Window(col_off, row_off,
                             min(window_size, width - col_off),
                             min(window_size, height - row_off))
                data = src.read(band, window=win)  # 2D array
                if data.size == 0:
                    continue

                rows, cols = data.shape
                col_indices = np.arange(cols)
                row_indices = np.arange(rows)
                xs, ys = src.transform * np.meshgrid(col_indices, row_indices)

                flat_xs   = xs.flatten()
                flat_ys   = ys.flatten()
                flat_vals = data.flatten()

                mask = flat_vals > nodata_threshold
                if not np.any(mask):
                    continue

                yield pd.DataFrame({
                    'x': flat_xs[mask],
                    'y': flat_ys[mask],
                    'value': flat_vals[mask],
                })


@validate_extension('tif', 'tiff')
def tif_to_df(
    fp: str,
    band: Optional[Union[int, List[int]]] = None,
) -> pd.DataFrame:
    """Alias for cog_to_df."""
    return cog_to_df(fp, band=band)


@validate_extension('tif', 'tiff')
def tif_to_csv(
    fp: str,
    csv_path: str,
    chunksize: int = 50000,
    band: Optional[Union[int, List[int]]] = None,
) -> bool:
    """Alias for cog_to_csv."""
    return cog_to_csv(fp, csv_path, chunksize, band=band)
    

# ========================= NetCDF (including .nc and .nc4) =========================

def _netcdf_to_df(fp: str, variable: Optional[str] = None, time_idx: int = 0) -> pd.DataFrame:
    """
    Internal: open NetCDF/GRIB and return a DataFrame for a specific variable and time step.
    If variable is None, uses the first data variable.
    """
    import xarray as xr
    if fp.lower().endswith(('.grib', '.grib2')):
        ds = xr.open_dataset(fp, engine='cfgrib', chunks='auto')
    else:
        ds = xr.open_dataset(fp, chunks='auto')

    if variable is None:
        variable = list(ds.data_vars.keys())[0]
        warnings.warn(f"No variable specified, using '{variable}'")

    da = ds[variable]
    if 'time' in da.dims:
        da = da.isel(time=time_idx)
    df = da.to_dataframe().reset_index()
    ds.close()
    gc.collect()
    return df


@validate_extension('nc', 'nc4')
def nc_to_df(fp: str, variable: Optional[str] = None, time_idx: int = 0) -> pd.DataFrame:
    """Convert NetCDF (.nc or .nc4) to DataFrame, optionally selecting variable and time index."""
    return _netcdf_to_df(fp, variable, time_idx)


@validate_extension('nc', 'nc4')
def nc_to_csv(fp: str, csv_path: str, chunksize: int = 100000,
              variable: Optional[str] = None, time_idx: int = 0) -> bool:
    """Convert NetCDF directly to CSV."""
    df = nc_to_df(fp, variable, time_idx)
    return optimized_df_to_csv(df, csv_path, chunksize)


# Keep for backward compatibility
nc4_to_df = nc_to_df
nc4_to_csv = nc_to_csv


# ========================= GRIB =========================

@validate_extension('grib', 'grib2')
def grib_to_df(fp: str, variable: Optional[str] = None, time_idx: int = 0) -> pd.DataFrame:
    """Convert GRIB file to DataFrame (uses cfgrib engine)."""
    return _netcdf_to_df(fp, variable, time_idx)


@validate_extension('grib', 'grib2')
def grib_to_csv(fp: str, csv_path: str, chunksize: int = 100000,
                variable: Optional[str] = None, time_idx: int = 0) -> bool:
    """Convert GRIB directly to CSV."""
    df = grib_to_df(fp, variable, time_idx)
    return optimized_df_to_csv(df, csv_path, chunksize)


def _get_engine(fp: str) -> Optional[str]:
    """Return 'cfgrib' for GRIB files, otherwise None for auto-detection."""
    if fp.lower().endswith(('.grib', '.grib2')):
        return 'cfgrib'
    return None


def _get_time_dim(ds) -> str:
    """Return the name of the time dimension (supports 'time', 't', 'Time')."""
    for candidate in ['time', 't', 'Time']:
        if candidate in ds.dims:
            return candidate
    return None


def timeseries_to_df_chunked(
    fp: str,
    variables: Optional[List[str]] = None,
    time_chunk_size: int = 100,
    output_csv: Optional[str] = None
):
    """
    Process timeseries data in chunks along the time dimension.
    
    Args:
        fp: Path to NetCDF or GRIB file
        variables: List of variable names (None = all variables)
        time_chunk_size: Number of time steps per chunk
        output_csv: If provided, write all chunks to this CSV file (no DataFrame returned).
                    If None, yields one DataFrame per chunk.
    
    Yields:
        DataFrame for each time chunk (if output_csv is None)
    Returns:
        bool (if output_csv provided) indicating success
    """
    import xarray as xr
    engine = _get_engine(fp)
    
    # Open dataset with chunking along time dimension
    ds = xr.open_dataset(fp, engine=engine, chunks={'time': time_chunk_size})
    
    if variables is None:
        # Skip auxiliary variables that xarray reports as data_vars
        # but are not geophysical fields: crs, spatial_ref, expver,
        # time_bnds, lat_bnds, lon_bnds, height, number, surface, depth.
        # The `ndim >= 2` clause catches anything else that isn't a
        # lat/lon field regardless of its name.
        _AUX = {"crs", "spatial_ref", "expver", "number", "surface",
                "height", "depth", "time_bnds", "lat_bnds", "lon_bnds"}
        variables = [
            v for v in ds.data_vars
            if v.lower() not in _AUX and ds[v].ndim >= 2
        ]
    
    time_dim = _get_time_dim(ds)
    if time_dim is None:
        # No time dimension – treat whole dataset as one chunk
        n_time = 1
        time_dim = None
    else:
        n_time = ds.dims[time_dim]
    
    csv_file = None
    try:
        if output_csv:
            csv_file = open(output_csv, 'w', encoding='utf-8', newline='')
        
        # Iterate over time chunks (or single chunk if no time dimension)
        if time_dim is None:
            ranges = [(0, 1)]
        else:
            ranges = [(start, min(start + time_chunk_size, n_time))
                      for start in range(0, n_time, time_chunk_size)]
        
        for start, end in ranges:
            if time_dim is not None:
                ds_chunk = ds.isel({time_dim: slice(start, end)})
            else:
                ds_chunk = ds
            
            # Build long-format DataFrame for this chunk
            chunk_dfs = []
            for var in variables:
                df = ds_chunk[var].to_dataframe().reset_index()
                df['variable'] = var
                df = df.rename(columns={var: 'value'})
                # Keep only dimension columns + variable + value
                dims = list(ds_chunk.dims.keys())
                keep_cols = [col for col in df.columns if col in dims] + ['variable', 'value']
                df = df[keep_cols]
                chunk_dfs.append(df)
            
            chunk_df = pd.concat(chunk_dfs, ignore_index=True)
            
            if output_csv:
                mode = 'w' if csv_file.tell() == 0 else 'a'
                header = (mode == 'w')
                chunk_df.to_csv(csv_file, index=False, mode=mode, header=header)
            else:
                yield chunk_df
            
            del chunk_df, chunk_dfs, ds_chunk
            gc.collect()
        
        if output_csv:
            logger.info(f"Timeseries data written to {output_csv} (total time steps: {n_time})")
            return True
    
    except Exception as e:
        if output_csv and csv_file:
            csv_file.close()
        raise e
    finally:
        ds.close()
        if output_csv and csv_file:
            csv_file.close()


@validate_extension('nc', 'nc4', 'grib', 'grib2')
def timeseries_to_df(
    fp: str,
    variables: Optional[List[str]] = None,
    warn_large: bool = True,
    max_expected_rows: int = 10_000_000
) -> pd.DataFrame:
    """
    Extract all time steps for given variables into a single DataFrame.
    WARNING: Only use for small to medium files. For large files, use timeseries_to_df_chunked.
    
    Args:
        fp: Path to NetCDF or GRIB file
        variables: List of variable names (None = all variables)
        warn_large: If True, show warning when estimated rows exceed max_expected_rows
        max_expected_rows: Threshold for warning (default 10 million)
    
    Returns:
        Single DataFrame with all time steps and variables (long format)
    """
    import xarray as xr
    engine = _get_engine(fp)
    ds = xr.open_dataset(fp, engine=engine, chunks='auto')
    
    try:
        if variables is None:
            # Skip auxiliary variables that xarray reports as data_vars
            # but are not geophysical fields: crs, spatial_ref, expver,
            # time_bnds, lat_bnds, lon_bnds, height, number, surface, depth.
            # The `ndim >= 2` clause catches anything else that isn't a
            # lat/lon field regardless of its name.
            _AUX = {"crs", "spatial_ref", "expver", "number", "surface",
                    "height", "depth", "time_bnds", "lat_bnds", "lon_bnds"}
            variables = [
                v for v in ds.data_vars
                if v.lower() not in _AUX and ds[v].ndim >= 2
            ]
        # Estimate size
        time_dim = _get_time_dim(ds)
        n_time = ds.dims[time_dim] if time_dim else 1
        sample_var = ds[variables[0]]
        n_pixels = sample_var.size // n_time if n_time > 1 else sample_var.size
        estimated_rows = n_time * n_pixels * len(variables)
        if warn_large and estimated_rows > max_expected_rows:
            warnings.warn(
                f"Estimated rows: {estimated_rows:,} (> {max_expected_rows:,}). "
                "This may cause memory issues. Use timeseries_to_df_chunked() instead.",
                UserWarning
            )
        dfs = []
        for var in variables:
            df = ds[var].to_dataframe().reset_index()
            df['variable'] = var
            df = df.rename(columns={var: 'value'})
            dims = list(ds.dims.keys())
            keep_cols = [col for col in df.columns if col in dims] + ['variable', 'value']
            df = df[keep_cols]
            dfs.append(df)
        result = pd.concat(dfs, ignore_index=True)
    finally:
        ds.close()
    return result


def timeseries_to_csv(
    fp: str,
    csv_path: str,
    variables: Optional[List[str]] = None,
    time_chunk_size: int = 100
) -> bool:
    """
    Convert timeseries file directly to CSV using chunked writing.
    This is the recommended method for large files.
    """
    return timeseries_to_df_chunked(
        fp=fp,
        variables=variables,
        time_chunk_size=time_chunk_size,
        output_csv=csv_path
    )


# ========================= Statistical Files (SPSS, Stata, SAS) =========================

def _readstat_chunked(fp: str, reader_func, chunksize: int = 50000) -> pd.DataFrame:
    """Generic chunked reader for pyreadstat-based formats."""
    import pyreadstat
    reader = reader_func(fp, chunksize=chunksize)
    chunks = []
    for df_chunk, _ in reader:
        chunks.append(df_chunk)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


@validate_extension('dta')
def dta_to_df(fp: str, chunksize: int = 50000) -> pd.DataFrame:
    """Convert Stata .dta file to DataFrame."""
    import pyreadstat
    return _readstat_chunked(fp, lambda f, cs: pyreadstat.read_file_in_chunks(
        pyreadstat.read_dta, f, chunksize=cs), chunksize)


@validate_extension('dta')
def dta_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = dta_to_df(fp, chunksize)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('sas7bdat')
def sas7bdat_to_df(fp: str, chunksize: int = 50000) -> pd.DataFrame:
    """Convert SAS .sas7bdat file to DataFrame."""
    import pyreadstat
    return _readstat_chunked(fp, lambda f, cs: pyreadstat.read_file_in_chunks(
        pyreadstat.read_sas7bdat, f, chunksize=cs), chunksize)


@validate_extension('sas7bdat')
def sas7bdat_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = sas7bdat_to_df(fp, chunksize)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('sav')
def sav_to_df(fp: str, chunksize: int = 50000) -> pd.DataFrame:
    """Convert SPSS .sav file to DataFrame."""
    import pyreadstat
    return _readstat_chunked(fp, lambda f, cs: pyreadstat.read_file_in_chunks(
        pyreadstat.read_sav, f, chunksize=cs), chunksize)


@validate_extension('sav')
def sav_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = sav_to_df(fp, chunksize)
    return optimized_df_to_csv(df, csv_path, chunksize)


# ========================= Geospatial Vector Formats =========================

def _vector_to_df(fp: str, read_geometry: bool = False) -> pd.DataFrame:
    """Internal: read vector file with pyogrio (fast) and optionally drop geometry."""
    import pyogrio
    df = pyogrio.read_dataframe(fp, read_geometry=read_geometry)
    return df


@validate_extension('geojson')
def geojson_to_df(fp: str) -> pd.DataFrame:
    """Convert GeoJSON to DataFrame (drops geometry)."""
    return _vector_to_df(fp, read_geometry=False)


@validate_extension('geojson')
def geojson_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = geojson_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('gpkg')
def gpkg_to_df(fp: str) -> pd.DataFrame:
    """Convert GeoPackage to DataFrame (drops geometry)."""
    return _vector_to_df(fp, read_geometry=False)


@validate_extension('gpkg')
def gpkg_to_csv(fp: str, csv_path: str, chunksize: int = 100000) -> bool:
    df = gpkg_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('shp')
def shp_to_df(fp: str) -> pd.DataFrame:
    """Convert Shapefile to DataFrame (drops geometry)."""
    return _vector_to_df(fp, read_geometry=False)


@validate_extension('shp')
def shp_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = shp_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


# ========================= JSON / TSV / Excel / XML =========================

@validate_extension('json')
def json_to_df(fp: str, lines: bool = True, chunksize: int = 50000) -> pd.DataFrame:
    """
    Convert JSON file to DataFrame.
    By default assumes line-delimited JSON (lines=True). For regular JSON, set lines=False.
    """
    reader = pd.read_json(fp, lines=lines, chunksize=chunksize)
    chunks = []
    for chunk in reader:
        chunks.append(chunk)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


@validate_extension('json')
def json_to_csv(fp: str, csv_path: str, chunksize: int = 50000, lines: bool = True) -> bool:
    df = json_to_df(fp, lines=lines, chunksize=chunksize)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('tsv')
def tsv_to_df(fp: str, chunksize: int = 50000, **kwargs) -> pd.DataFrame:
    """Convert TSV (tab-separated) file to DataFrame."""
    reader = pd.read_csv(fp, sep='\t', chunksize=chunksize, **kwargs)
    chunks = []
    for chunk in reader:
        chunks.append(chunk)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


@validate_extension('tsv')
def tsv_to_csv(fp: str, csv_path: str, chunksize: int = 50000, **kwargs) -> bool:
    df = tsv_to_df(fp, chunksize, **kwargs)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('xlsx', 'xls')
def xlsx_to_df(fp: str, sheet_name: Union[str, int] = 0, chunksize: int = 50000) -> pd.DataFrame:
    """
    Convert Excel file to DataFrame.

    pandas.read_excel() has no chunksize parameter, unlike read_csv. The
    whole sheet is read in one call. The `chunksize` argument is retained
    in the signature for API symmetry with the other readers; it is not
    passed to pandas and has no effect on the return.

    Uses 'calamine' engine if available (fast), otherwise 'openpyxl'.
    """
    try:
        return pd.read_excel(fp, sheet_name=sheet_name, engine='calamine')
    except ImportError:
        return pd.read_excel(fp, sheet_name=sheet_name, engine='openpyxl')


@validate_extension('xlsx', 'xls')
def xlsx_to_csv(fp: str, csv_path: str, chunksize: int = 50000,
                sheet_name: Union[str, int] = 0) -> bool:
    df = xlsx_to_df(fp, sheet_name=sheet_name, chunksize=chunksize)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('xml')
def xml_to_df(fp: str, record_tag: str = 'record', max_rows: Optional[int] = None) -> pd.DataFrame:
    """
    Convert XML file to DataFrame by streaming.
    Assumes a flat structure where each record is an element with child tags as columns.
    """
    import xml.etree.ElementTree as ET
    rows = []
    for event, elem in ET.iterparse(fp, events=('end',)):
        if elem.tag == record_tag:
            row = {child.tag: child.text for child in elem}
            rows.append(row)
            elem.clear()
            if max_rows is not None and len(rows) >= max_rows:
                break
    return pd.DataFrame(rows)


@validate_extension('xml')
def xml_to_csv(fp: str, csv_path: str, chunksize: int = 50000,
               record_tag: str = 'record') -> bool:
    """Convert XML to CSV, reading all records (no row limit)."""
    df = xml_to_df(fp, record_tag=record_tag, max_rows=None)
    return optimized_df_to_csv(df, csv_path, chunksize)


# ========================= YAML Config Loader =========================

@ensure_file_exists()
def load_yaml_config(fp: str) -> Dict[str, Any]:
    """Load YAML configuration file into a dictionary."""
    import yaml
    with open(fp, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)