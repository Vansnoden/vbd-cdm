# VBD CDM - Data Integration

# Foundations

* [The VBD CDM contract](cdm_contract.md)
  * [1. Purpose](cdm_contract.md#purpose)
  * [2. Storage](cdm_contract.md#storage)
  * [3. Grain](cdm_contract.md#grain)
  * [4. Nullable natural-key columns](cdm_contract.md#nullable-natural-key-columns)
  * [5. Reserved identifiers](cdm_contract.md#reserved-identifiers)
  * [6. Value domains](cdm_contract.md#value-domains)
  * [7. Upsert semantics — the loader is a *partial* update](cdm_contract.md#upsert-semantics-the-loader-is-a-partial-update)
  * [8. What the contract does *not* guarantee](cdm_contract.md#what-the-contract-does-not-guarantee)
  * [9. Change procedure](cdm_contract.md#change-procedure)
* [Data model](data_model.md)
  * [Entity-relationship diagram](data_model.md#entity-relationship-diagram)
  * [Layout](data_model.md#layout)
  * [Full schema](data_model.md#full-schema)
* [Supported data types](data_types.md)

# Pipeline

* [ETL pipeline](etl.md)
  * [Stage 1 - Config](etl.md#stage-1-config)
  * [Stage 2 - Extract](etl.md#stage-2-extract)
  * [Stage 3 - Transform](etl.md#stage-3-transform)
  * [Stage 4 - Load](etl.md#stage-4-load)
  * [Stage 5 - Consolidate](etl.md#stage-5-consolidate)
  * [Stage 6 - Export (optional)](etl.md#stage-6-export-optional)
* [Mapping CSV format](mapping.md)
  * [Required semantics](mapping.md#required-semantics)
  * [Partial-update interaction](mapping.md#partial-update-interaction)
  * [Dimension FK lookups](mapping.md#dimension-fk-lookups)
  * [Multi-table mappings](mapping.md#multi-table-mappings)
  * [Debugging a mapping](mapping.md#debugging-a-mapping)
* [Vocabulary standardisation](vocabulary.md)
  * [How it works](vocabulary.md#how-it-works)
  * [Building the vocabularies](vocabulary.md#building-the-vocabularies)
  * [Reviewing fuzzy matches](vocabulary.md#reviewing-fuzzy-matches)

# Reference

* [Utilities reference](utilities.md)
  * [`utilities.consolidation`](utilities.md#utilities-consolidation)
  * [`utilities.data_transform_library`](utilities.md#utilities-data-transform-library)
  * [`utilities.data_filter_library`](utilities.md#utilities-data-filter-library)
  * [`utilities.database_summary`](utilities.md#utilities-database-summary)
  * [`export_to_csv.py`](utilities.md#export-to-csv-py)

## Indices and tables

* [Index](genindex.md)
* [Module Index](py-modindex.md)
* [Search Page](search.md)
