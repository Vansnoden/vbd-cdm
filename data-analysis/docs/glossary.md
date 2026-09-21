# Glossary

Terms appear in the context of *this project*. Where a term has a
broader meaning in epidemiology or statistics, the definition here is
the one that applies to the code in `data-analysis/` and
`data-integration/`.

---

## Acronyms

| Term | Meaning |
|---|---|
| **CDM** | Common Data Model. The SQLite/PostgreSQL schema described in the [CDM contract](../../data-integration/docs/cdm_contract.md). |
| **ETL** | Extract, Transform, Load. The Python ingestion pipeline in `data-integration/`. |
| **VBD** | Vector-Borne Disease. |
| **WHO** | World Health Organization. Source of the insecticide-resistance mortality thresholds. |
| **IR** | Insecticide Resistance. Used in table names like `fact_insecticide_resistance`. |
| **IRS** | Indoor Residual Spraying. An intervention type; not currently in the analysis pipeline. |
| **ITN / LLIN** | Insecticide-Treated Net / Long-Lasting Insecticidal Net. An intervention type; not currently in the analysis pipeline. |
| **EIR** | Entomological Inoculation Rate. The rate at which a human population receives infective mosquito bites. **Not computed by this pipeline.** |
| **VGSC** | Voltage-Gated Sodium Channel. The gene carrying knockdown-resistance mutations; columns in `fact_insecticide_resistance` store VGSC genotype counts. |
| **FDR** | False Discovery Rate. Applied via Benjamini–Hochberg on lag correlations. |
| **DLNM** | Distributed-Lag Non-linear Model. Scaffolded in `analysis_dlnm.R`. |
| **BYM2** | Besag–York–Mollié spatial random effect, reparameterised. Scaffolded in `analysis_inla.R`. |
| **GADM** | Global Administrative Areas. Source of the location vocabulary. |
| **IDOMAL** | The malaria ontology, built on BFO. Source of the disease vocabulary. |
| **NCBI** | National Center for Biotechnology Information. Source of the vector species taxonomy. |
| **IRAC** | Insecticide Resistance Action Committee. Source of the chemical-class groupings. |

---

## The CDM schema

### Schema concepts

| Term | Meaning |
|---|---|
| **Dimension** | A `dim_*` table. Descriptive attributes shared across facts — the "who, what, when, where" of the warehouse. |
| **Fact** | A `fact_*` table. One row per observation or event. Facts carry foreign keys to dimensions. |
| **Star schema** | The overall shape: fact tables at the centre, dimension tables around the edge. No snowflaking — dimensions do not reference other dimensions except via `dim_concept`. |
| **Grain** | What one row of a fact table represents. The grain is enforced by a natural-key unique constraint, not by the primary key. |
| **Natural key** | The set of columns that uniquely identifies a row *without* using the autoincrement primary key. Configured per table in `NATURAL_KEY_CONFIG` (`data-integration/etl/loaders/db_loader.py`). |
| **Surrogate key** | An autoincrement integer primary key (`location_id`, `observation_id`, `case_id`, …). Stable across ETL runs; safe to store in derived artefacts. |
| **Sentinel** | A placeholder value written into a nullable natural-key column before insert. `''` for text, `-1.0` for `concentration`. Required because SQL treats `NULL` as distinct in unique indexes. |
| **Upsert** | Insert-or-update keyed on the natural key. The DBLoader's default mode. |

### Dimensions

| Table | What it describes |
|---|---|
| `dim_time` | One row per calendar day, 1900-01-01 … 2026-12-31. `date_id` is the `YYYY-MM-DD` string used by every fact table. |
| `dim_location` | Administrative units *and* environment grid cells. `admin_level = 'grid'` marks the latter. |
| `dim_source` | Provenance. Every fact row carries a `source_code`. |
| `dim_vector` | Species and subspecies. Joined to facts via `vector_code`. |
| `dim_disease` | Disease codes, ICD-10 aligned where possible. |
| `dim_insecticide` | Insecticide codes, IRAC-group aligned where possible. |
| `dim_identification_method` | Diagnostic method codes. `'ESTIMATE'` is reserved for synthetic rows. |
| `dim_resistance_test_type` | Bioassay type (WHO tube, CDC bottle, …). |
| `dim_socioeconomic_indicator` | World Bank indicator codes, e.g. `SP.POP.TOTL`. |
| `dim_vocabulary`, `dim_concept` | External vocabulary registry and its concepts (GADM, IDOMAL, NCBI). |

