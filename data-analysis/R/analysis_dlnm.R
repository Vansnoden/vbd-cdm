# R/analysis_dlnm.R
analyze_dlnm <- function(cases, environment, max_lag_months = 6) {
  panel <- cases |>
    dplyr::left_join(environment, by = "date") |>
    dplyr::arrange(date) |>
    dplyr::filter(!is.na(case_count), !is.na(t2m))
  
  cb_temp <- dlnm::crossbasis(
    panel$t2m,
    lag  = max_lag_months,
    argvar = list(fun = "ns", knots = stats::quantile(panel$t2m, c(.25,.5,.75))),
    arglag = list(fun = "ns", knots = logknots(max_lag_months, 3))
  )
  cb_rain <- dlnm::crossbasis(
    panel$tp,
    lag  = max_lag_months,
    argvar = list(fun = "ns", knots = stats::quantile(panel$tp, c(.25,.5,.75))),
    arglag = list(fun = "ns", knots = logknots(max_lag_months, 3))
  )
  
  model <- MASS::glm.nb(
    case_count ~ cb_temp + cb_rain +
      factor(lubridate::month(date)) +
      splines::ns(as.numeric(date), df = 3),
    data = panel
  )
  
  pred_temp <- dlnm::crosspred(cb_temp, model,
                               at = seq(min(panel$t2m), max(panel$t2m),
                                        length.out = 50),
                               cen = stats::median(panel$t2m))
  pred_rain <- dlnm::crosspred(cb_rain, model,
                               at = seq(min(panel$tp), max(panel$tp),
                                        length.out = 50),
                               cen = stats::median(panel$tp))
  
  list(model = model, pred_temp = pred_temp, pred_rain = pred_rain)
}

plot_dlnm <- function(dlnm_fit, study_name, out_dir) {
  fs::dir_create(out_dir)
  path <- file.path(out_dir,
                    glue::glue("{study_name}_dlnm_temp_overall.png"))
  
  # dlnm::plot.crosspred() is a base R plotting function. ggplot2::ggsave()
  # cannot capture base plots, so the previous version wrote an empty PNG.
  # Wrap the call in an explicit device.
  grDevices::png(path, width = 8, height = 5, units = "in", res = 300)
  on.exit(grDevices::dev.off(), add = TRUE)
  
  dlnm::plot.crosspred(
    dlnm_fit$pred_temp, "overall",
    xlab = "Temperature (°C)",
    ylab = "Cumulative RR",
    main = glue::glue("Temperature–malaria DLNM: {study_name}")
  )
  
  invisible(path)
}