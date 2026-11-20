ETL pipeline
============

The ETL is a sequence of stages, each a Python module with a public entry point, so
a stage can be exercised on its own. Two things thread through all of them: a
**run context** (run id, registry, registry digest) and **one** database URL.

.. code-block:: text

    ┌───────────┐   ┌────────────┐   ┌──────────────┐   ┌────────┐   ┌──────────────┐
    │ RunContext│──▶│ Extractor  │──▶│ Transformer  │──▶│ Loader │──▶│ Consolidator │
    │ registry  │   │ FileExtract│   │ MappingTrans │   │ DBLoader│  │ + vocabulary │
    │ run id    │   │            │   │ + resolver   │   │ + ledger│  │ + alignment  │
    └───────────┘   └────────────┘   └──────────────┘   └────────┘   └──────┬───────┘
         │                │                  │                │              │
      packs,          pandas DF         {table: DF}       upsert into     merge, dedup,
      digest,         (or a chunk        per target        CDM, ledger,    resolve,
      countries       generator)         table             projection      classify

.. contents:: On this page
   :local:
   :depth: 1

Run context
-----------

``main.RunContext`` holds one run's identity and is threaded through ingestion,
consolidation and the concept pass, so every ``Resolution`` and every report names
the same run and the same vocabulary release:

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Attribute
     - Meaning
   * - ``run_id``
     - ``YYYYmmddTHHMMSS-<6 hex>``. Part of ``fact_resolution_log``'s unique key.
   * - ``registry``
     - A ``VocabularyRegistry``, or ``None`` when no packs are shipped. A missing
       directory is a warning, not a failure; a malformed manifest raises.
   * - ``registry_digest``
     - SHA-256 over the sorted ``(pack_id, version)`` pairs. Recorded next to every
       decision so "which knowledge produced this" has an answer.
   * - ``countries``
     - The study-area filter, from ``core.countries``.

The resolved database URL is logged at start-up. ``database.session`` fixes the URL
at import time, so an entry point that does not load ``.env`` first falls back to
SQLite and silently creates an empty file with the full schema.

Stage 1 — Config
----------------

``base_config.yaml`` has three top-level blocks:

* ``core`` — the study: ``countries``, ``study_area``, ``grid_scope``,
  ``admin_boundaries.path``.
* ``data_sources`` — one entry per source.
* ``process.steps`` — which of ``ingest_data``, ``consolidate``,
  ``generate_summary`` run.

Each ``data_sources.<key>`` entry:

.. code-block:: yaml

    data_sources:
      vectoratlas:
        source_name: "Vector Atlas Occurrence"
        type: "file"                 # or "folder"
        extension: "csv"
        path: "data/raw/vectoratlas.csv"
        extract_mode: "single"       # single | timeseries | chunked
        extension_options:
          sep: ","
          encoding: "utf-8"
        matching: "mappings/vectoratlas.mapping.csv"

    irmapper_ir_1:
        source_name: "IRMapper 1"
        type: "file"
        extension: "csv"
        path: "data/raw/irmapper.csv"
        columns:                     # the inline alternative to `matching:`
          - source_column: site_code
            target_table: dim_location
            target_column: location_code
            required: true
          - source_column: species
            target_table: dim_vector
            target_column: species_name
            transform: {type: resolve}

``utilities.config.source_spec.load_source_specs`` is the only thing that decides
what an entry may contain, and it enforces three rules:

* **Exactly one of** ``matching:`` and ``columns:``. They are alternatives — a CSV
  of mapping rows versus the same information inline — and "both" has no defined
  precedence worth inventing.
* **Any key matching** ``^(vocab|concept)`` **is rejected anywhere in the
  document**, by dotted path. ``core.vocabulary.gadm_gpkg`` was read for one value
  and ignored for four others; a parse-time error is the only signal a user cannot
  miss. Vocabularies are chosen by the registry, not by configuration.
* **One writer per target column.** Two ``columns:`` entries writing
  ``dim_vector.species_name`` is an error, not a silent last-one-wins.

``core:`` itself is accepted for backward compatibility and read by ``main.py``
only. Relative paths inside a source are **not** resolved by the parser:
``main.get_file_paths`` resolves them against the project root, as it always has.

Stage 2 — Extract
-----------------

