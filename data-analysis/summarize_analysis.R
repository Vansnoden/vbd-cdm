#!/usr/bin/env Rscript
# summarize_analysis.R
#
# Produce a plain-text summary of the current analysis results.
#
# Reads the `analysis_results` bundle from the targets cache, formats
# every analysis object as text (no plots), and writes the result to
# analysis_summary.txt in the project root.
#
# Usage:
#   Rscript summarize_analysis.R
#   Rscript summarize_analysis.R --output custom_path.txt
#
# The script is read-only. It does not run the pipeline. If the cache is
# stale or missing, run `targets::tar_make()` first.

suppressPackageStartupMessages({
  library(targets)
  library(here)
  library(dplyr)
})

# ---------------------------------------------------------------------------
# Null-coalescing operator — defined before first use.
# ---------------------------------------------------------------------------

`%||%` <- function(a, b) if (is.null(a)) b else a

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

args <- commandArgs(trailingOnly = TRUE)
out_path <- if (length(args) >= 2 && args[1] %in% c("-o", "--output")) {
  args[2]
} else {
  here::here("analysis_summary.txt")
}

# ---------------------------------------------------------------------------
# Output
#
# Append-on-write rather than a persistent connection. `on.exit()` at the
# top level of a sourced script has no function frame to attach to and
# fires (or fails to fire) unpredictably, leaving the handle invalid. A
# stateless writer is immune to how the script is invoked — Rscript,
# source(), R -e — and to being sourced more than once in the same
# session.
# ---------------------------------------------------------------------------

# Truncate the output file at start.
cat("", file = out_path)

.write <- function(...) {
  cat(..., "\n", sep = "", file = out_path, append = TRUE)
}

.rule <- function(ch = "=", n = 72) {
  .write(strrep(ch, n))
}

.section <- function(title) {
  .write("")
  .rule()
  .write(title)
  .rule()
}

.subsection <- function(title) {
  .write("")
  .write("-- ", title, " ", strrep("-", max(0, 66 - nchar(title))))
}

# Formats a single scalar value with a fixed width; NA -> "NA".
.val <- function(x, digits = 4) {
  if (is.null(x) || length(x) == 0) return("(none)")
  if (length(x) > 1) {
    return(paste0("[", length(x), " values: ",
                  paste(head(x, 3), collapse = ", "),
                  if (length(x) > 3) ", ..." else "", "]"))
  }
  if (is.na(x)) return("NA")
  if (is.numeric(x)) return(formatC(x, digits = digits, format = "g"))
  as.character(x)
}

# Prints a data.frame / tibble with sensible defaults for the summary.
.print_df <- function(df, caption = NULL, max_rows = 50) {
  if (!is.null(caption)) .write(caption)
  if (is.null(df) || !is.data.frame(df) || nrow(df) == 0) {
    .write("  (empty)")
    return(invisible(NULL))
  }
  n <- nrow(df)
  show <- min(n, max_rows)
  out <- capture.output(
    print(as.data.frame(df[seq_len(show), , drop = FALSE]),
          row.names = FALSE, digits = 4)
  )
  for (line in out) .write("  ", line)
  if (n > show) .write("  ... ", n - show, " more row(s)")
}

# Unpacks a result that may be a list with an `error` field.
.has_error <- function(x) is.list(x) && !is.null(x$error)
.get_error <- function(x) if (.has_error(x)) x$error else NULL

# ---------------------------------------------------------------------------
# Load the bundle
# ---------------------------------------------------------------------------

.section("Analysis summary")
.write("Generated:  ", format(Sys.time(), "%Y-%m-%d %H:%M:%S %Z"))
.write("Host:       ", Sys.info()[["nodename"]])
.write("R version:  ", R.version.string)
.write("Project:    ", here::here())

if (!dir.exists(here::here("_targets"))) {
  .write("")
  .write("ERROR: no targets cache at ", here::here("_targets"))
  .write("Run `targets::tar_make()` first.")
  quit(status = 1)
}

res <- tryCatch(
  targets::tar_read(analysis_results),
  error = function(e) {
    .write("")
    .write("ERROR reading `analysis_results`: ", conditionMessage(e))
    .write("Run `targets::tar_make()` first.")
    quit(status = 1)
  }
)

cfg <- res$cfg %||% list()

# ---------------------------------------------------------------------------
# 1. Scope
# ---------------------------------------------------------------------------

.section("Scope")

