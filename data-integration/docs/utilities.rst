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

``export_to_csv.py``
--------------------

A standalone script at the project root. Streams the CDM to CSV for sharing.
Not part of the ingest pipeline; does not modify the database.

.. code-block:: bash

    python export_to_csv.py                    # single wide-union CSV
    python export_to_csv.py --per-table        # one CSV per fact table, zipped
    python export_to_csv.py --no-dims          # raw FK codes, no joins
    python export_to_csv.py --limit 5000       # smoke test
    python export_to_csv.py --chunk-size 20000 # rows per streaming chunk

Modes
~~~~~

* **Single CSV (default).** Every fact observation becomes one row. Columns
  that do not apply to a given row are left blank. Column order places
  common dimension fields first (``fact_table``, ``fact_id``,
  ``location_id``, ``location_name``, ``country_code``, ``date_id``,
  ``year``, ``species_name``, ...) followed by fact-specific columns
  alphabetically. Simple to open and filter in Excel or pandas; larger on
  disk because of the padding.

* **Per-table zip (``--per-table``).** Writes one denormalized CSV per fact
  table, plus a ``README.txt`` manifest, and bundles them into a zip. Each
  CSV has its own clean header with only the columns that table uses.

Memory
~~~~~~

The script streams with ``pd.read_sql_query(..., chunksize=N)`` and writes
each chunk to the output file before reading the next. Peak memory is one
chunk plus a file handle, independent of database size. The old
read-everything-then-concat pattern held the entire export in memory twice
at peak; it has been replaced.

Header discovery for the single-CSV mode is done with a ``LIMIT 0`` read on
each fact query, which fetches zero rows but returns the correct column
metadata.

Column layout
~~~~~~~~~~~~~

The canonical order for the wide-union CSV is declared in ``COMMON_COLUMNS``
at the top of the script. Columns that are not listed there are appended
alphabetically after the common block, so a new fact-specific column always
lands at the tail without disturbing the leading schema.