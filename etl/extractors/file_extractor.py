"""
FileExtractor - Reads various file formats into a pandas DataFrame using data_transform_library.
"""

import os
import logging
from typing import Optional, Dict, Any, Union, List

import pandas as pd

# Import the data transformation functions
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from utilities.data_transform_library import (
    cog_to_df, tif_to_df,
    nc_to_df, grib_to_df, tif_to_df_chunked, timeseries_to_df, timeseries_to_csv,
    dta_to_df, sas7bdat_to_df, sav_to_df,
    geojson_to_df, gpkg_to_df, shp_to_df,
    json_to_df, tsv_to_df, xlsx_to_df, xml_to_df,
    load_yaml_config 
)

logger = logging.getLogger(__name__)


class FileExtractor:
    """
    Extracts data from a file into a pandas DataFrame.
    Supports many formats: COG/TIFF, NetCDF, GRIB, Stata, SAS, SPSS, GeoJSON, GPKG, Shapefile,
    JSON, TSV, Excel, XML, and CSV (via pandas).
    """

    def __init__(self, extract_mode: str = 'single', large_df_threshold: int = 10_000_000):
        """
        Args:
            extract_mode: One of:
                - 'single' (default): read only the first variable and first time step (fast, low memory).
                - 'timeseries': read all time steps and all variables (may be huge).
            large_df_threshold: Warn if DataFrame rows exceed this value.
        """
        self.extract_mode = extract_mode
        self.large_df_threshold = large_df_threshold

    def extract(self, file_path: str, **reader_kwargs) -> pd.DataFrame:
        """
        Read a file into a pandas DataFrame.

        Args:
            file_path: Path to the input file.
            **reader_kwargs: Additional arguments passed to the specific reader
                             (e.g., sep for CSV, sheet_name for Excel, variable/time_idx for NetCDF).

        Returns:
            pandas DataFrame.

        Raises:
            ValueError: If file extension is not supported or extract_mode is invalid.
            FileNotFoundError: If file does not exist.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        ext = os.path.splitext(file_path)[1].lower().lstrip('.')
        logger.info(f"Extracting from {file_path} (extension: .{ext}, mode: {self.extract_mode})")

        # Dispatch based on extension
        if ext in ('cog', 'tif', 'tiff'):
            if self.extract_mode == 'chunked':
                # Return a generator
                window_size = reader_kwargs.pop('window_size', 512)
                return tif_to_df_chunked(file_path, window_size=window_size)
            else:
                df = cog_to_df(file_path) if ext == 'cog' else tif_to_df(file_path)
                
        elif ext in ('nc', 'nc4', 'grib', 'grib2'):
            if self.extract_mode == 'single':
                # Use single‑variable, single‑time reader
                variable = reader_kwargs.pop('variable', None)
                time_idx = reader_kwargs.pop('time_idx', 0)
                if ext in ('grib', 'grib2'):
                    df = grib_to_df(file_path, variable=variable, time_idx=time_idx)
                else:
                    df = nc_to_df(file_path, variable=variable, time_idx=time_idx)
            elif self.extract_mode == 'timeseries':
                # Full timeseries - read all variables and time steps (may be huge)
                variables = reader_kwargs.pop('variables', None)
                df = timeseries_to_df(file_path, variables=variables, warn_large=True)
            else:
                raise ValueError(f"Unsupported extract_mode for NetCDF/GRIB: {self.extract_mode}")

        elif ext in ('dta', 'sas7bdat', 'sav'):
            if ext == 'dta':
                df = dta_to_df(file_path, **reader_kwargs)
            elif ext == 'sas7bdat':
                df = sas7bdat_to_df(file_path, **reader_kwargs)
            else:  # sav
                df = sav_to_df(file_path, **reader_kwargs)

        elif ext in ('geojson', 'gpkg', 'shp'):
            if ext == 'geojson':
                df = geojson_to_df(file_path)
            elif ext == 'gpkg':
                df = gpkg_to_df(file_path)
            else:  # shp
                df = shp_to_df(file_path)

        elif ext == 'json':
            lines = reader_kwargs.pop('lines', True)
            df = json_to_df(file_path, lines=lines, **reader_kwargs)

        elif ext == 'tsv':
            df = tsv_to_df(file_path, **reader_kwargs)

        elif ext in ('xlsx', 'xls'):
            sheet = reader_kwargs.pop('sheet_name', 0)
            df = xlsx_to_df(file_path, sheet_name=sheet, **reader_kwargs)

        elif ext == 'xml':
            record_tag = reader_kwargs.pop('record_tag', 'record')
            df = xml_to_df(file_path, record_tag=record_tag, **reader_kwargs)

        elif ext == 'csv':
            # Use pandas directly (with optional chunking inside if needed)
            chunksize = reader_kwargs.pop('chunksize', None)
            if chunksize:
                reader = pd.read_csv(file_path, chunksize=chunksize, **reader_kwargs)
                chunks = [chunk for chunk in reader]
                df = pd.concat(chunks, ignore_index=True)
            else:
                df = pd.read_csv(file_path, **reader_kwargs)

        else:
            raise ValueError(f"Unsupported file extension: .{ext}")

        # Warn if DataFrame is large
        if len(df) > self.large_df_threshold:
            logger.warning(
                f"Extracted DataFrame has {len(df):,} rows (> {self.large_df_threshold:,}). "
                "Consider using chunked extraction or sampling."
            )

        logger.info(f"Extraction complete: {len(df)} rows, {len(df.columns)} columns")
        return df