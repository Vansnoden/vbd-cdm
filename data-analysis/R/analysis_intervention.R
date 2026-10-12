# R/analysis_intervention.R
#
# Intervention analysis.
#
# Two questions:
#   (a) What interventions exist, of what type, at what coverage, when?
#   (b) Do interventions associate with changes in disease burden or vector
#       abundance, measured as a pre/post comparison around each event?
#
# The pre/post analysis is a descriptive event study, not a formal causal
# estimate. There is no control group, no counterfactual, and no adjustment
# for seasonality beyond the fact that the before/after windows are
# symmetric. It answers "did the outcome change around intervention dates",
# not "did the intervention cause the change". Report accordingly.
#
# Public API:
#   analyze_intervention_summary(interventions)
#   analyze_intervention_impact(interventions, outcome, outcome_col, ...)
#   plot_intervention_summary(result, study_name, out_dir)
#   plot_intervention_impact(result, outcome_label, study_name, out_dir)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

#' Summarise the intervention catalogue
#'
#' @param interventions Output of `query_interventions()`.
#' @return A list with `by_type`, `by_year`, `coverage_by_type`,
#'   `n_interventions`, `n_types`, `date_range`.
#'   On empty input: `list(error = "...")`.
#' @export
analyze_intervention_summary <- function(interventions) {
  if (is.null(interventions) || nrow(interventions) == 0) {
    return(list(error = "No intervention data available."))
  }
  
  interventions <- interventions |>
    dplyr::mutate(date = as.Date(.data$date)) |>
    dplyr::filter(!is.na(.data$date))
  
  if (nrow(interventions) == 0) {
    return(list(error = "No interventions with a valid date."))
  }
  
  # Normalise blank / NA type labels so they group coherently.
  interventions <- interventions |>
    dplyr::mutate(
      intervention_type = dplyr::if_else(
        is.na(.data$intervention_type) | .data$intervention_type == "",
        "(unspecified)",
        .data$intervention_type
      )
    )
  
  by_type <- interventions |>
    dplyr::count(.data$intervention_type, name = "n") |>
    dplyr::arrange(dplyr::desc(.data$n))
  
  by_year <- interventions |>
    dplyr::count(.data$year, .data$intervention_type, name = "n")
  
  coverage_by_type <- interventions |>
    dplyr::filter(!is.na(.data$coverage)) |>
    dplyr::group_by(.data$intervention_type) |>
    dplyr::summarise(
      n          = dplyr::n(),
      mean_cov   = mean(.data$coverage, na.rm = TRUE),
      median_cov = stats::median(.data$coverage, na.rm = TRUE),
      .groups    = "drop"
    ) |>
    dplyr::arrange(dplyr::desc(.data$mean_cov))
  
  list(
    error            = NULL,
    by_type          = by_type,
    by_year          = by_year,
    coverage_by_type = coverage_by_type,
    n_interventions  = nrow(interventions),
    n_types          = dplyr::n_distinct(interventions$intervention_type),
    date_range       = range(interventions$date, na.rm = TRUE)
  )
}

# ---------------------------------------------------------------------------
# Impact analysis (pre / post around each intervention event)
# ---------------------------------------------------------------------------

