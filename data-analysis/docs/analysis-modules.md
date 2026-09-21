# Analysis modules

Each module lives in `R/analysis_*.R`. They are pure functions: input
tibbles, output lists. The corresponding plot function lives in the same
file.

---

## `analysis_vector_seasonal`

**Function:** `analyze_vector_seasonal(vectors, value_col = NULL)`
**Target:** `vector_seasonal`
**Figure:** `plot_vector_seasonal(result, study_name, out_dir)`

Species composition and seasonal patterns of vector abundance.

| Output | Description |
|---|---|
| `composition` | per-species total counts and share, sorted descending |
| `monthly` | climatological monthly mean (all species pooled) |
| `monthly_by_species` | monthly mean by species |
| `yoy` | year-over-year monthly means (empty tibble if one year) |
| `value_col` | the abundance column actually used |
| `n_observations` | row count of the input |
| `n_species` | distinct species count |

The `value_col` is auto-detected in this order: `count`, then
`density_per_trap`, then the first column of the input. The function
returns `list(error = "…")` only when the input is `NULL` or empty; a
missing `count` column is not an error, it just triggers the fallback.

**Figure:** four-panel (composition bar, monthly climatology, monthly by
species, year-over-year). When only one year of data is present, the
fourth panel is an empty placeholder titled *"Year-over-year (single year
of data)"*.

---

## `analysis_vector_climate`

**Function:**
```r
analyze_vector_climate(
  vectors,
  environment,
  vector_col        = NULL,
  climate_variables = NULL,
  group_cols        = NULL,
  aggregation       = c("monthly", "weekly", "daily"),
  max_lag           = 3L,
  min_n             = 5L,
  fdr_correct       = TRUE
)
```
**Target:** `vector_climate`
**Figure:** `plot_vector_climate(result, study_name, out_dir)`

The most complex module. Computes:

| Output | Description |
|---|---|
| `correlations` | Pearson r between vector abundance and each climate variable |
| `lag_correlations` | r at lags 0…`max_lag`, with 95% CI |
| `optimal_lags` | the lag maximising r, per variable |
| `seasonal` | monthly means normalised to [0, 1], both series |
| `envelope` | mean ± SD vector abundance within deciles of each climate var |
| `by_group_correlations` | per-species correlations (if `group_cols` set) |
| `by_group_lags` | per-species optimal lags |
| `merged_data` | the aggregated, joined panel |
| `summary` | list: total observations, date range, mean/max vector, aggregation, etc. |
| `aggregation`, `vector_col`, `climate_variables`, `group_cols` | the resolved arguments |

**Aggregation.** Vectors and environment are aggregated to a common period
before joining. `"monthly"`, `"weekly"`, or `"daily"`. The aggregation is
applied to the vector side using the *sum* when the column is `count`, and
the *mean* otherwise. Precipitation variables (`tp`, `precipitation`,
`rainfall_total`) are **summed** on the climate side; everything else is
**averaged**.

**Climate variable detection.** When `climate_variables = NULL`, the
function auto-detects numeric columns in `environment`, excluding `date`,
`year`, `month`, and `period`.

**FDR correction.** When `fdr_correct = TRUE` (default), a `p_adj` column
is added to `correlations` and `lag_correlations` using the
Benjamini–Hochberg method.

**Optimal lag selection.** `optimal_lags` picks the lag with the highest
`r`, *not* the lag with the smallest p-value. These can differ when
sample sizes vary across lags (early lags drop the first `lag` rows, so
later lags have fewer paired observations).

**Figure:** a correlation summary bar chart, plus one four-panel figure
per climate variable:

1. Scatter with loess fit and `r` / `p` annotation.
2. Lag correlation curve with 95% CI ribbon.
3. Climate envelope (deciles of the climate variable vs. mean vector
   abundance, ± 1 SD).
4. Seasonal overlay — both series normalised to [0, 1].

If `group_cols` is set, a per-group heatmap of correlations is written as
`<study_name>_vector_climate_by_group.png`.