### Facts

| Table | One row per… |
|---|---|
| `fact_vector_observation` | species × day × site × collection method |
| `fact_environment` | climate variable × day × site |
| `fact_insecticide_resistance` | resistance test (species × insecticide × test type × day × site) |
| `fact_bionomics` | species × day × site bionomic observation |
| `fact_disease_case` | reported case group (location × onset day × disease × method × source × case-status × age-group × sex) |
| `fact_disease_burden` | yearly burden estimate (location × year × disease) |
| `fact_intervention` | intervention event (location × start day × disease × type) |
| `fact_socioeconomic_condition` | yearly indicator (location × year × indicator) |
| `fact_health_facility` | facility (identified by `facility_code`) |
| `fact_concept_map` | mapping from a raw source value to a `dim_concept` |

### Reserved values

| Value | Meaning |
|---|---|
| `identification_method_code = 'ESTIMATE'` | Row synthesised by `fix_cases.py` / `consolidation._expand_burden_to_daily()`. Not an observation. |
| `admin_level = 'grid'` | Location is an environment pixel, not an administrative unit. |
| `location_code` starting with `GRID_` | Same as above. |
| `mortality` in `[0, 100]` | Percentage, not proportion. WHO thresholds are `98` and `90`. |
| `infection_rate` in `[0, 1]` | Proportion, not percentage. Deliberately different convention from `mortality`. |
| Sentinel `''` | Stands in for `NULL` in text natural-key columns. |
| Sentinel `-1.0` | Stands in for `NULL` in `fact_insecticide_resistance.concentration`. |

---

## Data access (R side)

| Term | Meaning |
|---|---|
| `cdm_connect(db_path)` | Opens a `DBIConnection` to the CDM. Priority: argument → `DATABASE_URL` → `SQL_LITE_DB_NAME`. |
| `cdm_query(sql, params, con, db_path)` | Runs a SQL string, returns a tibble. `list()` is treated as `NULL`. |
| `resolve_location_ids(location, country, db_path, explicit_ids)` | Turns a name or country code into a vector of `location_id`s. Aborts on zero matches. |
| `query_*()` | One function per fact table in `R/db_queries.R`. All take `cfg` and a vector of location IDs. |
| `db_path` | The `cfg` field holding the resolved path to the SQLite file. Config key is `database`; field name is `db_path`. |
| **The 900-ID limit** | `.loc_clause()` silently drops the location filter when more than 900 IDs are passed. The query then returns *all* locations. |
| **Grid-cell gap** | `query_vectors()` does not return `location_id`, so `attach_grid_cells()` cannot be composed with it as currently written. Known limitation. |

---

## Analysis (R side)

| Term | Meaning |
|---|---|
| **Aggregation** | The period to which vectors and climate are aggregated before joining. `"monthly"` (default), `"weekly"`, or `"daily"`. |
| **Vector abundance** | The column used as the response in vector–climate analysis. Auto-detected: `count` first, then `density_per_trap`, then the first column. |
| **Lag correlation** | Pearson r between the climate variable at time *t − k* and vector abundance at time *t*. Computed for lags 0…`max_lag_months`. |
| **Optimal lag** | The lag maximising `r` for a given climate variable. Selected by `slice_max(r)`, **not** by significance. |
| **Climate envelope** | The mean ± SD vector abundance within each decile of a climate variable. Shows the range of climate values over which abundance is high. |
| **Seasonal pattern** | Monthly means of both series (vector and climate), each normalised to `[0, 1]` so they can be plotted on the same axis. |
| **Benjamini–Hochberg (BH)** | FDR-controlling p-value adjustment. Adds a `p_adj` column to correlation tables when `fdr_correct = TRUE`. |
| **WHO thresholds** | Mortality cutoffs: `≥ 98%` susceptible, `90–97.99%` suspected resistance, `< 90%` resistant. Applied on the Python side; summarised on the R side. |
| **Bionomics** | Vector behaviour traits: feeding preference, biting location, resting behaviour, larval site type. |
| **Anthropophagic / Zoophagic / Mixed** | Feeding-preference categories: prefers humans / prefers animals / no strong preference. |
| **Endophagic / Exophagic / Mixed** | Biting-location categories: bites indoors / bites outdoors / both. |
| **Sporogonic cycle** | The period during which the malaria parasite develops inside the mosquito. Temperature-dependent, on the order of 10–14 days at tropical temperatures. Motivates the lag analysis. |
| **Purity** | The property that an analysis function depends only on its arguments. Required for `targets` cache correctness. |

