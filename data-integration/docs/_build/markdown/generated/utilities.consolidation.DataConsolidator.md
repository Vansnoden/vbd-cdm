# utilities.consolidation.DataConsolidator

### *class* utilities.consolidation.DataConsolidator(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Consolidates and standardizes data in the CDM with full foreign key alignment.

#### \_\_init_\_(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

### Methods

| [`__init__`](#id0)([session])                                                                                      |                                                                                                |
|--------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| [`align_all_foreign_keys`](#utilities.consolidation.DataConsolidator.align_all_foreign_keys)([dry_run])            | Align all foreign keys across all tables.                                                      |
| [`close`](#utilities.consolidation.DataConsolidator.close)()                                                       |                                                                                                |
| [`consolidate_all`](#utilities.consolidation.DataConsolidator.consolidate_all)([study_region])                     | Run all consolidation steps with full foreign key alignment.                                   |
| [`consolidate_locations`](#utilities.consolidation.DataConsolidator.consolidate_locations)([...])                  | Consolidate locations with country code standardization.                                       |
| [`consolidate_vocabulary`](#utilities.consolidation.DataConsolidator.consolidate_vocabulary)()                     | Consolidate vocabulary tables.                                                                 |
| [`detect_duplicates`](#utilities.consolidation.DataConsolidator.detect_duplicates)()                               | Detect duplicate records in fact tables.                                                       |
| [`fix_location_country_codes`](#utilities.consolidation.DataConsolidator.fix_location_country_codes)()             | Fix missing or incorrect country codes.                                                        |
| [`fix_orphaned_burden_records`](#utilities.consolidation.DataConsolidator.fix_orphaned_burden_records)()           | Fix fact_disease_burden rows whose location_id does not resolve to a valid dim_location entry. |
| [`resolve_fact_table_duplicates`](#utilities.consolidation.DataConsolidator.resolve_fact_table_duplicates)()       | Resolve duplicates across fact tables.                                                         |
| [`standardize_vocabularies`](#utilities.consolidation.DataConsolidator.standardize_vocabularies)([vocab_dir, ...]) | Standardize dimension values against external vocabularies.                                    |
| [`summarize_locations`](#utilities.consolidation.DataConsolidator.summarize_locations)()                           | Generate a summary of locations.                                                               |
| [`validate_country_codes`](#utilities.consolidation.DataConsolidator.validate_country_codes)()                     | Validate country codes.                                                                        |

#### \_\_init_\_(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

#### align_all_foreign_keys(dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Align all foreign keys across all tables. This is the master method that ensures
every fact table references valid dimension records.

* **Returns:**
  Dictionary with alignment results for each table

#### fix_orphaned_burden_records() → [int](https://docs.python.org/3/builtins/functions.html#int)

Fix fact_disease_burden rows whose location_id does not resolve to a
valid dim_location entry.

Repair strategy per row:
1. If location_id is a string, try to name-match against dim_location.name.
2. Otherwise fall back to the most common location_id in fact_disease_case.

#### resolve_fact_table_duplicates()

Resolve duplicates across fact tables.

#### consolidate_all(study_region: [str](https://docs.python.org/3/builtins/stdtypes.html#str) = 'Kenya') → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Run all consolidation steps with full foreign key alignment.

#### consolidate_locations(distance_threshold_km: [float](https://docs.python.org/3/builtins/functions.html#float) = 5.0, min_samples: [int](https://docs.python.org/3/builtins/functions.html#int) = 1, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False)

Consolidate locations with country code standardization.

Grid cells (admin_level=’grid’) are preserved as-is: they are uniquely
identified by location_code and their shared name is a label, not an
identity. All other locations are eligible for name-merge and
cluster-merge.

#### fix_location_country_codes() → [int](https://docs.python.org/3/builtins/functions.html#int)

Fix missing or incorrect country codes.

#### validate_country_codes() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Validate country codes.

#### consolidate_vocabulary() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Consolidate vocabulary tables.

#### detect_duplicates() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Detect duplicate records in fact tables.

#### summarize_locations() → DataFrame

Generate a summary of locations.

#### close()

#### standardize_vocabularies(vocab_dir: [str](https://docs.python.org/3/builtins/stdtypes.html#str) = 'vocabularies', fuzzy_threshold: [float](https://docs.python.org/3/builtins/functions.html#float) = 0.85, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Standardize dimension values against external vocabularies.

Steps per (table, column, domain):
: 1. Load the vocabulary concepts into dim_vocabulary + dim_concept.
  2. For each distinct raw value in the source column, attempt a match:
     : 1. exact match on name_normalized
       2. normalized match (strip diacritics, expand abbreviations)
       3. fuzzy match (token_set_ratio >= fuzzy_threshold)
  3. Record the mapping in fact_concept_map.
  4. Update the dimension’s standard_code / concept_id if match is confident.
