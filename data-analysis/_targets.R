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
#
#   Run with:   targets::tar_make()
#   Inspect:    targets::tar_visnetwork()
#   Force one:  targets::tar_invalidate(<name>); targets::tar_make()

library(targets)
library(tarchetypes)

tar_source()

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

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

# Recursively replace NA so the object can survive Quarto's YAML 1.2
# parameter round-trip. Numerics with NA become NaN; logicals with NA become
# character ""; characters with NA become "".
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

cfg_get <- function(key, default = NULL) {
  tryCatch(config::get(key), error = function(e) default)
}

normalize_list <- function(x) {
  if (is.null(x) || length(x) == 0) NULL else unlist(x, use.names = FALSE)
}

cfg <- new_analysis_config(
  study_name              = config::get("study_name"),
  location                = config::get("location"),
  country                 = config::get("country"),
  location_ids            = normalize_list(cfg_get("location_ids")),
  start_year              = config::get("start_year"),
  end_year                = config::get("end_year"),
  disease_code            = config::get("disease_code"),
  include_estimated_cases = isTRUE(cfg_get("include_estimated_cases", FALSE)),
  env_variables           = config::get("env_variables"),
  species_filter          = normalize_list(cfg_get("species_filter")),
  max_lag_months          = config::get("max_lag_months"),
  db_path                 = config::get("database"),
  output_dir              = {
    od <- cfg_get("output_dir")
    if (is.null(od)) "outputs/analysis" else od
  }
)

# ---------------------------------------------------------------------------
# Output directories
# ---------------------------------------------------------------------------

fig_dir  <- file.path(cfg$output_dir, "figures")
data_dir <- file.path(cfg$output_dir, "data")
rep_dir  <- file.path(cfg$output_dir, "reports")

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
  
  tar_target(
    locations,
    resolve_location_ids(
      cfg$location, cfg$country,
      db_path      = cfg$db_path,
      explicit_ids = cfg$location_ids
    )
  ),
  
  tar_target(
    vectors,
    query_vectors(cfg, locations$location_id,
                  species_filter = cfg$species_filter)
  ),
  
  tar_target(
    environment,
    query_environment(cfg, locations$location_id)
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
        loc_str <- paste(locations$location_id, collapse = ", ")
        cli::cli_abort(c(
          "query_environment() returned 0 rows.",
          "i" = "Locations: {loc_str}",
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
  
  # =========================================================================
  # 5. Case-side analyses (CONTEXT; may be empty)
  # =========================================================================
  
  tar_target(
    seasonal,
    analyze_seasonal(cases_validated)
  ),
  
  # =========================================================================
  # 6. Figures — vector first
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
      burden              = burden,
      seasonal            = seasonal,
      qa                  = qa,
      fig_vector_seasonal = fig_vector_seasonal,
      fig_vector_climate  = fig_vector_climate,
      fig_resistance      = fig_resistance,
      fig_bionomics       = fig_bionomics,
      fig_seasonal        = fig_seasonal
    )
  ),
  
  # =========================================================================
  # 8. Quarto-safe sanitization
  # =========================================================================
  
  tar_target(
    cfg_sanitized,
    sanitize_for_quarto(cfg)
  ),
  
  tar_target(
    qa_sanitized,
    sanitize_for_quarto(qa)
  ),
  
  tar_target(
    results_sanitized,
    sanitize_for_quarto(analysis_results)
  ),
  
  # =========================================================================
  # 9. Reports
  # =========================================================================
  
  tar_quarto(
    qa_report,
    path            = "inst/report/qa_report.qmd",
    quiet           = FALSE,
    execute_params  = list(qa = qa_sanitized, cfg = cfg_sanitized)
  ),
  
  tar_quarto(
    analysis_report,
    path            = "inst/report/analysis_report.qmd",
    quiet           = FALSE,
    execute_params  = list(cfg = cfg_sanitized, results = results_sanitized)
  ),
  
  NULL
)