.write("study_name       : ", .val(cfg$study_name))
.write("countries        : ", .val(cfg$countries))
.write("locations        : ", .val(cfg$locations))
.write("years            : ", .val(cfg$start_year), " - ", .val(cfg$end_year))
.write("diseases         : ", .val(cfg$diseases))
.write("env_variables    : ", .val(cfg$env_variables))
.write("species_filter   : ", .val(cfg$species_filter))
.write("max_lag_months   : ", .val(cfg$max_lag_months))
.write("output_dir       : ", .val(cfg$output_dir))

# ---------------------------------------------------------------------------
# 2. Data availability
# ---------------------------------------------------------------------------

.section("Data availability")

.write("Locations resolved:       ", nrow(res$locations %||% data.frame()))
if (!is.null(res$locations) && nrow(res$locations) > 0) {
  .print_df(
    as.data.frame(table(res$locations$admin_level)),
    caption = "  by admin_level:"
  )
}

.write("Grid locations resolved:  ", nrow(res$grid_locations %||% data.frame()))

.avail <- function(label, df) {
  n <- if (is.null(df)) 0L else nrow(df)
  .write(sprintf("%-26s %8d row(s)", paste0(label, ":"), n))
  if (n > 0 && "date" %in% names(df)) {
    rng <- range(df$date, na.rm = TRUE)
    .write(sprintf("%-26s %s to %s",
                   "", format(rng[1]), format(rng[2])))
  }
}

.avail("vectors",      res$vectors)
.avail("environment",  res$environment)
.avail("resistance",   res$resistance)
.avail("bionomics",    res$bionomics)
.avail("cases",        res$cases)
.avail("burden",       res$burden)

.write("")
.write("cases_available:         ", isTRUE(res$cases_available))
.write("cases_n:                 ", .val(res$cases_n))

if (!is.null(res$qa) && !is.null(res$qa$tables)) {
  .subsection("QA table inventory")
  .print_df(res$qa$tables)
}

# ---------------------------------------------------------------------------
# 3. Vector seasonality
# ---------------------------------------------------------------------------

.section("Vector seasonality  (analyze_vector_seasonal)")

vs <- res$vector_seasonal
if (.has_error(vs)) {
  .write("error: ", .get_error(vs))
} else if (is.list(vs)) {
  .write("value_col:        ", .val(vs$value_col))
  .write("n_observations:   ", .val(vs$n_observations))
  .write("n_species:        ", .val(vs$n_species))
  .print_df(vs$composition, caption = "\ncomposition:")
  .print_df(vs$monthly,     caption = "\nclimatological monthly mean:")
  .print_df(vs$yoy,         caption = "\nyear-over-year (first 24 rows):",
            max_rows = 24)
}

# ---------------------------------------------------------------------------
# 4. Vector-climate associations
# ---------------------------------------------------------------------------

.section("Vector-climate associations  (analyze_vector_climate)")

vc <- res$vector_climate
if (.has_error(vc)) {
  .write("error: ", .get_error(vc))
} else if (is.list(vc)) {
  .write("aggregation:       ", .val(vc$aggregation))
  .write("vector_col:        ", .val(vc$vector_col))
  .write("climate_variables: ", .val(vc$climate_variables))
  .write("group_cols:        ", .val(vc$group_cols))
  
  s <- vc$summary
  if (is.list(s)) {
    .write("total_observations:", .val(s$total_observations))
    .write("date_range:        ",
           paste(format(s$date_range), collapse = " to "))
    .write("mean_vector:       ", .val(s$mean_vector))
    .write("max_vector:        ", .val(s$max_vector))
    .write("n_groups:          ", .val(s$n_groups))
  }
  
  .print_df(vc$correlations,     caption = "\nPearson correlations:")
  .print_df(vc$optimal_lags,     caption = "\noptimal lags (max r):")
  .print_df(vc$lag_correlations, caption = "\nlag correlations (first 24 rows):",
            max_rows = 24)
  .print_df(vc$by_group_correlations,
            caption = "\nper-group correlations:")
}

# ---------------------------------------------------------------------------
# 5. Insecticide resistance
# ---------------------------------------------------------------------------

.section("Insecticide resistance  (analyze_resistance)")

rs <- res$resistance_summary
if (.has_error(rs)) {
  .write("error: ", .get_error(rs))
} else if (is.list(rs)) {
  .write("n_observations: ", .val(rs$n_observations))
  .write("n_insecticides: ", .val(rs$n_insecticides))
  .print_df(rs$status_by_class,          caption = "\nstatus by chemical class:")
  .print_df(rs$mortality_by_insecticide, caption = "\nmortality by insecticide:")
  .print_df(rs$temporal,                 caption = "\ntemporal trend:")
}

