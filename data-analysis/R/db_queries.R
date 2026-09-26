# R/db_queries.R
#
# All SQL lives here. Each function returns a tibble. Nothing in this file
# knows about analysis; it is purely a data-access layer.
#
# Every query that accepts `location_ids` builds its location filter via
# .loc_clause(). That helper returns an empty SQL fragment (and no bind
# parameters) only when the ID vector is NULL or empty. It imposes no
# upper bound on the number of IDs: the full set is passed as a single
# JSON parameter and expanded server-side by the database's JSON array
# function (json_each on SQLite, jsonb_array_elements_text on PostgreSQL).

# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

#' Resolve administrative location IDs from country and location-name filters.
#'
#' Grid cells (admin_level = 'grid') are excluded. The Python consolidation
#' step labels every environment pixel "<region> (Climate Grid)", so a
#' substring filter like "Siaya" matches both the admin row and hundreds of
#' pixel rows. Grid cells carry climate data only — no vector observation,
#' case, or resistance test ever points at one. Passing grid IDs to a fact
#' query silently fans out the filter and empties the result.
#'
#' When `descendants = TRUE` (the default), each name-matched location is
#' expanded to include every descendant in the parent chain. So
#' `locations = ["Siaya"]` returns the Siaya admin1 county, its admin2
#' sub-counties (Alego Usonga, Bondo, Gem, ...), and their admin3 wards —
#' everything a fact query would need to see study-area rows regardless of
#' the level the ETL filed them at.
#'
#' The recursive expansion uses UNION rather than UNION ALL, so a
#' self-referencing or cyclic parent chain cannot cause an infinite loop.
#' The same admin_filter is applied to both the roots and the descendants,
#' which keeps grid cells out of administrative queries and vice versa.
#'
#' @param countries   Character vector of ISO alpha-2 country codes, or NULL.
#' @param locations   Character vector of location names (matched exactly or
#'                    as substrings), or NULL.
#' @param db_path     Optional SQLite database path.
#' @param db_url      Optional full connection URL.
#' @param descendants When TRUE, expand matched locations to include their
#'                    full subtree. Default TRUE — the pipeline almost
#'                    always wants "everything inside the study area".
#' @return Tibble with `location_id`, `name`, `country_code`, `admin_level`.
#' @export
resolve_location_ids <- function(countries = NULL, locations = NULL,
                                 db_path = NULL, db_url = NULL,
                                 descendants = TRUE) {
  .resolve_locations(
    countries    = countries,
    locations    = locations,
    db_path      = db_path,
    db_url       = db_url,
    admin_filter = "COALESCE(admin_level, '') <> 'grid'",
    descendants  = descendants
  )
}

