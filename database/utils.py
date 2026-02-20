import xarray as xr
import geopandas as gpd
from shapely.geometry import mapping
import os
import tempfile
import pandas as pd
from database.session import SessionLocal
from database.models import DimTime
import pandas as pd
import traceback
import cfgrib
import xarray as xr
import cfgrib
import dask


def clip_era5_by_shapefile(grib_file_path, shapefile_path, filter_params=None, 
                          output_dir='./clipped_data', buffer_degrees=0.5):
    """
    Clip ERA5-Land GRIB files to features from any shapefile.
    (parameters unchanged)
    """
    os.makedirs(output_dir, exist_ok=True)

    def load_shapefile_features():
        """Load and filter features from shapefile (uses closure for filter_params, shapefile_path)"""
        try:
            gdf = gpd.read_file(shapefile_path)
            if gdf.crs is None:
                gdf = gdf.set_crs('EPSG:4326', allow_override=True)
            print(f"Shapefile loaded: {len(gdf)} features")
            print(f"Available columns: {list(gdf.columns)}")

            if filter_params is None:
                return gdf

            if isinstance(filter_params, dict):
                column = filter_params.get('column')
                value = filter_params.get('value')
                if column not in gdf.columns:
                    raise ValueError(f"Column '{column}' not found in shapefile")
                filtered_gdf = gdf[gdf[column] == value]

            elif isinstance(filter_params, list):
                masks = []
                for param in filter_params:
                    column = param.get('column')
                    value = param.get('value')
                    if column not in gdf.columns:
                        print(f"Warning: Column '{column}' not found, skipping")
                        continue
                    masks.append(gdf[column] == value)
                if not masks:
                    raise ValueError("No valid filter conditions found")
                combined_mask = masks[0]
                for mask in masks[1:]:
                    combined_mask = combined_mask | mask
                filtered_gdf = gdf[combined_mask]
            else:
                raise ValueError("filter_params must be dict, list of dicts, or None")

            print(f"Filtered to {len(filtered_gdf)} feature(s)")
            return filtered_gdf
        except Exception as e:
            print(f"Error loading shapefile: {e}")
            raise

    
    def process_feature(feature_gdf, feature_name, grib_file_path, output_dir):
        print(f"\nProcessing {feature_name}...")
        bbox = feature_gdf.total_bounds
        lon_slice = slice(bbox[0] - buffer_degrees, bbox[2] + buffer_degrees)
        lat_slice = slice(bbox[3] + buffer_degrees, bbox[1] - buffer_degrees)

        try:
            # Open all variable groups lazily with Dask chunks
            print("Opening GRIB file with lazy loading...")
            ds_list = cfgrib.open_datasets(
                grib_file_path,
                chunks={'time': 100},          # Load time in chunks of 100 steps
                backend_kwargs={'errors': 'raise'}
            )
            print(f"Found {len(ds_list)} separate variable groups.")

            # Subset each dataset to the bounding box and drop auxiliary variables
            subsets = []
            for i, ds in enumerate(ds_list):
                print(f"  Processing group {i+1}: {list(ds.data_vars)}")
                # Determine dimension names (CF convention)
                lon_dim = 'longitude' if 'longitude' in ds.dims else 'lon'
                lat_dim = 'latitude' if 'latitude' in ds.dims else 'lat'
                # Subset – this returns a lazy view (no data loaded yet)
                ds_sel = ds.sel(**{lon_dim: lon_slice, lat_dim: lat_slice})
                
                # Drop variables that often cause merge conflicts (non‑data coordinates)
                vars_to_drop = ['step', 'valid_time', 'number', 'surface', 'isobaricInhPa']
                to_drop = [v for v in vars_to_drop if v in ds_sel.variables]
                if to_drop:
                    ds_sel = ds_sel.drop_vars(to_drop)
                
                subsets.append(ds_sel)

            if not subsets:
                raise ValueError("No datasets loaded from GRIB file.")

            # Merge all subsets lazily – still no data loaded
            print("Merging variable groups (lazy)...")
            ds = xr.merge(subsets, compat='override', join='outer')
            print(f"Merged dataset variables: {list(ds.data_vars)}")

        except Exception as e:
            print(f"Error loading/processing GRIB: {e}")
            traceback.print_exc()
            return None

        # Ensure CRS for clipping (lazy, no data loaded)
        try:
            if not hasattr(ds, 'rio') or not ds.rio.crs:
                ds = ds.rio.write_crs("EPSG:4326")
        except Exception as e:
            print("Error setting CRS:")
            traceback.print_exc()
            return None

        # Clip to feature boundary (lazy)
        try:
            print("Clipping to feature boundary (lazy)...")
            clipped = ds.rio.clip(
                feature_gdf.geometry.apply(mapping),
                feature_gdf.crs,
                all_touched=True,
                drop=True
            )
        except Exception as e:
            print("Error during clipping:")
            traceback.print_exc()
            return None

        # Generate output filename
        base_name = os.path.basename(grib_file_path).replace('.grib', '').replace('.grb', '')
        safe_feature_name = feature_name.replace(' ', '_').replace('/', '_')
        output_path = os.path.join(output_dir, f"{base_name}_{safe_feature_name}.nc")

        # Save with compression – this triggers the actual computation chunk by chunk
        try:
            print(f"Saving to {output_path} (this will trigger computation)...")
            encoding = {var: {'zlib': True, 'complevel': 1, 'dtype': 'float32'}
                        for var in clipped.data_vars}
            # Use compute=False to write lazily, but we want to actually write now.
            # to_netcdf with compute=True will compute and write.
            clipped.to_netcdf(output_path, encoding=encoding, compute=True)
        except Exception as e:
            print("Error saving file:")
            traceback.print_exc()
            return None

        print(f"Success! Clipped shape: {clipped[list(clipped.data_vars)[0]].shape}")
        return output_path


    # --- Main flow of clip_era5_by_shapefile ---
    filtered_gdf = load_shapefile_features()
    if filtered_gdf.empty:
        print("No features found after filtering")
        return []

    output_files = []
    for idx, row in filtered_gdf.iterrows():
        single_feature_gdf = gpd.GeoDataFrame([row], geometry='geometry', crs=filtered_gdf.crs)

        # Try to create a meaningful feature name
        feature_name = None
        for name_col in ['NAME', 'name', 'COUNTY', 'county', 'ADM2_NAME', 'ADM1_NAME']:
            if name_col in row.index and pd.notna(row[name_col]):
                feature_name = str(row[name_col])
                break
        if feature_name is None:
            feature_name = f"feature_{idx}"
        for id_col in ['ID', 'id', 'OBJECTID', 'FID']:
            if id_col in row.index and pd.notna(row[id_col]):
                feature_name = f"{feature_name}_{row[id_col]}"
                break

        output_file = process_feature(single_feature_gdf, feature_name, grib_file_path, output_dir)
        if output_file:
            output_files.append(output_file)

    return output_files    


