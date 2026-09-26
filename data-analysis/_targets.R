# _targets.R
#
# Vector-centred pipeline for the VBD CDM.
#
# Ordering of concern:
#   1. Vectors (species, abundance, seasonality, climate response)
#   2. Vector biology (bionomics, insecticide resistance)
#   3. Vector infection status (pathogen carriage)
#   4. Environment (climate drivers) — feeds (1) and (2)
#   5. Cases / burden — contextual; may be empty and should not block.

library(targets)
library(tarchetypes)
library(here)

tar_source()

tar_option_set(
  packages = c(
    "dplyr", "tidyr", "purrr", "tibble", "lubridate",
    "ggplot2", "patchwork", "scales",
    "glue", "cli", "fs", "here", "yaml",
    "DBI", "RSQLite", "RPostgres",
    "MASS", "broom",
    "gt", "jsonlite"
  ),
  format = "qs",
  memory = "transient"
)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

# Recursively replace NA so the object can survive Quarto's YAML 1.2
# parameter round-trip.
sanitize_for_quarto <- function(x) {
  if (is.data.frame(x)) {
    x[] <- lapply(x, sanitize_for_quarto)
    return(x)
  }
  if (is.list(x)) {
    return(lapply(x, sanitize_for_quarto))
  }
  if (is.character(x)) {
    x[is.na(x)] <- ""
    return(x)
  }
  if (is.numeric(x) && anyNA(x)) {
    x[is.na(x)] <- NaN
    return(x)
  }
  if (is.logical(x) && anyNA(x)) {
    return(ifelse(is.na(x), "", as.character(x)))
  }
  x
}

# ---------------------------------------------------------------------------
# Config construction
#
# `vbd-cdm-analysis:` is read via read_cfg() / read_cfg_opt() (defined in
# R/config.R). The sibling `database:` block is read with read_db_block()
# and resolved into (db_path, db_url) by resolve_database().
# ---------------------------------------------------------------------------

db <- resolve_database(read_db_block())

cfg <- new_analysis_config(
  study_name              = read_cfg("study_name"),
  countries               = read_cfg_opt("countries"),
  locations               = read_cfg_opt("locations"),
  start_year              = read_cfg("start_year"),
  end_year                = read_cfg("end_year"),
  diseases                = read_cfg_opt("diseases"),
  include_estimated_cases = isTRUE(read_cfg_opt("include_estimated_cases", FALSE)),
  env_variables           = read_cfg("env_variables"),
  species_filter          = read_cfg_opt("species_filter"),
  max_lag_months          = read_cfg("max_lag_months"),
  db_path                 = db$db_path,
  db_url                  = db$db_url,
  output_dir              = {
    od <- read_cfg_opt("output_dir")
    if (is.null(od)) "outputs/analysis" else od
  }
)

# ---------------------------------------------------------------------------
# Output directories
# ---------------------------------------------------------------------------

fig_dir  <- here::here(cfg$output_dir, "figures")
data_dir <- here::here(cfg$output_dir, "data")
rep_dir  <- here::here(cfg$output_dir, "reports")

for (d in c(fig_dir, data_dir, rep_dir)) {
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
}

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

