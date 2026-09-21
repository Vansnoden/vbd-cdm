# utilities.data_filter_library.clip_era5_to_region

### utilities.data_filter_library.clip_era5_to_region(grib_file_path, geometry, region_name, output_dir='./clipped_data')

Clip ERA5 data to a custom geometry (GeoDataFrame, GeoSeries, or Shapely geometry).

## Parameters:

grib_file_path
: Path to the input GRIB file

geometry
: Geometry to clip to

region_name
: Name for the output file

output_dir
: Directory to save clipped NetCDF file

## Returns:

str
: Path to saved clipped file