def clip_era5_to_kenya_counties(grib_file_path, county_names, shapefile_path=None, 
                               county_column='COUNTY', output_dir='./clipped_data'):
    """
    Convenience function specifically for Kenya counties.
    
    Parameters:
    -----------
    grib_file_path : str
        Path to the input GRIB file
    county_names : list or str
        Name(s) of Kenyan county/counties to clip to
    shapefile_path : str, optional
        Path to Kenya counties shapefile. If None, tries default locations
    county_column : str
        Column name in shapefile containing county names
    output_dir : str
        Directory to save clipped NetCDF files
    
    Returns:
    --------
    list
        Paths to saved clipped files
    """
    
    # Convert county_names to list if string
    if isinstance(county_names, str):
        county_names = [county_names]
    
    # Try to find Kenya shapefile if not provided
    if shapefile_path is None:
        possible_paths = [
            "kenya_counties.shp",
            "/path/to/kenya/counties.shp",  # Add your common paths here
            "data/shapefiles/kenya/admin2.shp"
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                shapefile_path = path
                print(f"Using shapefile: {shapefile_path}")
                break
        
        if shapefile_path is None:
            raise FileNotFoundError(
                "Kenya shapefile not found. Please provide shapefile_path parameter or "
                "download from: https://data.humdata.org/dataset/ken-administrative-boundaries"
            )
    
    # Build filter parameters
    if len(county_names) == 1:
        filter_params = {'column': county_column, 'value': county_names[0]}
    else:
        filter_params = [
            {'column': county_column, 'value': county_name}
            for county_name in county_names
        ]
    
    # Use the general function
    return clip_era5_by_shapefile(
        grib_file_path=grib_file_path,
        shapefile_path=shapefile_path,
        filter_params=filter_params,
        output_dir=output_dir
    )


def clip_era5_to_region(grib_file_path, geometry, region_name, output_dir='./clipped_data'):
    """
    Clip ERA5 data to a custom geometry (GeoDataFrame, GeoSeries, or Shapely geometry).
    
    Parameters:
    -----------
    grib_file_path : str
        Path to the input GRIB file
    geometry : geopandas.GeoDataFrame, geopandas.GeoSeries, or shapely.geometry
        Geometry to clip to
    region_name : str
        Name for the output file
    output_dir : str
        Directory to save clipped NetCDF file
    
    Returns:
    --------
    str
        Path to saved clipped file
    """
    
    import pandas as pd
    
    os.makedirs(output_dir, exist_ok=True)
    
    # Convert geometry to GeoDataFrame if needed
    if isinstance(geometry, gpd.GeoDataFrame):
        gdf = geometry
    elif isinstance(geometry, gpd.GeoSeries):
        gdf = gpd.GeoDataFrame(geometry=geometry)
    else:
        # Assume it's a shapely geometry
        gdf = gpd.GeoDataFrame(geometry=[geometry], crs='EPSG:4326')
    
    # Ensure CRS is set
    if gdf.crs is None:
        gdf = gdf.set_crs('EPSG:4326', allow_override=True)
    
    # Create a temporary shapefile
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_shapefile = os.path.join(tmpdir, 'temp_region.shp')
        gdf.to_file(temp_shapefile)
        
        # Use the general function with no filtering
        output_files = clip_era5_by_shapefile(
            grib_file_path=grib_file_path,
            shapefile_path=temp_shapefile,
            filter_params=None,  # Use all features (just one in this case)
            output_dir=output_dir
        )
    
    # Rename the output file to use the region_name
    if output_files:
        original_path = output_files[0]
        base_name = os.path.basename(grib_file_path).replace('.grib', '').replace('.grb', '')
        new_path = os.path.join(output_dir, f"{base_name}_{region_name.replace(' ', '_')}.nc")
        
        # Rename the file
        if os.path.exists(original_path):
            os.rename(original_path, new_path)
            return new_path
    
    return None


def batch_clip_era5(grib_files, shapefile_path, filter_params=None, output_base_dir='./clipped_data'):
    """
    Batch clip multiple GRIB files using the same shapefile and filters.
    
    Parameters:
    -----------
    grib_files : list of str
        Paths to GRIB files
    shapefile_path : str
        Path to the shapefile
    filter_params : dict or list of dicts, optional
        Filter parameters (same as clip_era5_by_shapefile)
    output_base_dir : str
        Base directory for output
    
    Returns:
    --------
    dict
        Dictionary mapping input files to lists of output files
    """
    
    results = {}
    
    for grib_file in grib_files:
        print(f"\nProcessing: {grib_file}")
        
        # Create output subdirectory based on input filename
        base_name = os.path.basename(grib_file).replace('.grib', '').replace('.grb', '')
        output_dir = os.path.join(output_base_dir, base_name)
        
        try:
            output_files = clip_era5_by_shapefile(
                grib_file_path=grib_file,
                shapefile_path=shapefile_path,
                filter_params=filter_params,
                output_dir=output_dir
            )
            
            results[grib_file] = output_files
            print(f"Generated {len(output_files)} output files")
            
        except Exception as e:
            print(f"Error processing {grib_file}: {e}")
            results[grib_file] = []
            traceback.print_exc()
    
    return results


def ensure_dates_exist(start_date, end_date):
    session = SessionLocal()
    try:
        date_range = pd.date_range(start_date, end_date)
        for date in date_range:
            date_id = date.strftime('%Y-%m-%d')
            exists = session.query(DimTime).filter_by(date_id=date_id).first()
            if not exists:
                dt = DimTime(
                    date_id=date_id,
                    date=date,
                    year=date.year,
                    month=date.month,
                    week=date.isocalendar()[1],
                    day_of_year=date.timetuple().tm_yday,
                    season='dry' if date.month in [11,12,1,2,3] else 'wet',  # customize for your region
                    is_holiday='no'
                )
                session.add(dt)
        session.commit()
    finally:
        session.close()


# Example usage
if __name__ == "__main__":
    import pandas as pd

    BASE_DIR = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets"
    shapefile = "/mnt/monadworld/library/shapefiles/kenya/kenyan-counties/County.shp"
    
    print("\nBatch processing multiple GRIB files")
    grib_files = [
        BASE_DIR + "/climatic_timeseries_data/2024_data.grib",
        BASE_DIR + "/climatic_timeseries_data/2025_data.grib"
    ]
    
    batch_results = batch_clip_era5(
        grib_files=grib_files,
        shapefile_path=shapefile,
        filter_params=[
            {'column': 'COUNTY', 'value': 'Mombasa'},
            {'column': 'COUNTY', 'value': 'Siaya'}
        ],
        output_base_dir=BASE_DIR + '/clipped/batch_results'
    )
    
    # Print batch results summary
    for input_file, output_files in batch_results.items():
        print(f"{os.path.basename(input_file)}: {len(output_files)} output files")