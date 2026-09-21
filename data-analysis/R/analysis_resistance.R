# R/analysis_resistance.R
#
# Insecticide resistance: status distribution, mortality by compound,
# temporal trend. Operates on the output of query_resistance().

#' Summarise insecticide resistance data
#'
#' @param resistance Output of query_resistance().
#' @return List with status_by_class, mortality_by_insecticide, temporal,
#'   n_observations, n_insecticides. On error: list(error = "...").
#' @export
analyze_resistance <- function(resistance) {
  if (is.null(resistance) || nrow(resistance) == 0) {
    return(list(error = "No resistance data available."))
  }
  
  resistance <- resistance |>
    dplyr::mutate(
      date = as.Date(.data$date),
      year = lubridate::year(.data$date)
    )
  
  status_by_class <- resistance |>
    dplyr::filter(!is.na(.data$resistance_status)) |>
    dplyr::count(.data$chemical_class, .data$resistance_status) |>
    dplyr::group_by(.data$chemical_class) |>
    dplyr::mutate(share = .data$n / sum(.data$n)) |>
    dplyr::ungroup()
  
  mortality_by_insecticide <- resistance |>
    dplyr::filter(!is.na(.data$mortality)) |>
    dplyr::group_by(.data$insecticide_name) |>
    dplyr::summarise(
      n         = dplyr::n(),
      mean_mort = mean(.data$mortality, na.rm = TRUE),
      sd_mort   = stats::sd(.data$mortality, na.rm = TRUE),
      .groups   = "drop"
    ) |>
    dplyr::arrange(.data$mean_mort)
  
  temporal <- resistance |>
    dplyr::filter(!is.na(.data$mortality)) |>
    dplyr::group_by(.data$year) |>
    dplyr::summarise(
      n         = dplyr::n(),
      mean_mort = mean(.data$mortality, na.rm = TRUE),
      .groups   = "drop"
    ) |>
    dplyr::arrange(.data$year)
  
  list(
    error                    = NULL,
    status_by_class          = status_by_class,
    mortality_by_insecticide = mortality_by_insecticide,
    temporal                 = temporal,
    n_observations           = nrow(resistance),
    n_insecticides           = dplyr::n_distinct(resistance$insecticide_name,
                                                 na.rm = TRUE)
  )
}

#' Plot insecticide resistance summary
#' @export
plot_resistance <- function(result, study_name, out_dir) {
  if (!is.null(result$error)) {
    warning("Skipping resistance plot: ", result$error)
    return(invisible(character()))
  }
  fs::dir_create(out_dir)
  
  pA <- if (nrow(result$status_by_class) > 0) {
    ggplot2::ggplot(
      result$status_by_class,
      ggplot2::aes(x = .data$chemical_class, y = .data$share,
                   fill = .data$resistance_status)
    ) +
      ggplot2::geom_col(position = "stack", alpha = 0.85) +
      ggplot2::scale_fill_manual(
        values = c("Susceptible"         = "#10B981",
                   "Suspected Resistance" = "#F59E0B",
                   "Resistant"            = "#EF4444"),
        na.value = "grey70"
      ) +
      ggplot2::coord_flip() +
      ggplot2::labs(x = NULL, y = "Share", fill = "Status",
                    title = "Resistance status by chemical class") +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No classified resistance status")
  }
  
  pB <- if (nrow(result$mortality_by_insecticide) > 0) {
    ggplot2::ggplot(
      result$mortality_by_insecticide,
      ggplot2::aes(x = stats::reorder(.data$insecticide_name, .data$mean_mort),
                   y = .data$mean_mort)
    ) +
      ggplot2::geom_col(fill = "#3B82F6", alpha = 0.85) +
      ggplot2::geom_errorbar(
        ggplot2::aes(ymin = pmax(0, .data$mean_mort - .data$sd_mort),
                     ymax = pmin(100, .data$mean_mort + .data$sd_mort)),
        width = 0.2
      ) +
      ggplot2::geom_hline(yintercept = 98, linetype = "dashed",
                          color = "#10B981") +
      ggplot2::geom_hline(yintercept = 90, linetype = "dashed",
                          color = "#EF4444") +
      ggplot2::coord_flip() +
      ggplot2::labs(
        x = NULL, y = "Mean mortality (%)",
        title = "Mortality by insecticide (WHO thresholds dashed)"
      ) +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No mortality data")
  }
  
  pC <- if (nrow(result$temporal) > 0) {
    ggplot2::ggplot(
      result$temporal,
      ggplot2::aes(x = .data$year, y = .data$mean_mort)
    ) +
      ggplot2::geom_line(color = "#1E40AF", linewidth = 0.9) +
      ggplot2::geom_point(color = "#1E40AF", size = 2.5) +
      ggplot2::geom_hline(yintercept = 98, linetype = "dashed",
                          color = "#10B981") +
      ggplot2::geom_hline(yintercept = 90, linetype = "dashed",
                          color = "#EF4444") +
      ggplot2::labs(x = NULL, y = "Mean mortality (%)",
                    title = "Temporal trend") +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No temporal data")
  }
  
  combined <- patchwork::wrap_plots(pA, pB, pC, ncol = 2) +
    patchwork::plot_annotation(
      title = glue::glue("Insecticide resistance: {study_name}"),
      theme = ggplot2::theme(
        plot.title = ggplot2::element_text(face = "bold", size = 14)
      )
    )
  
  path <- file.path(out_dir, glue::glue("{study_name}_resistance.png"))
  ggplot2::ggsave(path, combined, width = 12, height = 8, dpi = 300,
                  bg = "white")
  invisible(path)
}