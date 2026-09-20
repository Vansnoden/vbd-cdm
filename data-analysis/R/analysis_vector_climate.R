# R/analysis_vector_climate.R
#
# Vector-climate association analysis.
#
# Port of analysis/analysis_modules.py::AnalysisModules.vector_climate_analysis
# with a few additions (95% CIs on r, optional FDR correction, ggplot output).
#
# Public API:
#   analyze_vector_climate(vectors, environment, ...)  ->  list of results
#   plot_vector_climate(result, study_name, out_dir)    ->  paths to PNG files
#
# The analysis function is pure (no I/O, no side effects), so it can be
# cached directly by {targets}. All file writing happens in the plot function.


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#' Safe Pearson correlation. Returns NA when data are insufficient or degenerate.
#' @noRd
.safe_pearson <- function(x, y, min_n = 5L) {
  empty <- list(r = NA_real_, p = NA_real_, n = 0L,
                lower = NA_real_, upper = NA_real_)
  if (length(x) != length(y) || length(x) == 0) return(empty)
  
  ok <- stats::complete.cases(x, y)
  n  <- sum(ok)
  if (n < min_n) { empty$n <- as.integer(n); return(empty) }
  
  x_ok <- x[ok]; y_ok <- y[ok]
  if (stats::sd(x_ok) == 0 || stats::sd(y_ok) == 0) {
    empty$n <- as.integer(n); return(empty)
  }
  
  ct <- tryCatch(stats::cor.test(x_ok, y_ok, method = "pearson"),
                 error = function(e) NULL)
  if (is.null(ct)) { empty$n <- as.integer(n); return(empty) }
  
  list(r     = unname(ct$estimate),
       p     = ct$p.value,
       n     = as.integer(n),
       lower = unname(ct$conf.int[1]),
       upper = unname(ct$conf.int[2]))
}

#' Auto-detect vector abundance column.
#' @noRd
.detect_vector_col <- function(df, preferred = c("count", "density_per_trap")) {
  for (col in preferred) if (col %in% names(df)) return(col)
  cli::cli_abort("No vector abundance column found in {.val {names(df)}}.")
}

#' Auto-detect numeric climate columns.
#' @noRd
.detect_climate_vars <- function(env) {
  ignore <- c("date", "year", "month", "period")
  names(env)[vapply(env, is.numeric, logical(1)) & !names(env) %in% ignore]
}

#' Choose the floor_date function for a given aggregation level.
#' @noRd
.floor_fun <- function(aggregation) {
  switch(aggregation,
         monthly = \(x) lubridate::floor_date(x, "month"),
         weekly  = \(x) lubridate::floor_date(x, "week", week_start = 1),
         daily   = \(x) as.Date(x),
         cli::cli_abort("Unsupported aggregation: {.val {aggregation}}")
  )
}

#' Precipitation variables are summed; all others are averaged.
#' @noRd
.is_rain_var <- function(v) v %in% c("tp", "precipitation", "rainfall_total")

#' Aggregate vector observations to the target period.
#' @noRd
.aggregate_vector <- function(vectors, date_col, aggregation,
                              vector_col, group_cols) {
  fun <- .floor_fun(aggregation)
  rule <- if (vector_col == "count") \(x) sum(x, na.rm = TRUE)
  else                        \(x) mean(x, na.rm = TRUE)
  
  vectors |>
    dplyr::mutate(date = as.Date(.data[[date_col]]),
                  period = fun(.data$date)) |>
    dplyr::group_by(dplyr::pick(dplyr::all_of(c("period", group_cols)))) |>
    dplyr::summarise(value = rule(.data[[vector_col]]), .groups = "drop") |>
    dplyr::rename(date = "period")
}

