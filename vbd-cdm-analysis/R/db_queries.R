# R/db_queries.R
#
# All SQL lives here. Each function returns a tibble. Nothing in this file
# knows about analysis; it is purely a data-access layer.

# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

#' Resolve location IDs from a location name / country code
#'
#' @param location Location name, or "ALL".
#' @param country  ISO alpha-2 code, or "ALL".
#' @param db_path  Optional path to the SQLite database.
#' @return A tibble with `location_id`, `name`, `country_code`.
#' @export
resolve_location_ids <- function(location, country, db_path = NULL) {
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
    "SELECT location_id, name, country_code FROM dim_location",
    if (length(where)) paste0(" WHERE ", paste(where, collapse = " AND ")) else "",
    " ORDER BY name"
  )
  df <- cdm_query(sql, params, db_path = db_path)
  if (nrow(df) == 0) cli::cli_abort("No locations matched the filter.")
  df
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

.placeholders <- function(n) paste(rep("?", n), collapse = ",")

.year_filter <- function(cfg, params, alias = "t") {
  if (is.null(cfg$start_year) || is.null(cfg$end_year))
    return(list(sql = "", params = params))
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
#' @param cfg          Analysis config (see `new_analysis_config`).
#' @param location_ids Integer vector of location IDs.
#' @return Tibble: date, year, month, week, season, case_count.
#' @export
query_cases <- function(cfg, location_ids) {
  id_ph  <- .placeholders(length(location_ids))
  params <- as.list(location_ids)
  
  yf     <- .year_filter(cfg, params)
  params <- yf$params
  
  disease_filter <- ""
  if (!is.null(cfg$disease_code)) {
    disease_filter <- "AND c.disease_code = ?"
    params         <- c(params, list(cfg$disease_code))
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
    WHERE c.location_id IN ({id_ph})
      {yf$sql}
      {disease_filter}
    GROUP BY t.date, t.year, t.month, t.week, t.season
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)   # <-- FIX
  df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------

#' Vector observations aggregated by date and species
#'
#' @export
query_vectors <- function(cfg, location_ids) {
  id_ph  <- .placeholders(length(location_ids))
  params <- as.list(location_ids)
  yf     <- .year_filter(cfg, params)
  params <- yf$params
  
  sql <- glue::glue("
    SELECT t.date, t.year, t.month,
           v.species_name,
           SUM(o.count)             AS count,
           AVG(o.density_per_trap)  AS density_per_trap,
           AVG(o.infection_rate)    AS infection_rate
    FROM fact_vector_observation o
    JOIN dim_time   t ON o.date_id = t.date_id
    JOIN dim_vector v ON o.vector_code = v.vector_code
    WHERE o.location_id IN ({id_ph})
      {yf$sql}
    GROUP BY t.date, t.year, t.month, v.species_name
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)   # <-- FIX
  df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

#' Environment: long format -> wide
#'
#' @export
query_environment <- function(cfg, location_ids) {
  id_ph  <- .placeholders(length(location_ids))
  var_ph <- .placeholders(length(cfg$env_variables))
  
  params <- c(
    as.list(location_ids),
    as.list(cfg$env_variables),
    list(cfg$start_year, cfg$end_year)
  )
  
  sql <- glue::glue("
    SELECT t.date, t.year, t.month, e.variable, e.value
    FROM fact_environment e
    JOIN dim_time t ON e.date_id = t.date_id
    WHERE e.location_id IN ({id_ph})
      AND e.variable    IN ({var_ph})
      AND t.year BETWEEN ? AND ?
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)   # <-- FIX
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
query_interventions <- function(cfg, location_ids) {
  id_ph  <- .placeholders(length(location_ids))
  params <- as.list(location_ids)
  yf     <- .year_filter(cfg, params)
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
    WHERE i.location_id IN ({id_ph})
      {yf$sql}
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)   # <-- FIX
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Resistance
# ---------------------------------------------------------------------------

#' Insecticide resistance tests
#'
#' @export
query_resistance <- function(cfg, location_ids) {
  id_ph  <- .placeholders(length(location_ids))
  params <- as.list(location_ids)
  yf     <- .year_filter(cfg, params)
  params <- yf$params
  
  sql <- glue::glue("
    SELECT r.resistance_id,
           r.location_id,
           t.date,
           t.year,
           t.month,
           v.species_name,
           ins.insecticide_name,
           ins.chemical_class,
           tt.test_type_name,
           r.concentration,
           r.exposure_time,
           r.n_insect_tested,
           r.n_insect_dead,
           r.mortality,
           r.resistance_status
    FROM fact_insecticide_resistance r
    JOIN dim_time       t   ON r.date_id           = t.date_id
    JOIN dim_vector     v   ON r.vector_code       = v.vector_code
    JOIN dim_insecticide ins ON r.insecticide_code = ins.insecticide_code
    JOIN dim_resistance_test_type tt
         ON r.test_type_code = tt.test_type_code
    WHERE r.location_id IN ({id_ph})
      {yf$sql}
    ORDER BY t.date
  ")
  df <- cdm_query(sql, params, db_path = cfg$db_path)   # <-- FIX
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Burden
# ---------------------------------------------------------------------------

#' Annual disease burden estimates
#'
#' @export
query_burden <- function(cfg, location_ids) {
  id_ph  <- .placeholders(length(location_ids))
  params <- as.list(location_ids)
  yf     <- .year_filter(cfg, params, alias = "b")
  params <- yf$params
  
  disease_filter <- ""
  if (!is.null(cfg$disease_code)) {
    disease_filter <- "AND b.disease_code = ?"
    params         <- c(params, list(cfg$disease_code))
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
    JOIN dim_location l ON b.location_id  = l.location_id
    WHERE b.location_id IN ({id_ph})
      {yf$sql}
      {disease_filter}
    ORDER BY b.year
  ")
  cdm_query(sql, params, db_path = cfg$db_path)         # <-- FIX
}

# ---------------------------------------------------------------------------
# Socioeconomic
# ---------------------------------------------------------------------------

#' Yearly socioeconomic indicators
#'
#' @export
query_socioeconomic <- function(cfg, location_ids) {
  id_ph  <- .placeholders(length(location_ids))
  params <- as.list(location_ids)
  yf     <- .year_filter(cfg, params, alias = "s")
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
    JOIN dim_location l ON s.location_id = l.location_id
    WHERE s.location_id IN ({id_ph})
      {yf$sql}
    ORDER BY s.year
  ")
  cdm_query(sql, params, db_path = cfg$db_path)         # <-- FIX
}