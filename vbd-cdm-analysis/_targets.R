# _targets.R
#
# Pipeline definition for the VBD CDM analysis project.
#   Run with:   targets::tar_make()
#   Inspect:    targets::tar_visnetwork()
#   Force one:  targets::tar_invalidate(<name>); targets::tar_make()
#
# Every target is a pure function defined in R/*.R. tar_source() loads those
# files into the pipeline's fresh R session — without it, nothing in R/ is
# visible and the manifest fails with "could not find function".
#
# Blocks marked UNCOMMENT correspond to modules not yet implemented. As each
# module is written, delete the "# " from the block and re-run tar_make().

library(targets)
library(tarchetypes)

# ---------------------------------------------------------------------------
# Load everything in R/ into the pipeline environment
# ---------------------------------------------------------------------------

tar_source()

# ---------------------------------------------------------------------------
# Package dependencies
#
# Every package that any target might load must be listed here. targets starts
# a fresh R session for each target; packages not listed will not be attached.
# ---------------------------------------------------------------------------

tar_option_set(
  packages = c(
    # Data manipulation
    "dplyr", "tidyr", "purrr", "tibble", "lubridate",
    # Plotting
    "ggplot2", "patchwork", "scales",
    # Utilities
    "glue", "cli", "fs", "config", "here",
    # Database
    "DBI", "RSQLite", "RPostgres",
    # Statistics
    "MASS", "glmmTMB", "dlnm", "broom",
    # Tables
    "gt"
  ),
  format  = "qs",       # fast, compact serialization
  memory  = "transient" # do not retain target objects between targets
)

# ---------------------------------------------------------------------------
# Configuration
#
# Read config.yml. See R/config.R for the schema.
# ---------------------------------------------------------------------------

cfg <- new_analysis_config(
  study_name     = config::get("study_name"),
  location       = config::get("location"),
  country        = config::get("country"),
  start_year     = config::get("start_year"),
  end_year       = config::get("end_year"),
  env_variables  = config::get("env_variables"),
  max_lag_months = config::get("max_lag_months"),
  db_path        = config::get("database"),
  # output_dir not read from config.yml; new_analysis_config() defaults it
  # to "outputs/analysis". Add it to config.yml and re-enable if you ever
  # want a different path.
)

