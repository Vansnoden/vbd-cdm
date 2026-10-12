Mapping CSV format
==================

A mapping CSV declares how one source DataFrame becomes one or more CDM
tables. It has exactly nine columns:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Column
     - Meaning
   * - ``source_column``
     - Column name in the source DataFrame. Ignored if ``static_value`` is set.
   * - ``source_data_type``
     - Optional. ``int`` / ``float`` / ``str``. Cast happens before the transform.
   * - ``static_value``
     - If set, this literal is used instead of ``source_column``.
   * - ``target_table``
     - CDM table name, e.g. ``fact_vector_observation``.
   * - ``target_column``
     - Column in the target table.
   * - ``transform``
     - See the transform table in :doc:`etl`.
   * - ``transform_params``
     - JSON. Example: ``{"ref": "dim_location.name"}``.
   * - ``required``
     - ``True`` / ``False``. See *Required semantics* below.
   * - ``default_value``
     - Used if the value is null after transformation and ``required`` is false.

Required semantics
------------------

``required = True`` means: *abort the source if any row produces a null
value for this column.* It is evaluated **after** the transform runs, on
the value that would be inserted. It does **not** mean "every input row must
have a non-null source cell".

Two consequences follow:

1. A column that may legitimately be null for *some* rows in a source (for
   example ``fact_environment.value`` when ``maskregion`` has set 29 of 121
   cells to missing) must set ``required = False``. The loader already drops
   null-valued rows for that table; ``required = True`` would abort the
   whole source on the first missing pixel.

2. A column that is null for *every* row in a source will never trigger the
   ``required`` check — the check fires on the first null, and if all values
   are null the first row already fails.

The appropriate use of ``required = True`` is for columns where a null
indicates a genuine data error (a missing FK lookup that should have
succeeded, a malformed date that should have parsed, and so on). Use it
sparingly.

Partial-update interaction
--------------------------

When a mapping targets a table that another source also writes to, be aware
that the loader's upsert is a partial update: only columns with at least one
non-null value in the incoming batch participate in the ``DO UPDATE SET``
clause. This is what allows six MAP burden sources to share a natural key
and each populate a distinct value column.

If your mapping's ``required = False`` and the source is entirely null in
some column, that column will simply not appear in the update, and any
existing value at the natural key will be preserved. See
:doc:`cdm_contract` §7 for the full rule.

Dimension FK lookups
--------------------

The ``map`` transform with a ``"ref"`` parameter triggers a lookup against a
dimension table. The ``ref`` string has the form ``<table>.<column>``:

.. code-block:: text

    target_table,target_column,source_column,transform,transform_params,required
    fact_vector_observation,location_id,site_name,map,"{""ref"": ""dim_location.name""}",true
    fact_vector_observation,vector_code,species,map,"{""ref"": ""dim_vector.species_name""}",true

Lookup semantics
~~~~~~~~~~~~~~~~

1. The value from ``source_column`` is matched against ``<dim_table>.<field>``.
2. On match, the dimension's primary key is returned.
3. On miss, ``None`` is returned and a warning is logged.
4. Lookups are cached per transform run (``_dimension_cache``) - repeated
   values cost one DB round-trip.

Composite lookups
~~~~~~~~~~~~~~~~~

If the source has no natural single-column match, use ``"columns"``:

.. code-block:: text

    fact_vector_observation,location_id,,map,"{""ref"": ""dim_location.name"", ""columns"": [""country"", ""site""], ""separator"": ""/""}",true

This builds ``"<country>/<site>"`` and looks for a ``dim_location`` whose
``name`` equals that string. **Note:** this requires the ETL to have written
``dim_location.name`` in the same composite form. If it hasn't, the lookup
will silently miss.

Multi-table mappings
--------------------

Because the same source DataFrame is used to populate every row in the
mapping, a single CSV can declare facts and dimensions in one pass:

.. code-block:: text

    target_table,target_column,source_column,transform,transform_params,required
    dim_vector,vector_code,species_code,cast,"{""type"": ""str""}",true
    dim_vector,species_name,species_name,none,,true
    fact_vector_observation,vector_code,species_code,map,"{""ref"": ""dim_vector.vector_code""}",true
    fact_vector_observation,count,count,cast,"{""type"": ""int""}",false

``main.run_ingestion_for_source`` always transforms *dimensions first*,
loads them, then transforms *facts*. This guarantees FK targets exist before
fact inserts.

Debugging a mapping
-------------------

The transformer logs every miss. To see them:

.. code-block:: bash

    python main.py --source anopheles_kenya 2>&1 | grep "No match found"