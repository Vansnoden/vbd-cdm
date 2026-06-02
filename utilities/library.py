import pandas as pd
import numpy as np
import os
from functools import wraps
from typing import Optional, Generator
import gc


# fp: file path, op: output path


def ensure_file_exists():
    def decorator(func):
        @wraps(func)
        def wrapper(fp, *args, **kwargs):
            if not os.path.exists(fp):
                raise FileNotFoundError(f"File not found: {fp}")
            return func(fp, *args, **kwargs)
        return wrapper
    return decorator


def validate_extension(*valid_extensions):
    def decorator(func):
        @wraps(func)
        def wrapper(fp, *args, **kwargs):
            if not os.path.exists(fp):
                raise FileNotFoundError(f"File not found: {fp}")
            ext = os.path.splitext(fp)[1].lower().lstrip('.')
            if ext not in valid_extensions:
                raise ValueError(f"Invalid file extension. Expected {valid_extensions}, got .{ext}")
            return func(fp, *args, **kwargs)
        return wrapper
    return decorator


def optimized_df_to_csv(df: pd.DataFrame, output_path: str, chunksize: int = 100000) -> bool:
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


@validate_extension('cog', 'tif', 'tiff')
def cog_to_df(fp: str) -> pd.DataFrame:
    import rioxarray
    raster = rioxarray.open_rasterio(fp, chunks='auto')
    df = raster.to_dataframe(name='value').reset_index()
    raster.close()
    gc.collect()
    return df


@validate_extension('cog', 'tif', 'tiff')
def cog_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = cog_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('dta')
def dta_to_df(fp: str) -> pd.DataFrame:
    import pyreadstat
    reader = pyreadstat.read_file_in_chunks(pyreadstat.read_dta, fp, chunksize=50000)
    chunks = []
    for df_chunk, _ in reader:
        chunks.append(df_chunk)
    return pd.concat(chunks, ignore_index=True)


@validate_extension('dta')
def dta_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = dta_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('geojson')
def geojson_to_df(fp: str) -> pd.DataFrame:
    import pyogrio
    df = pyogrio.read_dataframe(fp, read_geometry=False)
    return df


@validate_extension('geojson')
def geojson_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = geojson_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('gpkg')
def gpkg_to_df(fp: str) -> pd.DataFrame:
    import geopandas as gpd
    gdf = gpd.read_file(fp, engine='pyogrio', read_geometry=False)
    return gdf


@validate_extension('gpkg')
def gpkg_to_csv(fp: str, csv_path: str, chunksize: int = 100000) -> bool:
    df = gpkg_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('grib', 'grib2')
def grib_to_df(fp: str) -> pd.DataFrame:
    import xarray as xr
    ds = xr.open_dataset(fp, engine='cfgrib', chunks='auto')
    var = list(ds.data_vars.keys())[0]
    df = (ds[var] * 1.0).isel(time=0).to_dataframe().reset_index()
    ds.close()
    gc.collect()
    return df


@validate_extension('grib', 'grib2')
def grib_to_csv(fp: str, csv_path: str, chunksize: int = 100000) -> bool:
    df = grib_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('json')
def json_to_df(fp: str) -> pd.DataFrame:
    reader = pd.read_json(fp, lines=True, chunksize=50000)
    chunks = []
    for chunk in reader:
        chunks.append(chunk)
    return pd.concat(chunks, ignore_index=True)


@validate_extension('json')
def json_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = json_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('nc')
def nc_to_df(fp: str) -> pd.DataFrame:
    import xarray as xr
    ds = xr.open_dataset(fp, chunks='auto')
    var = list(ds.data_vars.keys())[0]
    df = (ds[var] * 1.0).isel(time=0).to_dataframe().reset_index()
    ds.close()
    gc.collect()
    return df


@validate_extension('nc')
def nc_to_csv(fp: str, csv_path: str, chunksize: int = 100000) -> bool:
    df = nc_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('nc4')
def nc4_to_df(fp: str) -> pd.DataFrame:
    import xarray as xr
    ds = xr.open_dataset(fp, chunks='auto')
    var = list(ds.data_vars.keys())[0]
    df = (ds[var] * 1.0).isel(time=0).to_dataframe().reset_index()
    ds.close()
    gc.collect()
    return df


@validate_extension('nc4')
def nc4_to_csv(fp: str, csv_path: str, chunksize: int = 100000) -> bool:
    df = nc4_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('sas7bdat')
def sas7bdat_to_df(fp: str) -> pd.DataFrame:
    import pyreadstat
    reader = pyreadstat.read_file_in_chunks(pyreadstat.read_sas7bdat, fp, chunksize=50000)
    chunks = []
    for df_chunk, _ in reader:
        chunks.append(df_chunk)
    return pd.concat(chunks, ignore_index=True)


@validate_extension('sas7bdat')
def sas7bdat_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = sas7bdat_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('sav')
def sav_to_df(fp: str) -> pd.DataFrame:
    import pyreadstat
    reader = pyreadstat.read_file_in_chunks(pyreadstat.read_sav, fp, chunksize=50000)
    chunks = []
    for df_chunk, _ in reader:
        chunks.append(df_chunk)
    return pd.concat(chunks, ignore_index=True)


@validate_extension('sav')
def sav_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = sav_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('shp')
def shp_to_df(fp: str) -> pd.DataFrame:
    import pyogrio
    df = pyogrio.read_dataframe(fp, read_geometry=False)
    return df


@validate_extension('shp')
def shp_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = shp_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('tif', 'tiff')
def tif_to_df(fp: str) -> pd.DataFrame:
    return cog_to_df(fp)


@validate_extension('tif', 'tiff')
def tif_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = tif_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('tsv')
def tsv_to_df(fp: str) -> pd.DataFrame:
    reader = pd.read_csv(fp, sep='\t', chunksize=50000)
    chunks = []
    for chunk in reader:
        chunks.append(chunk)
    return pd.concat(chunks, ignore_index=True)


@validate_extension('tsv')
def tsv_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = tsv_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('xlsx', 'xls')
def xlsx_to_df(fp: str) -> pd.DataFrame:
    chunks = []
    for chunk in pd.read_excel(fp, engine='calamine', chunksize=50000):
        chunks.append(chunk)
    return pd.concat(chunks, ignore_index=True)


@validate_extension('xlsx', 'xls')
def xlsx_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = xlsx_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@validate_extension('xml')
def xml_to_df(fp: str) -> pd.DataFrame:
    import xml.etree.ElementTree as ET
    context = ET.iterparse(fp, events=('end',))
    rows = []
    for event, elem in context:
        if elem.tag == 'record':
            row = {child.tag: child.text for child in elem}
            rows.append(row)
            elem.clear()
        if len(rows) >= 50000:
            break
    return pd.DataFrame(rows)


@validate_extension('xml')
def xml_to_csv(fp: str, csv_path: str, chunksize: int = 50000) -> bool:
    df = xml_to_df(fp)
    return optimized_df_to_csv(df, csv_path, chunksize)


@ensure_file_exists()
def load_yaml_config(fp: str) -> dict:
    import yaml
    with open(fp, "r") as f:
        return yaml.safe_load(f)