#' Aggregate environment to the target period.
#' @noRd
.aggregate_climate <- function(environment, date_col, aggregation,
                               climate_vars) {
  fun <- .floor_fun(aggregation)
  environment |>
    dplyr::mutate(date = as.Date(.data[[date_col]]),
                  period = fun(.data$date)) |>
    dplyr::group_by(period) |>
    dplyr::summarise(
      dplyr::across(
        dplyr::all_of(climate_vars),
        \(x) if (.is_rain_var(dplyr::cur_column()))
          sum(x, na.rm = TRUE)
        else
          mean(x, na.rm = TRUE),
        .names = "{.col}"
      ),
      .groups = "drop"
    ) |>
    dplyr::rename(date = "period")
}

#' Merge vector and climate on date (plus group columns).
#' @noRd
.merge_vector_climate <- function(vec_agg, clim_agg, group_cols) {
  dplyr::inner_join(vec_agg, clim_agg, by = "date")
}

#' Lag correlation curve for one climate variable.
#' @noRd
.lag_correlations <- function(merged, vector_col, climate_vars, max_lag, min_n) {
  merged <- merged |> dplyr::arrange(.data$date)
  
  purrr::map(climate_vars, function(v) {
    purrr::map(0:max_lag, function(l) {
      shifted <- dplyr::lag(merged[[v]], n = l)
      res <- .safe_pearson(shifted, merged[[vector_col]], min_n)
      tibble::tibble(
        climate_variable = v,
        lag              = as.integer(l),
        r                = res$r,
        p                = res$p,
        n                = res$n,
        ci_lower         = res$lower,
        ci_upper         = res$upper
      )
    }) |> purrr::list_rbind()
  }) |> purrr::list_rbind()
}

#' Mean ± SD of the vector series within deciles of one climate variable.
#' @noRd
.climate_envelope <- function(merged, vector_col, clim_var, n_bins = 10L) {
  x <- merged[[clim_var]]
  breaks <- unique(stats::quantile(x, probs = seq(0, 1, length.out = n_bins + 1),
                                   na.rm = TRUE))
  if (length(breaks) < 3) return(tibble::tibble())
  
  merged |>
    dplyr::mutate(bin = cut(x, breaks = breaks, include.lowest = TRUE,
                            labels = FALSE)) |>
    dplyr::filter(!is.na(.data$bin)) |>
    dplyr::group_by(.data$bin) |>
    dplyr::summarise(
      bin_value = mean(.data[[clim_var]], na.rm = TRUE),
      mean      = mean(.data[[vector_col]], na.rm = TRUE),
      sd        = stats::sd(.data[[vector_col]], na.rm = TRUE),
      n         = dplyr::n(),
      .groups   = "drop"
    ) |>
    dplyr::mutate(variable = clim_var) |>
    dplyr::rename(bin_center = .data$bin_value)
}

#' Monthly seasonal pattern, both series normalised to [0, 1].
#' @noRd
.seasonal_pattern <- function(merged, vector_col, climate_vars) {
  vars <- c(vector_col, climate_vars)
  long <- merged |>
    dplyr::mutate(month = lubridate::month(.data$date)) |>
    dplyr::group_by(.data$month) |>
    dplyr::summarise(
      dplyr::across(dplyr::all_of(vars),
                    \(x) mean(x, na.rm = TRUE), .names = "{.col}"),
      .groups = "drop"
    ) |>
    tidyr::pivot_longer(-"month", names_to = "variable",
                        values_to = "value") |>
    dplyr::group_by(.data$variable) |>
    dplyr::mutate(
      value_norm = {
        mn <- min(.data$value, na.rm = TRUE)
        mx <- max(.data$value, na.rm = TRUE)
        if (mx == mn) rep(0.5, dplyr::n())
        else (.data$value - mn) / (mx - mn)
      }
    ) |>
    dplyr::ungroup()
  
  long$variable[long$variable == vector_col] <- "vector"
  long
}

