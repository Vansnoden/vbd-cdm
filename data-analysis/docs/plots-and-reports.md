# Plots and reports

## The shared theme

`R/plot_helpers.R` provides the visual language for every figure in the
project. All `plot_*` functions call `theme_cdm()`, and most pull colours
from `cdm_palette`. Consistency across figures is maintained by using
these helpers rather than `ggplot2::theme_minimal()` directly.

### `theme_cdm(base_size = 11)`

A `ggplot2::theme_minimal()` with these overrides:

| Element | Setting |
|---|---|
| `panel.grid.minor` | Blank |
| `panel.grid.major` | `grey92`, `linewidth = 0.3` |
| `plot.title` | Bold, `base_size + 1` |
| `plot.subtitle` | `grey40`, `base_size - 1` |
| `plot.caption` | `grey50`, `base_size - 2` |
| `legend.position` | `"bottom"` |
| `legend.title` | Bold |
| `strip.text` | Bold |
| `axis.title` | `grey20` |
| `axis.text` | `grey30` |

Use it as the last `+` in a `ggplot` chain. To override a single element
for one figure, add a second `theme()` call *after* `theme_cdm()`:

```r
ggplot2::ggplot(...) +
  ... +
  theme_cdm() +
  ggplot2::theme(legend.position = "right")
```

### `cdm_palette`

A named list of hex strings and one named colour:

```r
cdm_palette <- list(
  primary    = "#1E40AF",   # deep blue   — main series
  secondary  = "#3B82F6",   # mid blue    — comparison series
  accent     = "#93C5FD",   # pale blue   — ribbons, fills
  positive   = "#3B82F6",   # same as secondary
  negative   = "#EF4444",   # red         — negative / resistant
  neutral    = "#9CA3AF",   # grey        — "other", "(null)"
  highlight  = "#F59E0B",   # amber       — "suspected resistance"
  grid       = "grey92"
)
```

Which colour means what in the current figures:

| Colour | Used for |
|---|---|
| `primary` | Species composition bars, monthly climatology bars, lag-correlation lines |
| `secondary` | Scatter points in climate-association panels, mortality bars |
| `accent` | Confidence-interval ribbons, climate-envelope fills |
| `negative` | Resistant fraction, negative correlations, vector series in the seasonal overlay |
| `neutral` | `(null)` category bars, "other" in location panels |
| `highlight` | "Suspected Resistance" fill in the status stack |

Do not hard-code hex strings in new plot functions. Add a named entry to
`cdm_palette` instead, even if it duplicates an existing colour — the
semantic name is the point.

### Helper functions

| Function | Signature | Purpose |
|---|---|---|
| `significance_stars(p)` | p-value → `""`, `"*"`, `"**"`, `"***"`, `"ns"` | Vectorised. For annotating correlation tables and bar charts. |
| `format_p(p)` | p-value → `"p < 0.001"`, `"p = 0.042"`, `"p = NA"` | Vectorised. For figure subtitles. |
| `ensure_dir(path)` | `fs::dir_create(path)` and returns `path` invisibly | Idempotent directory creation. |
| `save_plot(plot, path, width = 8, height = 5)` | Wraps `ggsave` at 300 dpi with `bg = "white"` | Convenience wrapper. **Not used by the current plot functions** — see *Known inconsistencies* below. |

---

## Figure naming convention

Every figure written by a `plot_*` function follows the pattern:

```
<study_name>_<analysis>[_<variable>].png
```

`study_name` comes from `cfg$study_name`. With the default config
(`study_name: "Pan_African_Anopheles"`), the figures are:

