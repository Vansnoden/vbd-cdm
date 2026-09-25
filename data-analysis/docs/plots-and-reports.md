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

A named list of hex strings:

```r
cdm_palette <- list(
  primary    = "#1E40AF",   # deep blue   - main series
  secondary  = "#3B82F6",   # mid blue    - comparison series
  accent     = "#93C5FD",   # pale blue   - ribbons, fills
  positive   = "#3B82F6",   # same as secondary
  negative   = "#EF4444",   # red         - negative / resistant
  neutral    = "#9CA3AF",   # grey        - "other", "(null)"
  highlight  = "#F59E0B",   # amber       - "suspected resistance"
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
`cdm_palette` instead, even if it duplicates an existing colour - the
semantic name is the point.

### Helper functions

| Function | Signature | Purpose |
|---|---|---|
| `significance_stars(p)` | p-value -> `""`, `"*"`, `"**"`, `"***"`, `"ns"` | Vectorised. For annotating correlation tables and bar charts. |
| `format_p(p)` | p-value -> `"p < 0.001"`, `"p = 0.042"`, `"p = NA"` | Vectorised. For figure subtitles. |
| `ensure_dir(path)` | `fs::dir_create(path)` and returns `path` invisibly | Idempotent directory creation. |
| `save_plot(plot, path, width = 8, height = 5)` | Wraps `ggsave` at 300 dpi with `bg = "white"` | Convenience wrapper. **Not used by the current plot functions** - each `plot_*()` calls `ggplot2::ggsave()` directly with its own width / height. |

---

## Figure naming convention

Every figure written by a `plot_*` function follows the pattern:

```
<study_name>_<analysis>[_<variable>].png
```

`study_name` comes from `cfg$study_name`. With the default config from
`config.yml`:

| Analysis | File name |
|---|---|
| Vector seasonality | `Case_Study_vector_seasonal.png` |
| Vector-climate correlation bar chart | `Case_Study_vector_climate_correlations.png` |
| Vector-climate per-variable (t2m) | `Case_Study_vector_climate_t2m.png` |
| Vector-climate per-variable (tp) | `Case_Study_vector_climate_tp.png` |
| Vector-climate by-group heatmap | `Case_Study_vector_climate_by_group.png` |
| Insecticide resistance | `Case_Study_resistance.png` |
| Vector bionomics | `Case_Study_bionomics.png` |
| Case seasonality | `Case_Study_seasonal.png` |

The by-group heatmap is written only when `group_cols` is non-empty in
`analyze_vector_climate()`. The pipeline sets `group_cols =
c("species_name")`, so it *is* written for every run - but only if at
least one group has a non-empty correlation table.

### Why the convention matters

The report loads some figures by **path convention** rather than from the
`analysis_results` list. See *The analysis report* below. If you rename a
plot function's output file, update the report's path expression too -
they are not derived from each other.

### Dimensions

All figures are 300 dpi with a white background (`bg = "white"`). Sizes:

| Figure | Width (in) | Height (in) |
|---|---|---|
| Vector seasonality | 12 | 10 |
| Vector-climate correlation bar | 8 | 4 |
| Vector-climate per-variable | 12 | 10 |
| Vector-climate by-group heatmap | 8 | 6 |
| Resistance | 12 | 8 |
| Bionomics | 12 | 8 |
| Case seasonality | 12 | 5 |

---

## The reports

Two Quarto documents live in `inst/report/`. Both are rendered by
`tar_quarto()` as file targets. Both consume sanitised parameter objects
serialised to RDS by the `qa_rds` and `results_rds` targets. See
*NA and Quarto* below.

Both use the same YAML front-matter pattern:

```yaml
---
title: "..."
format:
  html:
    toc: true
    code-fold: true
    embed-resources: true
params:
  cfg: null
  results_path: null     # or `qa_path: null` in qa_report.qmd
---
```

`code-fold: true` collapses every code chunk behind a "Show code" toggle
in the rendered HTML. This is deliberate: the reports are meant to be
read by non-programmers, but the code should be auditable without
leaving the page.

**Why `*_path` and not the objects directly.** Quarto's YAML 1.2
parameter round-trip cannot faithfully serialise every R object. Simple
lists of tibbles usually survive, but nested structures (a `ggplot`
grob, an S4 object, a raw `call`) do not. The pipeline therefore
serialises the sanitised bundle to RDS and passes the file path. The
report reads it back with `readRDS(params$results_path)` at the top of
its setup chunk.

### `qa_report.qmd`

**Path:** `inst/report/qa_report.qmd`
**Target:** `qa_report`
**Params:** `qa_path`, `cfg`

Data-availability inventory. Renders six sections:

1. **Configuration.** A `gt` table of every `cfg` field. Missing values
   render as `"(missing)"`, empty `countries`/`locations` as `"(all)"`,
   null `species_filter` as `"(all)"`, null `diseases` as `"(all)"`.
2. **Table inventory.** Row counts returned by each query, from
   `qa$tables` (built by `summarize_available_data()` in `R/qa.R`).
3. **Species present in vector query.** From `qa$species`. If empty,
   prints a bold warning: *"No vector rows returned. Verify
   `species_filter` and the location filter."*
4. **Species filter coverage.** For each value in `cfg$species_filter`,
   whether it matched at least one row.
5. **Environment variables.** From `qa$environment_vars`, showing which
   of `t2m`, `tp`, `d2m` are present and non-null.
6. **Monthly case counts.** A line plot from `qa$monthly_cases`. If
   empty, prints an italic note explaining that case-based analyses are
   contextual and an empty case table is expected in some deployments.

**Read this report first.** When a downstream figure looks wrong, the QA
report tells you whether the data are missing, filtered out, or
mis-joined - before you start debugging the analysis functions.

### `analysis_report.qmd`

**Path:** `inst/report/analysis_report.qmd`
**Target:** `analysis_report`
**Params:** `cfg`, `results_path`

The narrative report. Renders seven sections in this order:

| # | Section | Reads from `res` |
|---|---|---|
| 1 | Scope | - |
| 2 | Data availability | `qa$tables`, `cases_available` |
| 3 | Vector species composition and seasonality | `vector_seasonal`, `fig_vector_seasonal` |
| 4 | Vector-climate associations | `vector_climate`, plus figures by path |
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

# Emit raw markdown from inside an R chunk without knitr escaping it.
md <- function(...) knitr::asis_output(paste0(..., collapse = ""))

# Print an italicised note.
note <- function(...) md("_", paste0(..., collapse = ""), "_\n")

# Null-coalescing operator.
`%||%` <- function(a, b) if (is.null(a)) b else a
```

