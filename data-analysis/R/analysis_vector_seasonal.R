# R/analysis_vector_seasonal.R
#
# Vector-centred seasonal analysis.
#
# Where analyze_seasonal() operates on cases, this operates on vector
# observations. Vector abundance is the study subject here; cases are
# contextual.

#' Analyze vector species composition and seasonality
#'
#' @param vectors   Output of query_vectors().
#' @param value_col Which abundance column to summarise. Auto-detected from
#'                  "count" (preferred) or "density_per_trap".
#' @return A list with:
#'   - composition:        per-species totals and share
#'   - monthly:            climatological monthly mean abundance (pooled)
#'   - monthly_by_species: monthly mean by species
#'   - yoy:                monthly by year (empty when only one year)
#'   - value_col, n_observations, n_species
#'   On error: list(error = "...")
#' @export
analyze_vector_seasonal <- function(vectors, value_col = NULL) {
  if (is.null(vectors) || nrow(vectors) == 0) {
    return(list(error = "No vector data available."))
  }
  
  if (is.null(value_col)) {
    value_col <- if ("count" %in% names(vectors)) "count"
    else if ("density_per_trap" %in% names(vectors)) "density_per_trap"
    else names(vectors)[1]
  }
  if (!value_col %in% names(vectors)) {
    return(list(error = glue::glue("value_col '{value_col}' not present in data.")))
  }
  
  vectors <- vectors |>
    dplyr::mutate(
      date  = as.Date(.data$date),
      month = lubridate::month(.data$date),
      year  = lubridate::year(.data$date)
    )
  
  composition <- vectors |>
    dplyr::group_by(.data$species_name) |>
    dplyr::summarise(
      n_obs       = dplyr::n(),
      total_count = sum(.data[[value_col]], na.rm = TRUE),
      .groups     = "drop"
    ) |>
    dplyr::arrange(dplyr::desc(.data$total_count)) |>
    dplyr::mutate(share = .data$total_count / sum(.data$total_count))
  
  monthly <- vectors |>
    dplyr::group_by(.data$month) |>
    dplyr::summarise(
      mean = mean(.data[[value_col]], na.rm = TRUE),
      sd   = stats::sd(.data[[value_col]], na.rm = TRUE),
      n    = dplyr::n(),
      .groups = "drop"
    ) |>
    dplyr::mutate(month = factor(.data$month, levels = 1:12, labels = month.abb))
  
  monthly_by_species <- vectors |>
    dplyr::group_by(.data$species_name, .data$month) |>
    dplyr::summarise(
      mean = mean(.data[[value_col]], na.rm = TRUE),
      n    = dplyr::n(),
      .groups = "drop"
    ) |>
    dplyr::mutate(month = factor(.data$month, levels = 1:12, labels = month.abb))
  
  yoy <- if (dplyr::n_distinct(vectors$year) > 1) {
    vectors |>
      dplyr::group_by(.data$year, .data$month) |>
      dplyr::summarise(
        mean = mean(.data[[value_col]], na.rm = TRUE),
        .groups = "drop"
      )
  } else {
    tibble::tibble()
  }
  
  list(
    error              = NULL,
    composition        = composition,
    monthly            = monthly,
    monthly_by_species = monthly_by_species,
    yoy                = yoy,
    value_col          = value_col,
    n_observations     = nrow(vectors),
    n_species          = dplyr::n_distinct(vectors$species_name)
  )
}

#' Plot vector seasonal patterns
#'
#' @param result     Output of analyze_vector_seasonal().
#' @param study_name Study name used in titles and file names.
#' @param out_dir    Directory for PNG files.
#' @return Character vector of file paths (invisibly).
#' @export
plot_vector_seasonal <- function(result, study_name, out_dir) {
  if (!is.null(result$error)) {
    warning("Skipping vector-seasonal plot: ", result$error)
    return(invisible(character()))
  }
  fs::dir_create(out_dir)
  
  pA <- ggplot2::ggplot(
    result$composition,
    ggplot2::aes(x = stats::reorder(.data$species_name, .data$total_count),
                 y = .data$total_count)
  ) +
    ggplot2::geom_col(fill = "#3B82F6", alpha = 0.85) +
    ggplot2::coord_flip() +
    ggplot2::labs(x = NULL,
                  y = glue::glue("Total {result$value_col}"),
                  title = "Species composition") +
    theme_cdm()
  
  pB <- ggplot2::ggplot(
    result$monthly,
    ggplot2::aes(x = .data$month, y = .data$mean)
  ) +
    ggplot2::geom_col(fill = "#1E40AF", alpha = 0.85) +
    ggplot2::geom_errorbar(
      ggplot2::aes(ymin = pmax(0, .data$mean - .data$sd),
                   ymax = .data$mean + .data$sd),
      width = 0.2
    ) +
    ggplot2::labs(x = NULL,
                  y = glue::glue("Mean {result$value_col}"),
                  title = "Climatological monthly mean (all species)") +
    theme_cdm()
  
  pC <- ggplot2::ggplot(
    result$monthly_by_species,
    ggplot2::aes(x = .data$month, y = .data$mean,
                 color = .data$species_name, group = .data$species_name)
  ) +
    ggplot2::geom_line(linewidth = 0.9) +
    ggplot2::geom_point(size = 2) +
    ggplot2::labs(x = NULL,
                  y = glue::glue("Mean {result$value_col}"),
                  color = NULL,
                  title = "Monthly by species") +
    theme_cdm()
  
  pD <- if (nrow(result$yoy) > 0) {
    ggplot2::ggplot(
      result$yoy,
      ggplot2::aes(x = .data$month, y = .data$mean,
                   color = factor(.data$year), group = factor(.data$year))
    ) +
      ggplot2::geom_line(linewidth = 0.8) +
      ggplot2::geom_point(size = 1.8) +
      ggplot2::scale_x_continuous(breaks = 1:12, labels = month.abb) +
      ggplot2::labs(x = NULL,
                    y = glue::glue("Mean {result$value_col}"),
                    color = "Year", title = "Year-over-year") +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "Year-over-year (single year of data)")
  }
  
  combined <- patchwork::wrap_plots(pA, pB, pC, pD, ncol = 2) +
    patchwork::plot_annotation(
      title = glue::glue("Vector seasonal patterns: {study_name}"),
      theme = ggplot2::theme(
        plot.title = ggplot2::element_text(face = "bold", size = 14)
      )
    )
  
  path <- file.path(out_dir, glue::glue("{study_name}_vector_seasonal.png"))
  ggplot2::ggsave(path, combined, width = 12, height = 10, dpi = 300,
                  bg = "white")
  invisible(path)
}