| Analysis | File name |
|---|---|
| Vector seasonality | `Pan_African_Anopheles_vector_seasonal.png` |
| Vector–climate correlation bar chart | `Pan_African_Anopheles_vector_climate_correlations.png` |
| Vector–climate per-variable (t2m) | `Pan_African_Anopheles_vector_climate_t2m.png` |
| Vector–climate per-variable (tp) | `Pan_African_Anopheles_vector_climate_tp.png` |
| Vector–climate by-group heatmap | `Pan_African_Anopheles_vector_climate_by_group.png` |
| Insecticide resistance | `Pan_African_Anopheles_resistance.png` |
| Vector bionomics | `Pan_African_Anopheles_bionomics.png` |
| Case seasonality | `Pan_African_Anopheles_seasonal.png` |

The by-group heatmap is written only when `group_cols` is non-empty in
`analyze_vector_climate()`. The pipeline sets `group_cols =
c("species_name")`, so it *is* written for every run — but only if at
least one group has a non-empty correlation table.

### Why the convention matters

The report loads some figures by **path convention** rather than from the
`analysis_results` list. See *The analysis report* below. If you rename a
plot function's output file, update the report's path expression too —
they are not derived from each other.

### Dimensions

All figures are 300 dpi with a white background (`bg = "white"`). Sizes:

| Figure | Width (in) | Height (in) |
|---|---|---|
| Vector seasonality | 12 | 10 |
| Vector–climate correlation bar | 8 | 4 |
| Vector–climate per-variable | 12 | 10 |
| Vector–climate by-group heatmap | 8 | 6 |
| Resistance | 12 | 8 |
| Bionomics | 12 | 8 |
| Case seasonality | 12 | 5 |

---

## The reports

Two Quarto documents live in `inst/report/`. Both are rendered by
`tar_quarto()` as file targets; both consume sanitised parameter objects
rather than the live analysis results. See *NA and Quarto* below.

Both use the same YAML front-matter pattern:

```yaml
---
title: "..."
format:
  html:
    toc: true
    code-fold: true
params:
  cfg: null
  results: null     # or `qa: null` in qa_report.qmd
---
```

`code-fold: true` collapses every code chunk behind a "Show code" toggle
in the rendered HTML. This is deliberate: the reports are meant to be
read by non-programmers, but the code should be auditable without
leaving the page.

### `qa_report.qmd`

**Path:** `inst/report/qa_report.qmd`
**Target:** `qa_report`
**Params:** `qa`, `cfg`

Data-availability inventory. Renders six sections:

1. **Configuration.** A `gt` table of every `cfg` field. Missing values
   render as `"(missing)"`, empty `location_ids` as
   `"(name-based resolution)"`, null `species_filter` as `"(all)"`.
2. **Table inventory.** Row counts returned by each query, from
   `qa$tables` (built by `summarize_available_data()` in `R/qa.R`).
3. **Species present in vector query.** From `qa$species`. If empty,
   prints a bold warning: *"No vector rows returned. Verify
   `species_filter` and `location_ids`."*
4. **Species filter coverage.** For each value in `cfg$species_filter`,
   whether it matched at least one row. This is how you catch a filter
   value that silently matches nothing.
5. **Environment variables.** From `qa$environment_vars`, showing which
   of `t2m`, `tp`, `d2m` are present and non-null.
6. **Monthly case counts.** A line plot from `qa$monthly_cases`. If
   empty, prints an italic note explaining that case-based analyses are
   contextual and an empty case table is expected in some deployments.

**Read this report first.** When a downstream figure looks wrong, the QA
report tells you whether the data are missing, filtered out, or
mis-joined — before you start debugging the analysis functions.

### `analysis_report.qmd`

**Path:** `inst/report/analysis_report.qmd`
**Target:** `analysis_report`
**Params:** `cfg`, `results`

The narrative report. Renders seven sections in this order:

| # | Section | Reads from `results` |
|---|---|---|
| 1 | Scope | — |
| 2 | Data availability | `qa$tables`, `cases_available` |
| 3 | Vector species composition and seasonality | `vector_seasonal`, `fig_vector_seasonal` |
| 4 | Vector–climate associations | `vector_climate`, plus figures by path |
| 5 | Insecticide resistance | `resistance_summary`, `fig_resistance` |
| 6 | Vector bionomics | `bionomics_summary`, `fig_bionomics` |
| 7 | Case data (context) | `cases_available`, `seasonal`, `fig_seasonal` |

