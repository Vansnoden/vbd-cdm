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

        # Smaller chunk size to control memory
        chunk_size = 10  # time steps per chunk

        # Define daily aggregation methods per variable
        agg_methods = {
            't2m': 'mean',   # 2m temperature → daily mean
            'tp': 'sum',      # total precipitation → daily sum
        }

        try:
            # Open all variable groups lazily
            print("Opening GRIB file with lazy loading...")
            ds_list = cfgrib.open_datasets(
                grib_file_path,
                chunks={'time': chunk_size},
                backend_kwargs={'errors': 'raise'}
            )
            print(f"Found {len(ds_list)} separate variable groups.")
        except Exception as e:
            print(f"Error opening GRIB file: {e}")
            return None

        # Generate output filename
        base_name = os.path.basename(grib_file_path).replace('.grib', '').replace('.grb', '')
        safe_feature_name = feature_name.replace(' ', '_').replace('/', '_')
        output_path = os.path.join(output_dir, f"{base_name}_{safe_feature_name}.nc")

        daily_datasets = []  # store daily versions of each variable

        for i, ds in enumerate(ds_list):
            print(f"  Processing group {i+1}: {list(ds.data_vars)}")
            # Determine dimension names (CF convention)
            lon_dim = 'longitude' if 'longitude' in ds.dims else 'lon'
            lat_dim = 'latitude' if 'latitude' in ds.dims else 'lat'
            # Subset to bounding box
            ds_sel = ds.sel(**{lon_dim: lon_slice, lat_dim: lat_slice})

            # Ensure CRS for clipping
            ds_sel = ds_sel.rio.write_crs("EPSG:4326")
            # Clip to feature boundary
            try:
                clipped = ds_sel.rio.clip(
                    feature_gdf.geometry.apply(mapping),
                    feature_gdf.crs,
                    all_touched=True,
                    drop=True
                )
            except Exception as e:
                print(f"Error during clipping for group {i+1}: {e}")
                continue

            # For each variable in this group, aggregate to daily
            for var in clipped.data_vars:
                method_name = agg_methods.get(var, 'mean')
                print(f"    Aggregating {var} to daily using '{method_name}'...")
                resampler = clipped[var].resample(time='1D')
                daily = getattr(resampler, method_name)()
                daily_ds = daily.to_dataset()
                daily_datasets.append(daily_ds)

        if not daily_datasets:
            print("No data after processing.")
            return None

        # Merge all daily datasets (they now share the same time dimension)
        print("Merging daily datasets...")
        merged = xr.merge(daily_datasets, compat='override', join='outer')
        print(f"Merged dataset variables: {list(merged.data_vars)}")

        # Save with compression
        try:
            encoding = {var: {'zlib': True, 'complevel': 1, 'dtype': 'float32'}
                        for var in merged.data_vars}
            merged.to_netcdf(output_path, encoding=encoding, compute=True)
            print(f"Success! Output saved to {output_path}")
        except Exception as e:
            print(f"Error saving file: {e}")
            return None

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
        for name_col in ['NAME', 'name', 'COUNTY', 'county', 'ADM2_NAME', 'ADM1_NAME', 'adm1_name']:
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
                    is_holiday=False   # was 'no' – now correct boolean
                )
                session.add(dt)
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()
