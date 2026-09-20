# VBD CDM Analysis Pipeline

R-side analysis of the vector-borne disease Common Data Model produced by
the Python ingestion pipeline in `../data-integration/`.

## Prerequisites

- R ≥ 4.3
- The CDM SQLite database at `../data-integration/vbd_cdm.db`
  (produced by running the ingestion pipeline first)

## Setup (one-time)

    # From the project root (data-analysis/)
    Rscript -e 'renv::restore()'

`renv::restore()` installs every package pinned in `renv.lock` into a
project-local library. If `renv.lock` is missing or stale:

    Rscript -e 'renv::init()'
    Rscript -e 'renv::snapshot()'

## Run the pipeline

    # From the project root, in an interactive R session or via Rscript
    targets::tar_make()

    # Inspect what ran and what was cached
    targets::tar_visnetwork()

`tar_make()` reads `_targets.R`, resolves the config in `config.yml`,
extracts data from the CDM, runs the analyses, and writes outputs. Every
target is cached on disk; unchanged targets are skipped on subsequent
runs.

## Outputs

    outputs/analysis/
      figures/   PNG figures (one per climate variable + correlations + seasonal)
      data/      Intermediate tibbles (currently written to targets cache)
      reports/   Rendered reports (optional; see below)

To list the produced figures after a run:

    ls outputs/analysis/figures/

## Configuration

Edit `config.yml` to change the study name, location, country, date
range, disease filter, environmental variables, or species filter. Then
re-run `targets::tar_make()` — targets detects the config change and
rebuilds only the affected targets.

Key config keys:

| Key | Meaning |
|---|---|
| `study_name` | Label used in figures and file names |
| `location` | Location name or `"ALL"` |
| `country` | ISO alpha-2 code or `"ALL"` |
| `start_year`, `end_year` | Inclusive date filter |
| `disease_code` | Disease to filter on (e.g. `"MAL"`) |
| `env_variables` | Environmental variables to extract (`["t2m","tp"]`) |
| `species_filter` | Substrings matched against `dim_vector.species_name` |
| `max_lag_months` | Maximum lag for lag-correlation analysis |
| `database` | Path to the SQLite DB, relative to project root |

## Re-running after a code change

targets caches every step. After editing any file in `R/`, the affected
targets are automatically invalidated. To force a specific target:

    targets::tar_invalidate(seasonal)
    targets::tar_make()

To rebuild everything from scratch:

    targets::tar_destroy()
    targets::tar_make()

## Adding a new analysis

1. Write a function in `R/` that takes tibbles and returns a list or
   tibble. Keep it pure — no file I/O.
2. Write a plotting function if the result is meant to be visualised.
3. Add a `tar_target()` entry in `_targets.R`.
4. Run `targets::tar_make()`.

## Common issues

**`could not find function "tar_make"`**
Prefix with the package: `targets::tar_make()`. The targets package is
not attached by default.

**`The project is out-of-sync`**
`renv.lock` lists packages that don't match what's installed. Run
`renv::snapshot()` to refresh the lockfile.

**`SQLite database not found`**
The pipeline expects the DB at `../data-integration/vbd_cdm.db` relative
to this project root. Verify `config.yml` and that the ingestion pipeline
has been run at least once.

**`No locations matched the filter`**
`resolve_location_ids()` found zero rows matching `location` + `country`.
Check the values in `config.yml` against the current contents of
`dim_location` in the DB.

## Project layout

    data-analysis/
      _targets.R              pipeline definition
      config.yml              study configuration
      DESCRIPTION             package metadata
      renv.lock               pinned dependencies
      R/
        config.R              new_analysis_config()
        db.R                  cdm_connect(), cdm_query()
        db_queries.R          SQL layer (query_cases, query_vectors, ...)
        qa.R                  summarize_available_data()
        analysis_seasonal.R   analyze_seasonal(), plot_seasonal()
        analysis_vector_climate.R
        analysis_glm.R        (scaffold)
        analysis_dlnm.R       (scaffold)
        analysis_inla.R       (scaffold)
        plot_helpers.R        theme_cdm(), save_plot(), ...
      inst/report/            quarto report source (optional)
      outputs/analysis/       figures, data, reports