# R/config.R
#' Build a run configuration
#'
#' @param study_name     Human-readable study name.
#' @param location       Location name, or "ALL".
#' @param country        ISO country code, or "ALL".
#' @param start_year,end_year  Integers.
#' @param disease_code   Optional disease filter.
#' @param env_variables  Vector of variable names in fact_environment.
#' @param db_path        SQlite Database path
#' @param output_dir     Where targets writes.
new_analysis_config <- function(study_name,
                                location       = "ALL",
                                country        = "ALL",
                                start_year     = 1990L,
                                end_year       = 2024L,
                                disease_code   = NULL,
                                env_variables  = c("t2m", "tp", "d2m"),
                                max_lag_months = 6L,
                                db_path        = NULL, 
                                output_dir     = "outputs/analysis") {
  structure(
    list(
      study_name     = study_name,
      location       = location,
      country        = country,
      start_year     = as.integer(start_year),
      end_year       = as.integer(end_year),
      disease_code   = disease_code,
      env_variables  = env_variables,
      max_lag_months = as.integer(max_lag_months),
      db_path        = db_path, 
      output_dir     = output_dir
    ),
    class = "analysis_config"
  )
}