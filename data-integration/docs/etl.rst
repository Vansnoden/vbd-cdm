ETL pipeline
============

The ETL is a five-stage pipeline. Each stage is a Python module with a single
public function, so stages can be exercised independently.

.. code-block:: text

    ┌──────────┐   ┌─────────────┐   ┌───────────────┐   ┌────────┐   ┌──────────────┐
    │  Config  │──▶│  Extractor  │──▶│  Transformer  │──▶│ Loader │──▶│ Consolidator │
    │  (YAML)  │   │ (File*)     │   │ (Mapping*)    │   │ (DB*)  │   │ (Python)     │
    └──────────┘   └─────────────┘   └───────────────┘   └────────┘   └──────────────┘
         │              │                    │                 │              │
      source_         pandas              dict of            upsert        FK repair,
      config          DataFrame           {table: df}        into CDM      dedup,
                                                                           vocab match

Stage 1 - Config
----------------

``utilities.data_transform_library.load_yaml_config`` reads ``base_config.yaml``.
Each ``data_sources.<name>`` entry declares:

.. code-block:: yaml

    sources:
      anopheles_kenya:
        source_name: "anopheles_kenya"
        type: "folder"
        path: "data/raw/vectors"
        extension: "csv"
        matching: "mappings/vectors_to_cdm.csv"
        extract_mode: "single"     # or "chunked" for raster
        extension_options:
          sep: ";"
          encoding: "utf-8"

Stage 2 - Extract
-----------------

``etl.extractors.file_extractor.FileExtractor`` dispatches on file extension
via ``utilities.data_transform_library``. Supported formats:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Extension
     - Backend
   * - ``csv``, ``tsv``
     - ``pandas.read_csv`` (chunked if ``chunksize`` provided)
   * - ``xlsx``, ``xls``
     - ``calamine`` (fast) with ``openpyxl`` fallback
   * - ``json``
     - ``pandas.read_json`` (line-delimited by default)
   * - ``xml``
     - ``xml.etree.ElementTree.iterparse`` (streaming)
   * - ``nc``, ``nc4``
     - ``xarray``
   * - ``grib``, ``grib2``
     - ``cfgrib`` via ``xarray``
   * - ``cog``, ``tif``, ``tiff``
     - ``rioxarray``; ``tif_to_df_chunked`` yields windowed chunks
   * - ``dta``, ``sas7bdat``, ``sav``
     - ``pyreadstat`` chunked
   * - ``geojson``, ``gpkg``, ``shp``
     - ``pyogrio`` (geometry dropped by default)

Chunked extraction
~~~~~~~~~~~~~~~~~~

For very large rasters, set ``extract_mode: "chunked"``. The extractor yields
DataFrames per window; ``main.run_ingestion_for_source`` upserts each chunk
before reading the next. This keeps memory bounded and makes the pipeline
resumable.

Stage 3 - Transform
-------------------

``etl.transformers.mapping_transformer.MappingTransformer`` reads a mapping
CSV. See :doc:`mapping` for the format.

Supported transforms:

.. list-table::
   :header-rows: 1
   :widths: 15 85

   * - Name
     - Behaviour
   * - ``none``
     - Pass through.
   * - ``map``
     - ``{"value_a": "A", "value_b": "B"}`` - dict lookup; a ``"ref"`` key
       triggers a dimension FK lookup (see mapping).
   * - ``cast``
     - ``{"type": "int"}`` - int, float, str.
   * - ``date_parse``
     - ``{"format": "%d/%m/%Y"}`` - returns ``YYYY-MM-DD``.
   * - ``format_date``
     - ``{"columns": ["year","month","day"], "default": null}`` - builds a
       date string from three integer columns. Handles missing values.
   * - ``split``
     - ``{"delimiter": ",", "index": 0}`` - take one field from a delimited
       string.
   * - ``unit_convert``
     - ``{"factor": 0.1, "offset": 0}`` - linear transform.
   * - ``concat``
     - ``{"columns": ["a","b"], "separator": " "}`` - join string columns.
   * - ``coalesce``
     - ``{"columns": ["a","b"], "default": null}`` - first non-null value.
   * - ``derive_category``
     - Rule-based label from one or more numeric columns. See the class
       docstring in ``mapping_transformer.py``.

Stage 4 - Load
--------------

``etl.loaders.db_loader.DBLoader`` upserts DataFrames using natural keys.
The natural-key configuration is per-table (``NATURAL_KEY_CONFIG``).

