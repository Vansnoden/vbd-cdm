# Pipeline

The pipeline is defined in `_targets.R` at the project root. It is a
directed acyclic graph (DAG) of ~30 targets. `targets` runs only what
needs running, in the right order, and caches the rest on disk.

The pipeline is **vector-centred**: extraction and analysis of vectors,
environment, resistance, and bionomics all run unconditionally; case
data runs conditionally and does not block anything downstream.

---

## Running

```r
# Run everything that's out of date
targets::tar_make()

# Run one target and its dependencies
targets::tar_make(names = "fig_vector_climate")

# Run in-process (easier to debug; no child R sessions)
targets::tar_make(callr_function = NULL)
```

The first run will take several minutes. Subsequent runs are incremental.
The pipeline is safe to interrupt; `targets` commits each target's value
atomically, so an interrupted run leaves the cache consistent.

### A note on `config::get()` failures

`_targets.R` runs `config::get("study_name")` and similar calls at parse
time, before any target is defined. If a key is missing from
`config.yml`, `tar_make()` will error before doing any work, with a
message like:

```
Error: The config package could not find the config value 'study_name'.
```

Nine keys are read this way, with no fallback:

```
study_name, location, country, start_year, end_year,
disease_code, env_variables, max_lag_months, database
```

Even `disease_code` must be present — set it to `null` if you don't want
to filter. Only `location_ids`, `include_estimated_cases`,
`species_filter`, and `output_dir` use the safer `cfg_get()` wrapper
that returns a default on failure.

---

## Inspecting

```r
targets::tar_manifest()                # every target and its command
targets::tar_visnetwork()              # interactive DAG
targets::tar_progress()                # which targets ran / skipped / errored
targets::tar_outdated()                # what would run on next tar_make()
targets::tar_meta()                    # run times, warnings, errors, sizes
targets::tar_meta(fields = c("name", "time", "warnings", "error"))

targets::tar_load(analysis_results)    # load a target into the session
targets::tar_read(vectors)             # read a target without loading it
```

`tar_load()` requires `targets` to be attached. From a fresh session:

```r
targets::tar_load(analysis_results)
```

---

## Invalidating

```r
targets::tar_invalidate(seasonal)                    # force re-run of one target
targets::tar_invalidate(dplyr::everything())         # force re-run of everything
targets::tar_destroy()                               # wipe the cache
```

`targets` invalidates a target automatically when:

- the target's command changes (you edited the function it calls),
- any upstream target's value changes,
- any global that the target references changes (see *Config handling*
  below),
- the packages listed in `tar_option_set(packages = …)` change.

It does **not** detect changes to the SQLite database. See *Database
invalidation* below.

---

## Target graph