#' Pre/post impact of interventions on an outcome
#'
#' For each intervention event, the outcome is restricted to the event's
#' own location_id, split into a window of `window_months` before the
#' intervention start and the same window after, and summarised as a
#' before/after mean with percent change. Events whose location has
#' fewer than `min_obs` observations on either side are dropped.
#'
#' Requires the outcome tibble to carry a `location_id` column. The
#' extraction queries (query_cases, query_vectors) produce this column.
#'
#' @param interventions Output of `query_interventions()`.
#' @param outcome       Tibble with `date`, `location_id`, and an outcome
#'                      column.
#' @param outcome_col   Name of the outcome column in `outcome`.
#' @param window_months Months on each side of the intervention start.
#' @param min_events    Minimum number of usable events required to report
#'                      a by-type result.
#' @param min_obs       Minimum number of outcome observations required on
#'                      each side of an event for it to be included.
#' @return A list with `events`, `by_type`, `aligned`, `aligned_agg`,
#'   `outcome_col`, `window_months`, `n_events`, `n_events_usable`.
#'   On empty input: `list(error = "...")`.
#' @export
analyze_intervention_impact <- function(interventions,
                                        outcome,
                                        outcome_col,
                                        window_months = 6L,
                                        min_events    = 2L,
                                        min_obs       = 3L) {
  if (is.null(interventions) || nrow(interventions) == 0) {
    return(list(error = "No intervention data available."))
  }
  if (is.null(outcome) || nrow(outcome) == 0) {
    return(list(error = "No outcome data available."))
  }
  if (!outcome_col %in% names(outcome)) {
    return(list(error = glue::glue("Column '{outcome_col}' not found in outcome.")))
  }
  if (!"location_id" %in% names(outcome)) {
    return(list(error = paste0(
      "Outcome must carry a `location_id` column for per-site ",
      "intervention analysis. query_cases() and query_vectors() both ",
      "produce it."
    )))
  }
  
  interventions <- interventions |>
    dplyr::mutate(date = as.Date(.data$date)) |>
    dplyr::filter(!is.na(.data$date))
  
  if (nrow(interventions) == 0) {
    return(list(error = "No interventions with a valid date."))
  }
  
  # Aggregate the outcome to one row per (date, location_id). Multiple
  # rows for the same date and location (e.g., several species at the
  # same site on the same day) are summed so each event sees a single
  # series at its own location.
  outcome_by_loc_date <- outcome |>
    dplyr::mutate(date = as.Date(.data$date)) |>
    dplyr::filter(!is.na(.data$date), !is.na(.data[[outcome_col]])) |>
    dplyr::group_by(.data$date, .data$location_id) |>
    dplyr::summarise(
      value = sum(.data[[outcome_col]], na.rm = TRUE),
      .groups = "drop"
    )
  
  if (nrow(outcome_by_loc_date) == 0) {
    return(list(error = "No usable outcome rows after cleaning."))
  }
  
  window_months <- as.integer(window_months)
  min_events    <- as.integer(min_events)
  min_obs       <- as.integer(min_obs)
  
  win <- lubridate::period(months = window_months)
  
  # ---- Per-event pre/post ------------------------------------------------
  # Each event is evaluated against the outcome series AT ITS OWN
  # LOCATION. Events whose location_id has no outcome data in the window
  # are dropped.
  events <- purrr::map_dfr(seq_len(nrow(interventions)), function(i) {
    iv <- interventions[i, , drop = FALSE]
    before_start <- iv$date %m-% win
    after_end    <- iv$date %m+% win
    
    site <- outcome_by_loc_date |>
      dplyr::filter(.data$location_id == iv$location_id)
    
    if (nrow(site) == 0) return(tibble::tibble())
    
    before <- site |>
      dplyr::filter(.data$date >= before_start, .data$date <  iv$date)
    after  <- site |>
      dplyr::filter(.data$date >= iv$date,       .data$date <= after_end)
    
    if (nrow(before) < min_obs || nrow(after) < min_obs) {
      return(tibble::tibble())
    }
    
    pre_mean  <- mean(before$value, na.rm = TRUE)
    post_mean <- mean(after$value,  na.rm = TRUE)
    
    pct_change <- if (!is.finite(pre_mean) || pre_mean == 0) {
      NA_real_
    } else {
      (post_mean - pre_mean) / pre_mean * 100
    }
    
    tibble::tibble(
      intervention_id   = iv$intervention_id,
      intervention_type = iv$intervention_type %||% "(unspecified)",
      location_id       = iv$location_id,
      coverage          = iv$coverage,
      date              = iv$date,
      n_before          = nrow(before),
      n_after           = nrow(after),
      pre_mean          = pre_mean,
      post_mean         = post_mean,
      pct_change        = pct_change
    )
  })
  
  # ---- By-type aggregate -------------------------------------------------
  by_type <- if (nrow(events) > 0) {
    events |>
      dplyr::filter(!is.na(.data$pct_change)) |>
      dplyr::group_by(.data$intervention_type) |>
      dplyr::summarise(
        n_events          = dplyr::n(),
        mean_pct_change   = mean(.data$pct_change),
        median_pct_change = stats::median(.data$pct_change),
        sd_pct_change     = stats::sd(.data$pct_change),
        .groups = "drop"
      ) |>
      dplyr::filter(.data$n_events >= min_events) |>
      dplyr::arrange(.data$mean_pct_change)
  } else {
    tibble::tibble()
  }
  
  # ---- Event-study alignment --------------------------------------------
  # Restrict to the event's location before aligning on month offset.
  aligned <- purrr::map_dfr(seq_len(nrow(interventions)), function(i) {
    iv <- interventions[i, , drop = FALSE]
    before_start <- iv$date %m-% win
    after_end    <- iv$date %m+% win
    
    sub <- outcome_by_loc_date |>
      dplyr::filter(.data$location_id == iv$location_id,
                    .data$date >= before_start,
                    .data$date <= after_end)
    if (nrow(sub) == 0) return(tibble::tibble())
    
    sub$offset_months <- as.integer(round(
      as.numeric(difftime(sub$date, iv$date, units = "days")) / 30.44
    ))
    sub$intervention_id   <- iv$intervention_id
    sub$intervention_type <- iv$intervention_type %||% "(unspecified)"
    sub
  })
  
  aligned_agg <- if (nrow(aligned) > 0) {
    aligned |>
      dplyr::group_by(.data$offset_months) |>
      dplyr::summarise(
        mean_value = mean(.data$value, na.rm = TRUE),
        sd_value   = stats::sd(.data$value, na.rm = TRUE),
        n          = dplyr::n(),
        .groups    = "drop"
      ) |>
      dplyr::mutate(
        se = dplyr::if_else(.data$n > 1,
                            .data$sd_value / sqrt(.data$n),
                            NA_real_)
      ) |>
      dplyr::arrange(.data$offset_months)
  } else {
    tibble::tibble()
  }
  
  list(
    error            = NULL,
    events           = events,
    by_type          = by_type,
    aligned          = aligned,
    aligned_agg      = aligned_agg,
    outcome_col      = outcome_col,
    window_months    = window_months,
    n_events         = nrow(interventions),
    n_events_usable  = nrow(events)
  )
}

# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

#' Plot intervention summary
#' @export
plot_intervention_summary <- function(result, study_name, out_dir) {
  if (!is.null(result$error)) {
    warning("Skipping intervention summary plot: ", result$error)
    return(invisible(character()))
  }
  fs::dir_create(out_dir)
  
  pA <- ggplot2::ggplot(
    result$by_type,
    ggplot2::aes(x = stats::reorder(.data$intervention_type, .data$n),
                 y = .data$n)
  ) +
    ggplot2::geom_col(fill = "#3B82F6", alpha = 0.85) +
    ggplot2::coord_flip() +
    ggplot2::labs(x = NULL, y = "Events",
                  title = "Interventions by type") +
    theme_cdm()
  
  pB <- ggplot2::ggplot(
    result$by_year,
    ggplot2::aes(x = .data$year, y = .data$n,
                 fill = .data$intervention_type)
  ) +
    ggplot2::geom_col(position = "stack", alpha = 0.85) +
    ggplot2::labs(x = NULL, y = "Events", fill = NULL,
                  title = "Interventions over time") +
    theme_cdm()
  
  combined <- patchwork::wrap_plots(pA, pB, ncol = 2) +
    patchwork::plot_annotation(
      title = glue::glue("Intervention summary: {study_name}"),
      subtitle = glue::glue(
        "{result$n_interventions} events across {result$n_types} type(s)"
      ),
      theme = ggplot2::theme(
        plot.title    = ggplot2::element_text(face = "bold", size = 14),
        plot.subtitle = ggplot2::element_text(color = "grey40")
      )
    )
  
  path <- file.path(out_dir,
                    glue::glue("{study_name}_intervention_summary.png"))
  ggplot2::ggsave(path, combined, width = 12, height = 5, dpi = 300,
                  bg = "white")
  invisible(path)
}

