# Architecture

## Data flow

```
┌────────────────────┐         ┌──────────────────┐         ┌──────────────────┐
│  Python ETL        │  writes │  vbd_cdm.db      │  reads  │  R analysis      │
│  (data-integration)│────────▶│  (SQLite or      │◀────────│  (data-analysis) │
│                    │         │   PostgreSQL)    │         │                  │
└────────────────────┘         └──────────────────┘         └────────┬─────────┘
                                                                     │
                                                                     ▼
                                                             ┌──────────────────┐
                                                             │  outputs/        │
                                                             │    figures/      │
                                                             │    reports/      │
                                                             └──────────────────┘
```

The two projects share **only** the database. They have no shared code, no
shared configuration, and no shared test suite. This is deliberate: it
means the R side can be developed, versioned, and deployed independently
of the Python side, as long as both agree on the schema described in the
[CDM contract](../../data-integration/docs/cdm_contract.md).

The contract is one-directional in practice: the Python ETL writes it,
the R analysis reads it. The R side never writes to `vbd_cdm.db`.

## Project layout

```
data-analysis/
├── _targets.R              pipeline definition (see pipeline.md)
├── config.yml              study configuration (see configuration.md)
├── DESCRIPTION             package metadata
├── renv.lock               pinned dependencies
├── R/
│   ├── config.R            new_analysis_config()
│   ├── db.R                cdm_connect(), cdm_query()
│   ├── db_queries.R        all SQL — pure data access, no analysis
│   ├── qa.R                summarize_available_data()
│   ├── analysis_vector_seasonal.R    vector seasonality (primary)
│   ├── analysis_vector_climate.R     vector–climate associations (primary)
│   ├── analysis_resistance.R         insecticide resistance (primary)
│   ├── analysis_bionomics.R          vector bionomics (primary)
│   ├── analysis_seasonal.R           case seasonality (contextual)
│   ├── analysis_glm.R                negative-binomial GLM (scaffold)
│   ├── analysis_dlnm.R               distributed-lag model (scaffold)
│   ├── analysis_inla.R               spatial BYM2 (scaffold)
│   ├── spatial_join.R                grid-cell assignment
│   └── plot_helpers.R                theme_cdm(), palette, save_plot()
├── inst/report/
│   ├── analysis_report.qmd           narrative report
│   └── qa_report.qmd                 data-availability report
├── outputs/analysis/       created by _targets.R at runtime
└── docs/                   this documentation
```

## Layering

The R side has **four layers**, each of which may only depend on the ones
below it:

```
   ┌──────────────────────────────────────────┐
   │ 4. Reports      inst/report/*.qmd        │
   ├──────────────────────────────────────────┤
   │ 3. Plots        plot_*(), plot_helpers.R │
   ├──────────────────────────────────────────┤
   │ 2. Analysis     analyze_*()              │
   ├──────────────────────────────────────────┤
   │ 1. Data access  db.R, db_queries.R       │
   └──────────────────────────────────────────┘
```

Each layer has a single responsibility:

- **Layer 1 — Data access.** Knows SQL, knows the schema, knows how to
  connect. Returns tibbles. Contains no statistics, no plotting, no file
  I/O beyond what the DB driver requires.
- **Layer 2 — Analysis.** Pure functions: tibbles in, lists out. Contains
  no SQL, no file I/O, no plotting. This is the layer that makes the
  `targets` cache correct — see *Purity* below.
- **Layer 3 — Plots.** Takes an analysis result plus a study name and an
  output directory, writes PNGs, returns the file paths. No analysis
  logic. No SQL. Uses `theme_cdm()` and `cdm_palette` for visual
  consistency.
- **Layer 4 — Reports.** Quarto documents that render the analysis
  results and embed the figures. No SQL, no analysis, no plotting.

Dependencies point strictly downward. A Layer 2 module that calls into
Layer 3 would break the cache; a Layer 3 module that queries the database
directly would break testability.

## The exception: `spatial_join.R`

`R/spatial_join.R::attach_grid_cells()` violates the layering rule. It
calls `cdm_query()` to fetch location centroids, then joins them to a
vector-observations tibble by nearest-neighbour. Strictly, that makes it
a Layer 1 function that returns a Layer 2-shaped object.