#' Run the full analysis on one dataset (either global or a single group).
#' @noRd
.run_one <- function(merged, vector_col, climate_vars, max_lag, min_n) {
  cors <- purrr::map(climate_vars, function(v) {
    res <- .safe_pearson(merged[[vector_col]], merged[[v]], min_n)
    tibble::tibble(
      climate_variable = v,
      r                = res$r,
      p                = res$p,
      n                = res$n,
      ci_lower         = res$lower,
      ci_upper         = res$upper
    )
  }) |> purrr::list_rbind()
  
  lags <- .lag_correlations(merged, vector_col, climate_vars, max_lag, min_n)
  
  opt <- lags |>
    dplyr::filter(!is.na(.data$r)) |>
    dplyr::group_by(.data$climate_variable) |>
    dplyr::slice_max(order_by = .data$r, n = 1, with_ties = FALSE) |>
    dplyr::ungroup()
  
  seasonal <- .seasonal_pattern(merged, vector_col, climate_vars)
  
  envelope <- purrr::map(climate_vars, function(v)
    .climate_envelope(merged, vector_col, v)
  ) |> purrr::list_rbind()
  
  list(correlations = cors, lag_correlations = lags,
       optimal_lags = opt, seasonal = seasonal, envelope = envelope)
}

# ---------------------------------------------------------------------------
# Public: analyze_vector_climate
# ---------------------------------------------------------------------------

#' Vector-climate association analysis
#'
#' @param vectors           Tibble with columns `date`, `count` or
#'                          `density_per_trap`, and optional grouping columns.
#' @param environment       Tibble with one row per date, one column per
#'                          climate variable.
#' @param vector_col        Name of the vector abundance column. Auto-detected
#'                          if NULL.
#' @param climate_variables Character vector of climate column names.
#'                          Auto-detected if NULL.
#' @param group_cols        Optional grouping columns (e.g., `c("species_name")`).
#'                          When set, the analysis runs both globally and per
#'                          group.
#' @param aggregation       `"monthly"`, `"weekly"`, or `"daily"`.
#' @param max_lag           Maximum lag (in aggregation units) for lag analysis.
#' @param min_n             Minimum paired observations for a correlation.
#' @param fdr_correct       If TRUE, add a `p_adj` column with BH-FDR adjusted
#'                          p-values on the correlation and lag tables.
#'
#' @return A list with `correlations`, `lag_correlations`, `optimal_lags`,
#'   `seasonal`, `envelope`, `merged_data`, `by_group_correlations`,
#'   `by_group_lags`, and `summary`. On error, returns `list(error = "...")`.
#'
#' @export
analyze_vector_climate <- function(vectors,
                                   environment,
                                   vector_col         = NULL,
                                   climate_variables  = NULL,
                                   group_cols         = NULL,
                                   aggregation        = c("monthly", "weekly", "daily"),
                                   max_lag            = 3L,
                                   min_n              = 5L,
                                   fdr_correct        = TRUE) {
  aggregation <- match.arg(aggregation)
  
  if (is.null(vectors) || nrow(vectors) == 0)
    return(list(error = "No vector data available."))
  if (is.null(environment) || nrow(environment) == 0)
    return(list(error = "No environmental data available."))
  
  if (is.null(vector_col)) vector_col <- .detect_vector_col(vectors)
  if (!vector_col %in% names(vectors))
    return(list(error = glue::glue("Vector column '{vector_col}' not found.")))
  
  if (is.null(climate_variables))
    climate_variables <- .detect_climate_vars(environment)
  if (length(climate_variables) == 0)
    return(list(error = "No numeric climate variables found."))
  
  group_cols <- group_cols[group_cols %in% names(vectors)]
  max_lag    <- as.integer(max_lag)
  min_n      <- as.integer(min_n)
  
  # --- Aggregation ---------------------------------------------------------
  vec_agg  <- .aggregate_vector(vectors, "date", aggregation,
                                vector_col, group_cols)
  clim_agg <- .aggregate_climate(environment, "date", aggregation,
                                 climate_variables)
  merged   <- .merge_vector_climate(vec_agg, clim_agg, group_cols)
  
  if (nrow(merged) == 0)
    return(list(error = "No overlapping dates between vector and climate data."))
  
  # --- Global analysis -----------------------------------------------------
  overall <- .run_one(merged, "value", climate_variables, max_lag, min_n)
  
  if (fdr_correct && nrow(overall$correlations)) {
    overall$correlations <- overall$correlations |>
      dplyr::mutate(p_adj = stats::p.adjust(.data$p, method = "BH"))
  }
  if (fdr_correct && nrow(overall$lag_correlations)) {
    overall$lag_correlations <- overall$lag_correlations |>
      dplyr::mutate(p_adj = stats::p.adjust(.data$p, method = "BH"))
  }
  
  # --- Per-group analysis --------------------------------------------------
  by_group_cor  <- tibble::tibble()
  by_group_lags <- tibble::tibble()
  
  if (length(group_cols) > 0) {
    splits <- merged |>
      dplyr::group_by(dplyr::across(dplyr::all_of(group_cols))) |>
      dplyr::group_split()
    
    by_group_cor <- purrr::map(splits, function(g) {
      key <- g[1, group_cols, drop = FALSE]
      res <- .run_one(g, "value", climate_variables, max_lag, min_n)
      if (nrow(res$correlations) == 0) return(tibble::tibble())
      key_tiled <- key[rep(1, nrow(res$correlations)), , drop = FALSE]
      dplyr::bind_cols(key_tiled, res$correlations)
    }) |> purrr::list_rbind()
    
    by_group_lags <- purrr::map(splits, function(g) {
      key <- g[1, group_cols, drop = FALSE]
      res <- .run_one(g, "value", climate_variables, max_lag, min_n)
      if (nrow(res$optimal_lags) == 0) return(tibble::tibble())
      key_tiled <- key[rep(1, nrow(res$optimal_lags)), , drop = FALSE]
      dplyr::bind_cols(key_tiled, res$optimal_lags)
    }) |> purrr::list_rbind()
  }
  
  # --- Summary -------------------------------------------------------------
  summary_list <- list(
    total_observations   = nrow(merged),
    date_range           = range(merged$date, na.rm = TRUE),
    mean_vector          = mean(merged$value, na.rm = TRUE),
    max_vector           = max(merged$value, na.rm = TRUE),
    aggregation          = aggregation,
    vector_col           = vector_col,
    climate_variables    = climate_variables,
    n_groups             = if (length(group_cols)) dplyr::n_distinct(
      merged[group_cols]) else 1L
  )
  
  list(
    error              = NULL,
    aggregation        = aggregation,
    vector_col         = vector_col,
    climate_variables  = climate_variables,
    group_cols         = group_cols,
    merged_data        = merged,
    correlations       = overall$correlations,
    lag_correlations   = overall$lag_correlations,
    optimal_lags       = overall$optimal_lags,
    seasonal           = overall$seasonal,
    envelope           = overall$envelope,
    by_group_correlations = by_group_cor,
    by_group_lags         = by_group_lags,
    summary            = summary_list
  )
}

