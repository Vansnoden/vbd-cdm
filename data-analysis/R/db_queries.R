# R/db_queries.R
#
# All SQL lives here. Each function returns a tibble. Nothing in this file
# knows about analysis.
#
# Location filters are built by .loc_clause(), which passes the ID vector as
# one JSON parameter expanded server-side (json_each on SQLite,
# jsonb_array_elements_text on PostgreSQL). No upper bound on ID count.
#
# Administrative units and grid cells are resolved separately:
#
#   resolve_location_ids()       -> administrative rows only
#   resolve_grid_location_ids()  -> grid cells only
#
# A grid cell is a raster pixel with a name of "<lon>/<lat>" and no boundary
# code, so it can never be matched by a name filter. Its parent_location_id is
# assigned once, during Python consolidation
# (DataConsolidator._assign_grid_parents), and the study area is then whichever
# cells descend from the administrative units the config names. This file does
# not compute proximity: if a cell has no parent it is unreachable, and that is
# reported rather than worked around.

# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

#' Resolve administrative location IDs from country and location-name filters.
#'
#' Grid cells are excluded: a substring filter like "Siaya" would otherwise
#' match hundreds of environment pixels, and no vector, case or resistance row
#' ever points at one.
#'
#' With `descendants = TRUE` (default) each matched location is expanded to its
#' full subtree, so `locations = ["Siaya"]` returns the county, its sub-counties
#' and their wards. The walk uses UNION, so a cyclic parent chain cannot loop.
#'
#' @param countries   ISO alpha-2 codes, or NULL.
#' @param locations   Location names, matched exactly or as substrings, or NULL.
#' @param db_path     Optional SQLite path.
#' @param db_url      Optional full connection URL.
#' @param descendants Expand matched locations to their subtree. Default TRUE.
#' @return Tibble with location_id, name, country_code, admin_level.
#' @export
resolve_location_ids <- function(countries = NULL, locations = NULL,
                                 db_path = NULL, db_url = NULL,
                                 descendants = TRUE) {
  countries <- normalize_all_list(countries)
  if (!is.null(countries)) countries <- toupper(countries)
  locations <- normalize_all_list(locations)
  
  base <- .loc_rows(
    countries    = countries,
    locations    = locations,
    db_path      = db_path,
    db_url       = db_url,
    admin_filter = "COALESCE(admin_level, '') <> 'grid'"
  )
  
  if (nrow(base) == 0) {
    cli::cli_abort(c(
      "No locations matched the configured filter.",
      "i" = paste0("countries = ", .fmt_vals(countries)),
      "i" = paste0("locations = ", .fmt_vals(locations)),
      "i" = paste0(
        "Check: SELECT name, admin_level, country_code FROM dim_location ",
        "WHERE COALESCE(admin_level,'') <> 'grid' LIMIT 30;"
      )
    ))
  }
  
  if (!descendants) {
    .warn_many(base)
    return(base[order(base$name), , drop = FALSE])
  }
  
  out <- .expand_subtree(
    seed         = base,
    db_path      = db_path,
    db_url       = db_url,
    backend      = cdm_backend(db_path, db_url),
    admin_filter = "COALESCE(l.admin_level, '') <> 'grid'"
  )
  
  if (nrow(out) > 20) {
    cli::cli_warn(c(
      paste0("Location filter expanded to ", nrow(out), " rows in dim_location."),
      "i" = paste0(nrow(base), " matched by name; ",
                   nrow(out) - nrow(base), " added as descendants."),
      "i" = "If this is unexpected, narrow `locations` or `countries` in config.yml."
    ))
  }
  out
}

