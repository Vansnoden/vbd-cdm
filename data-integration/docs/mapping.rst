## `mapping.rst`

```rst
Mapping CSV format
==================

A mapping declares how one source DataFrame becomes one or more CDM tables. It has
exactly nine columns:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Column
     - Meaning
   * - ``source_column``
     - Column name in the source DataFrame. Ignored when ``static_value`` is set.
   * - ``source_data_type``
     - Optional ``int`` / ``float`` / ``str``. The cast happens **before** the
       transform runs.
   * - ``static_value``
     - If set, this literal is used instead of ``source_column``, for every row.
   * - ``target_table``
     - CDM table name, e.g. ``fact_vector_observation``.
   * - ``target_column``
     - Column in the target table.
   * - ``transform``
     - Transform name. See :ref:`mapping-transforms`.
   * - ``transform_params``
     - JSON object. ``{"ref": "dim_location.location_code"}``,
       ``{"type": "int"}``, ``{"columns": ["a","b"]}``, …
   * - ``required``
     - ``True`` / ``False``. See :ref:`mapping-required`.
   * - ``default_value``
     - Used when the value is null after transformation and ``required`` is false.

Rows are grouped by ``target_table``, so one file may describe dimensions and facts
together. ``etl.transformers.mapping_transformer.MappingTransformer`` builds one
``DataFrame`` per target table it finds and returns ``{table: DataFrame}``.

.. contents:: On this page
   :local:
   :depth: 1

How the file is read
--------------------

``MappingTransformer`` is constructed in exactly one of two ways, never both::

    MappingTransformer("mappings/vectoratlas.mapping.csv", registry=registry, run_id=run_id)
    MappingTransformer(column_specs=source_cfg["columns"], registry=registry, run_id=run_id)

Passing neither, or both, raises ``ValueError`` at construction: they describe the
same table and there is no defined precedence between them.

The frame is then normalised once, before anything reads it:

* **Blank strings become NaN** in ``source_column``, ``transform_params``,
  ``static_value``, ``default_value`` and ``source_data_type``. This is load-bearing
  twice over: ``pd.notna("")`` is ``True``, so an empty ``static_value`` cell would
  otherwise be returned as a literal value for every row, and an empty
  ``source_column`` would be looked up as a real column name.
* **``required`` becomes a real ``bool``.** ``true``, ``t``, ``1`` and ``yes``
  (any case) are true; everything else, including a blank cell, is false.
  ``bool("False")`` is ``True``, so a string column here would silently make every
  optional column required.

Validation happens now, not on row 1
------------------------------------

Four checks run at construction, each naming the offending row:

#. A missing mapping column (``source_column``, ``target_table``,
   ``target_column``, ``transform``, ``required``).
#. An unknown transform name.
#. An inline ``ColumnSpec`` whose ``transform_name`` is not one the transformer
   dispatches on.
#. **A row that would write ``NULL`` for every record** — one that names no
   ``source_column``, no ``static_value``, no ``default_value``, and uses a
   transform that reads no column. The exceptions are ``concat``, ``coalesce``,
   ``format_date``, ``derive_category`` and ``resolve``, which build their value
   from ``transform_params["columns"]`` or from the row context.

Scattered warnings you will see for the last case:

* a ``map`` row with a ``ref`` but no ``columns`` — there is no value to look up;
* an unparseable ``transform_params`` JSON — the transform then runs with ``{}``.

``default_value`` counts as a value source deliberately: a column with a fixed
fallback and no source column is a legitimate constant (GBIF's
``admin_level: occurrence``), not an empty mapping.

.. _mapping-required:

Required semantics
------------------

``required = True`` is enforced twice, and both are write-time rather than
read-time:

#. the named ``source_column`` is absent from the source frame →
   ``ValueError: Required source column '<name>' missing in data``;
#. the value is still null **after** the transform →
   ``ValueError: Required field '<column>' in table '<table>' has null value``.

So it means *this column must hold a value for every row that reaches the loader*,
not "every input row must have a non-null source cell". ``main`` catches the error
per source, logs it with a traceback, and continues with the next source.

Two consequences follow:

1. A column that may legitimately be null for *some* rows must set
   ``required = False``. ``fact_environment.value`` is the worked case: the
   environment ingest blanks 29 of 121 cells when ``maskregion`` clips to the study
   area, and ``main.load_facts`` drops the null rows for that table with the count
   logged. ``required = True`` would fail the whole source on the first missing
   pixel.
2. Use it where a null is a genuine data error: an FK lookup that should have
   succeeded, a date that should have parsed.

.. _mapping-transforms:

Transform reference
-------------------

``transform`` is matched case-insensitively after stripping. An unrecognised name
logs ``Unknown transform type`` and passes the value through unchanged.

.. list-table::
   :header-rows: 1
   :widths: 16 20 64

   * - Name
     - Params
     - Behaviour
   * - ``none``
     - —
     - Pass through. This is the default when ``transform`` is blank.
   * - ``map``
     - ``ref``, ``columns``, ``separator``; or a literal table plus ``default``
     - A dimension FK lookup, or a literal ``{raw: canonical}`` table. See
       :ref:`mapping-lookups`.
   * - ``resolve``
     - ``ref`` or ``pack``
     - Ask the registry which pack governs the field, resolve the cell through the
       concept ladder, and write the accepted concept code or ``NULL``. See
       :ref:`mapping-resolve`.
   * - ``cast``
     - ``type``: ``int`` / ``float`` / ``str``
     - Numeric strings are accepted; ``int`` goes via ``float`` so ``"12.0"``
       becomes ``12``. The null tokens ``NR``, ``NF``, ``ND``, ``-``, ``NA``,
       ``N/A``, ``NONE``, ``NULL``, ``NAN`` (any case) become ``NULL``. A value that
       will not cast logs a warning and becomes ``NULL``.
   * - ``date_parse``
     - ``format`` (default ``%Y-%m-%d``)
     - Returns a ``YYYY-MM-DD`` string. Tries the declared format first, then pandas
       inference. A ``Timestamp`` or ``datetime`` is formatted directly. An
       unparseable cell logs and returns ``NULL`` — one bad cell does not stop the
       table.
   * - ``format_date``
     - ``columns`` (1–3 names), ``month_default`` (1), ``day_default`` (1),
       ``min_year`` (1800), ``max_year`` (2200), ``clamp_invalid_day`` (true),
       ``default``
     - Builds a full ``dim_time.date_id`` from ``year``, ``year+month`` or
       ``year+month+day``. The missing components come from the defaults, and an
       impossible day is clamped to the last valid day of its month (Feb 30 →
       Feb 28/29) unless ``clamp_invalid_day`` is false. An unusable year returns
       ``default``.
   * - ``split``
     - ``delimiter`` (``,``), ``index`` (0)
     - Takes one field from a delimited string. Negative indexes count from the end.
       An out-of-range index logs and returns ``NULL``.
   * - ``unit_convert``
     - ``factor`` (1.0), ``offset`` (0.0), ``divide`` (false),
       ``fraction`` (false), ``clamp_min``, ``clamp_max``, ``round_to``,
       ``default``, ``treat_as_null``
     - ``value / factor`` when ``divide``, else ``value * factor``; then
       ``+ offset``; then clamp; then round. A zero ``factor`` with ``divide``
       returns ``default`` and logs, instead of raising. ``fraction: true`` strips a
       trailing ``%`` and divides by 100. Thousands separators are removed
       (``"1,234"`` → ``1234``). A NaN result returns ``default``.
   * - ``concat``
     - ``columns``, ``separator`` (a single space)
     - Joins the named **row-context** columns in order, skipping nulls and warning
       per missing column. If nothing survives, ``default_value`` is used.
   * - ``coalesce``
     - ``columns``, ``default``, ``treat_as_null``
     - The first non-null among the named columns. Zero **is** a value; only
       ``NULL``, NaN/NaT and the null tokens are skipped. For a source that stores
       one quantity in two mutually exclusive columns (IRMapper's ``Concentration
       (%)`` for WHO tube versus ``Concentration (µg)`` for CDC bottle).
   * - ``derive_category``
     - ``rules``, ``default``, ``treat_as_null``
     - Rule-based label. Rules are evaluated top to bottom and the first whose
       conditions **all** hold wins. See below.

``derive_category`` rule shape
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    {"rules": [
       {"when": [{"col": "indoor_host_percent", "op": ">=", "value": 80}],
        "label": "endophagic"},
       {"when": [{"cols": ["indoor_total", "outdoor_total"],
                  "op": "ratio_gte", "value": 0.8}],
        "label": "endophagic"}
     ],
     "default": "unknown",
     "treat_as_null": ["NR", "-"]}

Scalar ``ops``: ``>``, ``>=``, ``<``, ``<=``, ``==``, ``!=``.
Ratio ``ops`` (require ``cols`` of exactly two names, and compute ``a / (a + b)``):
``ratio_gt``, ``ratio_gte``, ``ratio_lt``, ``ratio_lte``.

A condition whose column is missing, null or unparseable is **false**, so the rule
does not fire and evaluation continues. ``treat_as_null`` adds tokens beyond the
built-in ``""``, ``NA``, ``N/A``, ``NONE``, ``NULL``, ``NAN``, ``NR``, ``-``.

.. _mapping-lookups:

Dimension FK lookups
--------------------

``map`` with a ``ref`` of the form ``<table>.<column>`` resolves a foreign key:

.. code-block:: text

    target_table,target_column,source_column,transform,transform_params,required,static_value,default_value,source_data_type
    fact_vector_observation,location_id,site_code,map,"{""ref"": ""dim_location.location_code""}",true,,,
    fact_vector_observation,vector_code,species,map,"{""ref"": ""dim_vector.vector_code""}",true,,,

How the lookup works
~~~~~~~~~~~~~~~~~~~~

The lookup goes through ``etl.transformers.dimension_index.DimensionKeyIndex``,
one index per ``(table, field)`` pair for the whole pass:

#. **Seeded once**, with a single ``SELECT ... ORDER BY <code_field>`` over the
   dimension table. The code field is the table's natural key, read from
   ``DBLoader.NATURAL_KEY_CONFIG`` — the same column the upsert conflicts on, so the
   index and the loader cannot disagree about what identifies a row.
#. The ref's own column is bound as an **alias** for the same row, which is what lets
   ``dim_location.location_code`` and ``dim_location.name`` both resolve to a single
   entry without a second query.
#. The value is matched through the index's key function: trim, collapse whitespace,
   case-fold. ``"Siaya"``, ``"SIAYA"`` and ``" Siaya "`` are one value. This replaces
   the per-value ``SELECT ... WHERE field = value`` the old path issued against an
   unindexed column, which was exact ``==`` only and therefore missed a two-space
   difference.
#. On a hit the index returns the **surrogate primary key** when the dimension has
   one (``dim_location.location_id``) and the **natural code** when the code *is*
   the key (``dim_vector.vector_code``, which is also what a fact FK holds there).
#. The entry also carries the concept (``concept_code``, ``concept_id``,
   ``standard_code``), so a caller never reads the FK from one place and the concept
   from another.

On a miss the transform returns ``None`` and logs
``No match found for <table>.<field> = <value>``.

If no index can be built at all — the table name is unknown, it has no declared
natural key, or an identifier fails ``require_identifier`` — the failure is cached
and logged **once**, and every ref into that table resolves to ``NULL`` for the rest
of the pass, rather than once per row.

Ref on the code, not the label
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``dim_location.location_code`` is a row's own identity: the ingest mappings write the
source's site code, and the raster and site mappings write ``"<name>/<lat>/<lon>"``.
Ref on ``location_code`` when the source has a stable key, and on ``name`` only when
it does not. A name is a *label*: consolidation may legitimately give two distinct
sites the same name (same-name groups are reported, never merged), and boundary
labelling may rewrite a name from the polygon a row sits in. Identity is the code;
boundary membership is ``standard_code``.

Composite lookups
~~~~~~~~~~~~~~~~~

When the source has no single-column match, build one from several:

.. code-block:: text

    target_table,target_column,source_column,transform,transform_params,required,static_value,default_value,source_data_type
    fact_vector_observation,location_id,,map,"{""ref"": ""dim_location.name"", ""columns"": [""country"", ""site""], ""separator"": ""/""}",true,,,

``_build_lookup_value`` joins the named row-context columns with ``separator``
(default ``/``), skipping nulls. This only matches if the ETL wrote
``dim_location.name`` in the same composite form; if it did not, the lookup misses
and says so.

Literal tables
~~~~~~~~~~~~~~

``map`` without a ``ref`` is a plain lookup with an optional default. Every key
other than ``default`` is part of the table; the cell is compared as ``str(value)``:

.. code-block:: text

    target_table,target_column,source_column,transform,transform_params,required,static_value,default_value,source_data_type
    fact_vector_observation,sex,sex,map,"{""F"": ""F"", ""M"": ""M"", ""default"": ""unknown""}",false,,,

This is retained **only** for fields no pack governs — a closed, source-declared
encoding such as ``sex``. Where a pack *does* govern the target field, the literal
table is a second, unversioned source of truth for a decision the registry owns.
``_governing_pack`` detects that and warns, naming the pack and the field, and the
literal is applied anyway: silently resolving instead would change the run's output
based on config the user cannot see. Declare the mapping in the pack's vocabulary,
or use ``resolve``.

.. _mapping-resolve:

The ``resolve`` transform
-------------------------

``resolve`` is the mapping-level entry point to the concept ladder:

.. code-block:: text

    target_table,target_column,source_column,transform,transform_params,required,static_value,default_value,source_data_type
    dim_vector,species_name,species,resolve,"{""ref"": ""dim_vector.species_name""}",false,,,
    dim_bionomics,resting_behavior,rest,,resolve,false,,,

Rules:

* the target field is ``transform_params["ref"]`` when present, otherwise
  ``<target_table>.<target_column>``, and must be a
  ``utilities.vocabulary.field_catalog`` field;
* ``transform_params["pack"]`` forces a specific ``pack_id``; otherwise
  ``registry.select_pack(field)`` picks the pack that governs it;
* the accepted ``concept_code`` is written, and ``NULL`` otherwise — a withheld
  decision (``needs_review``, ``ambiguous``, ``unresolved``) writes nothing, because
  the decision is what a review queue reads, not this column;
* ``registry=`` is required. Without one the transform raises rather than guessing,
  because guessing is exactly the policy-as-literal the registry removes.

``resolve`` runs **per cell**, not per series. ``ConceptResolver`` memoises on
``(field, value)``, so a repeated value is a dictionary hit and a chunked source pays
the resolution cost once per *distinct* value.

.. note::

   A ``resolve`` transform records each ``Resolution`` on the transformer
   (``pending_resolutions()``, sorted and de-duplicated by the source triple). Those
   are not written to the database by the transform itself: persist them with
   ``DBLoader.load_resolutions()``, and drain the buffer before ``close()`` — the
   buffer deliberately survives ``close()``, but the resolver's session does not.

   The orchestrator's own mapping passes do **not** persist them: each pass builds
   its own transformer and closes it, and the authoritative ledger write happens at
   consolidation stage A8, which reconciles every bound field column by column.

   So ``resolve`` is for a value you want standardised *at ingest time* — giving
   ``dim_vector.species_name`` its canonical label before the merge pass groups rows,
   say — not for producing the audit trail.

Multi-table mappings
--------------------

One file can declare dimensions and facts in a single pass, because the same source
DataFrame feeds every mapping row:

.. code-block:: text

    target_table,target_column,source_column,transform,transform_params,required,static_value,default_value,source_data_type
    dim_source,source_code,,none,,true,VECTORATLAS,,
    dim_source,source_name,,none,,true,Vector Atlas Occurrence,,
    dim_vector,vector_code,species,none,,true,,,
    dim_vector,species_name,species,none,,true,,,
    dim_location,location_code,site_code,none,,true,,,
    dim_location,name,site_name,none,,true,,,
    fact_vector_observation,location_id,site_code,map,"{""ref"": ""dim_location.location_code""}",true,,,
    fact_vector_observation,vector_code,species,map,"{""ref"": ""dim_vector.vector_code""}",true,,,
    fact_vector_observation,date_id,event_date,date_parse,"{""format"": ""%Y-%m-%d""}",true,,,
    fact_vector_observation,source_code,,none,,true,VECTORATLAS,,
    fact_vector_observation,count,count,cast,"{""type"": ""int""}",false,,,

``main.run_ingestion_for_source`` calls ``transform`` **twice per chunk**, with
different filters, and loads the dimensions in between:

.. code-block:: text

    chunk ──▶ transform(target_filter=DIMENSION_TABLES) ──▶ load_dimensions()
          ──▶ transform(target_filter=FACT_TABLES)      ──▶ load_facts()

That ordering is what guarantees an FK target exists before the fact insert. It is
also why each pass builds its own transformer: the fact pass's dimension indexes are
seeded *after* the dimension rows were written, so a chunk cannot look up a key that
does not exist yet.

The two table lists, in dependency order:

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Dimensions
     - Facts
   * - ``dim_source``
     - ``fact_vector_observation``
   * - ``dim_vector``
     - ``fact_disease_case``
   * - ``dim_location``
     - ``fact_insecticide_resistance``
   * - ``dim_disease``
     - ``fact_bionomics``
   * - ``dim_insecticide``
     - ``fact_intervention``
   * - ``dim_identification_method``
     - ``fact_socioeconomic_condition``
   * - ``dim_resistance_test_type``
     - ``fact_health_facility``
   * - ``dim_socioeconomic_indicator``
     - ``fact_disease_burden``
   * -
     - ``fact_environment``

A target table with no mapping rows produces no DataFrame and is simply absent from
the result. A frame that is entirely null is dropped (``dropna(how="all")``), so an
optional block of the mapping costs nothing when the source does not populate it.

Performance note
~~~~~~~~~~~~~~~~

The ``(table, field)`` index cache lives on the transformer instance, and one
transformer is built per pass per chunk. A chunked raster source therefore re-seeds
each dimension index once **per chunk**, not once per run. That is one ``SELECT ...
ORDER BY`` per dimension per chunk — cheap next to the fact batch it precedes, but
it is why the log shows the same "seeded dim_location" line repeatedly on a chunked
source.

Inline ``columns:`` instead of a CSV
-----------------------------------

A source may declare its projection inline in ``base_config.yaml`` and skip the
mapping file entirely:

.. code-block:: yaml

    data_sources:
      vectoratlas:
        source_name: "Vector Atlas Occurrence"
        type: "file"
        extension: "csv"
        path: "data/raw/vectoratlas.csv"
        extension_options: {sep: ",", encoding: "utf-8"}
        columns:
          - source_column: site_code
            target_table: dim_location
            target_column: location_code
            required: true

          - source_column: site_name
            target_table: dim_location
            target_column: name
            admin_level: site          # describes the source; never written
            domain_hint: location

          - source_column: latitude
            target_table: dim_location
            target_column: centroid_lat
            transform: {type: cast, params: {type: float}}

          - source_column: species
            target_table: dim_vector
            target_column: species_name
            transform: {type: resolve, params: {pack: ncbi}}
            domain_hint: vector

          - target_table: dim_source
            target_column: source_name
            static_value: "Vector Atlas Occurrence"

``utilities.config.source_spec.ColumnSpec`` accepts the nine mapping columns plus two
that describe the **source**:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Field
     - Meaning
   * - ``transform``
     - A bare name, or the structured ``{type: ..., params: {...}}`` form.
   * - ``transform_params``
     - A mapping (inline YAML) or JSON text (CSV shape). Supplying this *and*
       ``transform.params`` is an error.
   * - ``admin_level``
     - The source's own level label. Read by the concept pass; not written.
   * - ``domain_hint``
     - What the source column **means**, so a column that could feed two CDM
       destinations is unambiguous. One of ``location``, ``vector``, ``disease``,
       ``insecticide``, ``diagnostic_method``, ``collection_method``,
       ``resistance_test_type``, ``socioeconomic_indicator``. It never names a
       vocabulary: the domain is the *input* to ``select_pack``.

Three rules are enforced by the spec, not by the transformer:

* ``ColumnSpec`` is ``extra="forbid"`` — an unknown key is an error, because a
  silently ignored key is the defect the spec exists to remove.
* ``SourceSpec`` rejects a source declaring **both** ``matching:`` and ``columns:``,
  and a source declaring **neither**.
* Two ``columns:`` entries writing one ``target_table.target_column`` is an error.
  One target column takes one source column.

``ColumnSpec.mapping_row()`` emits the same nine-column row a CSV would produce —
with ``required`` as a real ``bool``, absent values as ``None`` rather than ``""``,
and params as JSON text — so both paths reach the identical DataFrame. That is what
makes the inline form additive rather than a second implementation.

Deprecated lookup
-----------------

``MappingTransformer._legacy_lookup_dimension_id`` (formerly
``_lookup_dimension_id``) survives for one release, for an out-of-tree caller.
Nothing in this repository reaches it. It emits a ``DeprecationWarning`` on every
call and is the fourth resolution mechanism the codebase used to have: exact ``==``
only, no case folding, no vocabulary, one query per distinct value against an
unindexed column.

Use ``_index_for()`` plus ``DimensionKeyIndex.lookup_raw()`` instead.

Debugging a mapping
-------------------

Every miss, every dropped row and every refusal is logged. The lines worth knowing:

.. code-block:: bash

    # a dimension lookup that found nothing
    python main.py --source vectoratlas 2>&1 | grep "No match found"

    # a ref into a table no index could be built for (logged once per pass)
    python main.py --source vectoratlas 2>&1 | grep "Cannot build a dimension index"

    # a required value that was null after transformation
    python main.py --source vectoratlas 2>&1 | grep "has null value"

    # a required source column missing from the frame entirely
    python main.py --source vectoratlas 2>&1 | grep "Required source column"

    # in-batch duplicates collapsed before the upsert
    python main.py --source vectoratlas 2>&1 | grep "Collapsed"

    # a literal {raw: code} table applied to a field a pack governs
    python main.py --source vectoratlas 2>&1 | grep "non-authoritative"

    # a resolve transform whose target field is not <table>.<column>
    python main.py --source vectoratlas 2>&1 | grep "is not '<table>.<column>'"

    # which tables a pass actually built, and how many rows
    python main.py --source vectoratlas 2>&1 | grep "Built"

An empty target table is the one silent outcome: no mapping row named it, or every
row was dropped. Check the mapping's ``target_table`` values against the list above
before assuming the source is at fault.
```