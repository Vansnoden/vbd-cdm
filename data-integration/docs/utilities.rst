Utilities reference
===================

``utilities.consolidation``
---------------------------

.. autosummary::
   :toctree: generated
   :nosignatures:

   utilities.consolidation.DataConsolidator
   utilities.consolidation.get_iso_country_code
   utilities.consolidation.get_country_name
   utilities.consolidation.get_country_from_coords
   utilities.consolidation.haversine_distance
   utilities.consolidation.enrich_location_dataframe
   utilities.consolidation.run_consolidation

``utilities.data_transform_library``
------------------------------------

.. autosummary::
   :toctree: generated
   :nosignatures:

   utilities.data_transform_library.cog_to_df
   utilities.data_transform_library.tif_to_df_chunked
   utilities.data_transform_library.nc_to_df
   utilities.data_transform_library.grib_to_df
   utilities.data_transform_library.timeseries_to_df
   utilities.data_transform_library.timeseries_to_df_chunked
   utilities.data_transform_library.dta_to_df
   utilities.data_transform_library.geojson_to_df
   utilities.data_transform_library.load_yaml_config

``utilities.data_filter_library``
---------------------------------

.. autosummary::
   :toctree: generated
   :nosignatures:

   utilities.data_filter_library.clip_era5_by_shapefile
   utilities.data_filter_library.clip_era5_to_region
   utilities.data_filter_library.batch_clip_era5
   utilities.data_filter_library.ensure_dates_exist

``utilities.database_summary``
------------------------------

.. autosummary::
   :toctree: generated
   :nosignatures:

   utilities.database_summary.schema_summary_to_csv
   utilities.database_summary.detailed_table_info
   utilities.database_summary.compare_schemas
   utilities.database_summary.export_all_table_samples
   utilities.database_summary.get_database_metadata