# ---------------------------------------------------------------------------
# 6. Bionomics
# ---------------------------------------------------------------------------

.section("Vector bionomics  (analyze_bionomics)")

bs <- res$bionomics_summary
if (.has_error(bs)) {
  .write("error: ", .get_error(bs))
} else if (is.list(bs)) {
  .write("n_observations: ", .val(bs$n_observations))
  for (nm in c("feeding", "biting_loc", "resting", "larval")) {
    .print_df(bs[[nm]], caption = paste0("\n", nm, ":"))
  }
}

# ---------------------------------------------------------------------------
# 7. Interventions
# ---------------------------------------------------------------------------

.section("Intervention summary  (analyze_intervention_summary)")

iv <- res$intervention_summary
if (.has_error(iv)) {
  .write("error: ", .get_error(iv))
} else if (is.list(iv)) {
  .write("n_interventions: ", .val(iv$n_interventions))
  .write("n_types:         ", .val(iv$n_types))
  .write("date_range:      ",
         paste(format(iv$date_range), collapse = " to "))
  .print_df(iv$by_type,          caption = "\nby type:")
  .print_df(iv$by_year,          caption = "\nby year and type:")
  .print_df(iv$coverage_by_type, caption = "\ncoverage by type:")
} else {
  .write("(not present in analysis_results)")
}

.section("Intervention impact on cases  (analyze_intervention_impact)")

ib <- res$intervention_burden
if (.has_error(ib)) {
  .write("error: ", .get_error(ib))
} else if (is.list(ib)) {
  .write("outcome_col:      ", .val(ib$outcome_col))
  .write("window_months:    ", .val(ib$window_months))
  .write("n_events:         ", .val(ib$n_events))
  .write("n_events_usable:  ", .val(ib$n_events_usable))
  .print_df(ib$events,  caption = "\nper-event pre/post:")
  .print_df(ib$by_type, caption = "\nby intervention type:")
} else {
  .write("(not present in analysis_results)")
}

.section("Intervention impact on vectors  (analyze_intervention_impact)")

ivv <- res$intervention_vector
if (.has_error(ivv)) {
  .write("error: ", .get_error(ivv))
} else if (is.list(ivv)) {
  .write("outcome_col:      ", .val(ivv$outcome_col))
  .write("window_months:    ", .val(ivv$window_months))
  .write("n_events:         ", .val(ivv$n_events))
  .write("n_events_usable:  ", .val(ivv$n_events_usable))
  .print_df(ivv$events,  caption = "\nper-event pre/post:")
  .print_df(ivv$by_type, caption = "\nby intervention type:")
} else {
  .write("(not present in analysis_results)")
}

# ---------------------------------------------------------------------------
# 8. Case seasonality
# ---------------------------------------------------------------------------

.section("Case seasonality  (analyze_seasonal)")

se <- res$seasonal
if (.has_error(se)) {
  .write("error: ", .get_error(se))
} else if (is.list(se)) {
  .print_df(se$monthly, caption = "\nmonthly mean / sd / n:")
  .print_df(se$yoy,     caption = "\nyear-over-year (first 24 rows):",
            max_rows = 24)
  
  if (is.null(se$decomposition)) {
    .write("decomposition:  (none)")
  } else {
    # stats::decompose() returns an object of class "decomposed.ts" with
    # slots x (the original ts), seasonal, trend, random, figure, type.
    # The time series is $x, not $ts.
    freq <- tryCatch(frequency(se$decomposition$x), error = function(e) NA)
    .write("decomposition:  ", class(se$decomposition)[1],
           " (frequency = ", freq, ")")
  }
}

# ---------------------------------------------------------------------------
# 9. Figures written (paths only)
# ---------------------------------------------------------------------------

.section("Figures written (paths only, not embedded)")

fig_fields <- c(
  "fig_vector_seasonal",
  "fig_vector_climate",
  "fig_resistance",
  "fig_bionomics",
  "fig_intervention_summary",
  "fig_intervention_burden",
  "fig_intervention_vector",
  "fig_seasonal"
)

for (f in fig_fields) {
  p <- res[[f]]
  .write(sprintf("%-28s %s", paste0(f, ":"),
                 if (length(p) == 0) "(none)"
                 else paste(p, collapse = ", ")))
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

.write("")
.rule()
.write("End of summary")
.rule()

