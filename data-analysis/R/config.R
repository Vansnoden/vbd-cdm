# R/config.R

#' Build a run configuration
#'
#' @param study_name              Human-readable study name.
#' @param location                Location name, or "ALL".
#' @param country                 ISO country code, or "ALL".
#' @param location_ids            Optional integer vector of explicit
#'                                dim_location.location_id values. NULL or
#'                                empty means "resolve by name + country".
#' @param start_year,end_year     Integers.
#' @param disease_code            Optional disease filter.
#' @param include_estimated_cases If FALSE (default), query_cases() excludes
#'                                rows with identification_method_code =
#'                                'ESTIMATE'.
#' @param env_variables           Character vector of fact_environment
#'                                variable names.
#' @param species_filter          Character vector of substrings matched
#'                                case-insensitively against
#'                                dim_vector.species_name (and, as a
#'                                fallback, fact_vector_observation.vector_code).
#'                                NULL or empty means "all species".
#' @param max_lag_months          Integer, maximum lag for lag analyses.
#' @param db_path                 SQLite database path.
#' @param output_dir              Where targets writes figures/data/reports.
new_analysis_config <- function(study_name,
                                location                = "ALL",
                                country                 = "ALL",
                                location_ids            = NULL,
                                start_year              = 1990L,
                                end_year                = 2024L,
                                disease_code            = NULL,
                                include_estimated_cases = FALSE,
                                env_variables           = c("t2m", "tp"),
                                species_filter          = NULL,
                                max_lag_months          = 6L,
                                db_path                 = NULL,
                                output_dir              = "outputs/analysis") {
  
  # Normalise list-shaped inputs coming from YAML.
  # `config::get()` returns YAML `[]` as `list()`, and `as.integer(list())`
  # would error. Convert empty lists to NULL and non-empty lists to
  # plain vectors.
  location_ids <- if (is.null(location_ids) || length(location_ids) == 0)
    NULL else as.integer(unlist(location_ids, use.names = FALSE))
  
  env_variables <- as.character(unlist(env_variables, use.names = FALSE))
  
  species_filter <- if (is.null(species_filter) || length(species_filter) == 0)
    NULL else as.character(unlist(species_filter, use.names = FALSE))
  
  structure(
    list(
      study_name              = study_name,
      location                = location,
      country                 = country,
      location_ids            = location_ids,
      start_year              = as.integer(start_year),
      end_year                = as.integer(end_year),
      disease_code            = disease_code,
      include_estimated_cases = isTRUE(include_estimated_cases),
      env_variables           = env_variables,
      species_filter          = species_filter,
      max_lag_months          = as.integer(max_lag_months),
      db_path                 = db_path,
      output_dir              = output_dir
    ),
    class = "analysis_config"
  )
}