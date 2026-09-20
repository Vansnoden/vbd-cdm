# R/analysis_inla.R
analyze_inla <- function(cases, locations, adjacency_file) {
  g <- INLA::inla.read.graph(adjacency_file)
  
  formula <- case_count ~ t2m + tp +
    f(month_id,    model = "rw1") +
    f(location_id, model = "bym2", graph = g, scale.model = TRUE) +
    f(month_id,    model = "ar1")
  
  INLA::inla(
    formula,
    family  = "nbinomial",
    data    = cases,
    E       = cases$population,
    control.compute = list(dic = TRUE, waic = TRUE, cpo = TRUE),
    control.predictor = list(compute = TRUE)
  )
}