If you want to bring it into compliance, the refactor is small:

1. Move the centroid query into `R/db_queries.R` as
   `query_location_centroids(cfg)`.
2. Change `attach_grid_cells()` to take the centroids as an argument:

   ```r
   attach_grid_cells <- function(vectors, centroids) { … }
   ```

3. Wire it in `_targets.R`:

   ```r
   tar_target(location_centroids, query_location_centroids(cfg)),
   tar_target(vectors_with_grid,
              attach_grid_cells(vectors_validated, location_centroids)),
   ```

Until that refactor happens, `attach_grid_cells` is the one documented
exception to the purity rule. Do not add more.

## The `cfg` object

Every layer above Layer 1 receives a `cfg` object — the result of
`new_analysis_config()` in `R/config.R`. It is a plain list with class
`"analysis_config"`. Its fields mirror `config.yml` plus a few normalised
derived fields.

`cfg` is passed by value; nothing mutates it. It is cached by `targets`
as a single target. Changing any key in `config.yml` invalidates `cfg`
and every target that reads it.

The distinction between `cfg` and the rest of the pipeline: `cfg` is
*identity* (which study, which years, which database), not *data*. It
never grows.

## Purity

The cache is the whole reason for the layering rules. `targets` decides
whether to skip a target by hashing:

- the target's command (the R code),
- the value of every other target it reads,
- the value of any global option it depends on.

It **cannot** see:

- files that a target reads directly,
- database rows that a target queries directly,
- random numbers (unless the seed is a target input),
- time (`Sys.time()`),
- environment variables.

If a Layer 2 function reads a file, calls `cdm_query`, or calls
`Sys.time()`, `targets` will cache a result whose value depends on
something it didn't hash. On the next run, if that external thing has
changed, `targets` won't notice and will silently return a stale result.

This is why:

- **All SQL lives in `R/db_queries.R`.** The extraction targets
  (`tar_target(vectors, query_vectors(cfg, locations$location_id))`) are
  the only place the database is touched. If the database changes, you
  invalidate those targets manually (see *Database invalidation* below).
- **All analysis functions are pure.** No file reads, no DB, no time, no
  randomness.
- **Plot functions write files but produce no analysis.** Their outputs
  are file paths, which `targets` hashes like any other value.

The `qa.R` module is pure and takes query outputs as arguments. It is
called from a target, not from a report, so its output is cached and
reusable.

## Why `targets`

Four properties matter here:

1. **Incrementality.** Editing `analysis_vector_climate.R` invalidates
   only the targets that call it, not the ones that don't. On a
   large-project rebuild, this is the difference between two minutes and
   two hours.
2. **Content hashing.** Changing `config.yml` invalidates only the targets
   that read the changed key. Because `cfg` is a single target, a change
   to `study_name` invalidates every target that reads `cfg` — but a
   change to a field that only the plot functions read invalidates only
   the plot targets, if you split `cfg` accordingly. (This project does
   not split `cfg`; the default behaviour is to invalidate everything
   downstream of `cfg` when any key changes.)
3. **Parallelism.** Independent branches (`vectors`, `environment`,
   `resistance`, `bionomics`, `cases`) can run concurrently if the DAG
   allows it. In practice the four SQL extraction targets are
   independent and run in parallel; everything downstream is
   sequential because it depends on the extraction results.
4. **Reproducibility.** `targets::tar_manifest()` shows exactly what ran.
   `targets::tar_meta()` shows when, how long, and with what warnings.

The cost of the four properties is the purity requirement above. A
`targets` pipeline is not a script; it's a DAG of pure functions. The
purity rules in this project exist to keep the DAG correct.

## `tar_option_set()` defaults

`_targets.R` sets:

```r
tar_option_set(
  packages = c(
    "dplyr", "tidyr", "purrr", "tibble", "lubridate",
    "ggplot2", "patchwork", "scales",
    "glue", "cli", "fs", "config", "here",
    "DBI", "RSQLite",
    "MASS", "broom",
    "gt"
  ),
  format = "qs",
  memory = "transient"
)
```

- **`packages`** — attached inside every target's process. Add any
  package a target calls. If you forget one, the target errors with a
  `could not find function` error that only appears when the target
  actually runs.
