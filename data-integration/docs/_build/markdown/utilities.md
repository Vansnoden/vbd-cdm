# Utilities reference

## `utilities.consolidation`

| [`utilities.consolidation.DataConsolidator`](generated/utilities.consolidation.DataConsolidator.md#utilities.consolidation.DataConsolidator)                            | Consolidates and standardizes data in the CDM with full foreign key alignment.   |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|
| [`utilities.consolidation.get_iso_country_code`](generated/utilities.consolidation.get_iso_country_code.md#utilities.consolidation.get_iso_country_code)                | Get ISO alpha-2 country code from a country name or code.                        |
| [`utilities.consolidation.get_country_name`](generated/utilities.consolidation.get_country_name.md#utilities.consolidation.get_country_name)                            | Get country name from ISO alpha-2 code.                                          |
| [`utilities.consolidation.get_country_from_coords`](generated/utilities.consolidation.get_country_from_coords.md#utilities.consolidation.get_country_from_coords)       | Get ISO country name and code from latitude and longitude coordinates.           |
| [`utilities.consolidation.haversine_distance`](generated/utilities.consolidation.haversine_distance.md#utilities.consolidation.haversine_distance)                      | Calculate the great circle distance between two points in kilometers.            |
| [`utilities.consolidation.enrich_location_dataframe`](generated/utilities.consolidation.enrich_location_dataframe.md#utilities.consolidation.enrich_location_dataframe) | Enrich location DataFrame with ISO country codes.                                |
| [`utilities.consolidation.run_consolidation`](generated/utilities.consolidation.run_consolidation.md#utilities.consolidation.run_consolidation)                         | Convenience function to run consolidation.                                       |

## `utilities.data_transform_library`

| [`utilities.data_transform_library.cog_to_df`](generated/utilities.data_transform_library.cog_to_df.md#utilities.data_transform_library.cog_to_df)                                              | Convert Cloud Optimized GeoTIFF (COG) or regular GeoTIFF to DataFrame.                   |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| [`utilities.data_transform_library.tif_to_df_chunked`](generated/utilities.data_transform_library.tif_to_df_chunked.md#utilities.data_transform_library.tif_to_df_chunked)                      | Generator that yields DataFrames for each block of a GeoTIFF.                            |
| [`utilities.data_transform_library.nc_to_df`](generated/utilities.data_transform_library.nc_to_df.md#utilities.data_transform_library.nc_to_df)                                                 | Convert NetCDF (.nc or .nc4) to DataFrame, optionally selecting variable and time index. |
| [`utilities.data_transform_library.grib_to_df`](generated/utilities.data_transform_library.grib_to_df.md#utilities.data_transform_library.grib_to_df)                                           | Convert GRIB file to DataFrame (uses cfgrib engine).                                     |
| [`utilities.data_transform_library.timeseries_to_df`](generated/utilities.data_transform_library.timeseries_to_df.md#utilities.data_transform_library.timeseries_to_df)                         | Extract all time steps for given variables into a single DataFrame.                      |
| [`utilities.data_transform_library.timeseries_to_df_chunked`](generated/utilities.data_transform_library.timeseries_to_df_chunked.md#utilities.data_transform_library.timeseries_to_df_chunked) | Process timeseries data in chunks along the time dimension.                              |
| [`utilities.data_transform_library.dta_to_df`](generated/utilities.data_transform_library.dta_to_df.md#utilities.data_transform_library.dta_to_df)                                              | Convert Stata .dta file to DataFrame.                                                    |
| [`utilities.data_transform_library.geojson_to_df`](generated/utilities.data_transform_library.geojson_to_df.md#utilities.data_transform_library.geojson_to_df)                                  | Convert GeoJSON to DataFrame (drops geometry).                                           |
| [`utilities.data_transform_library.load_yaml_config`](generated/utilities.data_transform_library.load_yaml_config.md#utilities.data_transform_library.load_yaml_config)                         | Load YAML configuration file into a dictionary.                                          |

## `utilities.data_filter_library`

| [`utilities.data_filter_library.clip_era5_by_shapefile`](generated/utilities.data_filter_library.clip_era5_by_shapefile.md#utilities.data_filter_library.clip_era5_by_shapefile)   | Clip ERA5-Land GRIB files to features from any shapefile.                           |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| [`utilities.data_filter_library.clip_era5_to_region`](generated/utilities.data_filter_library.clip_era5_to_region.md#utilities.data_filter_library.clip_era5_to_region)            | Clip ERA5 data to a custom geometry (GeoDataFrame, GeoSeries, or Shapely geometry). |
| [`utilities.data_filter_library.batch_clip_era5`](generated/utilities.data_filter_library.batch_clip_era5.md#utilities.data_filter_library.batch_clip_era5)                        | Batch clip multiple GRIB files using the same shapefile and filters.                |
| [`utilities.data_filter_library.ensure_dates_exist`](generated/utilities.data_filter_library.ensure_dates_exist.md#utilities.data_filter_library.ensure_dates_exist)               |                                                                                     |

## `utilities.database_summary`

| [`utilities.database_summary.schema_summary_to_csv`](generated/utilities.database_summary.schema_summary_to_csv.md#utilities.database_summary.schema_summary_to_csv)          |    |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----|
| [`utilities.database_summary.detailed_table_info`](generated/utilities.database_summary.detailed_table_info.md#utilities.database_summary.detailed_table_info)                |    |
| [`utilities.database_summary.compare_schemas`](generated/utilities.database_summary.compare_schemas.md#utilities.database_summary.compare_schemas)                            |    |
| [`utilities.database_summary.export_all_table_samples`](generated/utilities.database_summary.export_all_table_samples.md#utilities.database_summary.export_all_table_samples) |    |
| [`utilities.database_summary.get_database_metadata`](generated/utilities.database_summary.get_database_metadata.md#utilities.database_summary.get_database_metadata)          |    |