``etl.extractors.file_extractor.FileExtractor(extract_mode=..., large_df_threshold=...)``
dispatches on the lower-case extension:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Extension
     - Backend
   * - ``csv``
     - ``pandas.read_csv``; with ``chunksize`` it concatenates the chunks
   * - ``tsv``
     - ``pandas.read_csv(sep="\t")``, chunked
   * - ``xlsx``, ``xls``
     - ``calamine``, falling back to ``openpyxl``. Not chunkable — pandas has no
       ``chunksize`` for Excel, and the argument is accepted for symmetry only
   * - ``json``
     - ``pandas.read_json``; line-delimited by default
   * - ``xml``
     - ``xml.etree.ElementTree.iterparse``, streaming, one row per ``record_tag``
   * - ``nc``, ``nc4``
     - ``xarray``
   * - ``grib``, ``grib2``
     - ``cfgrib`` through ``xarray``
   * - ``cog``, ``tif``, ``tiff``
     - ``rioxarray``; ``tif_to_df_chunked`` yields windowed chunks
   * - ``dta``, ``sas7bdat``, ``sav``
     - ``pyreadstat``, chunked
   * - ``geojson``, ``gpkg``, ``shp``
     - ``pyogrio``; geometry dropped by default

``extract_mode``
~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Mode
     - Behaviour
   * - ``single``
     - Default. For NetCDF/GRIB: one variable, one time step
       (``variable=``, ``time_idx=0``). Lowest memory.
   * - ``timeseries``
     - NetCDF/GRIB only: every time step and every variable, long format, with a
       warning above ``large_df_threshold`` rows. Use
       ``utilities.data_transform_library.timeseries_to_csv`` for anything large.
   * - ``chunked``
     - Raster only. ``extract`` returns a **generator** of DataFrames, one per
       window (``window_size=512``, ``band=1``, values at or below
       ``nodata_threshold`` dropped). ``main`` upserts each chunk before reading
       the next, so memory is bounded by one window.

Anything else for NetCDF/GRIB raises ``ValueError``; only ``chunked`` changes the
return type, and ``main`` treats it explicitly.

Stage 3 — Transform
-------------------

``etl.transformers.mapping_transformer.MappingTransformer`` builds
``{target_table: DataFrame}`` from a mapping CSV or from inline ``column_specs``.
Its constructor takes ``registry=`` and ``run_id=`` and rejects being given both a
CSV and column specs. See :doc:`mapping` for the file format and the lookup rules.

Transforms:

.. list-table::
   :header-rows: 1
   :widths: 16 84

   * - Name
     - Behaviour
   * - ``none``
     - Pass through.
   * - ``map``
     - With ``{"ref": "dim_table.field"}``, a dimension lookup through a
       ``DimensionKeyIndex``; the surrogate primary key is written when the
       dimension has one, otherwise the natural code. Without a ``ref``, a literal
       ``{raw: canonical, "default": ...}`` table — authoritative only for fields
       no pack governs.
   * - ``resolve``
     - Ask the registry which pack governs the target field, resolve the cell, and
       write the accepted ``concept_code`` or ``NULL``. Requires ``registry=``.
   * - ``cast``
     - ``{"type": "int" | "float" | "str"}``. The NULL tokens ``NR``, ``NF``,
       ``ND``, ``-``, ``NA``, ``N/A``, ``NONE``, ``NULL``, ``NAN`` become ``NULL``.
   * - ``date_parse``
     - ``{"format": "%d/%m/%Y"}`` to ``YYYY-MM-DD``; falls back to pandas
       inference, and returns ``NULL`` with a warning on failure.
   * - ``format_date``
     - Build a ``dim_time.date_id`` from one to three columns
       (``year``/``month``/``day``), with ``month_default``, ``day_default`` and
       clamping of an impossible day (Feb 30 → Feb 28/29).
   * - ``split``
     - ``{"delimiter": ",", "index": 0}``.
   * - ``unit_convert``
     - ``value / factor`` when ``divide``, else ``value * factor``, then
       ``+ offset``; plus ``clamp_min``, ``clamp_max``, ``round_to``, ``fraction``
       (strips a trailing ``%`` and divides by 100), ``treat_as_null`` and
       ``default``. A zero factor returns ``default`` instead of raising.
   * - ``concat``
     - ``{"columns": ["a","b"], "separator": " "}``. Read from the row context, not
       from the cell.
   * - ``coalesce``
     - ``{"columns": ["a","b"], "default": null}``: the first non-null. Zero is a
       value; only ``NULL``/NaN/null-tokens are skipped.
   * - ``derive_category``
     - Rule list: ``{"rules": [{"when": [{"col": "x", "op": ">=", "value": 80}],
       "label": "endophagic"}], "default": "unknown"}``. Ops: ``>`` ``>=`` ``<``
       ``<=`` ``==`` ``!=`` ``ratio_gt`` ``ratio_gte`` ``ratio_lt`` ``ratio_lte``.

