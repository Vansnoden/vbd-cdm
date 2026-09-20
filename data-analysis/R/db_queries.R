# R/db_queries.R
#
# All SQL lives here. Each function returns a tibble. Nothing in this file
# knows about analysis; it is purely a data-access layer.
#
# Every query that accepts `location_ids` builds its location filter via
# .loc_clause(). That helper returns an empty SQL fragment (and no bind
# parameters) when the ID vector is NULL, empty, or larger than SQLite's
# IN-clause parameter budget. Semantically, "all locations" and "no
# location filter" are identical, so this changes nothing except that the
# query succeeds instead of silently returning zero rows.

# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

#' Resolve location IDs from a name / country code, or from an explicit list
#'
#' @param location     Location name, or "ALL".
#' @param country      ISO alpha-2 code, or "ALL".
#' @param db_path      Optional path to the SQLite database.
#' @param explicit_ids Optional integer vector of location_ids. When
#'                     non-empty, the name / country filters are ignored.
#' @return A tibble with `location_id`, `name`, `country_code`,
#'   `admin_level`.
#' @export
resolve_location_ids <- function(location, country, db_path = NULL,
                                 explicit_ids = NULL) {
  
  # Explicit IDs win.
  if (!is.null(explicit_ids) && length(explicit_ids) > 0) {
    id_ph <- .placeholders(length(explicit_ids))
    sql <- paste0(
      "SELECT location_id, name, country_code, admin_level ",
      "FROM dim_location WHERE location_id IN (", id_ph, ") ",
      "ORDER BY location_id"
    )
    df <- cdm_query(sql, as.list(as.integer(explicit_ids)), db_path = db_path)
    if (nrow(df) == 0) {
      cli::cli_abort(c(
        "No locations matched `location_ids` in config.",
        "i" = "IDs supplied: {.val {explicit_ids}}",
        "i" = "Verify against `SELECT location_id, name FROM dim_location;`"
      ))
    }
    missing_ids <- setdiff(as.integer(explicit_ids), df$location_id)
    if (length(missing_ids) > 0) {
      cli::cli_warn(c(
        "Some `location_ids` from config.yml were not found in dim_location.",
        "!" = "Missing: {.val {missing_ids}}"
      ))
    }
    return(df)
  }
  
  # Fall back to name + country matching.
  where  <- character()
  params <- list()
  
  if (!identical(country, "ALL")) {
    where  <- c(where, "country_code = ?")
    params <- c(params, list(country))
  }
  if (!identical(location, "ALL")) {
    where  <- c(where, "(name = ? OR name LIKE ?)")
    params <- c(params, list(location, paste0("%", location, "%")))
  }
  
  sql <- paste0(
    "SELECT location_id, name, country_code, admin_level FROM dim_location",
    if (length(where)) paste0(" WHERE ", paste(where, collapse = " AND ")) else "",
    " ORDER BY name"
  )
  df <- cdm_query(sql, params, db_path = db_path)
  if (nrow(df) == 0) {
    cli::cli_abort(c(
      "No locations matched the name filter.",
      "i" = "location = {.val {location}}, country = {.val {country}}",
      "i" = "Consider setting `location_ids:` in config.yml to pin the study area."
    ))
  }
  # Warn when the name filter matches a suspiciously large set. This usually
  # means the ETL's location merge produced duplicate labels.
  if (nrow(df) > 20) {
    cli::cli_warn(c(
      "Location name filter matched {nrow(df)} rows in dim_location.",
      "!" = "This is likely an ETL side-effect (merged labels).",
      "i" = "Set `location_ids:` in config.yml to avoid silent fan-out."
    ))
  }
  df
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

.placeholders <- function(n) paste(rep("?", n), collapse = ",")

#' Build a location-filter SQL fragment.
#'
#' Returns list(sql = , params = ). When `location_ids` is NULL, empty, or
#' longer than 900 entries, returns an empty fragment. SQLite's hard limit on
#' IN-clause parameters (SQLITE_MAX_VARIABLE_NUMBER) varies between builds;
#' 900 is a safe ceiling. Semantically "all locations" and "no filter" are
#' identical.
#'
#' @noRd
.loc_clause <- function(location_ids, alias = NULL, column = "location_id") {
  if (is.null(location_ids) || length(location_ids) == 0) {
    return(list(sql = "", params = list()))
  }
  if (length(location_ids) > 900) {
    return(list(sql = "", params = list()))
  }
  ph <- .placeholders(length(location_ids))
  prefix <- if (is.null(alias)) column else paste0(alias, ".", column)
  list(
    sql    = glue::glue("AND {prefix} IN ({ph})"),
    params = as.list(location_ids)
  )
}

.year_filter <- function(cfg, params, alias = "t") {
  if (is.null(cfg$start_year) || is.null(cfg$end_year)) {
    return(list(sql = "", params = params))
  }
  list(
    sql    = sprintf("AND %s.year BETWEEN ? AND ?", alias),
    params = c(params, list(cfg$start_year, cfg$end_year))
  )
}

# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

#' Aggregate daily case counts from fact_disease_case
#'
#' Excludes synthetic burden-expansion rows (identification_method_code =
#' 'ESTIMATE') unless `cfg$include_estimated_cases` is TRUE.
#'
#' @param cfg          Analysis config.
#' @param location_ids Integer vector of location IDs, or NULL for all.
#' @return Tibble: date, year, month, week, season, case_count.
#' @export
query_cases <- function(cfg, location_ids = NULL) {
  loc <- .loc_clause(location_ids, alias = "c")
  yf  <- .year_filter(cfg, loc$params, alias = "t")
  params <- yf$params
  
  disease_filter <- ""
  if (!is.null(cfg$disease_code)) {
    disease_filter <- "AND c.disease_code = ?"
    params <- c(params, list(cfg$disease_code))
  }
  
  method_filter <- ""
  if (!isTRUE(cfg$include_estimated_cases)) {
    method_filter <- "AND c.identification_method_code <> 'ESTIMATE'"
  }
  
  sql <- glue::glue("
    SELECT t.date          AS date,
           t.year          AS year,
           t.month         AS month,
           t.week          AS week,
           t.season        AS season,
           SUM(COALESCE(c.case_count, 1)) AS case_count
    FROM fact_disease_case c
    JOIN dim_time t ON c.date_id_reported = t.date_id
    WHERE 1=1
      {loc$sql}
      {yf$sql}
      {disease_filter}
      {method_filter}
    GROUP BY t.date, t.year, t.month, t.week, t.season
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------

#' Vector observations aggregated by date and species
#'
#' Uses a LEFT JOIN on dim_vector so rows whose vector_code did not resolve
#' to a dimension record are still returned (with the raw code as a fallback
#' label). The species filter matches against both dim_vector.species_name
#' and fact_vector_observation.vector_code.
#'
#' @param cfg             Analysis config.
#' @param location_ids    Integer vector of location IDs, or NULL for all.
#' @param species_filter  Optional character vector of substrings (case-
#'                        insensitive). NULL or empty means no filter.
#' @return Tibble: date, year, month, species_name, vector_code, count,
#'   density_per_trap, infection_rate.
#' @export
query_vectors <- function(cfg, location_ids = NULL, species_filter = NULL) {
  loc <- .loc_clause(location_ids, alias = "o")
  
  species_sql    <- ""
  species_params <- list()
  if (!is.null(species_filter) && length(species_filter) > 0) {
    # Each OR clause consumes two bind params: one for species_name, one
    # for vector_code. `rep(x, each = 2)` produces the right order.
    clause <- paste0(
      "(LOWER(COALESCE(v.species_name, '')) LIKE LOWER(?) ",
      "OR LOWER(COALESCE(o.vector_code, '')) LIKE LOWER(?))"
    )
    species_sql <- paste0(
      "AND (",
      paste(rep(clause, length(species_filter)), collapse = " OR "),
      ")"
    )
    species_params <- as.list(rep(paste0("%", species_filter, "%"), each = 2))
  }
  
  yf     <- .year_filter(cfg, list(), alias = "t")
  params <- c(loc$params, species_params, yf$params)
  
  sql <- glue::glue("
    SELECT t.date, t.year, t.month,
           COALESCE(v.species_name, o.vector_code) AS species_name,
           o.vector_code,
           SUM(o.count)             AS count,
           AVG(o.density_per_trap)  AS density_per_trap,
           AVG(o.infection_rate)    AS infection_rate
    FROM fact_vector_observation o
    JOIN dim_time t ON o.date_id = t.date_id
    LEFT JOIN dim_vector v ON o.vector_code = v.vector_code
    WHERE 1=1
      {loc$sql}
      {species_sql}
      {yf$sql}
    GROUP BY t.date, t.year, t.month,
             COALESCE(v.species_name, o.vector_code), o.vector_code
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

#' Environment: long format -> wide
#'
#' When `location_ids` is NULL or larger than the SQLite parameter budget,
#' every environment row in the year window is returned. The pivot then
#' takes a mean across whatever location_ids are present, which for the
#' "all locations" case is a continental mean — useful as a placeholder,
#' not as a physically meaningful series. Per-site climate requires
#' preserving location_id through the query and joining vector sites to
#' their nearest grid cell.
#'
#' @export
query_environment <- function(cfg, location_ids = NULL) {
  loc    <- .loc_clause(location_ids, alias = "e")
  var_ph <- .placeholders(length(cfg$env_variables))
  yf     <- .year_filter(cfg, list(), alias = "t")
  
  params <- c(
    loc$params,
    as.list(cfg$env_variables),
    yf$params
  )
  
  sql <- glue::glue("
    SELECT t.date, t.year, t.month, e.variable, e.value
    FROM fact_environment e
    JOIN dim_time t ON e.date_id = t.date_id
    WHERE 1=1
      {loc$sql}
      AND e.variable IN ({var_ph})
      {yf$sql}
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)
  df$date <- as.Date(df$date)
  
  tidyr::pivot_wider(
    df,
    id_cols     = c(date, year, month),
    names_from  = variable,
    values_from = value,
    values_fn   = mean
  )
}

# ---------------------------------------------------------------------------
# Interventions
# ---------------------------------------------------------------------------

#' Intervention records with start date resolved from dim_time
#'
#' @export
query_interventions <- function(cfg, location_ids = NULL) {
  loc <- .loc_clause(location_ids, alias = "i")
  yf  <- .year_filter(cfg, loc$params, alias = "t")
  params <- yf$params
  
  sql <- glue::glue("
    SELECT i.intervention_id,
           i.intervention_type,
           i.coverage,
           t.date          AS date,
           t.year          AS year,
           t.month         AS month,
           d.disease_name,
           d.disease_code
    FROM fact_intervention i
    JOIN dim_time  t ON i.date_start = t.date_id
    JOIN dim_disease d ON i.target_disease_code = d.disease_code
    WHERE 1=1
      {loc$sql}
      {yf$sql}
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Resistance
# ---------------------------------------------------------------------------

#' Insecticide resistance tests
#'
#' Uses LEFT JOINs on dim_vector / dim_insecticide / dim_resistance_test_type
#' so tests whose FK codes were never resolved are still returned (with the
#' raw code as a fallback label).
#'
#' @export
query_resistance <- function(cfg, location_ids = NULL) {
  loc <- .loc_clause(location_ids, alias = "r")
  yf  <- .year_filter(cfg, loc$params, alias = "t")
  params <- yf$params
  
  sql <- glue::glue("
    SELECT r.resistance_id,
           r.location_id,
           t.date,
           t.year,
           t.month,
           COALESCE(v.species_name,      r.vector_code)       AS species_name,
           COALESCE(ins.insecticide_name, r.insecticide_code) AS insecticide_name,
           COALESCE(ins.chemical_class,  'unknown')           AS chemical_class,
           COALESCE(tt.test_type_name,   r.test_type_code)    AS test_type_name,
           r.vector_code,
           r.insecticide_code,
           r.test_type_code,
           r.concentration,
           r.exposure_time,
           r.n_insect_tested,
           r.n_insect_dead,
           r.mortality,
           r.resistance_status
    FROM fact_insecticide_resistance r
    JOIN dim_time t ON r.date_id = t.date_id
    LEFT JOIN dim_vector              v   ON r.vector_code       = v.vector_code
    LEFT JOIN dim_insecticide         ins ON r.insecticide_code  = ins.insecticide_code
    LEFT JOIN dim_resistance_test_type tt ON r.test_type_code    = tt.test_type_code
    WHERE 1=1
      {loc$sql}
      {yf$sql}
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Bionomics
# ---------------------------------------------------------------------------

#' Vector bionomics observations
#'
#' Uses LEFT JOIN on dim_vector so rows whose vector_code did not resolve are
#' still returned.
#'
#' @export
query_bionomics <- function(cfg, location_ids = NULL) {
  loc <- .loc_clause(location_ids, alias = "b")
  yf  <- .year_filter(cfg, loc$params, alias = "t")
  params <- yf$params
  
  sql <- glue::glue("
    SELECT b.bionomics_id,
           b.location_id,
           t.date, t.year, t.month,
           COALESCE(v.species_name, b.vector_code) AS species_name,
           b.vector_code,
           b.biting_rate,
           b.feeding_preference,
           b.biting_location,
           b.resting_behavior,
           b.larval_site_type
    FROM fact_bionomics b
    JOIN dim_time t ON b.date_id = t.date_id
    LEFT JOIN dim_vector v ON b.vector_code = v.vector_code
    WHERE 1=1
      {loc$sql}
      {yf$sql}
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Burden
# ---------------------------------------------------------------------------

#' Annual disease burden estimates
#'
#' @export
query_burden <- function(cfg, location_ids = NULL) {
  loc <- .loc_clause(location_ids, alias = "b")
  yf  <- .year_filter(cfg, loc$params, alias = "b")
  params <- yf$params
  
  disease_filter <- ""
  if (!is.null(cfg$disease_code)) {
    disease_filter <- "AND b.disease_code = ?"
    params <- c(params, list(cfg$disease_code))
  }
  
  sql <- glue::glue("
    SELECT b.year,
           b.incidence_rate,
           b.prevalence_rate,
           b.mortality_rate,
           b.cases_mean,
           d.disease_name,
           d.disease_code,
           l.name AS location_name
    FROM fact_disease_burden b
    JOIN dim_disease  d ON b.disease_code = d.disease_code
    LEFT JOIN dim_location l ON b.location_id = l.location_id
    WHERE 1=1
      {loc$sql}
      {yf$sql}
      {disease_filter}
    ORDER BY b.year
  ")
  cdm_query(sql, params, db_path = cfg$db_path)
}

# ---------------------------------------------------------------------------
# Socioeconomic
# ---------------------------------------------------------------------------

#' Yearly socioeconomic indicators
#'
#' @export
query_socioeconomic <- function(cfg, location_ids = NULL) {
  loc <- .loc_clause(location_ids, alias = "s")
  yf  <- .year_filter(cfg, loc$params, alias = "s")
  params <- yf$params
  
  sql <- glue::glue("
    SELECT s.year,
           s.indicator_value,
           i.indicator_name,
           i.indicator_code,
           i.unit,
           l.name AS location_name
    FROM fact_socioeconomic_condition s
    JOIN dim_socioeconomic_indicator i
         ON s.indicator_code = i.indicator_code
    LEFT JOIN dim_location l ON s.location_id = l.location_id
    WHERE 1=1
      {loc$sql}
      {yf$sql}
    ORDER BY s.year
  ")
  cdm_query(sql, params, db_path = cfg$db_path)
}