**Caveat.** When environment is aggregated across many grid cells
(`location_ids = NULL`, or larger than the SQLite parameter budget), the
result is a *continental mean* — a placeholder, not a physically
meaningful series. Per-site climate requires `R/spatial_join.R` to map
vector sites to their nearest grid cell first. See
[Data access → What the queries don't do](data-access.md#what-the-queries-dont-do).

---

## `analysis_resistance`

**Function:** `analyze_resistance(resistance)`
**Target:** `resistance_summary`
**Figure:** `plot_resistance(result, study_name, out_dir)`

WHO-threshold classification of insecticide resistance tests.

| Output | Description |
|---|---|
| `status_by_class` | count and share of each status per chemical class |
| `mortality_by_insecticide` | mean and SD mortality per insecticide, sorted ascending |
| `temporal` | yearly mean mortality |
| `n_observations` | row count of the input |
| `n_insecticides` | distinct insecticide names |

**WHO thresholds** (mortality percentage):

| Category | Threshold |
|---|---|
| Susceptible | ≥ 98% |
| Suspected Resistance | 90–97.99% |
| Resistant | < 90% |

The thresholds are applied by `fix_resistance_status.py` on the Python
side, not computed here. This module only summarises what's already in
`fact_insecticide_resistance.resistance_status`.

**Figure:** three-panel (status stack, mortality by insecticide with error
bars, temporal trend). The dashed reference lines are the 98% (green) and
90% (red) WHO thresholds. Mortality error bars are clamped to `[0, 100]`.

---

## `analysis_bionomics`

**Function:** `analyze_bionomics(bionomics)`
**Target:** `bionomics_summary`
**Figure:** `plot_bionomics(result, study_name, out_dir)`

Categorical distributions of four bionomic traits:

| Output key | Underlying column |
|---|---|
| `feeding` | `feeding_preference` |
| `biting_loc` | `biting_location` |
| `resting` | `resting_behavior` |
| `larval` | `larval_site_type` |

Each is a tibble with `value`, `n`, `share`. A missing column, or a
column whose values are all `NA` or `""`, produces an empty tibble — not
an error. The function returns `list(error = "…")` only when the input
is `NULL` or empty.

**Figure:** four-panel bar chart. Panels with no data render as a blank
titled panel rather than aborting the figure.

---

## `analysis_seasonal`

**Function:** `analyze_seasonal(cases, value_col = "case_count")`
**Target:** `seasonal`
**Figure:** `plot_seasonal(result, study_name, out_dir)`

**Contextual analysis** — operates on case data, not vector data. Returns
`list(error = "Not enough data for seasonal analysis.")` when fewer than
two case rows are present.

| Output | Description |
|---|---|
| `monthly` | mean / SD / n per calendar month, pooled across years |
| `yoy` | monthly means per year (empty tibble if one year) |
| `decomposition` | `stats::decompose()` of the monthly time series, or `NULL` |

**Decomposition.** The function first aggregates cases to a monthly
total, then builds a `ts()` with `frequency = 12`. It requires at least
24 monthly observations to attempt a decomposition; otherwise the
`decomposition` element is `NULL`.

**Figure:** two-panel (climatological monthly bar with SD error bars,
year-over-year line). When fewer than two years of data are present, the
second panel is an empty placeholder titled *"Year-over-year
(insufficient data)"*.

---

## Scaffolds

Three modules are scaffolded but **not wired into `_targets.R`**. They
are documented here for completeness. To activate one, see
[Pipeline → extending](pipeline.md#extending).

### `analysis_glm.R::analyze_negbin_glm`

```r
analyze_negbin_glm(cases, environment, offset_population = NULL)
```

Negative-binomial GLM of case counts on `t2m`, `tp`, and month fixed
effects. Optional population offset via `offset_population`.

Returns `model`, `coefficients` (from `broom::tidy()`), `aic`, `theta`,
and `dispersion`. Note: for a `MASS::glm.nb` fit, the dispersion element
is `1/theta`, not a separate dispersion estimate. Consider using
`theta` directly.

### `analysis_dlnm.R::analyze_dlnm`

```r
analyze_dlnm(cases, environment, max_lag_months = 6)
```

Distributed-lag non-linear model for delayed climate effects. Uses
`dlnm::crossbasis` with natural-spline bases on both the exposure and
lag dimensions, and `MASS::glm.nb` for the outcome model. Returns
`model`, `pred_temp`, and `pred_rain` (both `crosspred` objects).

**Known bug:** `plot_dlnm()` calls `ggplot2::ggsave()` on a base R plot
produced by `dlnm::plot.crosspred()`. `ggsave` cannot capture base plots,
so the output PNG is empty. To fix, wrap the plot in a device:

```r
png(path, width = 8, height = 5, units = "in", res = 300)
dlnm::plot.crosspred(dlnm_fit$pred_temp, "overall")
dev.off()
```

See [Troubleshooting → DLNM](troubleshooting.md#dlnm-plot-is-empty).

### `analysis_inla.R::analyze_inla`

```r
analyze_inla(cases, locations, adjacency_file)
```

Spatial BYM2 model via INLA. Fits a negative-binomial model with a
`rw1` month effect and a `bym2` spatial random effect.

**Known bug:** the formula declares two `f(month_id, …)` terms — one
`rw1` and one `ar1` — on the same variable. INLA rejects this because a
single variable can carry only one `f()` term per formula. Merge into a
single term or use a different variable for the second effect.

See [Troubleshooting → INLA](troubleshooting.md#inla).

---

## A note on purity

Every `analyze_*` function:

- Takes tibbles as input.
- Returns a list (or tibble) as output.
- Never reads a file, never connects to the database, never writes
  output, never calls `set.seed()`.

This contract is what makes the `targets` cache correct. If you break
it — for example, by having an analysis read a CSV it "just needs" —
`targets` will cache a result that depends on something it cannot see,
and the cache will go stale without warning. Every I/O operation belongs
in `R/db_queries.R` (reads) or in the `plot_*` functions (writes).