The pattern has three cases:

1. **Result carries an error.** Print the error message as an italic
   note. This is the `list(error = "...")` path returned by every
   `analyze_*` function when its input is empty.
2. **Result has data.** Render the table or figure.
3. **Result is empty but not an error.** Print a generic italic note.
   This happens when the analysis ran but produced an empty tibble -
   e.g. a bionomics column that was all-`NA`.

The report never aborts. This is why the pipeline renders end-to-end
even when `fact_disease_case` is empty.

### Figures: two path sources

Sections 3, 5, 6, and 7 use the file path stored in `analysis_results`:

```r
p <- abs_fig(res$fig_vector_seasonal)
if (length(p) >= 1 && file.exists(p[1])) {
  knitr::include_graphics(p[1])
} else {
  note("No vector seasonal figure available.")
}
```

The `abs_fig()` helper resolves a stored relative path against the
Quarto render directory, so the report works regardless of where
`tar_quarto()` invokes it from.

Note `p[1]` - the report treats every `fig_*` as a character vector, even
though most `plot_*` functions return a single string.
`plot_vector_climate` returns a vector (correlation bar, one per
variable, optional heatmap), and the report uses only the first element
when it renders the section header figure.

Section 4 (vector-climate) takes a different approach. It **reconstructs
paths by convention** rather than reading them from `results`:

```r
fig_dir <- here::here(cfg$output_dir %||% "outputs/analysis", "figures")
p <- file.path(fig_dir,
               glue("{cfg$study_name}_vector_climate_correlations.png"))
if (file.exists(p)) knitr::include_graphics(p)

for (v in vc$climate_variables) {
  p <- file.path(fig_dir,
                 glue("{cfg$study_name}_vector_climate_{v}.png"))
  if (file.exists(p)) {
    md(sprintf("\n\n### %s\n\n", v))
    knitr::include_graphics(p)
  }
}
```

The reason for the divergence: `plot_vector_climate` returns a vector of
paths in no guaranteed order. To label each per-variable figure with its
variable name, the report has to know *which* file corresponds to
*which* variable. Reconstructing from `cfg$study_name` and the variable
name is the simplest way to get that mapping.

**Consequence:** if you change the file-naming pattern inside
`plot_vector_climate`, you must also change the two `glue()` expressions
in the report. They are not linked by code.

---

## Rendering manually

Rendering by hand is useful when iterating on a `.qmd`. Once you're happy
with the change, `targets::tar_make()` will pick it up automatically.

```r
targets::tar_load(cfg_sanitized)
targets::tar_load(results_rds)

quarto::quarto_render(
  "inst/report/analysis_report.qmd",
  execute_params = list(
    cfg          = cfg_sanitized,
    results_path = results_rds
  )
)
```

For the QA report:

```r
targets::tar_load(qa_sanitized)
targets::tar_load(cfg_sanitized)
targets::tar_load(qa_rds)

quarto::quarto_render(
  "inst/report/qa_report.qmd",
  execute_params = list(
    qa_path = qa_rds,
    cfg     = cfg_sanitized
  )
)
```

`tar_quarto()` writes the rendered HTML next to the source `.qmd`
(`inst/report/`). The `*_report_published` targets then copy it to
`outputs/analysis/reports/`. Running `quarto::quarto_render()` by hand
writes it to `inst/report/` only; the published copy is a `targets`
side effect.

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
  values because `cfg$study_name` etc. are never `NA` - they are
  guaranteed by `read_cfg()`. Fields that can legitimately be `NULL`
  (`cfg$species_filter`, `cfg$diseases`, `cfg$countries`,
  `cfg$locations`) are checked with `is.null()` and rendered as
  `"(all)"`.

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
parameters - for example, through the RDS file the report reads.

---

## Extending a report

### Adding a section

1. Write the analysis and plot functions. See
   [Pipeline → extending](pipeline.md#extending).
2. Add both to the `analysis_results` bundle in `_targets.R`. Because
   `results_sanitized` and `results_rds` depend on `analysis_results`,
   the new fields propagate automatically.
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
   p <- abs_fig(res$fig_trend)
   if (length(p) >= 1 && file.exists(p[1])) {
     knitr::include_graphics(p[1])
   } else {
     note("No trend figure available.")
   }
   ```