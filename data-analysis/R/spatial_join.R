# R/spatial_join.R
#
# Map vector-observation location_ids to the nearest environment grid cell
# by centroid proximity. Used when vectors and environment live on different
# sets of location IDs (as they do in the current CDM).

#' Attach nearest-grid-cell location_ids to a vector observations tibble
#'
#' @param vectors      Output of query_vectors(); must carry `location_id`.
#' @param env_locs     Tibble with `location_id`, `centroid_lat`, `centroid_lon`.
#' @param db_path      Path to the SQLite database.
#' @return `vectors` with two extra columns: `grid_location_id`,
#'   `grid_distance_km`.
#' @export
attach_grid_cells <- function(vectors, db_path = NULL) {
  if (is.null(vectors) || nrow(vectors) == 0)
    return(vectors)
  
  # Fetch centroids for all locations once.
  locs <- cdm_query(
    "SELECT location_id, centroid_lat, centroid_lon FROM dim_location",
    db_path = db_path
  )
  
  # Split into vector-site locations and grid locations.
  env_locs <- locs |>
    dplyr::filter(!is.na(.data$centroid_lat), !is.na(.data$centroid_lon)) |>
    dplyr::rename(grid_id = .data$location_id,
                  grid_lat = .data$centroid_lat,
                  grid_lon = .data$centroid_lon)
  
  vectors |>
    dplyr::left_join(
      locs |> dplyr::rename(vec_lat = .data$centroid_lat,
                            vec_lon = .data$centroid_lon),
      by = "location_id"
    ) |>
    dplyr::mutate(
      grid_location_id = purrr::pmap_int(
        list(.data$vec_lat, .data$vec_lon),
        function(lat, lon) {
          if (is.na(lat) || is.na(lon)) return(NA_integer_)
          d2 <- (env_locs$grid_lat - lat)^2 +
            (env_locs$grid_lon - lon)^2
          env_locs$grid_id[which.min(d2)]
        }
      ),
      grid_distance_km = purrr::pmap_dbl(
        list(.data$vec_lat, .data$vec_lon),
        function(lat, lon) {
          if (is.na(lat) || is.na(lon)) return(NA_real_)
          d2 <- (env_locs$grid_lat - lat)^2 +
            (env_locs$grid_lon - lon)^2
          i <- which.min(d2)
          # Rough: 1 deg ≈ 111 km; adjust longitude by cos(lat).
          sqrt((env_locs$grid_lat[i] - lat)^2 +
                 (env_locs$grid_lon[i] - lon)^2 *
                 cos(lat * pi / 180)^2) * 111
        }
      )
    ) |>
    dplyr::select(-.data$vec_lat, -.data$vec_lon)
}