# ---------------------------------------------------------------------------
# Public: plot_vector_climate
# ---------------------------------------------------------------------------

#' Format a p-value for annotation.
#' @noRd
.fmt_p <- function(p) {
  if (is.na(p)) return("p = NA")
  if (p < 0.001) return("p < 0.001")
  glue::glue("p = {formatC(p, digits = 3, format = 'f')}")
}

#' Significance stars.
#' @noRd
.stars <- function(p) {
  if (is.na(p))      return("")
  if (p < 0.001)     return("***")
  if (p < 0.01)      return("**")
  if (p < 0.05)      return("*")
  "ns"
}

#' Plot the four-panel figure for one climate variable.
#' @noRd
.plot_one_variable <- function(result, var, study_name, out_dir) {
  merged  <- result$merged_data
  vec_col <- "value"
  
  # (A) Scatter with loess fit
  ct <- result$correlations |> dplyr::filter(.data$climate_variable == var)
  ann <- if (nrow(ct) == 1L && !is.na(ct$r))
    glue::glue("r = {formatC(ct$r, digits = 2, format = 'f')}, {.fmt_p(ct$p)}")
  else "r = NA"
  
  pA <- ggplot2::ggplot(merged, ggplot2::aes(x = .data[[var]],
                                             y = .data[[vec_col]])) +
    ggplot2::geom_point(alpha = 0.5, color = "#3B82F6", size = 1.4) +
    ggplot2::geom_smooth(method = "loess", formula = y ~ x,
                         se = TRUE, color = "#1E40AF", fill = "#93C5FD",
                         linewidth = 0.8) +
    ggplot2::labs(x = var, y = "Vector abundance",
                  title = "Association", subtitle = ann) +
    theme_cdm()
  
  # (B) Lag correlation curve
  lags <- result$lag_correlations |>
    dplyr::filter(.data$climate_variable == var)
  pB <- if (nrow(lags) > 0) {
    ggplot2::ggplot(lags, ggplot2::aes(x = .data$lag, y = .data$r)) +
      ggplot2::geom_hline(yintercept = 0, linetype = "dashed",
                          color = "grey50") +
      ggplot2::geom_ribbon(ggplot2::aes(ymin = .data$ci_lower,
                                        ymax = .data$ci_upper),
                           fill = "#93C5FD", alpha = 0.4) +
      ggplot2::geom_line(color = "#1E40AF", linewidth = 0.8) +
      ggplot2::geom_point(color = "#1E40AF", size = 2) +
      ggplot2::labs(x = "Lag (months)", y = "Correlation (r)",
                    title = "Lag correlation") +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No lag data")
  }
  
  # (C) Climate envelope
  env <- result$envelope |> dplyr::filter(.data$variable == var)
  pC <- if (nrow(env) > 0) {
    ggplot2::ggplot(env, ggplot2::aes(x = .data$bin_center, y = .data$mean)) +
      ggplot2::geom_ribbon(ggplot2::aes(ymin = .data$mean - .data$sd,
                                        ymax = .data$mean + .data$sd),
                           fill = "#93C5FD", alpha = 0.4) +
      ggplot2::geom_line(color = "#1E40AF", linewidth = 0.8) +
      ggplot2::geom_point(color = "#1E40AF", size = 2) +
      ggplot2::labs(x = var, y = "Vector abundance",
                    title = "Climate envelope") +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No envelope data")
  }
  
  # (D) Seasonal overlay
  sea <- result$seasonal |>
    dplyr::filter(.data$variable %in% c("vector", var))
  pD <- if (nrow(sea) > 0) {
    ggplot2::ggplot(sea, ggplot2::aes(x = .data$month, y = .data$value_norm,
                                      color = .data$variable)) +
      ggplot2::geom_line(linewidth = 0.9) +
      ggplot2::geom_point(size = 2) +
      ggplot2::scale_color_manual(
        values = c("vector" = "#EF4444", stats::setNames("#3B82F6", var)),
        labels = c("vector" = "Vector", stats::setNames(var, var))
      ) +
      ggplot2::scale_x_continuous(breaks = 1:12, labels = month.abb) +
      ggplot2::labs(x = NULL, y = "Normalised (0-1)", color = NULL,
                    title = "Seasonal pattern") +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No seasonal data")
  }
  
  combined <- patchwork::wrap_plots(pA, pB, pC, pD, ncol = 2) +
    patchwork::plot_annotation(
      title = glue::glue("Vector vs {var}: {study_name}"),
      theme = ggplot2::theme(
        plot.title = ggplot2::element_text(face = "bold", size = 14)
      )
    )
  
  path <- file.path(out_dir,
                    glue::glue("{study_name}_vector_climate_{var}.png"))
  ggplot2::ggsave(path, combined, width = 12, height = 10, dpi = 300,
                  bg = "white")
  path
}

