# R/config.R
#
# Configuration layer. Reads config.yml directly via yaml::read_yaml() and
# produces the `cfg` object that every target above Layer 1 receives.
#
# Two top-level blocks:
#   vbd-cdm-analysis : what to analyse
#   database         : where the CDM lives
#
# The `config` package is NOT used. Its config::get() requires a `default:`
# block at the top level of config.yml as a fallback, which conflicts with
# the two-block layout. Reading the file directly with yaml::read_yaml()
# avoids the reshuffling that config::get() would otherwise force.

# ---------------------------------------------------------------------------
# Internal: read config.yml once per call
# ---------------------------------------------------------------------------

.read_yaml_config <- function(path = "config.yml") {
  yaml_path <- here::here(path)
  if (!fs::file_exists(yaml_path)) {
    cli::cli_abort("config.yml not found at {.file {yaml_path}}.")
  }
  yaml::read_yaml(yaml_path)
}

# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

#' Read a required value from the analysis config block.
read_cfg <- function(key, config_name = "vbd-cdm-analysis", path = "config.yml") {
  cfg_data <- .read_yaml_config(path)
  block <- cfg_data[[config_name]]
  if (is.null(block)) {
    cli::cli_abort("Config block {.val {config_name}} not found in config.yml.")
  }
  val <- block[[key]]
  if (is.null(val)) {
    cli::cli_abort(c(
      "Required config key {.val {key}} not found in {.val {config_name}}.",
      "i" = "Add it to config.yml or use read_cfg_opt() with a default."
    ))
  }
  val
}

#' Read an optional value from the analysis config block.
read_cfg_opt <- function(key, default = NULL, config_name = "vbd-cdm-analysis",
                         path = "config.yml") {
  cfg_data <- .read_yaml_config(path)
  block <- cfg_data[[config_name]]
  if (is.null(block)) return(default)
  val <- block[[key]]
  if (is.null(val)) default else val
}

#' Read the sibling `database:` block from config.yml.
read_db_block <- function(path = "config.yml") {
  cfg_data <- .read_yaml_config(path)
  cfg_data$database
}

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

`%||%` <- function(a, b) if (is.null(a)) b else a

#' Normalise a YAML list to NULL-or-character-vector.
#'
#' `NULL`, `[]`, `["ALL"]`, `["all"]`, and `["*"]` all mean "no filter" and
#' return NULL. Mixing "ALL" with explicit values is an error.
#'
#' @noRd
normalize_all_list <- function(x) {
  if (is.null(x)) return(NULL)
  if (is.list(x) && length(x) == 0) return(NULL)
  vals <- unlist(x, use.names = FALSE)
  if (length(vals) == 0) return(NULL)
  vals <- trimws(as.character(vals))
  vals <- vals[nzchar(vals)]
  if (length(vals) == 0) return(NULL)
  lower <- tolower(vals)
  all_tokens <- c("all", "any", "*")
  if (all(lower %in% all_tokens)) return(NULL)
  if (any(lower %in% all_tokens)) {
    cli::cli_abort(c(
      "Cannot mix 'ALL' with explicit values in the same list.",
      "i" = "Use either `[\"ALL\"]` or a list of specific values, not both."
    ))
  }
  vals
}

#' Coerce a YAML block into a named list.
#'
#' Handles both forms:
#'   sqlite:
#'     db_path: "..."          -> list(db_path = "...")
#'
#'   sqlite:
#'     - db_path: "..."        -> list(db_path = "...")
#'
#' @noRd
.normalize_db_block <- function(x) {
  if (is.null(x) || length(x) == 0) return(NULL)
  if (!is.list(x)) return(NULL)
  # Plain dict — names are non-empty for every element.
  if (!is.null(names(x)) && all(nzchar(names(x)))) return(x)
  # List of single-key dicts — merge.
  merged <- list()
  for (item in x) {
    if (is.list(item) && !is.null(names(item))) {
      merged <- c(merged, item)
    }
  }
  if (length(merged) == 0) NULL else merged
}