---

## Pipeline (`targets`)

| Term | Meaning |
|---|---|
| **Target** | A node in the DAG. Has a name, a command, and a cached value on disk. |
| **DAG** | Directed Acyclic Graph. The dependency structure of the pipeline. |
| **Cache / store** | The `_targets/` directory holding every target's serialised value. Safe to delete; expensive to rebuild. |
| **`tar_make()`** | Run the pipeline, executing only targets that are out of date. |
| **`tar_target()`** | Declare a target. First argument is the name, second is the command. |
| **`tar_quarto()`** | Render a `.qmd` file as a target. Its value is the rendered output path. |
| **`tar_load(name)`** | Load a target's value into the current R session. |
| **`tar_read(name)`** | Return a target's value without loading it into the session. |
| **`tar_invalidate(name)`** | Force a target to re-run on the next `tar_make()`, without deleting the cache. |
| **`tar_destroy()`** | Delete the entire cache. Everything re-runs. |
| **`tar_manifest()`** | Print every target and its command. |
| **`tar_visnetwork()`** | Interactive DAG visualisation. |
| **`tar_progress()`** | Show which targets ran, were skipped, or errored. |
| **`tar_meta()`** | Print metadata: run times, warnings, errors, sizes. |
| **`tar_outdated()`** | List targets that would run on the next `tar_make()`. |
| **`tar_option_set()`** | Set pipeline-wide defaults: attached packages, cache format, memory policy. |
| **`tar_source()`** | Source every file in `R/`. Called at the top of `_targets.R`. |
| **`cfg`** | The single configuration object passed to nearly every target. Built once from `config.yml`. |
| **`sanitize_for_quarto()`** | Recursively replaces `NA` with `NaN` / `""` so parameter objects can survive Quarto's YAML round-trip. |
| **`analysis_results`** | The bundled list of every analysis output and figure path. Consumed by `analysis_report.qmd`. |
| **`fig_dir`, `data_dir`, `rep_dir`** | Output directories derived from `cfg$output_dir`, created by `_targets.R`. |
| **`*_validated`** | A guard target that aborts the pipeline if its input is empty. Used for `vectors_validated`, `environment_validated`, `cases_validated`. |

---

## ETL (Python side)

| Term | Meaning |
|---|---|
| **Extractor** | Reads a source file into a pandas DataFrame. `FileExtractor` dispatches on file extension. |
| **Transformer** | Turns a source DataFrame into per-table DataFrames using a mapping CSV. `MappingTransformer`. |
| **Loader** | Upserts DataFrames into the CDM. `DBLoader`. |
| **Consolidator** | Post-load cleanup: FK repair, deduplication, vocabulary matching. `DataConsolidator`. |
| **Mapping CSV** | Declares how source columns become CDM columns. Nine columns; see [mapping.rst](../../data-integration/docs/mapping.rst). |
| **Transform** | A named operation on a value. `none`, `map`, `cast`, `date_parse`, `format_date`, `split`, `unit_convert`, `concat`. |
| **`ref`** | A transform parameter that triggers a dimension FK lookup: `{"ref": "dim_location.name"}`. |
| **Orphan** | A fact row whose foreign key does not exist in the referenced dimension. Detected and repaired (or quarantined) by `_align_fact_foreign_keys()`. |
| **Quarantine** | Orphan rows whose FK is `NOT NULL` and cannot be repaired are copied to `_quarantine_<table>` before deletion, for forensic inspection. |
| **DBSCAN** | Density-based clustering used to merge nearby locations. Operates on haversine distance with `eps = 5 km` by default. |
| **ISO alpha-2 / alpha-3** | Two- and three-letter country codes. The CDM stores alpha-2 (`KE`, `TZ`). `ISO_COUNTRY_CODES` in the consolidation module maps between them. |
| **Staging table** | A temporary table used by the PostgreSQL COPY path. Created with `LIKE <target> INCLUDING DEFAULTS`, filled by `COPY`, then merged with `INSERT … ON CONFLICT`. |
| **Alembic** | The schema-migration tool. Every change to `database/models.py` must be accompanied by an autogenerated migration under `alembic/versions/`. |
| **`alembic_version`** | The bookkeeping table Alembic writes. Excluded from every schema-inspection routine in this project. |