#' Resolve grid-cell location IDs for the environment query.
#'
#' Walks `parent_location_id` downward from the study's administrative units and
#' keeps the rows with `admin_level = 'grid'`. Nothing else: a grid cell's parent
#' is assigned once during Python consolidation, and a cell that has none cannot
#' belong to a study area by any definition this side is entitled to invent.
#'
#' Fails with a diagnosis rather than an empty tibble, because the three causes
#' of zero rows need three different fixes.
#'
#' @inheritParams resolve_location_ids
#' @return Tibble with location_id, name, country_code, admin_level.
#' @export
resolve_grid_location_ids <- function(countries = NULL, locations = NULL,
                                      db_path = NULL, db_url = NULL) {
  countries <- normalize_all_list(countries)
  if (!is.null(countries)) countries <- toupper(countries)
  locations <- normalize_all_list(locations)
  backend   <- cdm_backend(db_path, db_url)
  
  roots <- .loc_rows(
    countries    = countries,
    locations    = locations,
    db_path      = db_path,
    db_url       = db_url,
    admin_filter = "COALESCE(admin_level, '') <> 'grid'"
  )
  
  if (nrow(roots) == 0) {
    cli::cli_abort(c(
      "No administrative locations matched the configured filter.",
      "i" = paste0("countries = ", .fmt_vals(countries)),
      "i" = paste0("locations = ", .fmt_vals(locations)),
      "i" = paste0(
        "Check: SELECT name, admin_level, country_code FROM dim_location ",
        "WHERE COALESCE(admin_level,'') <> 'grid' LIMIT 30;"
      )
    ))
  }
  
  admin <- .expand_subtree(
    seed         = roots,
    db_path      = db_path,
    db_url       = db_url,
    backend      = backend,
    admin_filter = "COALESCE(l.admin_level, '') <> 'grid'"
  )
  
  grid <- .grid_descendants(as.integer(admin$location_id), db_path, db_url, backend)
  
  if (nrow(grid) > 0) {
    cli::cli_inform(c(
      "v" = paste0("Grid cells under the study area: ", nrow(grid), " cell(s), ",
                   "reached through parent_location_id."),
      "i" = paste0(nrow(admin), " administrative unit(s) in scope.")
    ))
    return(grid)
  }
  
  n_grid <- cdm_query(
    "SELECT COUNT(*) AS n FROM dim_location
      WHERE COALESCE(admin_level, '') = 'grid'",
    db_path = db_path, db_url = db_url)$n[[1]]
  
  n_orphan <- cdm_query(
    "SELECT COUNT(*) AS n FROM dim_location
      WHERE COALESCE(admin_level, '') = 'grid' AND parent_location_id IS NULL",
    db_path = db_path, db_url = db_url)$n[[1]]
  
  cause <- if (n_grid == 0) {
    paste0(
      "dim_location holds no admin_level = 'grid' rows. The MAP and ERA5 ",
      "sources have not been ingested, so there is no environment grid to read."
    )
  } else if (n_orphan > 0) {
    paste0(
      n_orphan, " of ", n_grid, " grid cell(s) have parent_location_id = NULL. ",
      "A grid cell is placed by the Python consolidation step ",
      "(DataConsolidator._assign_grid_parents), which assigns each cell to its ",
      "administrative unit. Re-run consolidation with the ingest skipped: ",
      "`python main.py --skip-ingest`."
    )
  } else {
    paste0(
      "All ", n_grid, " grid cell(s) are parented, but none of their ancestors ",
      "is in the study area. Widen `locations` or `countries` in config.yml, or ",
      "check that the cells fall under the named administrative units: ",
      "SELECT p.name AS parent, COUNT(*) FROM dim_location g ",
      "JOIN dim_location p ON g.parent_location_id = p.location_id ",
      "WHERE g.admin_level = 'grid' GROUP BY p.name ORDER BY 2 DESC LIMIT 20;"
    )
  }
  
  cli::cli_abort(c(
    "No grid cells found under the study area.",
    "i" = paste0("countries = ", .fmt_vals(countries)),
    "i" = paste0("locations = ", .fmt_vals(locations)),
    "i" = paste0(nrow(admin), " administrative unit(s) in scope."),
    "x" = cause
  ))
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

.placeholders <- function(n) paste(rep("?", n), collapse = ",")

#' Render a filter value for a cli message. Never returns NULL.
#' @noRd
.fmt_vals <- function(x) {
  if (is.null(x) || length(x) == 0) return("(all)")
  paste0("(", paste(sprintf('"%s"', x), collapse = ", "), ")")
}

#' Warn when a filter matched more rows than a name filter should.
#' @noRd
.warn_many <- function(df, limit = 20L) {
  if (nrow(df) > limit) {
    cli::cli_warn(c(
      paste0("Location filter matched ", nrow(df), " rows in dim_location."),
      "i" = "If this is unexpected, narrow `locations` or `countries` in config.yml."
    ))
  }
  invisible(df)
}

#' The JSON-array expansion expression for a backend.
#' @noRd
.json_expand_sql <- function(backend) {
  switch(
    backend,
    sqlite     = "SELECT value      FROM json_each(?)",
    postgresql = "SELECT value::int FROM jsonb_array_elements_text(?::jsonb)",
    stop("Unsupported backend: ", backend)
  )
}

#' Base selection of dim_location rows matching the filters.
#' @noRd
.loc_rows <- function(countries, locations, db_path, db_url, admin_filter) {
  where  <- c(admin_filter)
  params <- list()
  
  if (length(countries) > 0) {
    where  <- c(where, glue::glue("country_code IN ({.placeholders(length(countries))})"))
    params <- c(params, as.list(countries))
  }
  
  if (length(locations) > 0) {
    parts <- character()
    for (loc in locations) {
      parts  <- c(parts, "(name = ? OR name LIKE ?)")
      params <- c(params, list(loc, paste0("%", loc, "%")))
    }
    where <- c(where, paste0("(", paste(parts, collapse = " OR "), ")"))
  }
  
  sql <- paste0(
    "SELECT location_id, name, country_code, admin_level FROM dim_location WHERE ",
    paste(where, collapse = " AND ")
  )
  cdm_query(sql, params, db_path = db_path, db_url = db_url)
}

#' Expand a set of locations to everything reachable down the parent chain.
#' @noRd
.expand_subtree <- function(seed, db_path, db_url, backend, admin_filter) {
  if (nrow(seed) == 0) return(seed)
  
  json <- jsonlite::toJSON(as.integer(seed$location_id), auto_unbox = FALSE)
  sql  <- glue::glue("
    WITH RECURSIVE subtree(location_id) AS (
        SELECT location_id FROM dim_location
        WHERE location_id IN ({.json_expand_sql(backend)})
        UNION
        SELECT l.location_id
        FROM dim_location l
        JOIN subtree s ON l.parent_location_id = s.location_id
        WHERE {admin_filter}
    )
    SELECT DISTINCT l.location_id, l.name, l.country_code, l.admin_level
    FROM dim_location l
    JOIN subtree s ON l.location_id = s.location_id
    ORDER BY l.admin_level, l.name
  ")
  cdm_query(sql, list(json), db_path = db_path, db_url = db_url)
}

#' Grid cells reachable down the parent chain from the given roots.
#' @noRd
.grid_descendants <- function(study_ids, db_path, db_url, backend) {
  empty <- tibble::tibble(
    location_id = integer(), name = character(),
    country_code = character(), admin_level = character()
  )
  if (length(study_ids) == 0) return(empty)
  
  json <- jsonlite::toJSON(as.integer(study_ids), auto_unbox = FALSE)
  sql  <- glue::glue("
    WITH RECURSIVE subtree(location_id) AS (
        SELECT location_id FROM dim_location
        WHERE location_id IN ({.json_expand_sql(backend)})
        UNION
        SELECT l.location_id
        FROM dim_location l
        JOIN subtree s ON l.parent_location_id = s.location_id
    )
    SELECT DISTINCT l.location_id, l.name, l.country_code, l.admin_level
    FROM dim_location l
    JOIN subtree s ON l.location_id = s.location_id
    WHERE l.admin_level = 'grid'
    ORDER BY l.name
  ")
  cdm_query(sql, list(json), db_path = db_path, db_url = db_url)
}

#' Build a location-filter SQL fragment.
#'
#' Returns list(sql, params). Empty fragment when `location_ids` is NULL or
#' empty. IDs travel as one JSON parameter and are expanded server-side.
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
  
  list(
    sql    = glue::glue("AND {prefix} IN ({.json_expand_sql(backend)})"),
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

#' Daily case counts from fact_disease_case, per location.
#'
#' Excludes synthetic rows (identification_method_code = 'ESTIMATE') unless
#' cfg$include_estimated_cases is TRUE. location_id is preserved so per-site
#' analyses can use it; pooled consumers re-aggregate.
#'
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

#' Vector observations aggregated by date, location and species.
#'
#' The species filter matches exactly (case-insensitive) against
#' dim_vector.species_name. Unknown names abort with what is available.
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
        "!" = paste0("Unknown: ", .fmt_vals(unknown)),
        "i" = paste0("Available: ", .fmt_vals(sort(catalogue))),
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
  
  sql <- glue::glue("
    SELECT t.date, t.year, t.month,
           o.location_id,
           COALESCE(v.species_name, o.vector_code) AS species_name,
           o.vector_code,
           SUM(o.count)            AS count,
           AVG(o.density_per_trap) AS density_per_trap,
           AVG(o.infection_rate)   AS infection_rate
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

#' Environment in wide format: one row per date, one column per variable.
#'
#' Aggregated **in SQL** to the spatial mean per (date, variable). The previous
#' version selected every grid-cell row and pivoted in R — 2338 cells x ~4000
#' days x 2 variables is ~19M rows and several GB, for 8036 distinct values.
#' The mean is identical either way.
#'
#' This is a mean over the grid cells in scope. Pass one cell to `location_ids`
#' for a per-site series, or use `query_environment_cells()`.
#'
#' Aborts if any requested variable is entirely absent, so a partial miss
#' surfaces here rather than as an opaque `all_of()` error downstream.
#'
#' @export
query_environment <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc     <- .loc_clause(location_ids, alias = "e", backend = backend)
  yf      <- .year_filter(cfg, list(), alias = "t")
  
  params <- c(loc$params, as.list(cfg$env_variables), yf$params)
  
  sql <- glue::glue("
    SELECT t.date       AS date,
           t.year       AS year,
           t.month      AS month,
           e.variable   AS variable,
           AVG(e.value) AS value
    FROM fact_environment e
    JOIN dim_time t ON e.date_id = t.date_id
    WHERE 1=1
      {loc$sql}
      AND e.variable IN ({.placeholders(length(cfg$env_variables))})
      {yf$sql}
    GROUP BY t.date, t.year, t.month, e.variable
    ORDER BY t.date, e.variable
  ")
  df <- cdm_query_cfg(cfg, sql, params)
  
  if (nrow(df) == 0) {
    cli::cli_abort(c(
      "query_environment() returned no rows.",
      "i" = paste0("env_variables = ", .fmt_vals(cfg$env_variables)),
      "i" = paste0(length(location_ids), " grid location(s) in scope."),
      "i" = paste0(
        "Check: SELECT variable, COUNT(*) FROM fact_environment GROUP BY variable;"
      )
    ))
  }
  df$date <- as.Date(df$date)
  
  present_vars <- unique(df$variable)
  missing_vars <- setdiff(cfg$env_variables, present_vars)
  if (length(missing_vars) > 0) {
    cli::cli_abort(c(
      "query_environment() returned no rows for some requested variables.",
      "!" = paste0("Missing:  ", .fmt_vals(missing_vars)),
      "i" = paste0("Requested: ", .fmt_vals(cfg$env_variables)),
      "i" = paste0("Present:   ", .fmt_vals(sort(present_vars))),
      "i" = paste0(
        "Run `SELECT DISTINCT variable FROM fact_environment;` and adjust ",
        "`env_variables` in config.yml."
      )
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

#' Environment per grid cell, in long format.
#'
#' The per-site counterpart to `query_environment()`: keeps `location_id` so a
#' vector site can be paired with its own climate trace via
#' `attach_grid_cells()`. Not wired into the pipeline — it exists so the
#' per-site join has a query to draw on.
#'
#' @export
query_environment_cells <- function(cfg, location_ids = NULL) {
  backend <- cdm_backend(cfg$db_path, cfg$db_url)
  loc     <- .loc_clause(location_ids, alias = "e", backend = backend)
  yf      <- .year_filter(cfg, list(), alias = "t")
  
  params <- c(loc$params, as.list(cfg$env_variables), yf$params)
  
  sql <- glue::glue("
    SELECT e.location_id AS location_id,
           t.date        AS date,
           t.year        AS year,
           t.month       AS month,
           e.variable    AS variable,
           AVG(e.value)  AS value
    FROM fact_environment e
    JOIN dim_time t ON e.date_id = t.date_id
    WHERE 1=1
      {loc$sql}
      AND e.variable IN ({.placeholders(length(cfg$env_variables))})
      {yf$sql}
    GROUP BY e.location_id, t.date, t.year, t.month, e.variable
    ORDER BY e.location_id, t.date
  ")
  df <- cdm_query_cfg(cfg, sql, params)
  if (nrow(df)) df$date <- as.Date(df$date)
  df
}

# ---------------------------------------------------------------------------
# Interventions
# ---------------------------------------------------------------------------

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
    JOIN dim_time t ON i.date_start = t.date_id
    LEFT JOIN dim_time te ON i.date_end = te.date_id
    LEFT JOIN dim_disease  d ON i.target_disease_code = d.disease_code
    LEFT JOIN dim_location l ON i.location_id = l.location_id
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
    JOIN dim_socioeconomic_indicator i ON s.indicator_code = i.indicator_code
    LEFT JOIN dim_location l ON s.location_id = l.location_id
    WHERE 1=1
      {loc$sql}
      {yf$sql}
    ORDER BY s.year
  ")
  cdm_query_cfg(cfg, sql, params)
}