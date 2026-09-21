# R/analysis_bionomics.R
#
# Bionomics distributions: feeding preference, biting location, resting
# behaviour, larval site type.

#' Summarise vector bionomics
#' @param bionomics Output of query_bionomics().
#' @return List of categorical distributions plus n_observations.
#' @export
analyze_bionomics <- function(bionomics) {
  if (is.null(bionomics) || nrow(bionomics) == 0) {
    return(list(error = "No bionomics data available."))
  }
  
  cat_summary <- function(col) {
    if (!col %in% names(bionomics)) {
      return(tibble::tibble(value = character(), n = integer(),
                            share = numeric()))
    }
    bionomics |>
      dplyr::filter(!is.na(.data[[col]]), .data[[col]] != "") |>
      dplyr::count(value = .data[[col]], name = "n") |>
      dplyr::mutate(share = .data$n / sum(.data$n))
  }
  
  list(
    error      = NULL,
    feeding    = cat_summary("feeding_preference"),
    biting_loc = cat_summary("biting_location"),
    resting    = cat_summary("resting_behavior"),
    larval     = cat_summary("larval_site_type"),
    n_observations = nrow(bionomics)
  )
}

#' Plot bionomics distributions
#' @export
plot_bionomics <- function(result, study_name, out_dir) {
  if (!is.null(result$error)) {
    warning("Skipping bionomics plot: ", result$error)
    return(invisible(character()))
  }
  fs::dir_create(out_dir)
  
  mk_bar <- function(df, title) {
    if (nrow(df) == 0) {
      return(ggplot2::ggplot() + ggplot2::theme_void() +
               ggplot2::labs(title = title))
    }
    ggplot2::ggplot(df,
                    ggplot2::aes(x = stats::reorder(.data$value, .data$n),
                                 y = .data$n)) +
      ggplot2::geom_col(fill = "#3B82F6", alpha = 0.85) +
      ggplot2::coord_flip() +
      ggplot2::labs(x = NULL, y = "Observations", title = title) +
      theme_cdm()
  }
  
  combined <- patchwork::wrap_plots(
    mk_bar(result$feeding,    "Feeding preference"),
    mk_bar(result$biting_loc, "Biting location"),
    mk_bar(result$resting,    "Resting behaviour"),
    mk_bar(result$larval,     "Larval site type"),
    ncol = 2
  ) +
    patchwork::plot_annotation(
      title = glue::glue("Vector bionomics: {study_name}"),
      theme = ggplot2::theme(
        plot.title = ggplot2::element_text(face = "bold", size = 14)
      )
    )
  
  path <- file.path(out_dir, glue::glue("{study_name}_bionomics.png"))
  ggplot2::ggsave(path, combined, width = 12, height = 8, dpi = 300,
                  bg = "white")
  invisible(path)
}