#' Resolve grid-cell location IDs.
#'
#' A companion to `resolve_location_ids()` for the environment query.
#' Returns only rows with `admin_level = 'grid'`.
#'
#' Grid cells are leaves — they have no descendants. The `descendants`
#' argument is present for symmetry but is a no-op in practice.
#'
#' @inheritParams resolve_location_ids
#' @export
#' Resolve grid-cell location IDs inside the study area.
#'
#' Grid cells are named `<lon>/<lat>`, so a country-and-name filter cannot
#' match them directly. This function finds the administrative units whose
#' name matches, expands the parent chain downward, and returns the grid
#' cells that fall under them.
#'
#' If `locations` is NULL, every grid cell in the country is returned,
#' which is what the environment query wants when the study area is the
#' whole country.
#'
#' @inheritParams resolve_location_ids
#' @export
resolve_grid_location_ids <- function(countries = NULL, locations = NULL,
                                      db_path = NULL, db_url = NULL) {
  countries <- normalize_all_list(countries)
  if (!is.null(countries)) countries <- toupper(countries)
  locations <- normalize_all_list(locations)
  
  # No name filter: return every grid cell in the country.
  if (is.null(locations)) {
    return(.resolve_locations(
      countries    = countries,
      locations    = NULL,
      db_path      = db_path,
      db_url       = db_url,
      admin_filter = "admin_level = 'grid'",
      descendants  = FALSE
    ))
  }
  
  backend <- cdm_backend(db_path, db_url)
  
  # ---- Root selection: admin units matching the name filter. ----
  where  <- c("COALESCE(admin_level, '') <> 'grid'")
  params <- list()
  
  if (length(countries) > 0) {
    ph <- .placeholders(length(countries))
    where <- c(where, glue::glue("country_code IN ({ph})"))
    params <- c(params, as.list(countries))
  }
  
  loc_parts <- character()
  for (loc in locations) {
    loc_parts <- c(loc_parts, "(name = ? OR name LIKE ?)")
    params <- c(params, list(loc, paste0("%", loc, "%")))
  }
  where <- c(where, paste0("(", paste(loc_parts, collapse = " OR "), ")"))
  
  root_sql <- paste0(
    "SELECT location_id FROM dim_location WHERE ",
    paste(where, collapse = " AND ")
  )
  roots <- cdm_query(root_sql, params, db_path = db_path, db_url = db_url)
  
  if (nrow(roots) == 0) {
    cli::cli_abort(c(
      "No administrative locations matched the configured filter.",
      "i" = "countries = {.val {countries}}",
      "i" = "locations = {.val {locations}}",
      "i" = "Check `SELECT name, admin_level FROM dim_location " %+%
        "WHERE admin_level <> 'grid';`"
    ))
  }
  
  json_expand <- switch(
    backend,
    sqlite     = "SELECT value      FROM json_each(?)",
    postgresql = "SELECT value::int FROM jsonb_array_elements_text(?::jsonb)",
    stop("Unsupported backend: ", backend)
  )
  
  ids  <- roots$location_id
  json <- jsonlite::toJSON(as.integer(ids), auto_unbox = FALSE)
  
  # ---- Descendant expansion, filtered to grid cells. ----
  # UNION (not UNION ALL) makes the walk safe against any cycle in the
  # parent chain. The outer WHERE restricts the result to grid cells
  # only; intermediate admin levels are traversed but not returned.
  grid_sql <- glue::glue("
    WITH RECURSIVE subtree(location_id) AS (
        SELECT location_id FROM dim_location
        WHERE location_id IN ({json_expand})
        UNION
        SELECT l.location_id
        FROM dim_location l
        JOIN subtree s ON l.parent_location_id = s.location_id
    )
    SELECT DISTINCT
           l.location_id, l.name, l.country_code, l.admin_level
    FROM dim_location l
    JOIN subtree s ON l.location_id = s.location_id
    WHERE l.admin_level = 'grid'
    ORDER BY l.name
  ")
  
  df <- cdm_query(grid_sql, list(json), db_path = db_path, db_url = db_url)
  
  if (nrow(df) == 0) {
    cli::cli_abort(c(
      "No grid cells found under the study area.",
      "i" = "countries = {.val {countries}}",
      "i" = "locations = {.val {locations}}",
      "i" = "{nrow(roots)} admin units matched by name; " %+%
        "none of their descendants is a grid cell.",
      "i" = "Check whether `parent_location_id` is populated on the " %+%
        "grid cells under those admin units."
    ))
  }
  
  df
}

#' @noRd
.resolve_locations <- function(countries, locations, db_path, db_url,
                               admin_filter, descendants = FALSE) {
  countries <- normalize_all_list(countries)
  if (!is.null(countries)) countries <- toupper(countries)
  locations <- normalize_all_list(locations)
  
  # -------------------------------------------------------------------
  # Base selection: rows that directly match the filters.
  # -------------------------------------------------------------------
  where  <- c(admin_filter)
  params <- list()
  
  if (length(countries) > 0) {
    ph <- .placeholders(length(countries))
    where  <- c(where, glue::glue("country_code IN ({ph})"))
    params <- c(params, as.list(countries))
  }
  
  if (length(locations) > 0) {
    loc_parts <- character()
    for (loc in locations) {
      loc_parts <- c(loc_parts, "(name = ? OR name LIKE ?)")
      params <- c(params, list(loc, paste0("%", loc, "%")))
    }
    where <- c(where, paste0("(", paste(loc_parts, collapse = " OR "), ")"))
  }
  
  base_sql <- paste0(
    "SELECT location_id, name, country_code, admin_level FROM dim_location",
    " WHERE ", paste(where, collapse = " AND ")
  )
  base <- cdm_query(base_sql, params, db_path = db_path, db_url = db_url)
  
  if (nrow(base) == 0) {
    cli::cli_abort(c(
      "No locations matched the configured filter.",
      "i" = "countries = {.val {countries}}",
      "i" = "locations = {.val {locations}}",
      "i" = "admin_filter = {admin_filter}",
      "i" = "Check `SELECT DISTINCT country_code FROM dim_location;`, " %+%
        "`SELECT name, admin_level FROM dim_location LIMIT 30;`"
    ))
  }
  
  if (!descendants) {
    if (nrow(base) > 20) {
      cli::cli_warn(c(
        "Location filter matched {nrow(base)} rows in dim_location.",
        "i" = "If this is unexpected, narrow `locations` or `countries` " %+%
          "in config.yml."
      ))
    }
    return(base[order(base$name), , drop = FALSE])
  }
  
  # -------------------------------------------------------------------
  # Descendant expansion: walk the parent_location_id chain downward
  # from each matched root, and return everything reachable.
  # -------------------------------------------------------------------
  backend <- cdm_backend(db_path, db_url)
  json_expand <- switch(
    backend,
    sqlite     = "SELECT value      FROM json_each(?)",
    postgresql = "SELECT value::int FROM jsonb_array_elements_text(?::jsonb)",
    stop("Unsupported backend: ", backend)
  )
  
  ids  <- base$location_id
  json <- jsonlite::toJSON(as.integer(ids), auto_unbox = FALSE)
  
  # The recursive term re-applies `admin_filter` so descendants respect
  # the same level restriction as the roots. For an admin query this
  # excludes grid cells; for a grid query it excludes admin units.
  subtree_sql <- glue::glue("
    WITH RECURSIVE subtree(location_id) AS (
        SELECT location_id FROM dim_location
        WHERE location_id IN ({json_expand})
        UNION
        SELECT l.location_id
        FROM dim_location l
        JOIN subtree s ON l.parent_location_id = s.location_id
        WHERE {admin_filter}
    )
    SELECT DISTINCT
           l.location_id, l.name, l.country_code, l.admin_level
    FROM dim_location l
    JOIN subtree s ON l.location_id = s.location_id
    ORDER BY l.admin_level, l.name
  ")
  
  df <- cdm_query(subtree_sql, list(json),
                  db_path = db_path, db_url = db_url)
  
  if (nrow(df) > 20) {
    cli::cli_warn(c(
      "Location filter expanded to {nrow(df)} rows in dim_location.",
      "i" = "{nrow(base)} matched by name; " %+%
        "{nrow(df) - nrow(base)} added as descendants.",
      "i" = "If this is unexpected, narrow `locations` or `countries` " %+%
        "in config.yml."
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
#' Returns list(sql = , params = ). When `location_ids` is NULL or empty,
#' returns an empty fragment. There is no upper bound on the number of IDs:
#' they are passed as a single JSON-encoded parameter and expanded
#' server-side by the database's JSON array function.
#'
#' @noRd
.loc_clause <- function(location_ids, alias = NULL, column = "location_id",
                        backend = "sqlite") {
  if (is.null(location_ids) || length(location_ids) == 0) {
    return(list(sql = "", params = list()))
  }
  
  location_ids <- location_ids[!is.na(location_ids)]
  if (length(location_ids) == 0) {
    return(list(sql = "", params = list()))
  }
  
  prefix <- if (is.null(alias)) column else paste0(alias, ".", column)
  json   <- jsonlite::toJSON(as.integer(location_ids), auto_unbox = FALSE)
  
  inner <- switch(backend,
                  sqlite     = "SELECT value      FROM json_each(?)",
                  postgresql = "SELECT value::int FROM jsonb_array_elements_text(?::jsonb)",
                  stop("Unsupported backend: ", backend)
  )
  
  list(
    sql    = glue::glue("AND {prefix} IN ({inner})"),
    params = list(json)
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

#' Build a disease-code filter SQL fragment.
#' @noRd
.disease_filter <- function(cfg, alias = "c", column = "disease_code") {
  if (length(cfg$diseases) == 0) {
    return(list(sql = "", params = list()))
  }
  ph <- .placeholders(length(cfg$diseases))
  list(
    sql    = glue::glue("AND {alias}.{column} IN ({ph})"),
    params = as.list(cfg$diseases)
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
#' @export
#' @export
query_cases <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc <- .loc_clause(location_ids, alias = "c", backend = backend)
  yf  <- .year_filter(cfg, loc$params, alias = "t")
  df_ <- .disease_filter(cfg, alias = "c", column = "disease_code")
  
  params <- c(yf$params, df_$params)
  
  method_filter <- ""
  if (!isTRUE(cfg$include_estimated_cases)) {
    method_filter <- "AND c.identification_method_code <> 'ESTIMATE'"
  }
  
  # location_id is preserved so that per-site analyses (intervention
  # pre/post, spatial comparisons) can filter to the study unit rather
  # than a pooled continental average. Pooled consumers (case seasonality,
  # QA summary) re-aggregate across location before use.
  sql <- glue::glue("
    SELECT t.date          AS date,
           t.year          AS year,
           t.month         AS month,
           t.week          AS week,
           t.season        AS season,
           c.location_id   AS location_id,
           SUM(COALESCE(c.case_count, 1)) AS case_count
    FROM fact_disease_case c
    JOIN dim_time t ON c.date_id_reported = t.date_id
    WHERE 1=1
      {loc$sql}
      {yf$sql}
      {df_$sql}
      {method_filter}
    GROUP BY t.date, t.year, t.month, t.week, t.season, c.location_id
    ORDER BY t.date, c.location_id
  ")
  df <- cdm_query_cfg(cfg, sql, params)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}
# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------

#' Vector observations aggregated by date and species
#'
#' The species filter matches exactly (case-insensitive) against
#' dim_vector.species_name. Unknown names abort with a list of what's
#' actually available.
#'
#' @export
query_vectors <- function(cfg, location_ids = NULL, species_filter = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc <- .loc_clause(location_ids, alias = "o", backend = backend)
  
  species_sql    <- ""
  species_params <- list()
  
  if (!is.null(species_filter) && length(species_filter) > 0) {
    catalogue <- cdm_query_cfg(
      cfg,
      "SELECT DISTINCT species_name FROM dim_vector WHERE species_name IS NOT NULL"
    )$species_name
    
    canonical <- vapply(species_filter, function(want) {
      hit <- catalogue[tolower(catalogue) == tolower(want)]
      if (length(hit) == 0) NA_character_ else hit[1]
    }, character(1))
    
    unknown <- species_filter[is.na(canonical)]
    if (length(unknown) > 0) {
      cli::cli_abort(c(
        "species_filter contains names not present in dim_vector.species_name.",
        "!" = "Unknown: {.val {unknown}}",
        "i" = "Available: {.val {sort(catalogue)}}",
        "i" = "Use `species_filter: [All]` to include every species."
      ))
    }
    
    species_sql    <- glue::glue(
      "AND v.species_name IN ({.placeholders(length(canonical))})"
    )
    species_params <- as.list(canonical)
  }
  
  yf     <- .year_filter(cfg, list(), alias = "t")
  params <- c(loc$params, species_params, yf$params)
  
  # location_id is preserved for the same reason as in query_cases():
  # per-site intervention analysis needs it. Pooled consumers
  # (vector seasonality, vector-climate) re-aggregate across location
  # before use.
  sql <- glue::glue("
    SELECT t.date, t.year, t.month,
           o.location_id,
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
    GROUP BY t.date, t.year, t.month, o.location_id,
             COALESCE(v.species_name, o.vector_code), o.vector_code
    ORDER BY t.date, o.location_id
  ")
  df <- cdm_query_cfg(cfg, sql, params)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}
# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

#' Environment: long format -> wide
#'
#' The pivot collapses across whatever location_ids were matched. When
#' location_ids is NULL, this is a mean over every environment row in the
#' year window — a placeholder, not a per-site series.
#'
#' Aborts if any requested variable is entirely absent from the result.
#' A partial miss (some variables present, some not) otherwise surfaces
#' as a downstream `all_of()` error in `analyze_vector_climate()`, which
#' is much harder to read.
#'
#' @export
query_environment <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc    <- .loc_clause(location_ids, alias = "e", backend = backend)
  var_ph <- .placeholders(length(cfg$env_variables))
  yf     <- .year_filter(cfg, list(), alias = "t")
  
  params <- c(loc$params, as.list(cfg$env_variables), yf$params)
  
  sql <- glue::glue("
    SELECT t.date, t.year, t.month, e.variable, e.value
    FROM fact_environment e
    JOIN dim_time t ON e.date_id = t.date_id
    WHERE 1=1
      {loc$sql}
      AND e.variable IN ({var_ph})
      {yf$sql}
  ")
  df <- cdm_query_cfg(cfg, sql, params)
  df$date <- as.Date(df$date)
  
  # --- Partial-miss guard ---------------------------------------------------
  # Whatever of cfg$env_variables exist in fact_environment for the matched
  # locations and years comes back; the rest is silently absent. Report the
  # gap here rather than letting `dplyr::across(all_of(...))` trip on it
  # inside analyze_vector_climate().
  present_vars <- unique(df$variable)
  missing_vars <- setdiff(cfg$env_variables, present_vars)
  if (length(missing_vars) > 0) {
    cli::cli_abort(c(
      "query_environment() returned no rows for some requested variables.",
      "!" = "Missing: {.val {missing_vars}}",
      "i" = "Requested: {.val {cfg$env_variables}}",
      "i" = "Present:   {.val {sort(present_vars)}}",
      "i" = "Run `SELECT DISTINCT variable FROM fact_environment;` and " %+%
        "adjust `env_variables` in config.yml."
    ))
  }
  
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

#' @export
#' @export
query_interventions <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc <- .loc_clause(location_ids, alias = "i", backend = backend)
  yf  <- .year_filter(cfg, loc$params, alias = "t")
  params <- yf$params
  
  sql <- glue::glue("
    SELECT i.intervention_id,
           i.location_id,
           l.name          AS location_name,
           i.intervention_type,
           i.coverage,
           t.date          AS date,
           t.year          AS year,
           t.month         AS month,
           te.date         AS date_end,
           d.disease_name,
           d.disease_code
    FROM fact_intervention i
    JOIN dim_time  t  ON i.date_start          = t.date_id
    LEFT JOIN dim_time te ON i.date_end        = te.date_id
    LEFT JOIN dim_disease  d ON i.target_disease_code = d.disease_code
    LEFT JOIN dim_location l ON i.location_id  = l.location_id
    WHERE 1=1
      {loc$sql}
      {yf$sql}
    ORDER BY t.date
  ")
  df <- cdm_query_cfg(cfg, sql, params)
  if (nrow(df)) {
    df$date     <- as.Date(df$date)
    df$date_end <- as.Date(df$date_end)
  }
  df
}
# ---------------------------------------------------------------------------
# Resistance
# ---------------------------------------------------------------------------

#' @export
query_resistance <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc <- .loc_clause(location_ids, alias = "r", backend = backend)
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
           r.source_code,
           r.concentration,
           r.exposure_time,
           r.n_insect_tested,
           r.n_insect_susceptible,
           r.n_insect_resistant,
           r.n_insect_dead,
           r.vgsc_n_insect_tested,
           r.vgsc995l_vgsc995l_n,
           r.vgsc995l_vgsc995f_n,
           r.vgsc995f_vgsc995f_n,
           r.vgsc995l_vgsc995s_n,
           r.vgsc995s_vgsc995s_n,
           r.vgsc995f_vgsc995s_n,
           r.vgsc_generation,
           r.mortality,
           r.resistance_status
    FROM fact_insecticide_resistance r
    JOIN dim_time t ON r.date_id = t.date_id
    LEFT JOIN dim_vector              v   ON r.vector_code      = v.vector_code
    LEFT JOIN dim_insecticide         ins ON r.insecticide_code = ins.insecticide_code
    LEFT JOIN dim_resistance_test_type tt ON r.test_type_code   = tt.test_type_code
    WHERE 1=1
      {loc$sql}
      {yf$sql}
    ORDER BY t.date
  ")
  df <- cdm_query_cfg(cfg, sql, params)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Bionomics
# ---------------------------------------------------------------------------

#' @export
query_bionomics <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc <- .loc_clause(location_ids, alias = "b", backend = backend)
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
  df <- cdm_query_cfg(cfg, sql, params)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Burden
# ---------------------------------------------------------------------------

#' @export
query_burden <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc <- .loc_clause(location_ids, alias = "b", backend = backend)
  yf  <- .year_filter(cfg, loc$params, alias = "b")
  df_ <- .disease_filter(cfg, alias = "b", column = "disease_code")
  
  params <- c(yf$params, df_$params)
  
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
      {df_$sql}
    ORDER BY b.year
  ")
  cdm_query_cfg(cfg, sql, params)
}

# ---------------------------------------------------------------------------
# Socioeconomic
# ---------------------------------------------------------------------------

#' @export
query_socioeconomic <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc <- .loc_clause(location_ids, alias = "s", backend = backend)
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
  cdm_query_cfg(cfg, sql, params)
}