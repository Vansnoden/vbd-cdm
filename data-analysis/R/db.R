# R/db.R

cdm_connect <- function(db_path = NULL) {
  # 1. Explicit path wins.
  if (!is.null(db_path) && nzchar(db_path)) {
    path <- if (fs::is_absolute_path(db_path)) db_path else here::here(db_path)
    if (!fs::file_exists(path)) {
      cli::cli_abort("Database not found at {.file {path}}.")
    }
    return(DBI::dbConnect(RSQLite::SQLite(), path))
  }
  
  # 2. DATABASE_URL takes precedence over SQLite.
  url <- Sys.getenv("DATABASE_URL", unset = "")
  if (nzchar(url)) {
    if (grepl("^postgresql://", url)) {
      return(DBI::dbConnect(RPostgres::Postgres(), dbname = url))
    }
    if (grepl("^sqlite:///", url)) {
      p <- sub("^sqlite:///", "", url)
      return(DBI::dbConnect(RSQLite::SQLite(), p))
    }
    cli::cli_abort("Unsupported DATABASE_URL scheme: {.val {url}}")
  }
  
  # 3. Fallback: SQL_LITE_DB_NAME (default matches what the Python ETL writes).
  # FIX: previous default was "vbd_cdm.sqlite" which never existed.
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

#' Run a SQL query and return a tibble
#'
#' @param sql     SQL string.
#' @param params  Bind parameters (list, NULL, or an empty list).
#' @param con     Existing DBI connection. If NULL, opens one and closes it.
#' @param db_path Optional SQLite path, used only when `con` is NULL.
cdm_query <- function(sql, params = NULL, con = NULL, db_path = NULL) {
  own_con <- is.null(con)
  if (own_con) con <- cdm_connect(db_path)
  on.exit(if (own_con) DBI::dbDisconnect(con), add = TRUE)
  
  # RSQLite rejects an empty list when the SQL has no bind sites. Treat
  # `list()` the same as NULL.
  if (is.list(params) && length(params) == 0) params <- NULL
  
  DBI::dbGetQuery(con, sql, params = params) |> tibble::as_tibble()
}