# ---------------------------------------------------------------------------
# Output directories
#
# Created once at manifest-build time; no target depends on them.
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
    resolve_location_ids(cfg$location, cfg$country, db_path = cfg$db_path)
  ),
  
  tar_target(
    cases,
    query_cases(cfg, locations$location_id)
  ),
  
  tar_target(
    vectors,
    query_vectors(cfg, locations$location_id)
  ),
  
  tar_target(
    environment,
    query_environment(cfg, locations$location_id)
  ),
  
  # ---------------------------------------------------------------------
  # UNCOMMENT when R/db_queries.R gains these functions
  # ---------------------------------------------------------------------
  
  # tar_target(
  #   interventions,
  #   query_interventions(cfg, locations$location_id)
  # ),
  
  # tar_target(
  #   resistance,
  #   query_resistance(cfg, locations$location_id)
  # ),
  
  # tar_target(
  #   burden,
  #   query_burden(cfg, locations$location_id)
  # ),
  
  # tar_target(
  #   socioeconomic,
  #   query_socioeconomic(cfg, locations$location_id)
  # ),
  
  # =========================================================================
  # 2. Analyses
  # =========================================================================
  
  tar_target(
    seasonal,
    analyze_seasonal(cases)
  ),
  
  tar_target(
    vector_climate,
    analyze_vector_climate(
      vectors           = vectors,
      environment       = environment,
      climate_variables = cfg$env_variables,
      group_cols        = c("species_name"),
      aggregation       = "monthly",
      max_lag           = cfg$max_lag_months,
      min_n             = 5L,
      fdr_correct       = TRUE
    )
  ),
  
  # ---------------------------------------------------------------------
  # UNCOMMENT as each module lands in R/
  # ---------------------------------------------------------------------
  
  # tar_target(
  #   climate_correlation,
  #   analyze_climate_correlation(
  #     cases, environment,
  #     climate_variables = cfg$env_variables,
  #     max_lag           = cfg$max_lag_months
  #   )
  # ),
  
  # tar_target(
  #   vector_disease,
  #   analyze_vector_disease(
  #     vectors, cases,
  #     max_lag = cfg$max_lag_months
  #   )
  # ),
  
  # tar_target(
  #   lag_analysis,
  #   analyze_lags(
  #     cases, environment,
  #     max_lag = cfg$max_lag_months
  #   )
  # ),
  
  # tar_target(
  #   intervention_impact,
  #   analyze_interventions(cases, interventions)
  # ),
  
  # tar_target(
  #   resistance_trends,
  #   analyze_resistance(resistance)
  # ),
  
  # tar_target(
  #   glm_nb,
  #   analyze_negbin_glm(cases, environment)
  # ),
  
  # tar_target(
  #   dlnm_fit,
  #   analyze_dlnm(cases, environment,
  #                max_lag_months = cfg$max_lag_months)
  # ),
  
  # =========================================================================
  # 3. Figures
  # =========================================================================
  
  tar_target(
    fig_vector_climate,
    plot_vector_climate(
      result     = vector_climate,
      study_name = cfg$study_name,
      out_dir    = fig_dir
    )
  ),
  
  # ---------------------------------------------------------------------
  # UNCOMMENT as each plot function lands in R/
  # ---------------------------------------------------------------------
  
  # tar_target(
  #   fig_seasonal,
  #   plot_seasonal(seasonal, cfg$study_name, fig_dir)
  # ),
  
  # tar_target(
  #   fig_climate,
  #   plot_climate_correlation(climate_correlation,
  #                            cfg$study_name, fig_dir)
  # ),
  
  # tar_target(
  #   fig_vector_disease,
  #   plot_vector_disease(vector_disease,
  #                       cfg$study_name, fig_dir)
  # ),
  
  # tar_target(
  #   fig_lag,
  #   plot_lag_correlations(lag_analysis,
  #                         cfg$study_name, fig_dir)
  # ),
  
  # tar_target(
  #   fig_intervention,
  #   plot_intervention_impact(intervention_impact,
  #                            cfg$study_name, fig_dir)
  # ),
  
  # tar_target(
  #   fig_resistance,
  #   plot_resistance_trends(resistance_trends,
  #                          cfg$study_name, fig_dir)
  # ),
  
  # tar_target(
  #   fig_dlnm,
  #   plot_dlnm(dlnm_fit, cfg$study_name, fig_dir)
  # ),
  
  # =========================================================================
  # 4. Bundled results
  #
  # A single named list of everything the report needs. tar_quarto() will
  # receive this via `params`. Every time any analysis or figure target
  # changes, this bundle is regenerated and the report is re-rendered.
  # =========================================================================
  
  tar_target(
    analysis_results,
    list(
      cfg            = cfg,
      locations      = locations,
      cases          = cases,
      vectors        = vectors,
      environment    = environment,
      seasonal       = seasonal,
      vector_climate = vector_climate
      # UNCOMMENT as modules are wired in:
      # climate_correlation = climate_correlation,
      # vector_disease      = vector_disease,
      # lag_analysis        = lag_analysis,
      # intervention_impact = intervention_impact,
      # resistance_trends   = resistance_trends,
      # glm_nb              = glm_nb,
      # dlnm_fit            = dlnm_fit,
      # fig_seasonal        = fig_seasonal,
      # fig_vector_climate  = fig_vector_climate,
      # fig_climate         = fig_climate,
      # fig_vector_disease  = fig_vector_disease,
      # fig_lag             = fig_lag,
      # fig_intervention    = fig_intervention,
      # fig_resistance      = fig_resistance,
      # fig_dlnm            = fig_dlnm
    )
  ),
  
  # =========================================================================
  # 5. Report
  #
  # Renders inst/report/analysis_report.qmd with `analysis_results` passed
  # as a parameter. Requires Quarto CLI on PATH.
  # =========================================================================
  
  # UNCOMMENT when inst/report/analysis_report.qmd is ready:
  
  # tar_quarto(
  #   report,
  #   path   = "inst/report/analysis_report.qmd",
  #   quiet  = FALSE,
  #   execute_params = list(results = analysis_results)
  # )
  
  NULL
)