Two backends:

* **PostgreSQL** - ``COPY`` into a temp staging table, then ``INSERT ... ON
  CONFLICT DO UPDATE``. Only triggered when ``use_copy=True`` and the batch
  is larger than twice ``batch_size``.
* **SQLite** - batched ``INSERT ... ON CONFLICT DO UPDATE``.
  The loaders **never** use ``INSERT OR REPLACE`` (which would delete and
  reinsert the PK, breaking autoincrement IDs and cascade references).

Partial-update semantics
~~~~~~~~~~~~~~~~~~~~~~~~

An upsert updates only the columns the incoming batch actually carries
values for. Formally, a column participates in the ``DO UPDATE SET`` clause
only if at least one row in the batch has a non-null value for it.

This is what allows several sources to share a natural key and each populate
a distinct subset of columns. The six MAP burden sources, for example, all
write to ``fact_disease_burden`` at key ``(location_id, year, disease_code,
pathogen_species)`` but each writes exactly one of ``cases_mean``,
``incidence_rate``, or ``prevalence_rate``. Before the loader was changed,
the last source would overwrite the other two columns with NULL. Now each
source touches only its own column.

The rule is implemented in all three upsert paths
(``_copy_upsert_postgresql``, ``_upsert_postgresql``, ``_upsert_sqlite``) as::

    update_cols = [
        col for col in df.columns
        if col not in pk_cols
        and col not in conflict_targets
        and df[col].notna().any()
    ]

**Consequence.** A source that legitimately wants to clear an existing
column back to NULL cannot do so through the standard upsert path. That
case is not currently exercised by any mapping in ``base_config.yaml``.

Autoincrement primary keys
~~~~~~~~~~~~~~~~~~~~~~~~~~

``load_table`` exempts autoincrement PK columns from its NOT NULL guard. A
missing autoincrement PK means "the database will assign one", not "this
row is malformed". The upsert paths already pop None-valued PKs from each
record before executing.

Null sentinels
~~~~~~~~~~~~~~

Nullable natural-key columns are replaced with sentinel values before insert
(empty string for text, ``-1.0`` for concentration). This is required because
SQL treats NULLs as distinct in unique indexes. See
:doc:`cdm_contract` §4.

Stage 5 - Consolidate
---------------------

``utilities.consolidation.DataConsolidator`` runs eleven steps, in order:

#. Align all foreign keys - null or repair orphaned FK values.
#. Standardise country codes to ISO alpha-2.
#. Bind climate-grid coordinates to the study region.
#. Standardise vocabularies against GADM / IDOMAL / NCBI.
#. Fix orphaned burden records specifically.
#. Expand yearly burden to daily case rows.
#. Consolidate locations (name-based, then DBSCAN-proximity-based).
#. Consolidate vocabulary duplicates (disease, vector, insecticide, source).
#. Detect fact-table duplicates and, when any exist, resolve them.
#. Compute resistance status from mortality counts.
#. Final verification (country codes, remaining orphans).

The FK-alignment pass is the most important - it can be run standalone:

.. code-block:: python

    from utilities.consolidation import DataConsolidator
    with DataConsolidator() as c:
        c.align_all_foreign_keys(dry_run=True)

Duplicate resolution is guarded
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Step 9 rebuilds three fact tables (``fact_environment``,
``fact_insecticide_resistance``, ``fact_disease_case``) with a
delete-then-insert pattern, but only when genuine duplicates exist. On a
clean database the log reads ``No environment duplicates; fact_environment
untouched`` and no table is modified.

This guard exists because the rebuild is destructive by construction: any
column missing from the SELECT list is silently NULLed. The guard
eliminates the failure mode where an empty or partially-populated temp
table combined with an unconditional DELETE would silently wipe an entire
fact table.

Stage 6 - Export (optional)
---------------------------

``export_to_csv.py`` is a standalone script that streams the CDM to CSV for
sharing. It is not part of the ingest pipeline and does not modify the
database.

.. code-block:: bash

    # Single wide-union CSV (default: outputs/cdm_export.csv)
    python export_to_csv.py

    # Per-table CSVs bundled in a zip
    python export_to_csv.py --per-table

    # Raw FK codes, no dimension joins
    python export_to_csv.py --no-dims

    # Smoke test with capped row counts
    python export_to_csv.py --limit 5000

Peak memory is one chunk (default 50,000 rows), independent of database
size. See :doc:`utilities` for the exported column layout.