### The error-handling pattern

Every section is wrapped in a three-way check:

```r
vs <- res$vector_seasonal
if (!is.null(err_of(vs))) {
  note("Vector seasonal analysis did not run: ", err_of(vs))
} else if (is.data.frame(vs$composition) && nrow(vs$composition) > 0) {
  vs$composition |> gt::gt() |> gt::tab_header(title = "Species composition")
} else {
  note("No species composition available.")
}
```

Three helpers are defined in the setup chunk:

```r
# Return the error string if a result list carries one, otherwise NULL.
err_of <- function(x) if (is.list(x) && !is.null(x$error)) x$error else NULL

# Print an italicised note.
note <- function(...) cat(paste0("_", paste0(..., collapse = ""), "_\n"))

# Null-coalescing operator.
`%||%` <- function(a, b) if (is.null(a)) b else a
```

The pattern has three cases:

1. **Result carries an error.** Print the error message as an italic
   note. This is the `list(error = "…")` path returned by every
   `analyze_*` function when its input is empty.
2. **Result has data.** Render the table or figure.
3. **Result is empty but not an error.** Print a generic italic note.
   This happens when the analysis ran but produced an empty tibble —
   e.g. a bionomics column that was all-`NA`.

The report never aborts. This is why the pipeline renders end-to-end
even when `fact_disease_case` is empty.

### Figures: two path sources

Sections 3, 5, 6, and 7 use the file path stored in `analysis_results`:

```r
p <- res$fig_vector_seasonal
if (is.character(p) && length(p) >= 1 && file.exists(p[1])) {
  knitr::include_graphics(p[1])
} else {
  note("No vector seasonal figure available.")
}
```

Note `p[1]` — the report treats every `fig_*` as a character vector, even
though most `plot_*` functions return a single string. `plot_vector_climate`
returns a vector (one per variable plus the correlation bar and the
optional heatmap), and the report uses only the first element when it
renders the section header figure.

Section 4 (vector–climate) takes a different approach. It **reconstructs
paths by convention** rather than reading them from `results`:

```r
fig_dir <- file.path(cfg$output_dir %||% "outputs/analysis", "figures")
p <- file.path(fig_dir,
               glue("{cfg$study_name}_vector_climate_correlations.png"))
if (file.exists(p)) knitr::include_graphics(p)

for (v in vc$climate_variables) {
  p <- file.path(fig_dir,
                 glue("{cfg$study_name}_vector_climate_{v}.png"))
  if (file.exists(p)) {
    cat("\n\n### ", v, "\n\n")
    knitr::include_graphics(p)
  }
}
```

The reason for the divergence: `plot_vector_climate` returns a vector of
paths in no guaranteed order (the correlation bar first, then per-variable
figures in `result$climate_variables` order, then the optional heatmap).
To label each per-variable figure with its variable name, the report has
to know *which* file corresponds to *which* variable. Reconstructing from
`cfg$study_name` and the variable name is the simplest way to get that
mapping.

**Consequence:** if you change the file-naming pattern inside
`plot_vector_climate`, you must also change the two `glue()` expressions
in the report. They are not linked by code.

---

## Rendering manually

Rendering by hand is useful when iterating on a `.qmd`. Once you're happy
with the change, `targets::tar_make()` will pick it up automatically.

```r
targets::tar_load(cfg_sanitized)
targets::tar_load(results_sanitized)

quarto::quarto_render(
  "inst/report/analysis_report.qmd",
  execute_params = list(
    cfg     = cfg_sanitized,
    results = results_sanitized
  )
)
```

For the QA report:

```r
targets::tar_load(qa_sanitized)
targets::tar_load(cfg_sanitized)

quarto::quarto_render(
  "inst/report/qa_report.qmd",
  execute_params = list(qa = qa_sanitized, cfg = cfg_sanitized)
)
```

Both reports write their HTML output next to the source `.qmd` by
default. `tar_quarto` redirects the output to
`outputs/analysis/reports/` via its `output_file` handling — check
`targets::tar_meta(qa_report, fields = "path")` to see the exact
destination after a run.

---

## NA and Quarto

Quarto's YAML 1.2 parameter round-trip cannot represent `NA`. Passing an
R object containing `NA` as a `tar_quarto` parameter produces a cryptic
error about a YAML scalar.

`_targets.R` therefore sanitises every parameter object before passing it:

```r
sanitize_for_quarto <- function(x) {
  if (is.data.frame(x)) {
    x[] <- lapply(x, sanitize_for_quarto)
    return(x)
  }
  if (is.list(x)) {
    return(lapply(x, sanitize_for_quarto))
  }
  if (is.character(x)) {
    x[is.na(x)] <- ""
    return(x)
  }
  if (is.numeric(x) && anyNA(x)) {
    x[is.na(x)] <- NaN
    return(x)
  }
  if (is.logical(x) && anyNA(x)) {
    return(ifelse(is.na(x), "", as.character(x)))
  }
  x
}
```

| Type | `NA` becomes |
|---|---|
| character | `""` |
| numeric | `NaN` |
| logical | `""` (as character) |
| data.frame | recursed column-wise |
| list | recursed |

Three sanitised targets exist:

| Target | Sanitises |
|---|---|
| `cfg_sanitized` | `cfg` |
| `qa_sanitized` | `qa` |
| `results_sanitized` | `analysis_results` (the whole bundle) |

### Consequences

- **Character `NA` and empty string become indistinguishable** in the
  report. The QA configuration table shows `"(missing)"` for absent
  values because `cfg$study_name` etc. are never `NA` — they are
  guaranteed by `config::get()`. But `cfg$disease_code`, which *can* be
  `NULL`, is checked explicitly:

  ```r
  if (is.null(cfg$disease_code)) "(none)" else cfg$disease_code
  ```

- **Numeric `NA` becomes `NaN`.** `is.na(NaN)` is `TRUE` in R, so
  downstream `if (is.na(x))` checks still work. Displayed values show as
  `NaN`, not `NA`.

- **Logical `NA` becomes `""`, a character.** If your analysis has a
  logical column with `NA`, it will arrive in the report as a character
  column. Do not rely on logical semantics for parameter objects.

- **`NULL` is preserved.** `NULL` serialises fine through YAML; only
  `NA` is problematic. This is why the report prefers `is.null()` checks
  over `is.na()` checks on parameter fields.

If you need a value to survive as `NA` specifically, pass it outside the
parameters — for example, as a file path the report reads, or as a
substitute in `knitr::knit_child()`. This is not currently done anywhere
in the project.

---

## Extending a report

### Adding a section

1. Write the analysis and plot functions. See
   [Pipeline → extending](pipeline.md#extending).
2. Add both to the `analysis_results` bundle in `_targets.R`.
3. Add a section to `analysis_report.qmd`, following the three-way
   pattern:

   ````markdown
   ## Trend

   ```{r}
   tr <- res$trend
   if (!is.null(err_of(tr))) {
     note("Trend analysis did not run: ", err_of(tr))
   } else if (is.data.frame(tr$monthly) && nrow(tr$monthly) > 0) {
     tr$monthly |> gt::gt() |> gt::tab_header(title = "Monthly counts")
   } else {
     note("No trend data available.")
   }
   ```

   ```{r, fig.width=12, fig.height=5}
   p <- res$fig_trend
   if (is.character(p) && length(p) >= 1 && file.exists(p[1])) {
     knitr::include_graphics(p[1])
   } else {
     note("No trend figure available.")
   }
   ```