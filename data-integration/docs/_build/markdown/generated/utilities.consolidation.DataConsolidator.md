# utilities.consolidation.DataConsolidator

### *class* utilities.consolidation.DataConsolidator(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Consolidates and standardizes data in the CDM.

#### \_\_init_\_(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

### Methods

| [`__init__`](#id0)([session])                                                                                      |                                                                                                   |
|--------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| [`align_all_foreign_keys`](#utilities.consolidation.DataConsolidator.align_all_foreign_keys)([dry_run])            |                                                                                                   |
| [`close`](#utilities.consolidation.DataConsolidator.close)()                                                       |                                                                                                   |
| [`consolidate_all`](#utilities.consolidation.DataConsolidator.consolidate_all)([study_region])                     | Run all consolidation steps.                                                                      |
| [`consolidate_locations`](#utilities.consolidation.DataConsolidator.consolidate_locations)([...])                  | Consolidate locations with country code standardization.                                          |
| [`consolidate_vocabulary`](#utilities.consolidation.DataConsolidator.consolidate_vocabulary)()                     |                                                                                                   |
| [`detect_duplicates`](#utilities.consolidation.DataConsolidator.detect_duplicates)()                               |                                                                                                   |
| [`fix_location_country_codes`](#utilities.consolidation.DataConsolidator.fix_location_country_codes)()             |                                                                                                   |
| [`fix_orphaned_burden_records`](#utilities.consolidation.DataConsolidator.fix_orphaned_burden_records)()           | Repair fact_disease_burden rows whose location_id does not resolve to a valid dim_location entry. |
| [`resolve_fact_table_duplicates`](#utilities.consolidation.DataConsolidator.resolve_fact_table_duplicates)()       | Resolve duplicates across fact tables.                                                            |
| [`standardize_vocabularies`](#utilities.consolidation.DataConsolidator.standardize_vocabularies)([vocab_dir, ...]) |                                                                                                   |
| [`summarize_locations`](#utilities.consolidation.DataConsolidator.summarize_locations)()                           |                                                                                                   |
| [`validate_country_codes`](#utilities.consolidation.DataConsolidator.validate_country_codes)()                     |                                                                                                   |

#### \_\_init_\_(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

#### align_all_foreign_keys(dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

#### fix_orphaned_burden_records() → [int](https://docs.python.org/3/builtins/functions.html#int)

Repair fact_disease_burden rows whose location_id does not resolve to
a valid dim_location entry.

Each orphan is mapped to a target location (name-match, then the most
common location in fact_disease_case as fallback), then reassigned
via \_aggregate_and_reassign. When the reassignment collides on the
(location_id, year, disease_code) unique constraint, the rows are
aggregated (SUM cases_mean, AVG rates) rather than the orphan being
dropped.

#### resolve_fact_table_duplicates()

Resolve duplicates across fact tables.

Every rebuild MUST preserve every column of the target table.
Any column not present in the SELECT list is silently NULLed (or
reset to its column default) by the rebuild. This method was the
source of two silent data losses in earlier revisions:

> - fact_insecticide_resistance lost n_insect_susceptible,
>   n_insect_resistant, all vgsc_\* columns, exposure_time,
>   vgsc_generation, and resistance_status.
> - fact_disease_case lost case_count (reset to 1).

Both are fixed here by enumerating every column explicitly.

Additionally, every block now guards its DELETE-then-INSERT
rebuild with an explicit duplicate-count check. On a clean
database, no table is touched at all. This eliminates the
failure mode where an empty or partially-populated temp table
combined with an unconditional DELETE would silently wipe an
entire fact table.

#### consolidate_all(study_region: [str](https://docs.python.org/3/builtins/stdtypes.html#str) = 'Kenya') → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Run all consolidation steps.

## Ordering rationale

Step 4 (standardize_vocabularies) must run BEFORE step 8
(consolidate_vocabulary) so that dim_vector rows carry a
concept_id when the consolidation step groups them.

Step 8 (consolidate_vocabulary) MUST aggregate fact rows rather
than delete them — see \_aggregate_and_reassign.

Step 9 (resolve_fact_table_duplicates) rebuilds three fact
tables. It MUST list every column in its SELECTs, otherwise
those columns are silently lost. See the method docstring.

Step 10 (_compute_resistance_status) MUST run after step 9,
because step 9’s rebuild drops resistance_status.

#### consolidate_locations(distance_threshold_km: [float](https://docs.python.org/3/builtins/functions.html#float) = 5.0, min_samples: [int](https://docs.python.org/3/builtins/functions.html#int) = 1, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False)

Consolidate locations with country code standardization.

#### fix_location_country_codes() → [int](https://docs.python.org/3/builtins/functions.html#int)

#### validate_country_codes() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

#### consolidate_vocabulary() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

#### detect_duplicates() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

#### summarize_locations() → DataFrame

#### close()

#### standardize_vocabularies(vocab_dir: [str](https://docs.python.org/3/builtins/stdtypes.html#str) = 'vocabularies', fuzzy_threshold: [float](https://docs.python.org/3/builtins/functions.html#float) = 0.85, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]
