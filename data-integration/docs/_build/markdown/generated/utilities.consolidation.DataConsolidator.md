# utilities.consolidation.DataConsolidator

### *class* utilities.consolidation.DataConsolidator(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Consolidation phases over one CDM session.

#### \_\_init_\_(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

### Methods

| [`__init__`](#id0)([session])                                                                                          |                                                                       |
|------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------|
| [`align_all_foreign_keys`](#utilities.consolidation.DataConsolidator.align_all_foreign_keys)([dry_run])                | Repair orphans by name before NULLing what is left (not the reverse). |
| [`close`](#utilities.consolidation.DataConsolidator.close)()                                                           | Drain the pending resolution buffer, then close the session.          |
| [`consolidate_all`](#utilities.consolidation.DataConsolidator.consolidate_all)([gadm_gpkg, country_codes, ...])        | Run every phase in the one safe order.                                |
| [`consolidate_locations`](#utilities.consolidation.DataConsolidator.consolidate_locations)(\*[, gadm_gpkg, ...])       | A6a country -> hierarchy -> detach -> identity merge -> grid parents. |
| [`consolidate_vocabulary`](#utilities.consolidation.DataConsolidator.consolidate_vocabulary)([merge_policies, ...])    | Merge every dimension in the catalog, not only packed ones.           |
| [`detect_duplicates`](#utilities.consolidation.DataConsolidator.detect_duplicates)([tables, registry, max_groups])     |                                                                       |
| [`expand_burden_to_daily`](#utilities.consolidation.DataConsolidator.expand_burden_to_daily)([force, seed, ...])       | Expand yearly burden into daily cases summing to the annual total.    |
| [`fix_location_country_codes`](#utilities.consolidation.DataConsolidator.fix_location_country_codes)()                 | Deprecated: standardize_country_codes also fixes the spelling.        |
| [`fix_orphaned_burden_records`](#utilities.consolidation.DataConsolidator.fix_orphaned_burden_records)()               |                                                                       |
| [`resolve_fact_table_duplicates`](#utilities.consolidation.DataConsolidator.resolve_fact_table_duplicates)()           |                                                                       |
| [`resolve_location_names`](#utilities.consolidation.DataConsolidator.resolve_location_names)(\*[, gadm_gpkg, ...])     | Name every location from its coordinates.                             |
| [`standardize_country_codes`](#utilities.consolidation.DataConsolidator.standardize_country_codes)(\*[, dry_run, ...]) | Canonicalise country_code to ISO2 and country_name to its label.      |
| [`standardize_vocabularies`](#utilities.consolidation.DataConsolidator.standardize_vocabularies)([vocab_dir, ...])     | Reconcile every registered field against its pack.                    |
| [`summarize_locations`](#utilities.consolidation.DataConsolidator.summarize_locations)(\*[, registry, sample_limit])   | Read-only description of dim_location: coverage, hierarchy, names.    |
| [`validate_country_codes`](#utilities.consolidation.DataConsolidator.validate_country_codes)()                         | Country coverage, per-code counts, and code/name consistency.         |

#### \_\_init_\_(session: [Session](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.Session) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

#### standardize_country_codes(\*, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, fill_from_geometry: [bool](https://docs.python.org/3/builtins/functions.html#bool) = True, audit_geometry: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, sample_limit: [int](https://docs.python.org/3/builtins/functions.html#int) = 10) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Canonicalise country_code to ISO2 and country_name to its label.

Ladder: country_code -> country_name -> centroid. Geometry is fill and
audit only, never correction: a row that states a country keeps it and the
disagreement is counted. Idempotent; a canonical row is not written.

#### fix_location_country_codes() → [int](https://docs.python.org/3/builtins/functions.html#int)

Deprecated: standardize_country_codes also fixes the spelling.

#### consolidate_locations(\*, gadm_gpkg: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, country_codes: [Iterable](https://docs.python.org/3/library/typing.html#typing.Iterable)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)] = (), dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, audit_country_geometry: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

A6a country -> hierarchy -> detach -> identity merge -> grid parents.

#### resolve_location_names(\*, gadm_gpkg: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, include_grid: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, overwrite_human: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, similarity_threshold: [float](https://docs.python.org/3/builtins/functions.html#float) = 0.8, max_distance_km: [float](https://docs.python.org/3/builtins/functions.html#float) = 25.0, token_fallback: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, precision: [float](https://docs.python.org/3/builtins/functions.html#float) | [None](https://docs.python.org/3/builtins/constants.html#None) = 1e-05, sample_limit: [int](https://docs.python.org/3/builtins/functions.html#int) = 10) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Name every location from its coordinates.

Point-in-polygon at every level; the deepest human-named containing unit
supplies the label (dim_concept first, else the layer’s NAME_n). The source
token is evidence only: it can confirm or outvote depth, and its similarity
is recorded. Ladder: boundary -> nearest_boundary -> ancestor ->
nearest_anchor -> token (opt-in). Grid cells are skipped by default; a
pixel has no name and 28k identical labels would break the “<lon>/<lat>”
fallback in \_assign_grid_parents. location_code is never touched.
standard_code is NOT stamped: \_merge_locations_by_identity would collapse
distinct sites inside one ward.

#### standardize_vocabularies(vocab_dir: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, fuzzy_threshold: [float](https://docs.python.org/3/builtins/functions.html#float) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, \*, registry: VocabularyRegistry | [None](https://docs.python.org/3/builtins/constants.html#None) = None, run_id: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Reconcile every registered field against its pack.

Country codes are not here: they are static ISO data, standardised at A6a.

#### consolidate_vocabulary(merge_policies: [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)] | [None](https://docs.python.org/3/builtins/constants.html#None) = None, \*, registry: VocabularyRegistry | [None](https://docs.python.org/3/builtins/constants.html#None) = None, run_id: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Merge every dimension in the catalog, not only packed ones.

A merge is a property of the schema; driving the work list from
registry.merge_policies() left every dimension with no manifest
unconsolidated. One table failing rolls back that table and continues.

#### align_all_foreign_keys(dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Repair orphans by name before NULLing what is left (not the reverse).

#### validate_country_codes() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Country coverage, per-code counts, and code/name consistency.

A non-zero name_mismatches after A6a means something wrote the pair
directly: country_name is no longer the canonical label for its code.

#### fix_orphaned_burden_records() → [int](https://docs.python.org/3/builtins/functions.html#int)

#### expand_burden_to_daily(force: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, seed: [int](https://docs.python.org/3/builtins/functions.html#int) = 42, base_variation: [float](https://docs.python.org/3/builtins/functions.html#float) = 0.25, \*, default_population: [float](https://docs.python.org/3/builtins/functions.html#float) | [None](https://docs.python.org/3/builtins/constants.html#None) = None) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [int](https://docs.python.org/3/builtins/functions.html#int)]

Expand yearly burden into daily cases summing to the annual total.

default_population=None skips (and counts) a location with no SP.POP.TOTL.
dim_time is extended first, because date_id is a foreign key.

#### detect_duplicates(tables: [Iterable](https://docs.python.org/3/library/typing.html#typing.Iterable)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)] | [None](https://docs.python.org/3/builtins/constants.html#None) = None, \*, registry: [Any](https://docs.python.org/3/library/typing.html#typing.Any) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, max_groups: [int](https://docs.python.org/3/builtins/functions.html#int) = 50) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

#### resolve_fact_table_duplicates() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [int](https://docs.python.org/3/builtins/functions.html#int)]

#### consolidate_all(gadm_gpkg: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, country_codes: [List](https://docs.python.org/3/library/typing.html#typing.List)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)] | [None](https://docs.python.org/3/builtins/constants.html#None) = None, dry_run: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, \*, registry: VocabularyRegistry | [None](https://docs.python.org/3/builtins/constants.html#None) = None, run_id: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, name_locations: [bool](https://docs.python.org/3/builtins/functions.html#bool) = True, include_grid_names: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, overwrite_human_names: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False, audit_country_geometry: [bool](https://docs.python.org/3/builtins/functions.html#bool) = False) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Run every phase in the one safe order.

Country codes are canonicalised at A6a, before any phase that groups or
filters by country. audit_country_geometry=True also cross-checks rows that
were already canonical, at one coordinate lookup per row.

#### summarize_locations(\*, registry: [Any](https://docs.python.org/3/library/typing.html#typing.Any) | [None](https://docs.python.org/3/builtins/constants.html#None) = None, sample_limit: [int](https://docs.python.org/3/builtins/functions.html#int) = 5) → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Read-only description of dim_location: coverage, hierarchy, names.

#### close() → [Dict](https://docs.python.org/3/library/typing.html#typing.Dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]

Drain the pending resolution buffer, then close the session.
