# Getting started

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| R | >= 4.3 | `renv` will refuse older versions. |
| The CDM database | any | Produced by `data-integration`. Must exist before the pipeline runs. |
| `targets` | >= 1.5 | Installed automatically by `renv::restore()`. |
| Quarto | >= 1.4 | Optional. Only needed to render the reports. |
| Rtools / Xcode CLT / `build-essential` | latest | Required by `renv::restore()` for compiled packages (`RPostgres`, `RSQLite`, `glmmTMB`, `TMB`, `xml2`). |

The R side is read-only: it never writes to `vbd_cdm.db`. If the
database doesn't exist yet, run the Python ingestion pipeline first:

```bash
cd ../data-integration
python main.py --config base_config.yaml
```

See the [CDM contract](../../data-integration/docs/cdm_contract.md) for
what the database contains.

---

## Install

From the project root (`data-analysis/`):

```r
# One-time setup: install every package pinned in renv.lock
renv::restore()
```

`renv::restore()` installs into a project-local library at
`renv/library/`. Nothing is written to your system R library. The first
run will take 10-30 minutes depending on what's already cached on your
machine.

Verify the result:

```r
renv::status()
# No issues found -- the project is in a consistent state.
```

If `renv::restore()` fails, see
[Troubleshooting -> `renv::restore()` fails](troubleshooting.md#renvrestore-fails).

### If `renv.lock` is missing or stale

```r
renv::init()       # create the lockfile from what's currently installed
renv::snapshot()   # update an existing lockfile to match reality
```

`renv::init()` on a project that already has an `renv.lock` will prompt
before overwriting. Read the prompt.

---

## Verify the database is visible

Before running the pipeline, confirm the R side can reach the CDM:

```r
source("R/db.R")

con <- cdm_connect()
DBI::dbListTables(con)
#  [1] "alembic_version"                 "dim_concept"
#  [3] "dim_disease"                     "dim_identification_method"
#  [5] "dim_insecticide"                 "dim_location"
#  [7] "dim_resistance_test_type"        "dim_socioeconomic_indicator"
#  [9] "dim_source"                      "dim_time"
# [11] "dim_vocabulary"                  "dim_vector"
# [13] "fact_bionomics"                  "fact_concept_map"
# [15] "fact_disease_burden"             "fact_disease_case"
# [17] "fact_environment"                "fact_health_facility"
# [19] "fact_insecticide_resistance"     "fact_intervention"
# [21] "fact_socioeconomic_condition"    "fact_vector_observation"

DBI::dbDisconnect(con)
```

If `cdm_connect()` errors with `Database not found at '...'`, the
`database` key in `config.yml` is wrong. It is resolved relative to the
project root via `here::here()`, **not** the working directory. See
[Configuration -> `database`](configuration.md#database).

### Quick row-count sanity check

Once connected, verify the tables you intend to analyse are non-empty:

```r
con <- cdm_connect()

DBI::dbGetQuery(con, "
  SELECT 'vectors'     AS tbl, COUNT(*) AS n FROM fact_vector_observation
  UNION ALL SELECT 'environment',    COUNT(*)      FROM fact_environment
  UNION ALL SELECT 'resistance',     COUNT(*)      FROM fact_insecticide_resistance
  UNION ALL SELECT 'bionomics',      COUNT(*)      FROM fact_bionomics
  UNION ALL SELECT 'cases',          COUNT(*)      FROM fact_disease_case
")

DBI::dbDisconnect(con)
```

`fact_disease_case` is allowed to be empty - the pipeline is
vector-centred and will run anyway. The other four are **not** allowed
to be empty; the pipeline aborts if `vectors` or `environment` return
zero rows. See [Data access](data-access.md) for what each table holds.

---

## Run the pipeline

```r
targets::tar_make()
```

The first run will take several minutes. Subsequent runs are incremental
- `targets` hashes every input (code, config, cached values) and skips
targets whose inputs haven't changed.

### Watch what's happening

```r
# Preview what will run
targets::tar_visnetwork()

# Or, from the console after starting:
targets::tar_progress()
```

`tar_visnetwork()` opens an interactive DAG in the RStudio Viewer (or a
browser). Green nodes are up to date; grey nodes are out of date; red
nodes errored on the last run.

### Interrupting a run

`targets::tar_make()` is safe to interrupt (`Ctrl-C`). Each target's
value is committed atomically. An interrupted run leaves the cache
consistent; re-running picks up where it left off.

### Re-running in-process

By default, `targets` runs each target in a **child R session**. This is
robust but makes debugging harder, because errors from the child process
are reported tersely in the parent. To run everything in the current
session:

```r
targets::tar_make(callr_function = NULL)
```

The error messages are clearer, at the cost of losing parallelism and
isolation.

---

## Inspect the outputs

### Load the results bundle

Every analysis output and figure path is collected into one list, cached
as the target `analysis_results`:

```r
targets::tar_load(analysis_results)
str(analysis_results, max.level = 1)
```

The bundle contains:

| Element | What it is |
|---|---|
| `cfg` | The resolved configuration |
| `locations` | `dim_location` rows matching the study area |
| `vectors`, `environment`, `resistance`, `bionomics`, `cases`, `burden` | Extraction outputs |
| `vector_seasonal`, `vector_climate`, `resistance_summary`, `bionomics_summary`, `seasonal` | Analysis outputs |
| `cases_available` | Boolean |
| `qa` | Data-availability summary |
| `fig_*` | Character vector(s) of written figure paths |

### Open the reports

Two HTML reports are rendered by the pipeline:

```r
browseURL("outputs/analysis/reports/qa_report.html")
browseURL("outputs/analysis/reports/analysis_report.html")
```

**Read the QA report first.** It tells you whether the data are missing,
filtered out, or mis-joined, before you try to interpret the analysis.
See [Plots and reports](plots-and-reports.md) for what each section
contains.

### Inspect individual figures

```r
targets::tar_load(fig_vector_climate)
# [1] "outputs/analysis/figures/Pan_African_Anopheles_vector_climate_correlations.png"
# [2] "outputs/analysis/figures/Pan_African_Anopheles_vector_climate_t2m.png"
# [3] "outputs/analysis/figures/Pan_African_Anopheles_vector_climate_tp.png"
# [4] "outputs/analysis/figures/Pan_African_Anopheles_vector_climate_by_group.png"

browseURL(fig_vector_climate[2])
```

---

## The output directory

After a successful run, `outputs/analysis/` contains:

```
outputs/analysis/
|-- figures/
|   |-- Pan_African_Anopheles_vector_seasonal.png
|   |-- Pan_African_Anopheles_vector_climate_correlations.png
|   |-- Pan_African_Anopheles_vector_climate_t2m.png
|   |-- Pan_African_Anopheles_vector_climate_tp.png
|   |-- Pan_African_Anopheles_vector_climate_by_group.png
|   |-- Pan_African_Anopheles_resistance.png
|   |-- Pan_African_Anopheles_bionomics.png
|   `-- Pan_African_Anopheles_seasonal.png
|-- reports/
|   |-- qa_report.html
|   `-- analysis_report.html
`-- data/                     # reserved; currently unused
```

The filenames are derived from `cfg$study_name`. If you change
`study_name` in `config.yml`, the filenames change. See
[Configuration -> `study_name`](configuration.md#study_name).

### Where the cache lives

The `targets` cache is at **`data-analysis/_targets/`** - the project
root, not under `outputs/`. This is `targets`' default and the current
`_targets.R` does not override it.

```
data-analysis/
|-- _targets/                 # the cache
|-- outputs/analysis/         # the figures and reports
|-- R/
|-- _targets.R
`-- config.yml
```

The cache is safe to delete but expensive to rebuild. Use
`targets::tar_destroy()` rather than `rm -rf`:

```r
targets::tar_destroy()         # removes _targets/ cleanly
targets::tar_make()            # rebuilds from scratch
```

If you want the cache under `outputs/analysis/` instead, see
[Pipeline -> Cache location](pipeline.md#cache-location).

---

## Verify the run

A successful first run should produce:

1. **Between seven and eight PNG figures** under
   `outputs/analysis/figures/`. The by-group heatmap is written
   unconditionally given the current `_targets.R` (which sets
   `group_cols = c("species_name")`). The seasonal figure is written
   only when `fact_disease_case` contains rows; on a database with no
   case data, `plot_seasonal` returns `invisible(NULL)` and no file is
   written.
2. **Two HTML reports** under `outputs/analysis/reports/`.
3. **No errors** in `targets::tar_progress()`.

Check:

```r
targets::tar_progress()
# All targets should be "built" or "skipped", not "errored".

targets::tar_meta(fields = c("name", "error"), targets_only = TRUE)
# The `error` column should be NA for every row.
```

If any target errored, see [Troubleshooting](troubleshooting.md).

---

## Next steps

| I want to... | Read |
|---|---|
| Change the study area, year range, or species filter | [Configuration](configuration.md) |
| Understand the pipeline structure | [Pipeline](pipeline.md) |
| Understand a specific figure | [Analysis modules](analysis-modules.md) |
| Query the database directly | [Data access](data-access.md) |
| Add a new analysis | [Pipeline -> extending](pipeline.md#extending) |
| Debug an error | [Troubleshooting](troubleshooting.md) |
| Look up a term | [Glossary](glossary.md) |

---

## Cheat sheet

Everything you need after the initial setup:

```r
# Run everything that's out of date
targets::tar_make()

# See what will run
targets::tar_visnetwork()

# See what ran
targets::tar_progress()

# Load a result
targets::tar_load(analysis_results)

# Force a single target to re-run
targets::tar_invalidate(seasonal)
targets::tar_make()

# Force everything to re-run
targets::tar_destroy()
targets::tar_make()

# After re-running the ETL: invalidate the extraction targets
targets::tar_invalidate(c(
  locations, vectors, environment, resistance, bionomics, cases, burden
))
targets::tar_make()
```

---

## A note on first-run performance

The first `targets::tar_make()` on a fresh database has no cache to
draw on, so it runs every target in dependency order. Timings scale
roughly with:

| Target | Typical cost |
|---|---|
| Extraction queries | seconds each |
| `vector_climate` | seconds to a minute, depending on grid size |
| Plot functions | seconds each |
| Quarto renders | tens of seconds each |

On subsequent runs, only targets whose inputs changed re-run. A typical
"nothing changed" run finishes in under a minute.

If the first run feels slow, `targets::tar_visnetwork()` shows which
targets are running in parallel. The six extraction targets
(`vectors`, `environment`, `resistance`, `bionomics`, `cases`, `burden`)
are independent and run concurrently by default. The
`analysis_vector_climate` target is usually the longest single step.