Two passes, not one
~~~~~~~~~~~~~~~~~~~

``transform`` takes ``target_filter=``. ``main`` calls it twice per chunk — once
for the dimension tables, once for the fact tables — and loads the dimensions in
between. That ordering is what guarantees a fact row's FK target exists before the
fact insert, and it is why a breakpoint dimension key is looked up in an index
seeded after its rows were written.

The mapping is validated once, at construction, not per row: a row that names no
``source_column``, no ``static_value``, no ``default_value`` and no
column-sourced transform would write ``NULL`` for every record and raises
``ValueError`` naming that row.

Stage 4 — Load
--------------

``etl.loaders.db_loader.DBLoader(batch_size=1000, upsert=True, use_copy=False)``.
``main`` uses ``batch_size=10000, upsert=True, use_copy=True``.

Natural keys and sentinels
~~~~~~~~~~~~~~~~~~~~~~~~~

``NATURAL_KEY_CONFIG`` declares, per table, the conflict target (which must match a
real unique constraint or the upsert does not bite), the sentinels substituted for
``NULL`` in nullable key columns, and an optional in-batch key.

Three tables deserve a note:

* ``dim_location`` — the conflict target is ``location_code``, which is *unique and
  nullable*. NULLs are distinct in a unique index, so a row without a code cannot
  participate in the constraint at all. Duplicates within a batch are collapsed on
  ``(name, country_code, admin_level)`` instead, and that is logged: re-ingesting a
  source that omits ``location_code`` would otherwise multiply ``dim_location`` on
  every run.
* ``fact_disease_burden`` — ``pathogen_species`` is nullable and part of the key,
  and cannot take a sentinel without inventing an organism. In-batch duplicates
  collapse on ``(location_id, year, disease_code)``.
* ``fact_insecticide_resistance`` — ``concentration`` takes the sentinel ``-1.0``.

Rows are also collapsed on the conflict target whenever every one of its columns is
present and non-null, so PostgreSQL never sees one statement touching the same
target row twice.

NOT NULL columns
~~~~~~~~~~~~~~~~

``load_table`` makes two passes over the target table's NOT NULL columns:

#. **Fill from the model's Python-side default** when it is a plain scalar. This
   rescues ``fact_disease_case.case_count``, which is NOT NULL with
   ``server_default='1'`` but which a mapping can leave as ``None``.
#. **Drop rows** still NaN in a NOT NULL column that has **no** default, with the
   count logged. A column carrying only a *server* default is not dropped — an
   explicit ``NULL`` in the INSERT column list cannot be rescued by one, so
   dropping would hide a configuration bug.

Autoincrement primary keys are exempt throughout: their absence means "the
database will assign one", not "this row is malformed". Columns present in the
model but absent from the frame are added as ``None``; columns in the frame that
the model does not declare are dropped. Datetimes are converted to dates.

``main.load_facts`` additionally drops rows with a null ``value`` or ``location_id``
for ``fact_environment``, logging the count: ERA5-Land is land-only, so an ocean or
off-mask pixel arrives as NaN and ``fact_environment.value`` is NOT NULL by design.

The two write paths
~~~~~~~~~~~~~~~~~~~

* **PostgreSQL** — for a batch larger than twice ``batch_size`` with
  ``use_copy=True``: ``COPY`` into a ``TEMP`` staging table and merge with
  ``INSERT ... ON CONFLICT DO UPDATE``. Staging table creation, the copy and the
  merge all happen on one connection with no intervening commit (a commit returns
  the connection to the pool, and a ``TEMP`` table is connection-scoped). A cell
  containing the literal ``\N`` is refused before the copy rather than silently
  becoming NULL.
* **SQLite** — batched ``INSERT ... ON CONFLICT DO UPDATE``. Never
  ``INSERT OR REPLACE``: that deletes and re-inserts the row, so an autoincrement
  primary key changes and every reference to it dangles.

Partial-update semantics
~~~~~~~~~~~~~~~~~~~~~~~~