#' Plot vector-climate results
#'
#' Writes one PNG per climate variable plus a correlation summary bar chart.
#' If group analysis was run, writes a heatmap of correlations by group.
#'
#' @param result      Output of `analyze_vector_climate()`.
#' @param study_name  Study name used in file names and titles.
#' @param out_dir     Directory for PNG files.
#'
#' @return Character vector of file paths (invisibly).
#' @export
plot_vector_climate <- function(result, study_name, out_dir) {
  if (!is.null(result$error)) {
    warning("Skipping vector-climate plot: ", result$error)
    return(invisible(character()))
  }
  fs::dir_create(out_dir)
  paths <- character()
  
  # ---- 1. Correlation summary bar chart -----------------------------------
  if (nrow(result$correlations) > 0) {
    cors_df <- result$correlations |>
      dplyr::mutate(
        climate_variable = stats::reorder(.data$climate_variable, .data$r),
        stars            = vapply(.data$p, .stars, character(1))
      )
    
    p_cor <- ggplot2::ggplot(cors_df,
                             ggplot2::aes(x = .data$climate_variable,
                                          y = .data$r,
                                          fill = .data$r > 0)) +
      ggplot2::geom_col(alpha = 0.85, width = 0.7) +
      ggplot2::geom_text(ggplot2::aes(label = .data$stars),
                         hjust = ifelse(cors_df$r > 0, -0.3, 1.3),
                         color = "grey20", size = 3.5) +
      ggplot2::geom_hline(yintercept = 0, color = "grey40") +
      ggplot2::scale_fill_manual(values = c(`TRUE` = "#3B82F6",
                                            `FALSE` = "#EF4444"),
                                 guide = "none") +
      ggplot2::coord_flip() +
      ggplot2::labs(
        title    = glue::glue("Vector-climate correlations: {study_name}"),
        subtitle = "Pearson r.  * p<0.05,  ** p<0.01,  *** p<0.001",
        x = NULL, y = "Correlation (r)"
      ) +
      theme_cdm()
    
    f1 <- file.path(out_dir,
                    glue::glue("{study_name}_vector_climate_correlations.png"))
    ggplot2::ggsave(f1, p_cor, width = 8, height = 4, dpi = 300, bg = "white")
    paths <- c(paths, f1)
  }
  
  # ---- 2. One four-panel figure per climate variable ----------------------
  for (var in result$climate_variables) {
    p <- tryCatch(
      .plot_one_variable(result, var, study_name, out_dir),
      error = function(e) {
        warning("Could not plot variable ", var, ": ", conditionMessage(e))
        NULL
      }
    )
    if (!is.null(p)) paths <- c(paths, p)
  }
  
  # ---- 3. Optional per-group heatmap --------------------------------------
  bg <- result$by_group_correlations
  if (!is.null(bg) && nrow(bg) > 0 && length(result$group_cols) > 0) {
    group_label <- do.call(paste, c(bg[result$group_cols], sep = " / "))
    
    heat_df <- bg |>
      dplyr::mutate(.group = group_label) |>
      dplyr::select(.data$.group, .data$climate_variable, .data$r)
    
    p_heat <- ggplot2::ggplot(
      heat_df,
      ggplot2::aes(x = .data$climate_variable,
                   y = .data$.group,
                   fill = .data$r)
    ) +
      ggplot2::geom_tile(color = "white", linewidth = 0.5) +
      ggplot2::geom_text(ggplot2::aes(label = formatC(.data$r, digits = 2,
                                                      format = "f")),
                         color = "grey10", size = 3) +
      ggplot2::scale_fill_gradient2(low = "#EF4444", mid = "white",
                                    high = "#3B82F6", midpoint = 0,
                                    limits = c(-1, 1)) +
      ggplot2::labs(title = glue::glue("Vector-climate r by group: {study_name}"),
                    x = NULL, y = NULL, fill = "r") +
      theme_cdm() +
      ggplot2::theme(axis.text.x = ggplot2::element_text(angle = 30, hjust = 1))
    
    f3 <- file.path(out_dir,
                    glue::glue("{study_name}_vector_climate_by_group.png"))
    ggplot2::ggsave(f3, p_heat, width = 8, height = 6, dpi = 300, bg = "white")
    paths <- c(paths, f3)
  }
  
  invisible(paths)
}