#' Resolve the `database:` block into (db_path, db_url).
#'
#' Priority: postgres block > sqlite block > DATABASE_URL env var.
#' Returns list(db_path = , db_url = ), either of which may be NULL.
#'
#' @noRd
resolve_database <- function(db_block) {
  db_block <- .normalize_db_block(db_block)
  
  if (!is.null(db_block) && !is.null(db_block$postgres)) {
    pg <- .normalize_db_block(db_block$postgres)
    required <- c("host", "port", "username", "password")
    if (!is.null(pg) && all(required %in% names(pg))) {
      dbname <- pg$dbname %||% "vbd_cdm"
      url <- sprintf("postgresql://%s:%s@%s:%s/%s",
                     pg$username, pg$password, pg$host, pg$port, dbname)
      return(list(db_path = NULL, db_url = url))
    }
    cli::cli_warn(c(
      "`database.postgres` is present but incomplete.",
      "i" = "Required: {.field host}, {.field port}, {.field username}, {.field password}.",
      "i" = "Falling back to sqlite / DATABASE_URL."
    ))
  }
  
  if (!is.null(db_block) && !is.null(db_block$sqlite)) {
    sq <- .normalize_db_block(db_block$sqlite)
    if (!is.null(sq) && !is.null(sq$db_path) && nzchar(sq$db_path)) {
      return(list(db_path = sq$db_path, db_url = NULL))
    }
  }
  
  url <- Sys.getenv("DATABASE_URL", unset = "")
  if (nzchar(url)) return(list(db_path = NULL, db_url = url))
  
  list(db_path = NULL, db_url = NULL)
}

# ---------------------------------------------------------------------------
# new_analysis_config()
# ---------------------------------------------------------------------------

#' Build a run configuration
#'
#' @param study_name              Human-readable study name.
#' @param countries               Character vector of ISO alpha-2 codes, or
#'                                NULL / "ALL" for every country.
#' @param locations               Character vector of location names, matched
#'                                exactly or as substrings against
#'                                dim_location.name. NULL / "ALL" for every
#'                                location inside the country filter.
#' @param start_year,end_year     Integers.
#' @param diseases                Character vector of dim_disease.disease_code
#'                                values. NULL / "ALL" for every disease.
#' @param include_estimated_cases If FALSE (default), query_cases() excludes
#'                                rows with identification_method_code =
#'                                'ESTIMATE'.
#' @param env_variables           Character vector of fact_environment
#'                                variable names.
#' @param species_filter          Character vector of species names matched
#'                                against dim_vector.species_name.
#'                                NULL / "ALL" for every species.
#' @param max_lag_months          Integer, maximum lag for lag analyses.
#' @param db_path                 SQLite database path (may be NULL).
#' @param db_url                  Full connection URL for Postgres (may be NULL).
#' @param output_dir              Where targets writes figures/data/reports.
new_analysis_config <- function(study_name,
                                countries               = NULL,
                                locations               = NULL,
                                start_year              = 1990L,
                                end_year                = 2024L,
                                diseases                = NULL,
                                include_estimated_cases = FALSE,
                                env_variables           = c("t2m", "tp"),
                                species_filter          = NULL,
                                max_lag_months          = 6L,
                                db_path                 = NULL,
                                db_url                  = NULL,
                                output_dir              = "outputs/analysis") {
  
  countries <- normalize_all_list(countries)
  if (!is.null(countries)) countries <- toupper(countries)
  
  locations <- normalize_all_list(locations)
  diseases  <- normalize_all_list(diseases)
  
  env_variables <- as.character(unlist(env_variables, use.names = FALSE))
  if (length(env_variables) == 0) {
    cli::cli_abort("`env_variables` must contain at least one variable name.")
  }
  
  species_filter <- normalize_all_list(species_filter)
  
  structure(
    list(
      study_name              = study_name,
      countries               = countries,
      locations               = locations,
      start_year              = as.integer(start_year),
      end_year                = as.integer(end_year),
      diseases                = diseases,
      include_estimated_cases = isTRUE(include_estimated_cases),
      env_variables           = env_variables,
      species_filter          = species_filter,
      max_lag_months          = as.integer(max_lag_months),
      db_path                 = db_path,
      db_url                  = db_url,
      output_dir              = output_dir
    ),
    class = "analysis_config"
  )
}