A column participates in ``DO UPDATE SET`` only if the incoming batch has at least
one non-null value for it::

    update_cols = [
        col for col in df.columns
        if col not in pk_cols
        and col not in conflict_targets
        and df[col].notna().any()
    ]

This is what lets several sources share a natural key and each populate a distinct
subset of columns — the six MAP burden sources all write
``fact_disease_burden`` at ``(location_id, year, disease_code, pathogen_species)``
and each writes exactly one of ``cases_mean``, ``incidence_rate`` or
``prevalence_rate``. Before the fix the last source wiped the other two to NULL.

**Consequence.** A source cannot clear an existing column to NULL through the
standard path. That is a known limitation; see :doc:`cdm_contract` §8.

The resolution ledger
~~~~~~~~~~~~~~~~~~~~~

``DBLoader.load_resolutions(resolutions)`` persists one run's decisions, and is the
only writer of both tables:

* ``fact_resolution_log`` — append-only, keyed
  ``(source_table, source_column, source_value, run_id)``, written with
  ``ON CONFLICT DO NOTHING``. **Every** resolution goes here, including
  ``unresolved`` and ``needs_review``: a decision that can be rewritten is not a
  record of what was decided.
* ``fact_concept_map`` — the current state, upserted. Only
  ``decision == 'auto_accepted'`` rows with a ``concept_code`` are projected; a
  resolution claiming ``auto_accepted`` without a code is kept in the ledger only
  and logged as an error, because that combination is not supposed to exist.

The projection's ``DO UPDATE`` refuses to change ``concept_code`` on a row whose
``is_approved = TRUE`` and whose stored code differs. A re-run must not silently
re-decide a row a person approved while leaving ``reviewed_by`` and ``reviewed_at``
in place. ``is_approved``, ``reviewed_by``, ``reviewed_at`` and ``mapped_at`` are
never written by this path.

Stage 5 — Consolidate
---------------------

``utilities.consolidation.DataConsolidator.consolidate_all`` runs the phases below
in this order. The order is the contract: stage A6a canonicalises country codes
*before* any phase groups or filters by country, the vocabulary load precedes
reconciliation because ``dim_concept`` has one writer, and the value rewrites
follow reconciliation because they need ``concept_id``.

#. **FK alignment** — repair orphans by name, then NULL (or quarantine) what is
   left. Legacy: substring matching, no score, no concept-map row. Warns on first
   use.
#. **A6 locations** — ``standardize_country_codes`` (code → name → centroid, ISO
   3166-1 alpha-2 plus its canonical label, idempotent), then
   ``resolve_parent_locations`` when a boundary file is configured (GID equality
   first, point-in-polygon second), then detach dangling parents, merge rows
   sharing a non-empty ``standard_code``, place rows from the GID's own parent
   encoding, and parent grid cells to their nearest administrative unit *within the
   same country*.
#. **VOCAB load** — ``load_shipped_vocabularies``, idempotent by checksum.
#. **A8 reconcile** — ``standardize_vocabularies`` records the registry's packs in
   ``dim_vocabulary`` and resolves every bound field, writing decisions and
   stamping ``concept_id`` / ``standard_code`` on dimensions.
#. **A8b / A8c** — rewrite dimension labels and fact free-text values to the
   canonical concept name.
#. **A9 spatial concepts** — ``attach_admin_concepts`` stamps
   ``dim_location.concept_id`` from the row's ``standard_code``, then the
   ``concept_id``-vs-``standard_code`` consistency check runs.
#. **A9b location names** — ``resolve_location_names`` names each location from its
   coordinates, deepest containing polygon first, then the nearest polygon, the
   parent chain, and the nearest named unit. It never writes ``standard_code``
   (that would let the identity merge collapse distinct sites inside one ward) and
   never touches ``location_code``.
#. **B5 merge** — ``consolidate_vocabulary`` merges every dimension in the field
   catalog, not only the packed ones. A merge is a property of the schema; driving
   the work list from the registry left every unpacked dimension unconsolidated.
#. **Facts re-pointed** — for each merge, fact FKs move to the canonical key,
   aggregating when a plain ``UPDATE`` would violate a natural key.
#. **Remaining orphans** — audited from ``Base.metadata``, never a hand-written
   map (a hand-written map once omitted ``dim_resistance_test_type``, and the merge
   deleted rows a fact table still referenced).
