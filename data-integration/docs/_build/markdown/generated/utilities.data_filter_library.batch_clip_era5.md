# utilities.data_filter_library.batch_clip_era5

### utilities.data_filter_library.batch_clip_era5(grib_files, shapefile_path, filter_params=None, output_base_dir='./clipped_data')

Batch clip multiple GRIB files using the same shapefile and filters.

## Parameters:

grib_files
: Paths to GRIB files

shapefile_path
: Path to the shapefile

filter_params
: Filter parameters (same as clip_era5_by_shapefile)

output_base_dir
: Base directory for output

## Returns:

dict
: Dictionary mapping input files to lists of output files
