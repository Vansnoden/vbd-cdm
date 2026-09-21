# R/qa.R
#
# Data QA: what is actually available for this study?
# Returns a plain list of small tibbles.
#
# Missing date strings are represented as "" (empty string), not NA, because
# NA_character_ cannot survive Quarto's YAML 1.2 parameter serialisation
# (see targets::tar_quarto()).

#' Summarize availability across the core data sources
#'
#' @param cases        Output of `query_cases()`.
#' @param vectors      Output of `query_vectors()`.
#' @param environment  Output of `query_environment()`.
#' @param locations    Output of `resolve_location_ids()`.
#' @param cfg          Analysis config.
#' @return A named list of tibbles: `tables`, `species`, `environment_vars`,
#'   `monthly_cases`.
#' @export
summarize_available_data <- function(cases, vectors, environment,
                                     locations, cfg) {
  # Quarto-safe empty string.
  .emptydate <- function(x) {
    if (is.null(x) || length(x) == 0 || is.na(x)) "" else as.character(x)
  }
  
  tbl <- function(name, df) {
    has_date <- "date" %in% names(df) && nrow(df) > 0
    tibble::tibble(
      dataset   = name,
      n_records = nrow(df),
      date_min  = if (has_date) .emptydate(min(df$date, na.rm = TRUE)) else "",
      date_max  = if (has_date) .emptydate(max(df$date, na.rm = TRUE)) else ""
    )
  }
  
  tables <- dplyr::bind_rows(
    tibble::tibble(
      dataset   = "locations",
      n_records = nrow(locations),
      date_min  = "",
      date_max  = ""
    ),
    tbl("cases",       cases),
    tbl("vectors",     vectors),
    tbl("environment", environment)
  )
  
  species <- if ("species_name" %in% names(vectors) && nrow(vectors) > 0) {
    vectors |>
      dplyr::filter(!is.na(.data$species_name), .data$species_name != "") |>
      dplyr::count(.data$species_name, name = "n_records", sort = TRUE)
  } else {
    tibble::tibble(species_name = character(), n_records = integer())
  }
  
  env_vars <- if (nrow(environment) > 0) {
    vars <- intersect(c("t2m", "tp", "d2m"), names(environment))
    tibble::tibble(
      variable = vars,
      n_values = vapply(vars, function(v) sum(!is.na(environment[[v]])),
                        integer(1))
    )
  } else {
    tibble::tibble(variable = character(), n_values = integer())
  }
  
  monthly_cases <- if (nrow(cases) > 0) {
    cases |>
      dplyr::mutate(month_start =
                      lubridate::floor_date(.data$date, "month")) |>
      dplyr::group_by(.data$month_start) |>
      dplyr::summarise(case_count = sum(.data$case_count, na.rm = TRUE),
                       .groups = "drop") |>
      dplyr::arrange(.data$month_start)
  } else {
    tibble::tibble(month_start = as.Date(character()),
                   case_count  = numeric())
  }
  
  list(
    config           = cfg,
    tables           = tables,
    species          = species,
    environment_vars = env_vars,
    monthly_cases    = monthly_cases
  )
}