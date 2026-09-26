# R/analysis_inla.R
analyze_inla <- function(cases, locations, adjacency_file) {
  if (is.null(cases) || nrow(cases) == 0) {
    cli::cli_abort("analyze_inla(): cases is empty.")
  }
  if (!"date" %in% names(cases)) {
    cli::cli_abort("analyze_inla(): cases must carry a `date` column.")
  }
  
  g <- INLA::inla.read.graph(adjacency_file)
  
  # Build the temporal indices. INLA allows only one f() term per variable
  # per formula, so seasonal smoothness and residual autocorrelation must
  # use different variables:
  #
  #   month_num  : 1..12, used inside sin/cos as fixed effects
  #   time_index : 1..N months since the first observation, used as the
  #                AR(1) index (a rw1 / ar1 term cannot span year boundaries
  #                with a repeating 1..12 index because it would be treated
  #                as circular)
  cases <- cases |>
    dplyr::mutate(
      month_num  = lubridate::month(.data$date),
      time_index = as.integer(
        (lubridate::year(.data$date) -
           min(lubridate::year(.data$date))) * 12L +
          (lubridate::month(.data$date) -
             min(lubridate::month(.data$date[lubridate::year(.data$date) ==
                                               min(lubridate::year(.data$date))]))) +
          1L
      )
    )
  
  formula <- case_count ~ t2m + tp +
    sin(2 * pi * month_num / 12) +
    cos(2 * pi * month_num / 12) +
    f(location_id, model = "bym2", graph = g, scale.model = TRUE) +
    f(time_index,  model = "ar1")
  
  INLA::inla(
    formula,
    family           = "nbinomial",
    data             = cases,
    E                = cases$population,
    control.compute  = list(dic = TRUE, waic = TRUE, cpo = TRUE),
    control.predictor = list(compute = TRUE)
  )
}