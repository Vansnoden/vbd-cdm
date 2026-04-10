# -*- coding: utf-8 -*-
"""
Revised ETL Loader for the Vector-Borne Disease CDM.
Supports all dimension and fact tables with source provenance.
Updated to align with the vocabulary-driven dimension tables
(description, source_standard fields) and the environmental fact table.
"""

import json
import numpy as np
from pathlib import Path
from sqlalchemy.orm import Session
from database import models, schemas
from typing import List, Dict, Optional, Any, Union
import pandas as pd
import geopandas as gpd
import xarray as xr
import rasterio
from rasterio.mask import mask
from shapely import Point, wkt
from datetime import datetime
import logging
import re
from . import mappings
from . import netcdf_utils

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DatabaseLoader:
    def __init__(self, db_session: Session):
        self.session = db_session

    # ==================== HELPER METHODS ====================

    def _get_or_create_dimension(self, model_class, lookup_field, lookup_value, create_attrs=None):
        """
        Generic helper to get or create a dimension record.
        Returns the instance.
        """
        existing = self.session.query(model_class).filter_by(**{lookup_field: lookup_value}).first()
        if existing:
            return existing
        if create_attrs is None:
            create_attrs = {}
        create_attrs[lookup_field] = lookup_value
        new_obj = model_class(**create_attrs)
        self.session.add(new_obj)
        self.session.flush()
        return new_obj

    def _get_vector_code(self, species_name, subspecies=None):
        """Look up or create vector code from species name."""
        parts = species_name.lower().split()
        if len(parts) >= 2:
            code = (parts[0][:3] + parts[1][:3]).upper()
        else:
            code = species_name[:6].upper()
        existing = self.session.query(models.DimVector).filter_by(vector_code=code).first()
        if existing:
            return existing.vector_code
        new_vec = models.DimVector(
            vector_code=code,
            species_name=species_name,
            subspecies=subspecies,
            genus=parts[0].capitalize() if parts else None,
            family='Culicidae'
        )
        self.session.add(new_vec)
        self.session.flush()
        return new_vec.vector_code

    def _get_disease_code(self, disease_name):
        """Look up or create disease code."""
        code = disease_name[:3].upper()
        existing = self.session.query(models.DimDisease).filter_by(disease_code=code).first()
        if existing:
            return existing.disease_code
        new_disease = models.DimDisease(disease_code=code, disease_name=disease_name)
        self.session.add(new_disease)
        self.session.flush()
        return new_disease.disease_code

    def _get_method_code(self, method_name):
        """Look up or create identification method code."""
        code = method_name[:3].upper()
        existing = self.session.query(models.DimIdentificationMethod).filter_by(method_code=code).first()
        if existing:
            return existing.method_code
        new_method = models.DimIdentificationMethod(method_code=code, method_name=method_name)
        self.session.add(new_method)
        self.session.flush()
        return new_method.method_code

    def _get_source_code(self, source_name):
        """Look up or create source code."""
        code = source_name[:4].upper()
        existing = self.session.query(models.DimSource).filter_by(source_code=code).first()
        if existing:
            return existing.source_code
        new_source = models.DimSource(source_code=code, source_name=source_name)
        self.session.add(new_source)
        self.session.flush()
        return new_source.source_code

    # ==================== VOCABULARY DRIVEN DIMENSION LOADER ====================

    def load_dimensions_from_vocabulary(self, csv_path: Union[str, Path]) -> Dict[str, Any]:
        """
        Load all dimension tables from a single vocabulary CSV file.
        Expected columns: domain, code, name, description, source_standard, additional_attributes
        Uses the existing field names of each model (disease_code, species_name, etc.).
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"Vocabulary file not found: {csv_path}")

        df = pd.read_csv(csv_path)
        required_cols = ['domain', 'code', 'name']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Vocabulary CSV missing required column: {col}")

        # Mapping from domain to (model_class, field_mapping)
        domain_config = {
            'disease': {
                'model': models.DimDisease,
                'fields': {
                    'code': 'disease_code',
                    'name': 'disease_name',
                    'description': 'description',
                    'source_standard': 'source_standard',
                }
            },
            'vector': {
                'model': models.DimVector,
                'fields': {
                    'code': 'vector_code',
                    'name': 'species_name',
                    'description': 'description',
                    'source_standard': 'source_standard',
                }
            },
            'insecticide': {
                'model': models.DimInsecticide,
                'fields': {
                    'code': 'insecticide_code',
                    'name': 'insecticide_name',
                    'description': 'description',
                    'source_standard': 'source_standard',
                }
            },
            'identification_method': {
                'model': models.DimIdentificationMethod,
                'fields': {
                    'code': 'method_code',
                    'name': 'method_name',
                    'description': 'description',
                    'source_standard': 'source_standard',
                }
            },
            'resistance_test_type': {
                'model': models.DimResistanceTestType,
                'fields': {
                    'code': 'test_type_code',
                    'name': 'test_type_name',
                    'description': 'description',
                    'source_standard': 'source_standard',
                }
            },
            'socioeconomic_indicator': {
                'model': models.DimSocioeconomicIndicator,
                'fields': {
                    'code': 'indicator_code',
                    'name': 'indicator_name',
                    'description': 'description',
                    'source_standard': 'source_standard',
                }
            },
            'source': {
                'model': models.DimSource,
                'fields': {
                    'code': 'source_code',
                    'name': 'source_name',
                    'description': 'source_description',
                    'source_standard': 'source_standard',
                }
            },
        }

        stats = {
            'total_rows': len(df),
            'inserted': 0,
            'updated': 0,
            'errors': [],
            'by_domain': {}
        }

        for domain, group in df.groupby('domain'):
            if domain not in domain_config:
                stats['errors'].append(f"Unknown domain: {domain}")
                continue

            config = domain_config[domain]
            model_class = config['model']
            field_map = config['fields']

            domain_stats = {'inserted': 0, 'updated': 0}

            for _, row in group.iterrows():
                code = row['code']
                name = row['name']
                description = row.get('description', None)
                source_standard = row.get('source_standard', None)

                attrs = {}
                if 'code' in field_map:
                    attrs[field_map['code']] = code
                if 'name' in field_map:
                    attrs[field_map['name']] = name
                if description and 'description' in field_map:
                    attrs[field_map['description']] = description
                if source_standard and 'source_standard' in field_map:
                    attrs[field_map['source_standard']] = source_standard

                # Parse additional attributes JSON
                if pd.notna(row.get('additional_attributes')):
                    try:
                        extra = json.loads(row['additional_attributes'])
                        for key, value in extra.items():
                            if hasattr(model_class, key):
                                attrs[key] = value
                            else:
                                stats['errors'].append(f"Attribute '{key}' not found in {domain} model, skipping")
                    except json.JSONDecodeError as e:
                        stats['errors'].append(f"Invalid JSON for {domain}:{code} - {e}")

                # Upsert using the code field as primary key
                pk_field = field_map['code']
                existing = self.session.query(model_class).filter_by(**{pk_field: code}).first()
                if existing:
                    for key, value in attrs.items():
                        setattr(existing, key, value)
                    domain_stats['updated'] += 1
                else:
                    new_obj = model_class(**attrs)
                    self.session.add(new_obj)
                    domain_stats['inserted'] += 1

            self.session.flush()
            stats['by_domain'][domain] = domain_stats
            stats['inserted'] += domain_stats['inserted']
            stats['updated'] += domain_stats['updated']
            logger.info(f"Loaded {domain}: {domain_stats['inserted']} inserted, {domain_stats['updated']} updated")

        self.session.commit()
        logger.info(f"Vocabulary loading complete. Total inserted: {stats['inserted']}, updated: {stats['updated']}")
        return stats

    # ==================== LOCATION METHODS ====================

    def load_location_from_gpkg(self, gpkg_path: Union[str, Path],
                                admin_level: str,
                                layer: Optional[str] = None,
                                parent_code: Optional[str] = None,
                                auto_populate_country: bool = True) -> Dict[str, Any]:
        """Load locations from GeoPackage into DimLocation."""
        gpkg_path = Path(gpkg_path)
        if not gpkg_path.exists():
            raise FileNotFoundError(f"GeoPackage not found: {gpkg_path}")

        gdf = gpd.read_file(gpkg_path, layer=layer)
        if gdf.crs and gdf.crs.to_string() != 'EPSG:4326':
            gdf = gdf.to_crs('EPSG:4326')

        stats = {'total_features': len(gdf), 'inserted': 0, 'updated': 0, 'errors': []}

        for idx, row in gdf.iterrows():
            try:
                name = None
                for col in ['NAME', 'name', 'COUNTY', 'county', 'ADMIN', 'admin', 'adm1_name']:
                    if col in row and row[col]:
                        name = str(row[col])
                        break
                if not name:
                    name = f"Feature_{idx}"

                location_code = name.lower().replace(' ', '_').replace('-', '_')
                geom_wkt = row.geometry.wkt if row.geometry else None
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

                existing = self.session.query(models.DimLocation).filter_by(location_code=location_code).first()
                if existing:
                    for key, value in location_data.items():
                        setattr(existing, key, value)
                    stats['updated'] += 1
                else:
                    self.session.add(models.DimLocation(**location_data))
                    stats['inserted'] += 1
            except Exception as e:
                stats['errors'].append(f"Row {idx}: {str(e)}")

        if auto_populate_country:
            self._populate_location_countries()

        self.session.commit()
        logger.info(f"Location loading complete. Inserted: {stats['inserted']}, Updated: {stats['updated']}")
        return stats

    def _populate_location_countries(self, location_ids=None):
        from database.geoutils import get_country_from_coords
        query = self.session.query(models.DimLocation)
        if location_ids:
            query = query.filter(models.DimLocation.id.in_(location_ids))
        locations = query.all()
        updated = 0
        for loc in locations:
            if loc.centroid_lat and loc.centroid_lon:
                country_name, country_code = get_country_from_coords(loc.centroid_lat, loc.centroid_lon)
                if country_name != loc.country_name or country_code != loc.country_code:
                    loc.country_name = country_name
                    loc.country_code = country_code
                    updated += 1
        if updated:
            self.session.commit()
            logger.info(f"Updated country info for {updated} locations")
        return updated

    # ==================== ENVIRONMENTAL FACT LOADER (NetCDF) ====================

    def load_environmental_from_netcdf(self, data_root: Union[str, Path], source_name: str = "ERA5-Land") -> Dict[str, Any]:
        """
        Load environmental data from NetCDF files into FactEnvironmental.
        Assumes NetCDF files contain daily data for a specific location (clipped).
        """
        from database.utils import ensure_dates_exist

        data_root = Path(data_root)
        nc_files = list(data_root.rglob("*.nc"))
        if not nc_files:
            logger.warning(f"No NetCDF files found in {data_root}")
            return {'processed_files': 0, 'errors': ['No files found']}

        source_code = self._get_source_code(source_name)
        stats = {'total_files': len(nc_files), 'processed_files': 0, 'errors': []}

        for nc_file in nc_files:
            year, loc_code = self._parse_netcdf_filename(nc_file)
            if not loc_code:
                logger.debug(f"Skipping unrecognized file: {nc_file.name}")
                continue

            location = self.session.query(models.DimLocation).filter_by(location_code=loc_code).first()
            if not location:
                logger.warning(f"Location '{loc_code}' not found. Skipping {nc_file.name}")
                continue

            try:
                ds = xr.open_dataset(nc_file)
                times = pd.to_datetime(ds.time.values)
                dates = sorted(set(t.date() for t in times))
                if dates:
                    ensure_dates_exist(dates[0].strftime('%Y-%m-%d'), dates[-1].strftime('%Y-%m-%d'))

                for nc_var, mapping in mappings.NC_VARIABLE_MAP.items():
                    if nc_var not in ds:
                        continue
                    data = ds[nc_var]
                    if 'step' in data.dims:
                        data = data.sum(dim='step')

                    method_func = {'mean': np.mean, 'sum': np.sum}.get(mapping.get('agg_method', 'mean'), np.mean)
                    temp_ds = data.to_dataset()
                    daily_da = netcdf_utils.aggregate_to_daily(temp_ds, nc_var, method=method_func)

                    for time_idx in range(len(daily_da.time)):
                        day_slice = daily_da.isel(time=time_idx)
                        current_date = pd.to_datetime(daily_da.time.values[time_idx]).date()
                        date_id = current_date.strftime('%Y-%m-%d')

                        if location.geometry_wkt:
                            geom = wkt.loads(location.geometry_wkt)
                            value = netcdf_utils.extract_polygon(day_slice, geom)
                        else:
                            value = netcdf_utils.extract_point(day_slice, location.centroid_lat, location.centroid_lon)

                        # Skip None or NaN values
                        if value is None:
                            continue
                        if np.isnan(value):
                            continue

                        if mapping.get('convert'):
                            value = mapping['convert'](value)

                        # Use the new FactEnvironmental model
                        existing = self.session.query(models.FactEnvironmental).filter_by(
                            location_code=loc_code, date_id=date_id, variable=nc_var
                        ).first()
                        if existing:
                            existing.value = value
                            existing.source_code = source_code
                        else:
                            env_fact = models.FactEnvironmental(
                                location_code=loc_code,
                                date_id=date_id,
                                variable=nc_var,
                                value=value,
                                source_code=source_code
                            )
                            self.session.add(env_fact)
                ds.close()
                stats['processed_files'] += 1
                self.session.commit()
            except Exception as e:
                stats['errors'].append(f"{nc_file.name}: {str(e)}")
                logger.error(f"Error processing {nc_file.name}: {e}")
                self.session.rollback()  # Rollback failed transaction for this file

        logger.info(f"Environmental loading complete. Processed: {stats['processed_files']} files")
        return stats

    def _parse_netcdf_filename(self, filepath: Path):
        pattern1 = r'(?P<year>\d{4})_data_(?P<location>[A-Za-z_]+)_\d+\.nc$'
        match = re.search(pattern1, filepath.name)
        if match:
            return int(match.group('year')), match.group('location').lower()
        pattern2 = r'(?P<year>\d{4})_data_(?P<location>[A-Za-z_]+)\.nc$'
        match = re.search(pattern2, filepath.name)
        if match:
            return int(match.group('year')), match.group('location').lower()
        return None, None

    # ==================== VECTOR OBSERVATION LOADER ====================

    def load_vector_from_csv(self, data_dir: Union[str, Path],
                             pattern: str = "*.csv",
                             column_mapping: Optional[Dict] = None,
                             source_name: str = "GBIF",
                             **pandas_kwargs) -> Dict[str, Any]:
        """Load vector observation data from CSV files into FactVectorObservation."""
        data_dir = Path(data_dir)
        files = list(data_dir.glob(pattern))
        if not files:
            logger.warning(f"No files matching {pattern} in {data_dir}")
            return {'total_files': 0, 'processed_files': 0}

        source_code = self._get_source_code(source_name)
        stats = {'total_files': len(files), 'processed_files': 0, 'total_records': 0, 'loaded_records': 0, 'errors': []}

        for file_path in files:
            try:
                file_stats = self._process_vector_file(file_path, column_mapping, source_code, **pandas_kwargs)
                stats['processed_files'] += 1
                stats['total_records'] += file_stats['total_records']
                stats['loaded_records'] += file_stats['loaded_records']
                stats['errors'].extend(file_stats.get('errors', []))
            except Exception as e:
                stats['errors'].append(f"{file_path.name}: {str(e)}")
                self.session.rollback()
        logger.info(f"Vector loading complete. Loaded: {stats['loaded_records']} records")
        return stats

    def _process_vector_file(self, file_path: Path, column_mapping: Dict, source_code: str, **pandas_kwargs):
        from database.utils import ensure_dates_exist
        stats = {'total_records': 0, 'loaded_records': 0, 'errors': []}

        if file_path.suffix.lower() == '.csv':
            df = pd.read_csv(file_path, low_memory=False, **pandas_kwargs)
        elif file_path.suffix.lower() in ['.xls', '.xlsx']:
            df = pd.read_excel(file_path, **pandas_kwargs)
        else:
            raise ValueError(f"Unsupported file type: {file_path.suffix}")

        stats['total_records'] = len(df)
        logger.info(f"Processing {file_path.name}: {len(df)} rows")

        lat_col = column_mapping.get('__latitude__')
        lon_col = column_mapping.get('__longitude__')
        year_col = column_mapping.get('__year__')
        month_col = column_mapping.get('__month__')
        count_spec = column_mapping.get('__count__')
        event_date_col = column_mapping.get('__event_date_full__')
        species_col = column_mapping.get('vector_species', 'scientificName')

        # Date handling
        if year_col in df.columns and month_col in df.columns:
            df['year'] = pd.to_numeric(df[year_col], errors='coerce')
            df['month'] = pd.to_numeric(df[month_col], errors='coerce')
        else:
            df['year'] = np.nan
            df['month'] = np.nan

        if event_date_col and event_date_col in df.columns:
            if df['year'].isna().any() or df['month'].isna().any():
                df['eventDate_parsed'] = pd.to_datetime(df[event_date_col], errors='coerce')
                df.loc[df['year'].isna(), 'year'] = df['eventDate_parsed'].dt.year
                df.loc[df['month'].isna(), 'month'] = df['eventDate_parsed'].dt.month

        df = df.dropna(subset=['year', 'month'])
        df = df[(df['year'] >= 1900) & (df['year'] <= 2100) & (df['month'].between(1, 12))]
        if df.empty:
            return stats

        df['year'] = df['year'].astype(int)
        df['month'] = df['month'].astype(int)
        df['date_id'] = df.apply(lambda r: f"{r['year']:04d}-{r['month']:02d}-01", axis=1)

        # Count
        if isinstance(count_spec, str):
            if count_spec not in df.columns:
                df['_count'] = 1
            else:
                df['_count'] = pd.to_numeric(df[count_spec], errors='coerce').fillna(1)
        else:
            df['_count'] = 1

        # Coordinates
        df['_lat'] = pd.to_numeric(df[lat_col], errors='coerce')
        df['_lon'] = pd.to_numeric(df[lon_col], errors='coerce')
        df = df.dropna(subset=['_lat', '_lon'])

        # Location and vector code
        df['location_code'] = df.apply(lambda r: self._generate_location_code(r['_lat'], r['_lon']), axis=1)

        # Ensure unique location codes before inserting (drop duplicates within this file)
        unique_locs = df[['location_code', '_lat', '_lon']].drop_duplicates(subset=['location_code'])
        self._upsert_vector_locations(unique_locs)

        if species_col in df.columns:
            df['vector_code'] = df[species_col].apply(lambda x: self._get_vector_code(str(x)))
        else:
            df['vector_code'] = 'UNKNOWN'

        ensure_dates_exist(df['date_id'].min(), df['date_id'].max())

        field_mapping = {k: v for k, v in column_mapping.items() if not k.startswith('__') and v in df.columns}
        records = []
        for _, row in df.iterrows():
            record = {
                'location_code': row['location_code'],
                'date_id': row['date_id'],
                'vector_code': row['vector_code'],
                'source_code': source_code,
                'count': int(row['_count']),
            }
            for out_field, src_col in field_mapping.items():
                if pd.notna(row[src_col]):
                    record[out_field] = row[src_col]
            try:
                validated = schemas.FactVectorObservationCreate(**record)
                records.append(validated.model_dump())
            except Exception as e:
                stats['errors'].append(f"Validation error: {e}")

        if records:
            for i in range(0, len(records), 1000):
                batch = records[i:i+1000]
                self.load_from_dataframe(pd.DataFrame(batch), models.FactVectorObservation)
            stats['loaded_records'] = len(records)
        return stats

    def _generate_location_code(self, lat, lon):
        return f"point_{lat:.4f}_{lon:.4f}".replace('-', 'n').replace('.', '_')

    def _upsert_vector_locations(self, unique_locs):
        """
        Insert vector point locations into DimLocation if they do not already exist.
        Uses a set to avoid duplicate inserts within the same batch.
        """
        added_codes = set()
        for _, row in unique_locs.iterrows():
            code = row['location_code']
            if code in added_codes:
                continue
            existing = self.session.query(models.DimLocation).filter_by(location_code=code).first()
            if not existing:
                point = Point(row['_lon'], row['_lat'])
                loc = models.DimLocation(
                    location_code=code,
                    name=f"Sampling site at {row['_lat']:.4f}, {row['_lon']:.4f}",
                    admin_level='site',
                    geometry_wkt=point.wkt,
                    centroid_lat=row['_lat'],
                    centroid_lon=row['_lon']
                )
                self.session.add(loc)
                added_codes.add(code)
                # Flush to catch any integrity error immediately
                self.session.flush()
        self.session.commit()

    # ==================== CASE DATA LOADER ====================

    def load_cases_from_file(self, file_path: Union[str, Path],
                             column_mapping: Optional[Dict[str, str]] = None,
                             constants: Optional[Dict[str, Any]] = None,
                             source_name: str = "WHO",
                             date_format: Optional[str] = None,
                             batch_size: int = 1000,
                             **pandas_kwargs) -> Dict[str, Any]:
        """Load case data into FactDiseaseCase."""
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        source_code = self._get_source_code(source_name)
        if file_path.suffix.lower() == '.csv':
            df = pd.read_csv(file_path, **pandas_kwargs)
        elif file_path.suffix.lower() in ['.xls', '.xlsx']:
            df = pd.read_excel(file_path, **pandas_kwargs)
        else:
            raise ValueError(f"Unsupported file type: {file_path.suffix}")

        logger.info(f"Read {len(df)} rows from {file_path}")
        return self._process_case_dataframe(df, column_mapping, constants, source_code, date_format, batch_size)

    def _process_case_dataframe(self, df: pd.DataFrame, column_mapping: Dict, constants: Dict,
                                source_code: str, date_format: Optional[str], batch_size: int):
        from database.utils import ensure_dates_exist
        stats = {'total_records': len(df), 'loaded_records': 0, 'failed_records': 0, 'errors': []}

        if column_mapping:
            df = df.rename(columns=column_mapping)
        if constants:
            for field, value in constants.items():
                df[field] = value

        df = self._clean_case_data(df, date_format)

        all_dates = []
        for date_field in ['date_id_onset', 'date_id_reported']:
            if date_field in df.columns:
                all_dates.extend(df[date_field].dropna().unique())
        if all_dates:
            ensure_dates_exist(min(all_dates), max(all_dates))

        if 'disease' in df.columns:
            df['disease_code'] = df['disease'].apply(self._get_disease_code)
        if 'confirmation_method' in df.columns:
            df['identification_method_code'] = df['confirmation_method'].apply(self._get_method_code)

        records = df.to_dict('records')
        validated_records = []
        for i, rec in enumerate(records):
            try:
                clean = {k: v for k, v in rec.items() if pd.notna(v)}
                clean['source_code'] = source_code
                validated = schemas.FactDiseaseCaseCreate(**clean)
                validated_records.append(validated.model_dump())
            except Exception as e:
                stats['failed_records'] += 1
                stats['errors'].append(f"Record {i}: {e}")

        if validated_records:
            for i in range(0, len(validated_records), batch_size):
                batch = validated_records[i:i+batch_size]
                self.load_from_dataframe(pd.DataFrame(batch), models.FactDiseaseCase)
                stats['loaded_records'] += len(batch)
        return stats

    def _clean_case_data(self, df: pd.DataFrame, date_format: Optional[str]) -> pd.DataFrame:
        df = df.copy()
        for field in ['date_id_onset', 'date_id_reported']:
            if field in df.columns:
                df[field] = pd.to_datetime(df[field], errors='coerce').dt.strftime('%Y-%m-%d')
        if 'age_years' in df.columns:
            df['age_years'] = pd.to_numeric(df['age_years'], errors='coerce')
            df.loc[df['age_years'] < 0, 'age_years'] = None
        if 'sex' in df.columns:
            df['sex'] = df['sex'].astype(str).str.lower().str.strip()
            df['sex'] = df['sex'].replace({'m': 'male', 'f': 'female'})
        required = ['case_identifier', 'location_code', 'date_id_onset', 'disease']
        for req in required:
            if req not in df.columns:
                df[req] = None
        df = df.dropna(subset=required)
        return df

    # ==================== GENERIC LOADER ====================

    def load_from_dataframe(self, df: pd.DataFrame, model_class):
        """Generic method to load a DataFrame into any model."""
        records = df.to_dict('records')
        for record in records:
            filtered = {k: v for k, v in record.items() if hasattr(model_class, k)}
            self.session.add(model_class(**filtered))
        self.session.commit()