# R/analysis_seasonal.R

#' Compute monthly, year-over-year, and decomposition summaries
analyze_seasonal <- function(cases, value_col = "case_count") {
  if (is.null(cases) || nrow(cases) < 2) {
    return(list(error = "Not enough data for seasonal analysis."))
  }
  
  cases <- cases |>
    dplyr::mutate(
      date  = as.Date(date),
      month = lubridate::month(date),
      year  = lubridate::year(date)
    )
  
  monthly <- cases |>
    dplyr::group_by(month) |>
    dplyr::summarise(
      mean  = mean(.data[[value_col]], na.rm = TRUE),
      sd    = stats::sd(.data[[value_col]], na.rm = TRUE),
      n     = dplyr::n(),
      .groups = "drop"
    ) |>
    dplyr::mutate(
      month_name = month.abb[month],
      month      = factor(month, levels = 1:12, labels = month.abb)
    )
  
  yoy <- if (dplyr::n_distinct(cases$year) > 1) {
    cases |>
      dplyr::group_by(year, month) |>
      dplyr::summarise(mean = mean(.data[[value_col]], na.rm = TRUE),
                       .groups = "drop")
  } else {
    tibble::tibble()
  }
  
  ts_decomp <- NULL
  if (nrow(cases) >= 24) {
    ts <- cases |>
      dplyr::arrange(date) |>
      dplyr::pull(.data[[value_col]]) |>
      stats::ts(frequency = 12)
    ts_decomp <- tryCatch(
      stats::decompose(ts, type = "additive"),
      error = function(e) NULL
    )
  }
  
  list(monthly = monthly, yoy = yoy, decomposition = ts_decomp)
}

#' Plot seasonal patterns
plot_seasonal <- function(seasonal, study_name, out_dir) {
  if (!is.null(seasonal$error)) return(invisible(NULL))
  
  p1 <- ggplot2::ggplot(seasonal$monthly,
                        ggplot2::aes(x = month, y = mean)) +
    ggplot2::geom_col(fill = "#3B82F6", alpha = 0.8) +
    ggplot2::geom_errorbar(ggplot2::aes(ymin = mean - sd, ymax = mean + sd),
                           width = 0.2) +
    ggplot2::labs(
      title = glue::glue("Seasonal pattern: {study_name}"),
      x = NULL, y = "Average cases"
    ) +
    theme_cdm()
  
  p2 <- if (nrow(seasonal$yoy) > 0) {
    ggplot2::ggplot(seasonal$yoy,
                    ggplot2::aes(x = month, y = mean, color = factor(year))) +
      ggplot2::geom_line(linewidth = 0.8) +
      ggplot2::geom_point() +
      ggplot2::scale_x_continuous(breaks = 1:12, labels = month.abb) +
      ggplot2::labs(x = NULL, y = "Cases", color = "Year",
                    title = "Year-over-year") +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "Year-over-year (insufficient data)")
  }
  
  combined <- patchwork::wrap_plots(p1, p2, ncol = 2)
  path <- file.path(out_dir, glue::glue("{study_name}_seasonal.png"))
  ggplot2::ggsave(path, combined, width = 12, height = 5, dpi = 300)
  path
}