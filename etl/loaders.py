#-*- coding: utf-8 -*-

from pathlib import Path
from sqlalchemy.orm import Session
from database import models, schemas
from typing import List, Dict
import pandas as pd
from sqlalchemy.orm import Session
from database.models import Location, EnvironmentalFactor
from database.session import SessionLocal
from shapely import wkt
import geopandas as gpd
import pandas as pd
from . import mappings
from . import grib_utils
from . import netcdf_utils


class DatabaseLoader:
    def __init__(self, db_session: Session):
        self.session = db_session
    

    def bulk_load_cases(self, cases_data: List[Dict]):
        """Load multiple cases efficiently"""
        for case_dict in cases_data:
            # Validate with Pydantic
            case = schemas.HostCaseCreate(**case_dict)
            
            # Convert to ORM model
            db_case = models.HostCase(**case.dict())
            
            self.session.add(db_case)
        
        self.session.commit()
    

    def upsert_location(self, location_data: Dict):
        """Insert or update location"""
        location = self.session.query(models.Location)\
            .filter(models.Location.location_code == location_data['location_code'])\
            .first()
        
        if location:
            # Update existing
            for key, value in location_data.items():
                setattr(location, key, value)
        else:
            # Create new
            location = models.Location(**location_data)
            self.session.add(location)
        
        self.session.commit()
        return location
    
    
    def load_from_dataframe(self, df: pd.DataFrame, model_class):
        """Generic method to load pandas DataFrame to any model"""
        records = df.to_dict('records')
        for record in records:
            # Handle missing columns
            filtered_record = {k: v for k, v in record.items() 
                             if hasattr(model_class, k)}
            db_obj = model_class(**filtered_record)
            self.session.add(db_obj)
        
        self.session.commit()


    def load_from_grib(self, start_date, end_date, grib_file_pattern):
        """
        grib_file_pattern: string with {date} placeholder, e.g., '/data/era5_{date}.grib'
        """
        # 1. Load all locations with geometry
        locations = self._get_locations_as_geodataframe()
        
        # 2. Generate date range
        date_range = pd.date_range(start_date, end_date)
        
        for date in date_range:
            date_str = date.strftime('%Y-%m-%d')
            date_id = date_str
            file_path = grib_file_pattern.format(date=date.strftime('%Y%m%d'))
            
            # Dictionary to hold combined data for this date: key = (loc_code, date_id)
            records = {}
            for _, row in locations.iterrows():
                records[(row.location_code, date_id)] = {
                    'location_code': row.location_code,
                    'date_id': date_id
                }
            
            # Process each variable from mapping
            for grib_var, mapping in mappings.GRIB_VARIABLE_MAP.items():
                try:
                    ds = grib_utils.open_grib(file_path, variable=grib_var)
                    
                    # If sub-daily, aggregate to daily
                    if 'time' in ds.dims and ds.dims['time'] > 1:
                        ds = grib_utils.aggregate_to_daily(ds, grib_var)
                    
                    # Extract for each location
                    for _, row in locations.iterrows():
                        geom = row.geometry
                        if geom is None or geom.is_empty:
                            # Use centroid
                            lat, lon = row.centroid_lat, row.centroid_lon
                            if lat is None or lon is None:
                                continue
                            val = grib_utils.extract_point(ds, lat, lon, grib_var)
                        else:
                            val = grib_utils.extract_polygon(ds, geom, grib_var)
                        
                        if val is None:
                            continue
                        
                        # Apply unit conversion
                        if mapping['convert']:
                            val = mapping['convert'](val)
                        
                        # Store in records
                        key = (row.location_code, date_id)
                        records[key][mapping['column']] = val
                        
                except Exception as e:
                    print(f"Error processing variable {grib_var} for {file_path}: {e}")
                    continue
            
            # Now insert records into database
            for key, data in records.items():
                # Skip if no environmental data was collected (all vars missing)
                if any(col in data for col in ['temperature_mean', 'rainfall_total', 
                                                'temperature_min', 'temperature_max',
                                                'relative_humidity']):
                    # Check if record already exists
                    existing = self.session.query(EnvironmentalFactor).filter_by(
                        location_code=data['location_code'],
                        date_id=data['date_id']
                    ).first()
                    if existing:
                        # Update existing record with new values (optional)
                        for col, val in data.items():
                            if col not in ['location_code', 'date_id']:
                                setattr(existing, col, val)
                    else:
                        env_factor = EnvironmentalFactor(**data)
                        self.session.add(env_factor)
            
            self.session.commit()
            print(f"Loaded environmental data for {date_str}")


    def _get_locations_as_geodataframe(self):
        """Load all locations with WKT geometry into a GeoDataFrame."""
        locations = self.session.query(Location).all()
        data = []
        for loc in locations:
            geom = wkt.loads(loc.geometry_wkt) if loc.geometry_wkt else None
            data.append({
                'location_code': loc.location_code,
                'geometry': geom,
                'centroid_lat': loc.centroid_lat,
                'centroid_lon': loc.centroid_lon
            })
        # Create GeoDataFrame and explicitly set the geometry column
        gdf = gpd.GeoDataFrame(data, crs='EPSG:4326', geometry='geometry')
        return gdf

    
    def load_from_netcdf(self, start_date, end_date, file_path_func):
        """
        file_path_func: a function with signature (location_code, date) -> str
                       that returns the full path to the NetCDF file.
        Example: lambda loc, d: f"/data/{loc}_{d.strftime('%Y%m%d')}.nc"
        """
        # 1. Load all locations with geometries
        locations = self._get_locations_as_geodataframe()
        if locations.empty:
            print("No locations found in database. Run load_locations.py first.")
            return

        # 2. Generate date range
        date_range = pd.date_range(start_date, end_date)

        # 3. For each location and date, extract and store
        for date in date_range:
            date_str = date.strftime('%Y-%m-%d')
            date_id = date_str

            for _, loc_row in locations.iterrows():
                loc_code = loc_row.location_code
                file_path = file_path_func(loc_code, date)

                if not Path(file_path).exists():
                    print(f"File not found: {file_path}")
                    continue

                # Dictionary to hold data for this location+date
                record = {'location_code': loc_code, 'date_id': date_id}

                # Open the NetCDF file once per location+date
                try:
                    ds = netcdf_utils.open_netcdf(file_path)
                except Exception as e:
                    print(f"Error opening {file_path}: {e}")
                    continue

                # Process each variable in the mapping that exists in the dataset
                for nc_var, mapping in mappings.NC_VARIABLE_MAP.items():
                    if nc_var not in ds:
                        # Variable not in this file – skip silently
                        continue

                    try:
                        # If sub-daily, aggregate to daily using the specified method
                        if 'time' in ds.dims and ds.sizes['time'] > 1:
                            da = netcdf_utils.aggregate_to_daily(
                                ds, nc_var, method=mapping.get('agg_method', 'mean')
                            )
                        else:
                            da = ds[nc_var]

                        # Extract value for this location (using polygon or centroid)
                        geom = loc_row.geometry
                        if geom is None or geom.is_empty:
                            lat, lon = loc_row.centroid_lat, loc_row.centroid_lon
                            if lat is None or lon is None:
                                continue
                            val = netcdf_utils.extract_point(da, lat, lon)
                        else:
                            val = netcdf_utils.extract_polygon(da, geom)

                        if val is None:
                            continue

                        # Apply unit conversion
                        if mapping['convert']:
                            val = mapping['convert'](val)

                        record[mapping['column']] = val

                    except Exception as e:
                        print(f"Error processing {nc_var} for {loc_code} on {date_str}: {e}")
                        continue

                # After processing all variables, upsert the record
                if len(record) > 2:  # at least one environmental variable besides loc/date
                    self._upsert_environmental(record)

            # Commit after each date to avoid large transactions
            self.session.commit()
            print(f"Loaded environmental data for {date_str}")


    def _get_locations_as_geodataframe(self):
        """Load all locations with WKT geometry into a GeoDataFrame."""
        locations = self.session.query(Location).all()
        data = []
        for loc in locations:
            geom = wkt.loads(loc.geometry_wkt) if loc.geometry_wkt else None
            data.append({
                'location_code': loc.location_code,
                'geometry': geom,
                'centroid_lat': loc.centroid_lat,
                'centroid_lon': loc.centroid_lon
            })
        gdf = gpd.GeoDataFrame(data, crs='EPSG:4326', geometry='geometry')
        return gdf


    def _upsert_environmental(self, record):
        """Insert or update a record in environmental_factor."""
        existing = self.session.query(EnvironmentalFactor).filter_by(
            location_code=record['location_code'],
            date_id=record['date_id']
        ).first()
        if existing:
            for key, value in record.items():
                if key not in ['location_code', 'date_id']:
                    setattr(existing, key, value)
        else:
            env_factor = EnvironmentalFactor(**record)
            self.session.add(env_factor)