list(
  
  # =========================================================================
  # 1. Data extraction
  # =========================================================================
  
  # Administrative locations. Grid cells are excluded — see the resolver
  # docstring. These are the rows every fact query except environment joins
  # against.
  tar_target(
    locations,
    resolve_location_ids(
      countries = cfg$countries,
      locations = cfg$locations,
      db_path   = cfg$db_path,
      db_url    = cfg$db_url,
      descendants = TRUE
    )
  ),
  
  # Grid cells. The environment side of the CDM is stored on grid pixels
  # (admin_level = 'grid'), which the Python consolidation step labels as
  # "<region> (Climate Grid)". Vector observations, cases, and resistance
  # tests never point at grid cells, so they get their own target here and
  # the environment query uses this one instead.
  tar_target(
    grid_locations,
    resolve_grid_location_ids(
      countries = cfg$countries,
      locations = cfg$locations,
      db_path   = cfg$db_path,
      db_url    = cfg$db_url
    )
  ),
  
  tar_target(
    vectors,
    query_vectors(cfg, locations$location_id,
                  species_filter = cfg$species_filter)
  ),
  
  tar_target(
    environment,
    query_environment(cfg, grid_locations$location_id)
  ),
  
  tar_target(
    resistance,
    query_resistance(cfg, locations$location_id)
  ),
  
  tar_target(
    bionomics,
    query_bionomics(cfg, locations$location_id)
  ),
  
  tar_target(
    cases,
    query_cases(cfg, locations$location_id)
  ),
  
  tar_target(
    burden,
    query_burden(cfg, locations$location_id)
  ),
  
  tar_target(
    interventions,
    query_interventions(cfg, locations$location_id)
  ),
  
  # =========================================================================
  # 2. Validation
  # =========================================================================
  
  tar_target(
    vectors_validated,
    {
      if (nrow(vectors) == 0) {
        loc_str <- paste(locations$location_id, collapse = ", ")
        cli::cli_abort(c(
          "query_vectors() returned 0 rows.",
          "i" = "Locations: {loc_str}",
          "i" = "species_filter = {paste(cfg$species_filter, collapse = ', ')}"
        ))
      }
      vectors
    }
  ),
  
  tar_target(
    environment_validated,
    {
      if (nrow(environment) == 0) {
        loc_str <- paste(grid_locations$location_id, collapse = ", ")
        cli::cli_abort(c(
          "query_environment() returned 0 rows.",
          "i" = "Grid locations: {loc_str}",
          "i" = "env_variables = {paste(cfg$env_variables, collapse = ', ')}"
        ))
      }
      environment
    }
  ),
  
  tar_target(
    cases_available,
    nrow(cases) > 0
  ),
  
  tar_target(
    cases_validated,
    {
      if (nrow(cases) == 0) {
        cli::cli_warn(c(
          "query_cases() returned 0 rows.",
          "i" = "Vector-centred analyses will continue.",
          "i" = "Set `include_estimated_cases: true` in config.yml to include modelled burden."
        ))
      }
      cases
    }
  ),
  
  # =========================================================================
  # 3. QA
  # =========================================================================
  
  tar_target(
    qa,
    summarize_available_data(
      cases_validated, vectors_validated, environment_validated, locations, cfg
    )
  ),
  
  # =========================================================================
  # 4. Vector-centred analyses (PRIMARY)
  # =========================================================================
  
  tar_target(
    vector_seasonal,
    analyze_vector_seasonal(vectors_validated)
  ),
  
  tar_target(
    vector_climate,
    analyze_vector_climate(
      vectors           = vectors_validated,
      environment       = environment_validated,
      climate_variables = cfg$env_variables,
      group_cols        = c("species_name"),
      aggregation       = "monthly",
      max_lag           = cfg$max_lag_months,
      min_n             = 5L,
      fdr_correct       = TRUE
    )
  ),
  
  tar_target(
    resistance_summary,
    analyze_resistance(resistance)
  ),
  
  tar_target(
    bionomics_summary,
    analyze_bionomics(bionomics)
  ),
  
  tar_target(
    intervention_summary,
    analyze_intervention_summary(interventions)
  ),
  
  tar_target(
    intervention_burden,
    analyze_intervention_impact(
      interventions = interventions,
      outcome       = cases_validated,
      outcome_col   = "case_count",
      window_months = 24L,
      min_events    = 1L,
      min_obs       = 2L
    )
  ),
  
  tar_target(
    intervention_vector,
    analyze_intervention_impact(
      interventions = interventions,
      outcome       = vectors_validated,
      outcome_col   = "count",
      window_months = 24L,
      min_events    = 1L,
      min_obs       = 2L
    )
  ),
  
  # =========================================================================
  # 5. Case-side analyses (CONTEXT; may be empty)
  # =========================================================================
  
  tar_target(
    seasonal,
    analyze_seasonal(cases_validated)
  ),
  
  # =========================================================================
  # 6. Figures
  # =========================================================================
  
  tar_target(
    fig_vector_seasonal,
    plot_vector_seasonal(vector_seasonal, cfg$study_name, fig_dir)
  ),
  
  tar_target(
    fig_vector_climate,
    plot_vector_climate(
      result     = vector_climate,
      study_name = cfg$study_name,
      out_dir    = fig_dir
    )
  ),
  
  tar_target(
    fig_resistance,
    plot_resistance(resistance_summary, cfg$study_name, fig_dir)
  ),
  
  tar_target(
    fig_bionomics,
    plot_bionomics(bionomics_summary, cfg$study_name, fig_dir)
  ),
  
  tar_target(
    fig_intervention_summary,
    plot_intervention_summary(intervention_summary, cfg$study_name, fig_dir)
  ),
  
  tar_target(
    fig_intervention_burden,
    plot_intervention_impact(
      result        = intervention_burden,
      outcome_label = "cases",
      study_name    = cfg$study_name,
      out_dir       = fig_dir
    )
  ),
  
  tar_target(
    fig_intervention_vector,
    plot_intervention_impact(
      result        = intervention_vector,
      outcome_label = "vectors",
      study_name    = cfg$study_name,
      out_dir       = fig_dir
    )
  ),
  
  tar_target(
    fig_seasonal,
    plot_seasonal(seasonal, cfg$study_name, fig_dir)
  ),
  
  # =========================================================================
  # 7. Bundled results
  # =========================================================================
  
  tar_target(
    analysis_results,
    list(
      cfg                 = cfg,
      locations           = locations,
      grid_locations      = grid_locations,
      vectors             = vectors_validated,
      environment         = environment_validated,
      resistance          = resistance,
      bionomics           = bionomics,
      vector_seasonal     = vector_seasonal,
      vector_climate      = vector_climate,
      resistance_summary  = resistance_summary,
      bionomics_summary   = bionomics_summary,
      cases               = cases_validated,
      cases_available     = cases_available,
      cases_n             = nrow(cases_validated),
      burden              = burden,
      seasonal            = seasonal,
      qa                  = qa,
      fig_vector_seasonal = fig_vector_seasonal,
      fig_vector_climate  = fig_vector_climate,
      fig_resistance      = fig_resistance,
      fig_bionomics       = fig_bionomics,
      fig_intervention_summary = fig_intervention_summary,
      fig_intervention_burden  = fig_intervention_burden,
      fig_intervention_vector  = fig_intervention_vector,
      fig_seasonal        = fig_seasonal
    )
  ),
  
  # --- 8. Quarto-safe sanitization ---
  tar_target(cfg_sanitized,     sanitize_for_quarto(cfg)),
  tar_target(qa_sanitized,      sanitize_for_quarto(qa)),
  tar_target(results_sanitized, sanitize_for_quarto(analysis_results)),
  
  # --- 8b. Serialise deep structures to RDS ---
  tar_target(
    qa_rds,
    {
      path <- file.path(data_dir, "qa.rds")
      saveRDS(qa_sanitized, path)
      path
    },
    format = "file"
  ),
  
  tar_target(
    results_rds,
    {
      path <- file.path(data_dir, "analysis_results.rds")
      saveRDS(results_sanitized, path)
      path
    },
    format = "file"
  ),
  
  # --- 9. Reports ---
  tar_quarto(
    qa_report,
    path           = "inst/report/qa_report.qmd",
    quiet          = FALSE,
    execute_params = list(qa_path = qa_rds, cfg = cfg_sanitized)
  ),
  
  tar_target(
    qa_report_published,
    {
      fs::dir_create(rep_dir)
      dest <- file.path(rep_dir, basename(qa_report))
      fs::file_copy(qa_report, dest, overwrite = TRUE)
      dest
    },
    format = "file"
  ),
  
  tar_quarto(
    analysis_report,
    path           = "inst/report/analysis_report.qmd",
    quiet          = FALSE,
    execute_params = list(cfg = cfg_sanitized, results_path = results_rds)
  ),
  
  tar_target(
    analysis_report_published,
    {
      fs::dir_create(rep_dir)
      dest <- file.path(rep_dir, basename(analysis_report))
      fs::file_copy(analysis_report, dest, overwrite = TRUE)
      dest
    },
    format = "file"
  ),
  
  NULL
)