---

## Vocabularies

| Term | Meaning |
|---|---|
| **Vocabulary** | A named external taxonomy: `GADM`, `IDOMAL`, `NCBI`. Registered in `dim_vocabulary`. |
| **Concept** | One term in a vocabulary. `dim_concept` stores code, name, class, parent, and synonyms. |
| **`concept_id`** | The FK from a dimension row (e.g. `dim_location`) to `dim_concept`. Populated only on a confident match. |
| **`standard_code`** | The vocabulary's own code for the matched concept (e.g. `GID_2_KEN.3_1` for a GADM admin-2). Stored on the dimension row. |
| **Exact match** | Raw value equals `concept_name` case-insensitively after whitespace collapse. Auto-approved. |
| **Normalized match** | Match after stripping diacritics and collapsing whitespace. Auto-approved. |
| **Fuzzy match** | RapidFuzz `token_set_ratio ≥ 0.85`. Recorded with `is_approved = FALSE`, awaiting human review. |
| **`fact_concept_map`** | Records every match attempt: source table, source column, source value, matched concept, method, score, approval flag. |

---

## Environment variables

| Variable | Effect |
|---|---|
| `DATABASE_URL` | `sqlite:///…` or `postgresql://…`. Checked by `cdm_connect()` after an explicit `db_path` and before `SQL_LITE_DB_NAME`. |
| `SQL_LITE_DB_NAME` | Filename only. Searched in the project root and its parent. Note the misspelling — `LITE`, not `LITE`. |
| `R_CONFIG_ACTIVE` | Selects a top-level block in `config.yml`. Unset means the `default:` block. |

---

## Climate variables

The `variable` column of `fact_environment` stores short names, mostly
ERA5-style:

| Name | Meaning | Aggregation rule |
|---|---|---|
| `t2m` | Temperature at 2 m above ground | Mean |
| `tp` | Total precipitation | **Sum** |
| `d2m` | Dewpoint temperature at 2 m | Mean |
| `rh` | Relative humidity | Mean |
| `ndvi` | Normalised Difference Vegetation Index | Mean |
| `evi` | Enhanced Vegetation Index | Mean |
| `lst_day`, `lst_night` | Land surface temperature, day / night | Mean |
| `temperature_mean`, `temperature_min`, `temperature_max` | Pre-aggregated temperature summaries | Mean |
| `rainfall_total` | Pre-aggregated rainfall | **Sum** |
| `relative_humidity` | Relative humidity (alt. name) | Mean |
| `aridity_index` | Aridity index | Mean |
| `potential_breeding_sites` | Count of potential breeding sites | Mean |

The aggregation rule is applied by
`analysis_vector_climate.R::.is_rain_var()`: names in `{tp, precipitation,
rainfall_total}` are summed; everything else is averaged.

---

## Common confusions

Things that look like synonyms but are not:

| Looks similar | Actually |
|---|---|
| `mortality` vs `infection_rate` | Percentage in `[0, 100]` vs proportion in `[0, 1]`. |
| `location_id` vs `location_code` | Surrogate integer PK vs natural text key (`GRID_…` for grid cells). |
| `date_id` vs `date` | `date_id` is the string FK on fact tables. `date` is the `Date` column on `dim_time`. Always join on `date_id`. |
| `cfg$db_path` vs `config.yml` `database` | Same thing. The config key is `database`; the R field is `db_path`. |
| `vector_code` vs `species_name` | Code is the FK (`ANOGAM`); name is the label (`Anopheles gambiae`). Always join on `vector_code`. |
| `case_count` vs `COUNT(*)` | `case_count` is a value column (aggregated rows carry > 1). `COUNT(*)` counts rows. |
| `is_approved` in `fact_concept_map` | `TRUE` for exact/normalized matches (auto-approved), `FALSE` for fuzzy matches awaiting review. |