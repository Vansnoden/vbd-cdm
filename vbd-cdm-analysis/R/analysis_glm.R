# R/analysis_glm.R
analyze_negbin_glm <- function(cases, environment, offset_population = NULL) {
  panel <- cases |>
    dplyr::mutate(month = lubridate::month(date)) |>
    dplyr::left_join(environment, by = "date") |>
    dplyr::filter(!is.na(case_count))
  
  # Poisson vs NB
  fit_pois <- MASS::glm.nb  # placeholder
  m_nb <- MASS::glm.nb(
    case_count ~ t2m + tp + factor(month) + offset(log_population),
    data = panel
  )
  
  list(
    model     = m_nb,
    coefficients = broom::tidy(m_nb, exponentiate = TRUE, conf.int = TRUE),
    aic       = AIC(m_nb),
    theta     = m_nb$theta,
    dispersion = summary(m_nb)$dispersion
  )
}