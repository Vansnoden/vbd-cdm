# R/analysis_glm.R

analyze_negbin_glm <- function(cases, environment, offset_population = NULL) {
  panel <- cases |>
    dplyr::mutate(month = lubridate::month(.data$date)) |>
    dplyr::left_join(environment, by = "date") |>
    dplyr::filter(!is.na(.data$case_count))
  
  m_nb <- if (is.null(offset_population)) {
    MASS::glm.nb(
      case_count ~ t2m + tp + factor(month),
      data = panel
    )
  } else {
    MASS::glm.nb(
      case_count ~ t2m + tp + factor(month) + offset(log(offset_population)),
      data = panel
    )
  }
  
  list(
    model        = m_nb,
    coefficients = broom::tidy(m_nb, exponentiate = TRUE, conf.int = TRUE),
    aic          = stats::AIC(m_nb),
    
    # Negative-binomial dispersion parameter. As theta -> Inf the NB
    # distribution collapses to Poisson. This is the value to report when
    # describing overdispersion.
    theta        = m_nb$theta,
    
    # Kept for callers that read $dispersion. For a MASS::glm.nb fit this
    # is 1/theta, NOT a variance-mean ratio in the quasi-Poisson sense.
    # Prefer $theta above.
    dispersion   = 1 / m_nb$theta
  )
}