#. **Burden** — ``expand_burden_to_daily`` and ``fix_orphaned_burden_records``.
#. **Resistance** — ``_compute_resistance_status`` with the cut-offs read from the
   ``who-resistance`` pack, then a verification pass that reports any stored status
   contradicting them.
#. **Fact duplicates** — ``resolve_fact_table_duplicates``.
#. **Validation** — country-code coverage and consistency, and the hierarchy check.

Duplicate resolution is guarded
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Both the fact-duplicate pass and the aggregation path only act on groups that
actually exist. On a clean database the log reads ``fact_environment: no
duplicates; untouched`` and no row is modified.

The guard matters because the rebuild is destructive by construction: rows are
deleted and re-inserted, so any column missing from the SELECT list would come back
NULL. Grouping uses ``dropna=False`` — pandas otherwise drops NaN-key groups and
deletes exactly the rows the merge exists to keep.

Running consolidation standalone
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    from utilities.consolidation import run_consolidation

    result = run_consolidation(
        {"countries": ["KEN"], "admin_boundaries": {"path": "data/gadm41_KEN.gpkg"}},
        dry_run=True,          # report only; nothing is written
        registry=registry,     # optional; loaded from vocabularies/registry
        run_id="20261119T120000-ab12cd",
        close=True,            # drains the pending-resolution buffer
    )

``close()`` is the only drain of the resolution buffer: it flushes anything the
run still holds into ``fact_resolution_log`` / ``fact_concept_map`` and then closes
the session. If the flush fails the buffer is *kept*, so a retry is possible.

Smaller entry points are available and safe on their own:
``align_all_foreign_keys(dry_run=True)``, ``consolidate_locations()``,
``resolve_location_names()``, ``summarize_locations()``,
``detect_duplicates()``, ``validate_country_codes()``.

Stage 6 — Report, export and repair scripts
-------------------------------------------

``main.generate_summary()`` (step ``generate_summary``):

.. code-block:: bash

    python main.py --skip-ingest --skip-consolidation
    # outputs/schema_summary.csv
    # outputs/table_samples/<table>_sample.csv   (100 rows each)

Standalone scripts, none of which is part of the ingest pipeline:

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Script
     - Purpose
   * - ``scripts/explore_data.py``
     - Eleven exploratory figures plus ``summary.txt``. Read-only;
       ``--url``, ``--out``, ``--only``.
   * - ``scripts/export_to_csv.py``
     - Streams the CDM to CSV for sharing. ``--single-csv`` (wide union,
       default), ``--per-table`` (zipped), ``--no-dims``, ``--limit``,
       ``--chunk-size``. Peak memory is one chunk.
   * - ``scripts/snapshot_golden.py``
     - Exports every ``dim_*``/``fact_*`` table to canonical CSV: volatile columns
       blanked, surrogate keys masked, rows sorted. The single implementation the
       regression test imports.
   * - ``scripts/fix_resistance_status.py``
     - Recomputes ``mortality`` and ``resistance_status`` by calling
       ``DataConsolidator._compute_resistance_status``. Carries no counting logic
       and no threshold of its own. ``--verify-only`` reports without writing.
   * - ``scripts/fix_cases.py``
     - Thin CLI over ``DataConsolidator.expand_burden_to_daily``.
       ``--force``, ``--seed``, ``--variation``.
   * - ``scripts/generate_er.py``, ``scripts/generate_er_neo.py``
     - ER diagram via ERAlchemy, or a hand-laid-out Graphviz grid reflecting the
       schema.

CLI
---

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Flag
     - Effect
   * - ``--config PATH``
     - YAML config (default ``base_config.yaml``).
   * - ``--source KEY``
     - Run ingestion for one ``data_sources`` key only.
   * - ``--skip-ingest``
     - Skip stage 2–4.
   * - ``--skip-consolidation``
     - Skip stage 5.
   * - ``--skip-summary``
     - Skip stage 6.
   * - ``--expand-burden``
     - Run burden expansion when consolidation was skipped.
   * - ``--dry-run``
     - **Accepted but not currently forwarded.** Consolidation runs for real.
       Use ``run_consolidation(..., dry_run=True)`` from Python, or
       ``--verify-only`` on the resistance script.

A failing source is logged and skipped; the run continues with the next one. A
failing consolidation exits with status 2 and skips the summary, because a summary
of a half-consolidated database reads as a successful run.