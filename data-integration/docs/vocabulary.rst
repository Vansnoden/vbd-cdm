Vocabulary standardisation
==========================

Three external vocabularies are supported, each mapping to a CDM domain:

.. list-table::
   :header-rows: 1
   :widths: 15 15 70

   * - Vocabulary
     - Domain
     - Source
   * - GADM
     - ``location``
     - Administrative boundaries (levels 0-3). Loaded from a ``.gpkg``.
   * - IDOMAL
     - ``disease``
     - Malaria ontology, OBO format. Provides term IDs, definitions, synonyms.
   * - NCBI Taxonomy
     - ``vector``
     - Subtree of each genus in ``VECTOR_TAXA``.

How it works
------------

1. ``utilities.vocabulary.build`` downloads / reads the source and writes a
   CSV with columns ``concept_code, concept_name, concept_class, parent_code,
   synonyms, vocabulary_id, domain, name_normalized``.
2. ``VocabularyLoader.load_csv`` upserts those concepts into
   ``dim_vocabulary`` and ``dim_concept``.
3. ``DataConsolidator.standardize_vocabularies`` walks each dimension table
   and matches every distinct value:

   * **exact** - string equality (case-insensitive, whitespace-collapsed).
   * **normalized** - after stripping diacritics and collapsing whitespace.
   * **fuzzy** - RapidFuzz ``token_set_ratio`` ≥ 0.85.
4. Matches are recorded in ``fact_concept_map`` with a confidence score and
   an ``is_approved`` flag (``True`` for exact / normalized, ``False`` for
   fuzzy, awaiting human review).
5. Confident matches are stamped onto the dimension row's ``standard_code``
   and ``concept_id``.

Building the vocabularies
-------------------------

.. code-block:: bash

    python -m utilities.vocabulary.build \
        --gadm   /path/to/gadm41_KEN.gpkg \
        --idomal /path/to/idomal.obo \
        --out    vocabularies/

    # Then, in the consolidation step:
    python -c "
    from utilities.consolidation import DataConsolidator
    c = DataConsolidator()
    c.standardize_vocabularies(vocab_dir='vocabularies')
    c.close()
    "

Reviewing fuzzy matches
-----------------------

.. code-block:: sql

    SELECT source_table, source_column, source_value,
           concept_code, match_score
    FROM fact_concept_map
    WHERE is_approved = FALSE
    ORDER BY match_score DESC;

To approve a match manually:

.. code-block:: sql

    UPDATE fact_concept_map SET is_approved = TRUE
    WHERE source_table = 'dim_location'
      AND source_column = 'name'
      AND source_value = 'Nairobi';

To reject, delete the row. The next consolidation run will re-attempt.