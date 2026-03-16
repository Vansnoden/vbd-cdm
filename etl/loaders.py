#-*- coding: utf-8 -*-

from pathlib import Path
from sqlalchemy.orm import Session
from database import models, schemas
from typing import List, Dict, Optional, Any, Union
import pandas as pd
import numpy as np
from sqlalchemy.orm import Session
from database.models import Location, EnvironmentalFactor, HostCase, VectorObservation, DiseaseBurden
from database.session import SessionLocal
from shapely import Point, wkt
import geopandas as gpd
import pandas as pd
import xarray as xr
import rasterio
from rasterio.mask import mask
from datetime import datetime
import logging
import re
from . import mappings
from . import grib_utils
from . import netcdf_utils

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DatabaseLoader:
    def __init__(self, db_session: Session):
        self.session = db_session
    
    # ==================== LOCATION METHODS ====================
    
    def load_location_from_gpkg(self, gpkg_path: Union[str, Path], 
                                admin_level: str, 
                                layer: Optional[str] = None,
                                parent_code: Optional[str] = None) -> Dict[str, Any]:
        """
        Load locations from a GeoPackage file.
        """
        gpkg_path = Path(gpkg_path)
        if not gpkg_path.exists():
            raise FileNotFoundError(f"GeoPackage not found: {gpkg_path}")
        
        # Read the GeoPackage
        gdf = gpd.read_file(gpkg_path, layer=layer)
        
        # Ensure CRS is WGS84
        if gdf.crs and gdf.crs.to_string() != 'EPSG:4326':
            gdf = gdf.to_crs('EPSG:4326')
        
        stats = {
            'total_features': len(gdf),
            'inserted': 0,
            'updated': 0,
            'errors': []
        }
        
        for idx, row in gdf.iterrows():
            try:
                # Extract name from common attribute fields
                name = None
                for col in ['NAME', 'name', 'COUNTY', 'county', 'ADMIN', 'admin', 'adm1_name']:
                    if col in row and row[col]:
                        name = str(row[col])
                        break
                if not name:
                    name = f"Feature_{idx}"
                
                # Generate location code
                location_code = name.lower().replace(' ', '_').replace('-', '_')
                
                # Get geometry as WKT
                geom_wkt = row.geometry.wkt if row.geometry else None
                
                # Compute centroid
                if row.geometry:
                    centroid = row.geometry.centroid
                    centroid_lat = centroid.y
                    centroid_lon = centroid.x
                else:
                    centroid_lat = centroid_lon = None
                
                location_data = {
                    'location_code': location_code,
                    'name': name,
                    'admin_level': admin_level,
                    'parent_location_code': parent_code,
                    'geometry_wkt': geom_wkt,
                    'centroid_lat': centroid_lat,
                    'centroid_lon': centroid_lon
                }
                
                # Upsert
                existing = self.session.query(models.Location).filter_by(
                    location_code=location_code
                ).first()
                
                if existing:
                    for key, value in location_data.items():
                        setattr(existing, key, value)
                    stats['updated'] += 1
                else:
                    location = models.Location(**location_data)
                    self.session.add(location)
                    stats['inserted'] += 1
                    
            except Exception as e:
                stats['errors'].append(f"Row {idx}: {str(e)}")
                logger.error(f"Error processing row {idx}: {e}")
        
        self.session.commit()
        logger.info(f"Location loading complete. Inserted: {stats['inserted']}, Updated: {stats['updated']}")
        return stats
    
    # ==================== ENVIRONMENTAL METHODS ====================
    
    def load_environmental_from_netcdf(self, data_root: Union[str, Path]) -> Dict[str, Any]:
        """
        Load environmental data from NetCDF files.
        """
        data_root = Path(data_root)
        
        # Load all locations with their geometries
        locations = self.session.query(models.Location).all()
        loc_dict = {}
        for loc in locations:
            geom = wkt.loads(loc.geometry_wkt) if loc.geometry_wkt else None
            loc_dict[loc.location_code] = {
                'geometry': geom,
                'centroid_lat': loc.centroid_lat,
                'centroid_lon': loc.centroid_lon
            }
        
        if not loc_dict:
            logger.error("No locations found in database")
            return {'total_files': 0, 'processed_files': 0, 'errors': ['No locations found']}
        
        # Find all NetCDF files
        nc_files = list(data_root.rglob("*.nc"))
        logger.info(f"Found {len(nc_files)} NetCDF files")
        
        stats = {
            'total_files': len(nc_files),
            'processed_files': 0,
            'skipped_files': 0,
            'errors': []
        }
        
        for nc_file in nc_files:
            year, loc_code = self._parse_netcdf_filename(nc_file)
            if loc_code is None:
                logger.debug(f"Skipping unrecognized file: {nc_file.name}")
                stats['skipped_files'] += 1
                continue
            if loc_code not in loc_dict:
                logger.warning(f"Location '{loc_code}' not found in database. Skipping {nc_file.name}")
                stats['skipped_files'] += 1
                continue
            
            logger.info(f"Processing {nc_file.name} (location: {loc_code}, year: {year})")
            
            try:
                self._process_netcdf_file(nc_file, loc_code, loc_dict[loc_code])
                stats['processed_files'] += 1
            except Exception as e:
                stats['errors'].append(f"{nc_file.name}: {str(e)}")
                logger.error(f"Error processing {nc_file.name}: {e}")
        
        logger.info(f"Environmental loading complete. Processed: {stats['processed_files']}")
        return stats
    
    def _parse_netcdf_filename(self, filepath: Path):
        """Extract year and location from NetCDF filename."""
        pattern1 = r'(?P<year>\d{4})_data_(?P<location>[A-Za-z_]+)_\d+\.nc$'
        match = re.search(pattern1, filepath.name)
        if match:
            year = int(match.group('year'))
            location = match.group('location').lower()
            return year, location
        
        pattern2 = r'(?P<year>\d{4})_data_(?P<location>[A-Za-z_]+)\.nc$'
        match = re.search(pattern2, filepath.name)
        if match:
            year = int(match.group('year'))
            location = match.group('location').lower()
            return year, location
        
        return None, None
    
    def _process_netcdf_file(self, nc_file: Path, loc_code: str, geom_info: dict):
        """Process a single NetCDF file and load data."""
        from database.utils import ensure_dates_exist
        
        ds = xr.open_dataset(nc_file)
        
        # Get all unique dates
        times = pd.to_datetime(ds.time.values)
        dates = sorted(set(t.date() for t in times))
        
        # Ensure dates exist in dim_time
        min_date = dates[0].strftime('%Y-%m-%d')
        max_date = dates[-1].strftime('%Y-%m-%d')
        ensure_dates_exist(min_date, max_date)
        
        geometry = geom_info['geometry']
        lat, lon = geom_info['centroid_lat'], geom_info['centroid_lon']
        
        for nc_var, mapping in mappings.NC_VARIABLE_MAP.items():
            if nc_var not in ds:
                continue
            
            logger.info(f"  Processing variable: {nc_var}")
            data = ds[nc_var]
            
            # Sum over step dimension if present
            if 'step' in data.dims:
                data = data.sum(dim='step')
            
            # Aggregate to daily
            method_name = mapping.get('agg_method', 'mean')
            method_func = {
                'mean': np.mean,
                'sum': np.sum,
                'max': np.max,
                'min': np.min,
            }.get(method_name, np.mean)
            
            temp_ds = data.to_dataset()
            daily_da = netcdf_utils.aggregate_to_daily(temp_ds, nc_var, method=method_func)
            
            # Process each day
            for time_idx in range(len(daily_da.time)):
                day_slice = daily_da.isel(time=time_idx)
                current_date = pd.to_datetime(daily_da.time.values[time_idx]).date()
                date_id = current_date.strftime('%Y-%m-%d')
                
                # Extract value
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
                
                # Upsert
                self._upsert_environmental({
                    'location_code': loc_code,
                    'date_id': date_id,
                    mapping['column']: value
                })
        
        ds.close()
    
    def _upsert_environmental(self, record):
        """Insert or update environmental factor record with better error handling."""
        try:
            existing = self.session.query(models.EnvironmentalFactor).filter_by(
                location_code=record['location_code'],
                date_id=record['date_id']
            ).first()
            
            if existing:
                # Update existing record
                for key, value in record.items():
                    if key not in ['location_code', 'date_id'] and value is not None:
                        setattr(existing, key, value)
                logger.debug(f"Updated environmental record for {record['location_code']} on {record['date_id']}")
            else:
                # Create new record
                env_factor = models.EnvironmentalFactor(**record)
                self.session.add(env_factor)
                logger.debug(f"Added environmental record for {record['location_code']} on {record['date_id']}")
            
            # Flush immediately to catch any errors
            self.session.flush()
            
        except Exception as e:
            logger.error(f"Error in _upsert_environmental: {e}")
            raise
    
    # ==================== VECTOR OBSERVATION METHODS ====================
    
    def load_vector_from_csv(self, data_dir: Union[str, Path], 
                            pattern: str = "*.csv",
                            column_mapping: Optional[Dict] = None,
                            **pandas_kwargs) -> Dict[str, Any]:
        """
        Load vector observation data from CSV/Excel files.
        """
        data_dir = Path(data_dir)
        files = list(data_dir.glob(pattern))
        
        if not files:
            logger.warning(f"No files matching {pattern} found in {data_dir}")
            return {'total_files': 0, 'processed_files': 0}
        
        stats = {
            'total_files': len(files),
            'processed_files': 0,
            'total_records': 0,
            'loaded_records': 0,
            'errors': []
        }
        
        for file_path in files:
            try:
                file_stats = self._process_vector_file(file_path, column_mapping, **pandas_kwargs)
                stats['processed_files'] += 1
                stats['total_records'] += file_stats['total_records']
                stats['loaded_records'] += file_stats['loaded_records']
                if file_stats.get('errors'):
                    stats['errors'].extend(file_stats['errors'])
            except Exception as e:
                stats['errors'].append(f"{file_path.name}: {str(e)}")
                logger.error(f"Error processing {file_path.name}: {e}")
        
        logger.info(f"Vector loading complete. Loaded: {stats['loaded_records']} records")
        return stats
    
    def _process_vector_file(self, file_path: Path, column_mapping: Optional[Dict], 
                            **pandas_kwargs) -> Dict[str, Any]:
        """Process a single vector observation file."""
        from database.utils import ensure_dates_exist
        
        stats = {
            'total_records': 0,
            'loaded_records': 0,
            'errors': []
        }
        
        # Read file
        if file_path.suffix.lower() == '.csv':
            df = pd.read_csv(file_path, **pandas_kwargs)
        elif file_path.suffix.lower() in ['.xls', '.xlsx']:
            df = pd.read_excel(file_path, **pandas_kwargs)
        else:
            raise ValueError(f"Unsupported file type: {file_path.suffix}")
        
        stats['total_records'] = len(df)
        logger.info(f"Processing {file_path.name}: {len(df)} rows")
        
        # Extract required columns
        lat_col = column_mapping.get('__latitude__')
        lon_col = column_mapping.get('__longitude__')
        year_col = column_mapping.get('__year__')
        month_col = column_mapping.get('__month__')
        count_spec = column_mapping.get('__count__')
        
        # Handle count column
        if isinstance(count_spec, str):
            if count_spec not in df.columns:
                df['_count'] = 1
            else:
                df['_count'] = pd.to_numeric(df[count_spec], errors='coerce').fillna(1)
        else:
            df['_count'] = 1
        
        # Process coordinates
        df['_lat'] = pd.to_numeric(df[lat_col], errors='coerce')
        df['_lon'] = pd.to_numeric(df[lon_col], errors='coerce')
        df = df.dropna(subset=['_lat', '_lon'])
        
        # Generate location codes and upsert locations
        df['location_code'] = df.apply(
            lambda r: self._generate_location_code(r['_lat'], r['_lon']), axis=1
        )
        
        # Upsert unique locations
        unique_locs = df[['location_code', '_lat', '_lon']].drop_duplicates(subset=['location_code'])
        self._upsert_vector_locations(unique_locs)
        
        # Process dates
        df['year'] = pd.to_numeric(df[year_col], errors='coerce')
        df['month'] = pd.to_numeric(df[month_col], errors='coerce')
        df = df.dropna(subset=['year', 'month'])
        df = df[(df['year'] >= 1900) & (df['year'] <= 2100) & (df['month'].between(1, 12))]
        
        if df.empty:
            logger.warning("No rows with valid year/month after cleaning")
            return stats
        
        df['date_id'] = df.apply(
            lambda r: f"{int(r['year']):04d}-{int(r['month']):02d}-01", axis=1
        )
        
        # Ensure dim_time records exist
        min_date = df['date_id'].min()
        max_date = df['date_id'].max()
        ensure_dates_exist(min_date, max_date)
        
        # Build field mapping
        field_mapping = {}
        for key, value in column_mapping.items():
            if not key.startswith('__') and value in df.columns:
                field_mapping[key] = value
        
        # Create records
        records = []
        for _, row in df.iterrows():
            record = {
                'location_code': row['location_code'],
                'date_id': row['date_id'],
                'count': int(row['_count']),
            }
            for out_field, src_col in field_mapping.items():
                if pd.notna(row[src_col]):
                    record[out_field] = row[src_col]
            
            try:
                validated = schemas.VectorObservationCreate(**record)
                records.append(validated.model_dump())
            except Exception as e:
                stats['errors'].append(f"Validation error: {e}")
        
        # Load records
        if records:
            self.load_from_dataframe(pd.DataFrame(records), models.VectorObservation)
            stats['loaded_records'] = len(records)
        
        return stats
    
    def _generate_location_code(self, lat, lon):
        """Generate a unique location code from coordinates."""
        return f"point_{lat:.4f}_{lon:.4f}".replace('-', 'n').replace('.', '_')
    
    def _upsert_vector_locations(self, unique_locs):
        """Upsert unique vector sampling locations."""
        for _, row in unique_locs.iterrows():
            existing = self.session.query(models.Location).filter_by(
                location_code=row['location_code']
            ).first()
            
            if not existing:
                point = Point(row['_lon'], row['_lat'])
                loc = models.Location(
                    location_code=row['location_code'],
                    name=f"Sampling site at {row['_lat']:.4f}, {row['_lon']:.4f}",
                    admin_level='site',
                    geometry_wkt=point.wkt,
                    centroid_lat=row['_lat'],
                    centroid_lon=row['_lon']
                )
                self.session.add(loc)
        
        self.session.commit()
    
    # ==================== CASE DATA METHODS ====================
    
    def load_cases_from_file(self, file_path: Union[str, Path],
                            column_mapping: Optional[Dict[str, str]] = None,
                            constants: Optional[Dict[str, Any]] = None,
                            date_format: Optional[str] = None,
                            batch_size: int = 1000,
                            **pandas_kwargs) -> Dict[str, Any]:
        """
        Load case data from a CSV or Excel file.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        # Read file
        logger.info(f"Reading file: {file_path}")
        if file_path.suffix.lower() == '.csv':
            df = pd.read_csv(file_path, **pandas_kwargs)
        elif file_path.suffix.lower() in ['.xls', '.xlsx']:
            df = pd.read_excel(file_path, **pandas_kwargs)
        else:
            raise ValueError(f"Unsupported file type: {file_path.suffix}")
        
        logger.info(f"Read {len(df)} rows from file")
        
        return self._process_case_dataframe(df, column_mapping, constants, date_format, batch_size)
    
    def _process_case_dataframe(self, df: pd.DataFrame,
                               column_mapping: Optional[Dict[str, str]] = None,
                               constants: Optional[Dict[str, Any]] = None,
                               date_format: Optional[str] = None,
                               batch_size: int = 1000) -> Dict[str, Any]:
        """Process and load case data from a DataFrame."""
        from database.utils import ensure_dates_exist
        
        stats = {
            'total_records': len(df),
            'loaded_records': 0,
            'failed_records': 0,
            'errors': []
        }
        
        # Apply column mapping
        if column_mapping:
            df = df.rename(columns=column_mapping)
        
        # Add constants
        if constants:
            for field, value in constants.items():
                df[field] = value
        
        # Clean data
        df = self._clean_case_data(df, date_format)
        
        # Ensure dates exist in dim_time
        all_dates = []
        for date_field in ['date_id_onset', 'date_id_reported']:
            if date_field in df.columns:
                all_dates.extend(df[date_field].dropna().unique())
        
        if all_dates:
            all_dates = pd.Series(all_dates).dropna().unique()
            if len(all_dates) > 0:
                ensure_dates_exist(min(all_dates), max(all_dates))
        
        # Convert to records and validate
        records = df.to_dict('records')
        validated_records = []
        
        for i, record in enumerate(records):
            try:
                clean_record = {k: v for k, v in record.items() 
                              if pd.notna(v) and v is not None}
                validated = schemas.HostCaseCreate(**clean_record)
                validated_records.append(validated.model_dump())
            except Exception as e:
                stats['failed_records'] += 1
                stats['errors'].append(f"Record {i}: {str(e)}")
        
        # Load in batches
        if validated_records:
            for i in range(0, len(validated_records), batch_size):
                batch = validated_records[i:i+batch_size]
                self.load_from_dataframe(pd.DataFrame(batch), models.HostCase)
                stats['loaded_records'] += len(batch)
        
        logger.info(f"Case loading complete. Loaded: {stats['loaded_records']}")
        return stats
    
    def _clean_case_data(self, df: pd.DataFrame, date_format: Optional[str] = None) -> pd.DataFrame:
        """Clean and validate case data."""
        df = df.copy()
        
        # Convert date fields
        date_fields = ['date_id_onset', 'date_id_reported']
        for field in date_fields:
            if field in df.columns:
                if date_format:
                    df[field] = pd.to_datetime(df[field], format=date_format, errors='coerce')
                else:
                    df[field] = pd.to_datetime(df[field], errors='coerce')
                df[field] = df[field].dt.strftime('%Y-%m-%d')
        
        # Clean age_years
        if 'age_years' in df.columns:
            df['age_years'] = pd.to_numeric(df['age_years'], errors='coerce')
            df.loc[df['age_years'] < 0, 'age_years'] = None
            df.loc[df['age_years'] > 120, 'age_years'] = None
        
        # Clean categorical fields
        if 'sex' in df.columns:
            df['sex'] = df['sex'].astype(str).str.lower().str.strip()
            df['sex'] = df['sex'].replace({'m': 'male', 'f': 'female'})
            valid_sex = {'male', 'female', 'other', 'unknown'}
            df.loc[~df['sex'].isin(valid_sex), 'sex'] = None
        
        if 'case_status' in df.columns:
            df['case_status'] = df['case_status'].astype(str).str.lower().str.strip()
            valid_status = {'suspected', 'probable', 'confirmed'}
            df.loc[~df['case_status'].isin(valid_status), 'case_status'] = 'suspected'
        
        # Remove rows with missing required fields
        required = ['case_identifier', 'location_code', 'date_id_onset', 'disease']
        for req in required:
            if req not in df.columns:
                df[req] = None
        
        initial_len = len(df)
        df = df.dropna(subset=required)
        dropped = initial_len - len(df)
        if dropped > 0:
            logger.warning(f"Dropped {dropped} rows with missing required fields")
        
        # Handle duplicates
        if 'case_identifier' in df.columns:
            duplicates = df['case_identifier'].duplicated().sum()
            if duplicates > 0:
                logger.warning(f"Found {duplicates} duplicate case_identifiers")
                df = df.drop_duplicates(subset=['case_identifier'], keep='first')
        
        return df
    
    # ==================== DISEASE BURDEN METHODS ====================
    
    def load_disease_burden_from_raster(self, 
                                       raster_path: Union[str, Path],
                                       year: int,
                                       disease: str = 'malaria',
                                       source: str = 'Malaria Atlas Project',
                                       band: int = 1,
                                       stat: str = 'mean') -> Dict[str, Any]:
        """
        Load disease burden data from a raster file.
        """
        raster_path = Path(raster_path)
        if not raster_path.exists():
            raise FileNotFoundError(f"Raster file not found: {raster_path}")
        
        logger.info(f"Loading disease burden from raster: {raster_path}")
        
        # Load all locations with geometries
        locations = self.session.query(models.Location).all()
        if not locations:
            logger.error("No locations found in database")
            return {'loaded': 0, 'total_locations': 0, 'failed': 0, 'errors': ['No locations found']}
        
        stats = {
            'total_locations': len(locations),
            'loaded': 0,
            'failed': 0,
            'errors': [],
            'raster_file': str(raster_path),
            'year': year,
            'disease': disease
        }
        
        with rasterio.open(raster_path) as src:
            raster_crs = src.crs or 'EPSG:4326'
            nodata = src.nodata
            
            for loc in locations:
                try:
                    if not loc.geometry_wkt:
                        stats['failed'] += 1
                        stats['errors'].append(f"Location {loc.location_code} has no geometry")
                        continue
                    
                    geom = wkt.loads(loc.geometry_wkt)
                    
                    # Transform CRS if needed
                    if raster_crs != 'EPSG:4326':
                        gdf = gpd.GeoDataFrame([{'geometry': geom}], crs='EPSG:4326')
                        gdf = gdf.to_crs(raster_crs)
                        geom = gdf.geometry.iloc[0]
                    
                    # Extract values
                    out_image, _ = mask(src, [geom], crop=True, all_touched=True)
                    values = out_image[band-1]
                    
                    if nodata is not None:
                        values = values[values != nodata]
                    
                    if len(values) > 0:
                        if stat == 'mean':
                            value = float(np.mean(values))
                        elif stat == 'median':
                            value = float(np.median(values))
                        elif stat == 'min':
                            value = float(np.min(values))
                        elif stat == 'max':
                            value = float(np.max(values))
                        elif stat == 'sum':
                            value = float(np.sum(values))
                        else:
                            value = float(np.mean(values))
                    else:
                        stats['failed'] += 1
                        stats['errors'].append(f"{loc.location_code}: No data in polygon")
                        continue
                    
                    # Upsert
                    existing = self.session.query(models.DiseaseBurden).filter_by(
                        location_code=loc.location_code,
                        year=year,
                        disease=disease
                    ).first()
                    
                    burden_data = {
                        'location_code': loc.location_code,
                        'year': year,
                        'disease': disease,
                        'incidence_rate': value,
                        'source': source,
                        'raster_file': raster_path.name,
                        'extraction_method': stat
                    }
                    
                    if existing:
                        for key, val in burden_data.items():
                            setattr(existing, key, val)
                    else:
                        new_burden = models.DiseaseBurden(**burden_data)
                        self.session.add(new_burden)
                    
                    stats['loaded'] += 1
                    
                except Exception as e:
                    logger.error(f"Error processing location {loc.location_code}: {e}")
                    stats['failed'] += 1
                    stats['errors'].append(f"{loc.location_code}: {str(e)}")
            
            self.session.commit()
        
        logger.info(f"Disease burden loading complete. Loaded: {stats['loaded']}, Failed: {stats['failed']}")
        return stats
    
    # ==================== GENERIC METHODS ====================
    
    def load_from_dataframe(self, df: pd.DataFrame, model_class):
        """Generic method to load pandas DataFrame to any model."""
        records = df.to_dict('records')
        for record in records:
            filtered_record = {k: v for k, v in record.items() 
                             if hasattr(model_class, k)}
            db_obj = model_class(**filtered_record)
            self.session.add(db_obj)
        self.session.commit()
    
    def load_from_file(self, file_path, model_class, schema_class=None, 
                      column_mapping=None, **pandas_kwargs):
        """Generic method to load from CSV/Excel file."""
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        # Read file based on extension
        if file_path.suffix.lower() == '.csv':
            df = pd.read_csv(file_path, **pandas_kwargs)
        elif file_path.suffix.lower() in ['.xls', '.xlsx']:
            df = pd.read_excel(file_path, **pandas_kwargs)
        else:
            raise ValueError(f"Unsupported file type: {file_path.suffix}")
        
        # Apply column mapping
        if column_mapping:
            df = df.rename(columns=column_mapping)
        
        # Validate with Pydantic if schema provided
        records = df.to_dict('records')
        if schema_class:
            validated_records = []
            for rec in records:
                schema_fields = set(schema_class.model_fields.keys())
                filtered = {k: v for k, v in rec.items() if k in schema_fields}
                validated = schema_class(**filtered)
                validated_records.append(validated.model_dump())
            records = validated_records
        
        self.load_from_dataframe(pd.DataFrame(records), model_class)
    
    def _get_locations_as_geodataframe(self):
        """Load all locations with WKT geometry into a GeoDataFrame."""
        locations = self.session.query(models.Location).all()
        data = []
        for loc in locations:
            geom = wkt.loads(loc.geometry_wkt) if loc.geometry_wkt else None
            data.append({
                'location_code': loc.location_code,
                'geometry': geom,
                'centroid_lat': loc.centroid_lat,
                'centroid_lon': loc.centroid_lon
            })
        return gpd.GeoDataFrame(data, crs='EPSG:4326', geometry='geometry')