```
   ┌──────────────────────────────────────────────────────────┐
   │  cfg (global, from config.yml)                           │
   └────────────────────────────┬─────────────────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │                                   │
       ┌──────▼──────┐                            │
       │  locations  │                            │
       └──────┬──────┘                            │
              │                                   │
   ┌──────────┼──────────┬──────────┬──────────┐  │
   │          │          │          │          │  │
┌──▼───┐ ┌────▼────┐ ┌───▼────┐ ┌───▼─────┐ ┌──▼───┐
│vectors│ │environ- │ │resist- │ │bionomics│ │cases │
│      │ │ ment    │ │ ance   │ │         │ │      │
└──┬───┘ └────┬────┘ └───┬────┘ └───┬─────┘ └──┬───┘
   │          │          │          │          │
   │          │          │          │     ┌────▼────┐
   │          │          │          │     │ cases_  │
   │          │          │          │     │ avail.  │
   │          │          │          │     └────┬────┘
   │          │          │          │          │
   │          │          │          │     ┌────▼──────┐
   │          │          │          │     │ cases_    │
   │          │          │          │     │ validated │
   │          │          │          │     └────┬──────┘
   │          │          │          │          │
   │          │          │          │          │
┌──▼──────┐ ┌─▼───────┐ │          │          │
│ vectors_│ │environ- │ │          │          │
│validated│ │ment_val.│ │          │          │
└──┬──────┘ └────┬────┘ │          │          │
   │             │      │          │          │
   │             │  ┌───▼─────┐    │          │
   │             │  │resist-  │    │          │
   │             │  │ance_summ│    │          │
   │             │  └───┬─────┘    │          │
   │             │      │          │          │
   │             │      │      ┌───▼──────┐   │
   │             │      │      │bionomics_│   │
   │             │      │      │summary   │   │
   │             │      │      └───┬──────┘   │
   │             │      │          │          │
┌──▼──────────┐  │      │          │          │
│vector_      │  │      │          │          │
│seasonal     │  │      │          │          │
└──┬──────────┘  │      │          │          │
   │             │      │          │          │
   │  ┌──────────▼───┐  │          │          │
   │  │vector_climate│  │          │          │
   │  └──────┬───────┘  │          │          │
   │         │          │          │          │
   │         │          │          │   ┌──────▼────────┐
   │         │          │          │   │   seasonal    │
   │         │          │          │   └──────┬────────┘
   │         │          │          │          │
   │         │          │          │          │
┌──▼─────────▼──────────▼──────────▼──────────▼─────────┐
│                    qa                                 │
│  (cases_validated, vectors_validated, environment_    │
│   validated, locations, cfg)                          │
└────────────────────────┬──────────────────────────────┘
                         │
   ┌─────────────────────┴─────────────────────────────┐
   │              figures                              │
   │  fig_vector_seasonal ◀── vector_seasonal, cfg     │
   │  fig_vector_climate  ◀── vector_climate, cfg      │
   │  fig_resistance      ◀── resistance_summary, cfg  │
   │  fig_bionomics       ◀── bionomics_summary, cfg   │
   │  fig_seasonal        ◀── seasonal, cfg            │
   └──────────────────────┬────────────────────────────┘
                          │
                  ┌───────▼──────────┐
                  │ analysis_results │
                  │ (list of all)    │
                  └───────┬──────────┘
                          │
   ┌──────────────────────┼──────────────────────────┐
   │                      │                          │
┌──▼─────────┐ ┌──────────▼────────┐ ┌───────────────▼──┐
│cfg_        │ │qa_sanitized       │ │results_sanitized │
│sanitized   │ │                   │ │                  │
└──┬─────────┘ └────────┬──────────┘ └────────┬─────────┘
   │                    │                     │
   │                    │                     │
   │            ┌───────▼───────┐    ┌────────▼────────┐
   │            │  qa_report    │    │ analysis_report │
   │            │  (.qmd)       │    │ (.qmd)          │
   │            └───────────────┘    └─────────────────┘
   │
   └───────────────▶ analysis_report (also reads cfg_sanitized)
```

`cfg` is a **global**, not a `tar_target`. `targets` detects it as a
dependency of every target that references it, and hashes its value. See
*Config handling* below.

`fig_dir` is also a global (a plain string), and is passed as the
`out_dir` argument to every plot function. Changing `cfg$output_dir`
changes `fig_dir`, which changes the plot targets' commands — but only
because `cfg` changed. If you edit `fig_dir` directly in `_targets.R`
without changing `cfg`, `targets` will *not* invalidate the plot targets.

---

## Target reference

### Extraction

| Target | Command | Notes |
|---|---|---|
| `locations` | `resolve_location_ids(cfg$location, cfg$country, db_path = cfg$db_path, explicit_ids = cfg$location_ids)` | Aborts on zero matches. |
| `vectors` | `query_vectors(cfg, locations$location_id, species_filter = cfg$species_filter)` | |
| `environment` | `query_environment(cfg, locations$location_id)` | |
| `resistance` | `query_resistance(cfg, locations$location_id)` | |
| `bionomics` | `query_bionomics(cfg, locations$location_id)` | |
| `cases` | `query_cases(cfg, locations$location_id)` | Excludes `'ESTIMATE'` rows unless `include_estimated_cases`. |
| `burden` | `query_burden(cfg, locations$location_id)` | |

