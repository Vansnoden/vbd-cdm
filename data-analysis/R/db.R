# R/db.R

#' Detect the SQL dialect implied by the current connection settings.
#'
#' Priority mirrors cdm_connect(): explicit db_url, then explicit db_path,
#' then DATABASE_URL. Falls back to "sqlite" when nothing is set.
#'
#' @param db_path Optional explicit SQLite path.
#' @param db_url  Optional full connection URL.
#' @return One of "sqlite" or "postgresql".
#' @export
cdm_backend <- function(db_path = NULL, db_url = NULL) {
  if (!is.null(db_url) && nzchar(db_url)) {
    if (grepl("^postgresql://", db_url)) return("postgresql")
    if (grepl("^sqlite:///", db_url))   return("sqlite")
  }
  if (!is.null(db_path) && nzchar(db_path)) return("sqlite")
  url <- Sys.getenv("DATABASE_URL", unset = "")
  if (grepl("^postgresql://", url)) return("postgresql")
  "sqlite"
}

#' Connect from a full URL string.
#' @noRd
.connect_from_url <- function(url) {
  if (grepl("^postgresql://", url)) {
    return(DBI::dbConnect(RPostgres::Postgres(), dbname = url))
  }
  if (grepl("^sqlite:///", url)) {
    p <- sub("^sqlite:///", "", url)
    return(DBI::dbConnect(RSQLite::SQLite(), p))
  }
  cli::cli_abort("Unsupported DATABASE_URL scheme: {.val {url}}")
}

#' Open a DBI connection to the CDM.
#'
#' Resolution order:
#'   1. `db_url` argument (Postgres or SQLite URL)
#'   2. `db_path` argument (SQLite file path)
#'   3. `DATABASE_URL` environment variable
#'   4. `SQL_LITE_DB_NAME` environment variable (default "vbd_cdm.db")
#'      searched in the project root and its parent.
#'
#' @export
cdm_connect <- function(db_path = NULL, db_url = NULL) {
  # 1. Explicit URL wins.
  if (!is.null(db_url) && nzchar(db_url)) {
    return(.connect_from_url(db_url))
  }
  
  # 2. Explicit SQLite path.
  if (!is.null(db_path) && nzchar(db_path)) {
    path <- if (fs::is_absolute_path(db_path)) db_path else here::here(db_path)
    if (!fs::file_exists(path)) {
      cli::cli_abort("Database not found at {.file {path}}.")
    }
    return(DBI::dbConnect(RSQLite::SQLite(), path))
  }
  
  # 3. DATABASE_URL environment variable.
  url <- Sys.getenv("DATABASE_URL", unset = "")
  if (nzchar(url)) return(.connect_from_url(url))
  
  # 4. SQL_LITE_DB_NAME fallback.
  db_name <- Sys.getenv("SQL_LITE_DB_NAME", unset = "vbd_cdm.db")
  candidates <- c(here::here(db_name), here::here("..", db_name))
  hit <- candidates[fs::file_exists(candidates)]
  if (length(hit) == 0) {
    cli::cli_abort(c(
      "SQLite database not found.",
      "i" = "Looked for {.file {db_name}} in:",
      " " = paste0("* ", candidates, collapse = "\n")
    ))
  }
  DBI::dbConnect(RSQLite::SQLite(), hit[1])
}

#' Run a SQL query and return a tibble.
#'
#' @param sql     SQL string.
#' @param params  Bind parameters (list, NULL, or an empty list).
#' @param con     Existing DBI connection. If NULL, opens one and closes it.
#' @param db_path Optional SQLite path, used only when `con` is NULL.
#' @param db_url  Optional full URL, used only when `con` is NULL.
cdm_query <- function(sql, params = NULL, con = NULL,
                      db_path = NULL, db_url = NULL) {
  own_con <- is.null(con)
  if (own_con) con <- cdm_connect(db_path, db_url)
  on.exit(if (own_con) DBI::dbDisconnect(con), add = TRUE)
  
  if (is.list(params) && length(params) == 0) params <- NULL
  if (!is.null(params)) params <- unname(params)
  
  DBI::dbGetQuery(con, sql, params = params) |> tibble::as_tibble()
}

#' Query helper for functions that carry a cfg object.
#' @noRd
cdm_query_cfg <- function(cfg, sql, params = NULL) {
  cdm_query(sql, params, db_path = cfg$db_path, db_url = cfg$db_url)
}