- **`format = "qs"`** — the on-disk cache format. Fast, compressed,
  schema-stable. Do not change this without invalidating the cache;
  `targets` will error on an unreadable cache.
- **`memory = "transient"`** — targets are dropped from RAM after their
  last downstream consumer finishes. This keeps peak memory bounded on
  large pipelines, at the cost of re-loading targets when multiple
  downstream targets need them. For this project the trade-off is
  correct.

## Database invalidation

`targets` does not hash the database. If the CDM changes — a new batch
of vector observations is ingested, the environment grid is re-cut, the
resistance dataset is corrected — `targets` will happily reuse the
cached extraction results and produce stale figures.

Two ways to handle this:

**Manual (recommended for now).** After re-running the ETL, invalidate
the extraction targets explicitly:

```r
targets::tar_invalidate(c(vectors, environment, resistance, bionomics, cases, burden))
targets::tar_make()
```

**Automatic.** Add a hash target:

```r
tar_target(db_hash, tools::md5sum(cfg$db_path), format = "qs"),
tar_target(vectors, query_vectors(cfg, locations$location_id, db_hash = db_hash)),
```

…and add an unused `db_hash` argument to each `query_*` function so the
hash is a dependency. When the DB changes, `db_hash` changes, and
everything downstream invalidates automatically.

The automatic approach is cleaner but requires touching every query
function. If you adopt it, do it uniformly — a half-migrated pipeline is
worse than either extreme.

## Outputs and the cache

Two directories matter:

```
outputs/analysis/
├── figures/     PNG files written by plot_*() functions
├── reports/     HTML files written by tar_quarto()
└── (data/)      reserved; currently unused
```

The `targets` cache lives at `_targets/` in the project root by default.
The `_targets.R` file does **not** override the cache location, so the
cache is at `data-analysis/_targets/`, not under `outputs/`.

If you want the cache under `outputs/analysis/`, add to `_targets.R`:

```r
tar_option_set(store = file.path(cfg$output_dir, "_targets"))
```

…but note this must happen *after* `cfg` is defined. The current
`_targets.R` calls `tar_option_set()` before building `cfg`, so moving
the store requires reordering the file.

`figures/` and `reports/` are **not** cached; they are file targets. The
plot functions run every time their inputs change, and `tar_quarto` re-
renders when its `.qmd` or its parameters change. This is correct: file
outputs should be regenerated when their inputs change, not persisted in
the cache.

## Adding a new module

Follow the existing layout:

1. New analysis function in `R/analysis_<name>.R`. Pure. Returns a list
   or tibble.
2. New plot function, either in the same file or in `plot_helpers.R` if
   it is genuinely shared. Writes PNGs, returns file paths.
3. New extraction function in `R/db_queries.R` if the analysis needs a
   table that no existing query returns.
4. New targets in `_targets.R` for extraction (if needed), analysis, and
   plotting.
5. Add the analysis result and figure path to the `analysis_results`
   bundle so the report can access them.
6. Add a section to `inst/report/analysis_report.qmd`.
7. Run `targets::tar_make()`. Only the new targets run.

See [Pipeline → extending](pipeline.md#extending) for the concrete
walkthrough.

## What this architecture deliberately does not do

- **No database writes from R.** The analysis side is read-only. All
  mutations — deduplication, FK repair, vocabulary standardisation —
  happen on the Python side, under explicit operator control.
- **No R↔Python bridge.** No `reticulate`, no shared config file, no
  shared library. The database is the interface. This means a Python
  upgrade cannot break the R side, and vice versa.
- **No test suite for the R side (yet).** `DESCRIPTION` declares
  `testthat` and `mockery` in `Suggests`, but the project has no
  `tests/` directory. The QA report serves as a smoke test: if the
  pipeline renders end-to-end, the extraction and analysis paths are
  working. This is a known gap; adding unit tests for the pure
  Layer 2 functions would be straightforward.
- **No CI configuration.** The pipeline is run interactively. CI would
  need to (a) provision the CDM, (b) cache `renv/` and `_targets/`,
  (c) run `tar_make()` and assert on the rendered reports. None of this
  is currently scripted.