#' Plot intervention impact (event study + by-type pre/post change)
#' @export
plot_intervention_impact <- function(result, outcome_label,
                                     study_name, out_dir) {
  if (!is.null(result$error)) {
    warning("Skipping intervention impact plot: ", result$error)
    return(invisible(character()))
  }
  fs::dir_create(out_dir)
  
  # ---- Event-study curve -------------------------------------------------
  pA <- if (nrow(result$aligned_agg) > 0) {
    df <- result$aligned_agg
    ggplot2::ggplot(df, ggplot2::aes(x = .data$offset_months,
                                     y = .data$mean_value)) +
      ggplot2::geom_ribbon(
        ggplot2::aes(ymin = .data$mean_value - 1.96 * .data$se,
                     ymax = .data$mean_value + 1.96 * .data$se),
        fill = "#93C5FD", alpha = 0.4
      ) +
      ggplot2::geom_vline(xintercept = 0, linetype = "dashed",
                          color = "grey40") +
      ggplot2::geom_line(color = "#1E40AF", linewidth = 0.9) +
      ggplot2::geom_point(color = "#1E40AF", size = 2) +
      ggplot2::labs(
        x = "Months relative to intervention start",
        y = glue::glue("Mean {outcome_label}"),
        title = "Event-study: aligned outcomes",
        subtitle = "Dashed line = intervention start"
      ) +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No aligned event data")
  }
  
  # ---- Percent change by intervention type ------------------------------
  pB <- if (nrow(result$by_type) > 0) {
    df <- result$by_type |>
      dplyr::mutate(
        intervention_type = stats::reorder(.data$intervention_type,
                                           .data$mean_pct_change),
        direction = dplyr::if_else(.data$mean_pct_change < 0,
                                   "decrease", "increase")
      )
    ggplot2::ggplot(df, ggplot2::aes(x = .data$intervention_type,
                                     y = .data$mean_pct_change,
                                     fill = .data$direction)) +
      ggplot2::geom_col(alpha = 0.85) +
      ggplot2::geom_hline(yintercept = 0, color = "grey40") +
      ggplot2::scale_fill_manual(
        values = c("decrease" = "#10B981", "increase" = "#EF4444"),
        guide = "none"
      ) +
      ggplot2::coord_flip() +
      ggplot2::labs(
        x = NULL,
        y = glue::glue("Mean % change in {outcome_label}"),
        title = "Pre/post change by intervention type"
      ) +
      theme_cdm()
  } else {
    ggplot2::ggplot() + ggplot2::theme_void() +
      ggplot2::labs(title = "No usable pre/post comparisons")
  }
  
  combined <- patchwork::wrap_plots(pA, pB, ncol = 2) +
    patchwork::plot_annotation(
      title = glue::glue("Intervention impact on {outcome_label}: {study_name}"),
      subtitle = glue::glue(
        "{result$n_events_usable} of {result$n_events} events had sufficient ",
        "data for a pre/post comparison"
      ),
      theme = ggplot2::theme(
        plot.title    = ggplot2::element_text(face = "bold", size = 14),
        plot.subtitle = ggplot2::element_text(color = "grey40")
      )
    )
  
  path <- file.path(out_dir,
                    glue::glue("{study_name}_intervention_{outcome_label}.png"))
  ggplot2::ggsave(path, combined, width = 12, height = 6, dpi = 300,
                  bg = "white")
  invisible(path)
}