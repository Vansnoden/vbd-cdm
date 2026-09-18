# R/plot_helpers.R
#
# Shared plotting infrastructure. Every analysis module uses these helpers so
# figures are visually consistent and text sizes match at publication dpi.

#' Project-wide ggplot2 theme
#'
#' Minimal theme with sensible publication defaults: no minor gridlines,
#' bold titles, legends at the bottom.
#'
#' @param base_size Base font size in points.
#' @export
theme_cdm <- function(base_size = 11) {
  ggplot2::theme_minimal(base_size = base_size) +
    ggplot2::theme(
      panel.grid.minor  = ggplot2::element_blank(),
      panel.grid.major  = ggplot2::element_line(color = "grey92", linewidth = 0.3),
      plot.title        = ggplot2::element_text(face = "bold", size = base_size + 1),
      plot.subtitle     = ggplot2::element_text(color = "grey40", size = base_size - 1),
      plot.caption      = ggplot2::element_text(color = "grey50", size = base_size - 2),
      legend.position   = "bottom",
      legend.title      = ggplot2::element_text(face = "bold"),
      strip.text        = ggplot2::element_text(face = "bold"),
      axis.title        = ggplot2::element_text(color = "grey20"),
      axis.text         = ggplot2::element_text(color = "grey30")
    )
}

#' Project colour palette
#'
#' @export
cdm_palette <- list(
  primary    = "#1E40AF",
  secondary  = "#3B82F6",
  accent     = "#93C5FD",
  positive   = "#3B82F6",
  negative   = "#EF4444",
  neutral    = "#9CA3AF",
  highlight  = "#F59E0B",
  grid       = "grey92"
)

#' Signifance star annotation
#'
#' @param p Numeric p-value.
#' @return A character vector: one of "", "*", "**", "***", "ns".
#' @export
significance_stars <- function(p) {
  ifelse(is.na(p),   "",
         ifelse(p < 0.001,  "***",
                ifelse(p < 0.01,   "**",
                       ifelse(p < 0.05,   "*", "ns"))))
}

#' Format a p-value for display
#'
#' @param p Numeric p-value.
#' @return A character vector.
#' @export
format_p <- function(p) {
  ifelse(
    is.na(p),     "p = NA",
    ifelse(p < 0.001, "p < 0.001",
           sprintf("p = %.3f", p))
  )
}

#' Ensure a directory exists and return it
#'
#' @param path Directory path.
#' @return The path, invisibly.
#' @export
ensure_dir <- function(path) {
  fs::dir_create(path)
  invisible(path)
}

#' Save a ggplot with project defaults
#'
#' @param plot A ggplot object.
#' @param path Output file path.
#' @param width,height Dimensions in inches.
#' @export
save_plot <- function(plot, path, width = 8, height = 5) {
  ggplot2::ggsave(
    filename = path, plot = plot,
    width = width, height = height,
    dpi = 300, bg = "white"
  )
  path
}