### Validation

| Target | Behaviour |
|---|---|
| `vectors_validated` | Aborts the pipeline with `cli::cli_abort()` if `nrow(vectors) == 0`. Message includes the resolved location IDs and species filter. |
| `environment_validated` | Aborts with `cli::cli_abort()` if `nrow(environment) == 0`. Message includes the resolved location IDs and env variables. |
| `cases_available` | Boolean: `nrow(cases) > 0`. Consumed by the report. |
| `cases_validated` | Warns with `cli::cli_warn()` if empty, and returns `cases` unchanged. **Does not abort.** This is why the pipeline runs end-to-end on a database with no case data. |

### QA

| Target | Command |
|---|---|
| `qa` | `summarize_available_data(cases_validated, vectors_validated, environment_validated, locations, cfg)` |

### Analysis

| Target | Command |
|---|---|
| `vector_seasonal` | `analyze_vector_seasonal(vectors_validated)` |
| `vector_climate` | `analyze_vector_climate(vectors = vectors_validated, environment = environment_validated, climate_variables = cfg$env_variables, group_cols = c("species_name"), aggregation = "monthly", max_lag = cfg$max_lag_months, min_n = 5L, fdr_correct = TRUE)` |
| `resistance_summary` | `analyze_resistance(resistance)` |
| `bionomics_summary` | `analyze_bionomics(bionomics)` |
| `seasonal` | `analyze_seasonal(cases_validated)` |

Note that `resistance_summary` and `bionomics_summary` depend on the
**unvalidated** extraction targets. There is no `resistance_validated` or
`bionomics_validated`. This is intentional: an empty resistance dataset
should produce a `list(error = "…")` from `analyze_resistance`, not abort
the pipeline. The same applies to `seasonal` (via `cases_validated`).

### Figures

| Target | Command |
|---|---|
| `fig_vector_seasonal` | `plot_vector_seasonal(vector_seasonal, cfg$study_name, fig_dir)` |
| `fig_vector_climate` | `plot_vector_climate(result = vector_climate, study_name = cfg$study_name, out_dir = fig_dir)` |
| `fig_resistance` | `plot_resistance(resistance_summary, cfg$study_name, fig_dir)` |
| `fig_bionomics` | `plot_bionomics(bionomics_summary, cfg$study_name, fig_dir)` |
| `fig_seasonal` | `plot_seasonal(seasonal, cfg$study_name, fig_dir)` |

Each returns a character vector of written file paths (invisibly). On
error, they warn and return `character()` — they never abort.

### Bundling and sanitisation

| Target | Purpose |
|---|---|
| `analysis_results` | A single list containing `cfg`, `locations`, all extraction outputs, all analysis results, all figure paths, `cases_available`, and `qa`. This is what the report consumes. |
| `cfg_sanitized` | `sanitize_for_quarto(cfg)` — recursive `NA` replacement. |
| `qa_sanitized` | `sanitize_for_quarto(qa)`. |
| `results_sanitized` | `sanitize_for_quarto(analysis_results)`. |

### Reports

| Target | Command |
|---|---|
| `qa_report` | `tar_quarto(path = "inst/report/qa_report.qmd", quiet = FALSE, execute_params = list(qa = qa_sanitized, cfg = cfg_sanitized))` |
| `analysis_report` | `tar_quarto(path = "inst/report/analysis_report.qmd", quiet = FALSE, execute_params = list(cfg = cfg_sanitized, results = results_sanitized))` |

Both use `quiet = FALSE`, so Quarto's own output is visible during
`tar_make()`. This is deliberate — the render is the slowest single step,
and the progress lines are useful.

---

## Config handling

`_targets.R` builds a single object, `cfg`, from `config.yml` at parse
time:

