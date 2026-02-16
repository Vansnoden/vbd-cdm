import xarray as xr
import geopandas as gpd
from shapely.geometry import mapping
import os
import tempfile
import pandas as pd
from database.session import SessionLocal
from database.models import DimTime
import pandas as pd


def clip_era5_by_shapefile(grib_file_path, shapefile_path, filter_params=None, 
                          output_dir='./clipped_data', buffer_degrees=0.5):
    """
    Clip ERA5-Land GRIB files to features from any shapefile.
    
    Parameters:
    -----------
    grib_file_path : str
        Path to the input GRIB file
    shapefile_path : str
        Path to the shapefile (.shp) containing geometries
    filter_params : dict or list of dicts, optional
        Parameters to filter features from shapefile. Can be:
        - dict: Single filter condition, e.g., {'column': 'COUNTY', 'value': 'Siaya'}
        - list of dicts: Multiple OR conditions, e.g., [
            {'column': 'COUNTY', 'value': 'Siaya'},
            {'column': 'COUNTY', 'value': 'Nairobi'}
          ]
        - None: Use all features in shapefile
    output_dir : str
        Directory to save clipped NetCDF files
    buffer_degrees : float
        Buffer around geometries in degrees for initial data loading
    
    Returns:
    --------
    list
        Paths to saved clipped files, one per filtered feature
    """
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    def load_shapefile_features():
        """Load and filter features from shapefile"""
        try:
            # Load the shapefile
            gdf = gpd.read_file(shapefile_path)
            
            # Check if shapefile has a CRS, assign WGS84 if not
            if gdf.crs is None:
                gdf = gdf.set_crs('EPSG:4326', allow_override=True)
            
            print(f"Shapefile loaded: {len(gdf)} features")
            print(f"Available columns: {list(gdf.columns)}")
            
            # If no filter params, use all features
            if filter_params is None:
                return gdf
            
            # Handle single filter dict
            if isinstance(filter_params, dict):
                column = filter_params.get('column')
                value = filter_params.get('value')
                
                if column not in gdf.columns:
                    raise ValueError(f"Column '{column}' not found in shapefile")
                
                # Apply filter
                filtered_gdf = gdf[gdf[column] == value]
                
            # Handle list of filter dicts (OR conditions)
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
                
                # Combine masks with OR logic
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
        """Process a single feature, preserving all desired variables."""
        print(f"\nProcessing {feature_name}...")

        # Get bounding box for initial subset
        bbox = feature_gdf.total_bounds
        lon_slice = slice(bbox[0] - buffer_degrees, bbox[2] + buffer_degrees)
        lat_slice = slice(bbox[3] + buffer_degrees, bbox[1] - buffer_degrees)

        # Define the variables we want (add more as needed)
        variables = ['2t', 'tp']   # short names from GRIB

        datasets = []
        for var in variables:
            try:
                print(f"Loading variable: {var}")
                ds_var = xr.open_dataset(
                    grib_file_path,
                    engine='cfgrib',
                    backend_kwargs={'filter_by_keys': {'shortName': var}},
                    chunks={'time': 100}
                )
                # Subset to region (before clipping)
                lon_dim = 'longitude' if 'longitude' in ds_var.dims else 'lon'
                lat_dim = 'latitude' if 'latitude' in ds_var.dims else 'lat'
                ds_var = ds_var.sel(**{lon_dim: lon_slice, lat_dim: lat_slice})
                datasets.append(ds_var)
            except Exception as e:
                print(f"Warning: Could not load variable {var}: {e}")
                continue

        if not datasets:
            raise ValueError("No variables could be loaded.")

        # Merge all variables into one dataset (they share time/lat/lon)
        ds = xr.merge(datasets)
        print(f"Merged dataset variables: {list(ds.data_vars)}")

        # Ensure CRS for clipping
        if not hasattr(ds, 'rio') or not ds.rio.crs:
            ds = ds.rio.write_crs("EPSG:4326")

        # Clip to feature boundary
        print("Clipping to feature boundary...")
        clipped = ds.rio.clip(
            feature_gdf.geometry.apply(mapping),
            feature_gdf.crs,
            all_touched=True,
            drop=True
        )

        # Generate output filename
        base_name = os.path.basename(grib_file_path).replace('.grib', '').replace('.grb', '')
        safe_feature_name = feature_name.replace(' ', '_').replace('/', '_')
        output_path = os.path.join(output_dir, f"{base_name}_{safe_feature_name}.nc")

        # Save with compression
        print(f"Saving to {output_path}")
        encoding = {var: {'zlib': True, 'complevel': 1, 'dtype': 'float32'}
                    for var in clipped.data_vars}
        clipped.to_netcdf(output_path, encoding=encoding)

        print(f"Success! Clipped shape: {clipped[list(clipped.data_vars)[0]].shape}")
        return output_path


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