```r
cfg <- new_analysis_config(
  study_name              = config::get("study_name"),
  location                = config::get("location"),
  country                 = config::get("country"),
  location_ids            = normalize_list(cfg_get("location_ids")),
  start_year              = config::get("start_year"),
  end_year                = config::get("end_year"),
  disease_code            = config::get("disease_code"),
  include_estimated_cases = isTRUE(cfg_get("include_estimated_cases", FALSE)),
  env_variables           = config::get("env_variables"),
  species_filter          = normalize_list(cfg_get("species_filter")),
  max_lag_months          = config::get("max_lag_months"),
  db_path                 = config::get("database"),
  output_dir              = {
    od <- cfg_get("output_dir")
    if (is.null(od)) "outputs/analysis" else od
  }
)
```

### `cfg` is a global, not a target

`cfg` is a plain R object in `_targets.R`'s environment. It is **not** a
`tar_target()`. However, `targets` detects it as a dependency of every
target that references it, and hashes its value. So changing any key in
`config.yml` invalidates every target that reads `cfg` — which is nearly
all of them, including the extraction queries.

The practical consequence: changing `study_name` invalidates
`query_vectors`, `query_environment`, `query_cases`, and the rest, even
though `study_name` has no effect on the SQL. On a large database this
turns a 30-second re-run into a 30-minute one.

If this becomes a problem, the fix is to make `cfg` an explicit target:

```r
tar_target(cfg, new_analysis_config(...))
```

…and then split it into per-consumer targets so `study_name` invalidates
only the plot targets. Neither change is currently made.

### Two config accessors

- `config::get(key)` — errors if `key` is missing. Used for the nine
  required keys.
- `cfg_get(key, default)` — `tryCatch`es and returns `default` on
  failure. Used for the four optional keys.

### `normalize_list()`

YAML `[]` becomes an R `list()` with `length == 0`, and
`as.integer(list())` would error inside `new_analysis_config`. The
`normalize_list()` helper converts empty lists to `NULL` before they
reach the constructor:

```r
normalize_list <- function(x) {
  if (is.null(x) || length(x) == 0) NULL else unlist(x, use.names = FALSE)
}
```

---

## Extending

### Adding a new analysis

1. **Write a pure function** in `R/`. It takes tibbles, returns a tibble
   or list. It must not read files, connect to the DB, call `Sys.time()`,
   or use unseeded randomness.

   ```r
   # R/analysis_trend.R
   analyze_trend <- function(vectors) {
     if (is.null(vectors) || nrow(vectors) == 0) {
       return(list(error = "No vector data available."))
     }
     monthly <- vectors |>
       dplyr::mutate(month = lubridate::floor_date(date, "month")) |>
       dplyr::group_by(month) |>
       dplyr::summarise(n = sum(count, na.rm = TRUE), .groups = "drop")
     fit <- stats::lm(n ~ as.numeric(month), data = monthly)
     list(monthly = monthly, slope = unname(stats::coef(fit)[2]), error = NULL)
   }
   ```

2. **Write a plot function** if the result should be visualised. Same
   contract: pure, takes the analysis result plus `study_name` and
   `out_dir`, returns the written file path(s) invisibly. Use
   `theme_cdm()` and `cdm_palette` from `plot_helpers.R` for consistency.

3. **Add targets** to `_targets.R`, in the analysis section:

   ```r
   tar_target(trend, analyze_trend(vectors_validated)),
   ```

   …and in the figures section:

   ```r
   tar_target(fig_trend, plot_trend(trend, cfg$study_name, fig_dir)),
   ```

4. **Add the results** to the `analysis_results` bundle so the report
   can access them:

   ```r
   tar_target(
     analysis_results,
     list(
       …,
       trend     = trend,
       fig_trend = fig_trend
     )
   ),
   ```

   The bundle is a hard-coded list; new fields do not appear
   automatically.

5. **Add a section** to `inst/report/analysis_report.qmd`, following the
   pattern of the existing sections:

   ````markdown
   ## Trend

   ```{r}
   tr <- res$trend
   if (!is.null(tr$error)) {
     note("Trend analysis did not run: ", tr$error)
   } else {
     tr$monthly |> gt::gt() |> gt::tab_header(title = "Monthly counts")
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