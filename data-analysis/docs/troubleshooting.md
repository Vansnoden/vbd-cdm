# Troubleshooting

This page is organised by symptom. Each entry has a **diagnose** step
and a **fix**. Where a symptom has multiple causes, they are ordered by
likelihood.

If your issue isn't here, run these two commands first:

```r
targets::tar_progress()   # which targets ran, skipped, or errored
targets::tar_meta(fields = c("name", "error", "warnings", "time"))
```

`tar_meta()` records the error message, the target that produced it, and
the time it happened for every failed target. Most of the time it tells
you exactly where to look.

---

## Setup failures

### `renv::restore()` fails

**Diagnose.** Read the error message. Common causes:

1. **System library missing.** `sf`, `RPostgres`, and `xml2` need
   external libraries: GDAL/GEOS/PROJ (for `sf`), libpq (for
   `RPostgres`), libxml2 (for `xml2`). On Linux, install the `-dev` /
   `-devel` packages. On macOS, `brew install`. On Windows, Rtools plus
   the system libraries.

   Note that **`sf` is in `DESCRIPTION` Imports but never called by the
   pipeline code.** It is declared for future spatial analysis. If
   installing `sf` is the blocker, you can comment it out of
   `DESCRIPTION` and remove it from `renv.lock` - but the analysis
   pipeline doesn't need it, so this is safe. Do *not* remove
   `RPostgres` if you plan to point `DATABASE_URL` at a Postgres
   instance.

2. **`dlnm` fails to download.** `dlnm` is locked to a URL source
   (`https://cran.r-project.org/src/contrib/Archive/dlnm/dlnm_2.4.7.tar.gz`).
   If that archive URL is unavailable, restore fails. Fix:

   ```r
   renv::install("https://cran.r-project.org/src/contrib/Archive/dlnm/dlnm_2.4.7.tar.gz")
   ```

   Or remove `dlnm` from `DESCRIPTION` if you don't plan to run the DLNM
   scaffold.

3. **Compiler missing.** `RPostgres`, `RSQLite`, `glmmTMB`, `TMB`, and
   `sf` all need a C++ compiler. On Linux this is usually already
   present. On Windows install Rtools. On macOS install Xcode Command
   Line Tools.

**Fix.** After resolving the underlying issue:

```r
renv::restore()
renv::status()   # should print "No issues found"
```

If the lockfile itself is the problem:

```r
renv::snapshot()   # regenerate from what's currently installed
```

### `config::get()` errors before any target runs

**Symptom.**

```
Error: The config package could not find the config value 'study_name'.
```

This happens at parse time, before `targets` starts. No target runs.

**Diagnose.** `_targets.R` reads nine keys without a fallback:

```
study_name, location, country, start_year, end_year,
disease_code, env_variables, max_lag_months, database
```

Each must be present in `config.yml`. Even `disease_code` - set it to
`null` if you don't want a disease filter.

**Fix.** Add the missing key. To see what's currently in the file:

```bash
Rscript -e 'str(config::get())'
```

### `here::here()` resolves to the wrong directory

**Symptom.** `cdm_connect()` can't find the database even though the
path in `config.yml` looks correct.

**Diagnose.** `here::here()` walks *up* from the current working
directory looking for a project marker (`.git`, `.Rproj`, `DESCRIPTION`,
`renv.lock`, etc.). It anchors to the first ancestor that has one. If
you're running from inside a subdirectory that has its own marker, or
from a directory tree that has no marker at all, the anchor is wrong.

```r
here::here()        # prints the anchor
here::here("R")     # prints what it resolves R/ to
getwd()             # the current working directory
```

**Fix.** Run from the project root, or ensure exactly one marker file
exists at the top level. The project has `DESCRIPTION`, `renv.lock`,
and `.Rbuildignore` - enough markers for `here` to work reliably as
long as no subdirectory also has one.

### `tar_source()` fails

**Symptom.** `tar_make()` prints `Error in tar_source()` immediately.

**Diagnose.** `tar_source()` sources every `.R` file in `R/`, in
alphabetical order. If any file has a **syntax error** or a
**top-level call to a function that doesn't exist yet**, sourcing
aborts and no targets are created.

Top-level code - outside a function definition - is a common cause.
For example:

```r
# Don't do this at the top of an R/ file
template <- new_analysis_config(study_name = "test")   # runs at source time
```

If `new_analysis_config` isn't yet defined (because `config.R` hasn't
been sourced), this fails. Since files are sourced alphabetically,
`analysis_*.R` are sourced **before** `config.R`.

**Fix.** Keep top-level code in `R/` files to function definitions only.
If you need a constant, define it as a function that returns the
constant.

---

## Database connection failures

### `Database not found at '<path>'.`

This is the explicit-path abort from `cdm_connect()`. The `database`
key in `config.yml` points somewhere that doesn't exist.

**Diagnose.**

```r
config::get("database")
here::here(config::get("database"))
file.exists(here::here(config::get("database")))
```

**Fix.** Correct the path, or run the Python ingestion pipeline first
to create the database:

```bash
cd ../data-integration
python main.py --config base_config.yaml
```

### `SQLite database not found.`

This is the fallback abort when no explicit path is given and
`SQL_LITE_DB_NAME` isn't found in either search location. The full error
lists the two candidates:

- `<project_root>/<db_name>`
- `<project_root>/../<db_name>`

**Fix.** Set `database` in `config.yml`, or set `DATABASE_URL`, or move
the database file to one of the two search locations.

### `Unsupported DATABASE_URL scheme`

**Diagnose.** `DATABASE_URL` is set to something that isn't
`sqlite:///...` or `postgresql://...`.

```r
Sys.getenv("DATABASE_URL")
```

**Fix.** Correct the URL. Note the three slashes for SQLite:
`sqlite:///absolute/path.db`, not `sqlite://path.db`.

### `RSQLite` fails to load

**Symptom.** `library(RSQLite)` errors, or `cdm_connect()` fails with
a message about `SQLiteDriver`.

**Diagnose.** `RSQLite` bundles its own SQLite, so system SQLite is not
required. The failure is almost always a build issue during
`renv::restore()`.

**Fix.**

```r
renv::install("RSQLite")
```

If that fails, the compiler toolchain is the problem. See
*`renv::restore()` fails* above.

### Postgres connection refused

**Symptom.** `DATABASE_URL=postgresql://...` but `cdm_connect()` errors
with a message about the connection being refused, or a
password-authentication failure.

**Diagnose.** Verify the URL and that the Postgres server is reachable:

```r
Sys.getenv("DATABASE_URL")

# From a shell
psql "$DATABASE_URL" -c 'SELECT 1'
```

**Fix.** The URL is malformed, the credentials are wrong, or the
database name doesn't exist. Note that `cdm_connect()` does **not**
call `_postgres_url()` from `database/session.py` - that function
exists but is never invoked. The only path to Postgres is an explicit
`DATABASE_URL`.

---

## Location resolution failures

### `No locations matched the name filter.`

**Diagnose.** `resolve_location_ids()` found zero rows matching
`location` + `country`. Run the same query manually:

```r
con <- cdm_connect(config::get("database"))
DBI::dbGetQuery(con, "SELECT DISTINCT country_code FROM dim_location")
DBI::dbGetQuery(con, "SELECT DISTINCT name FROM dim_location LIMIT 30")
DBI::dbDisconnect(con)
```

Common causes:

1. `country` is an ISO alpha-3 code (`KEN`) instead of alpha-2 (`KE`).
2. `location` has different capitalisation or a different spelling.
3. `dim_location` is empty - the ETL hasn't run.
4. `country_code` is `NULL` for the rows you expected. The ETL's
   country-code resolver failed.

**Fix.** For (1), correct the code in `config.yml`. For (4), run the
Python-side fixer:

```bash
cd ../data-integration
python -m scripts.verify_country_codes --fix
```

For (3), run the ingestion pipeline first.

### `Location name filter matched N rows` (N > 20)

**Symptom.** This is a **warning**, not an error. The pipeline
continues, but the study area is probably much larger than intended.

**Diagnose.** The name filter matched more than 20 rows. This usually
means the ETL merged location labels, so `location: "Siaya"` now
matches every grid cell renamed to `"Siaya"` as well as the actual
administrative unit.

```r
con <- cdm_connect(config::get("database"))
DBI::dbGetQuery(con,
  "SELECT location_id, name, admin_level FROM dim_location
   WHERE name LIKE '%Siaya%'")
DBI::dbDisconnect(con)
```

**Fix.** Pin the study area with `location_ids` in `config.yml`. Get
the IDs from the query above, then:

```yaml
location_ids: [12, 47, 83]
```

The `location` and `country` fields are then ignored.

### `No locations matched `location_ids` in config.`

**Diagnose.** None of the IDs in `cfg$location_ids` exist in
`dim_location`.

```r
con <- cdm_connect(config::get("database"))
DBI::dbGetQuery(con, "SELECT location_id, name FROM dim_location
                     WHERE location_id IN (12, 47, 83)")
DBI::dbDisconnect(con)
```

**Fix.** Correct the IDs. The full error message lists exactly which
IDs were supplied.

### `Some `location_ids` from config.yml were not found in dim_location.`

This is a **warning**. Some IDs matched, some didn't. The pipeline
continues with the ones that did. Verify the missing IDs against the
current `dim_location` table:

```r
DBI::dbGetQuery(con, "SELECT MAX(location_id) FROM dim_location")
```

If the missing IDs are higher than the current maximum, the ETL needs
to run again.

### Location IDs silently become `NA`

**Diagnose.** If `location_ids` in `config.yml` contains non-numeric
values (`"KE-001"`, `"Siaya"`), the coercion to integer produces `NA`
with a warning, and the resolver then reports `NA` as a missing ID.

**Fix.** Use only integer IDs. If you have string codes, resolve them
to integers first:

```r
DBI::dbGetQuery(con,
  "SELECT location_id FROM dim_location WHERE location_code = 'KE-001'")
```

---

## Query failures

### `query_vectors() returned 0 rows.`

This aborts the `vectors_validated` target. The full error lists the
resolved location IDs and the species filter.

**Diagnose.** In order of likelihood:

1. **The species filter matches nothing.** `species_filter` is
   case-insensitive substring match against `dim_vector.species_name`
   and `fact_vector_observation.vector_code`. If the ETL stored species
   names differently than expected, the filter misses everything.

   ```r
   con <- cdm_connect(config::get("database"))
   DBI::dbGetQuery(con, "
     SELECT DISTINCT COALESCE(v.species_name, o.vector_code) AS sp
     FROM fact_vector_observation o
     LEFT JOIN dim_vector v ON o.vector_code = v.vector_code
     LIMIT 30")
   DBI::dbDisconnect(con)
   ```

   If a `species_filter` value (`"gambiae"`) doesn't appear in the
   output, remove it or broaden it.

2. **The date range excludes all data.**

   ```r
   DBI::dbGetQuery(con,
     "SELECT MIN(t.year), MAX(t.year) FROM fact_vector_observation o
      JOIN dim_time t ON o.date_id = t.date_id")
   ```

3. **The location filter is wrong.** Check `locations` after a
   partially successful run:

   ```r
   targets::tar_load(locations)
   print(locations)
   ```

4. **`fact_vector_observation` is empty.** The ETL hasn't ingested
   vectors.

**Fix.** Adjust `config.yml` accordingly. The QA report is designed to
answer exactly this question; run it:

```r
targets::tar_make(names = "qa_report")
browseURL("outputs/analysis/reports/qa_report.html")
```

### `query_environment() returned 0 rows.`

This aborts the `environment_validated` target. Same diagnostic pattern
as above, but the filter is `cfg$env_variables`.

**Diagnose.**

```r
con <- cdm_connect(config::get("database"))
DBI::dbGetQuery(con, "SELECT DISTINCT variable FROM fact_environment")
DBI::dbDisconnect(con)
```

If the requested names (`t2m`, `tp`) aren't in the result, either the
ETL hasn't run for environment, or it stored the variables under
different names.

**Fix.** Correct `env_variables` in `config.yml` to match what's
actually in `fact_environment`.

### `query_cases() returned 0 rows.`

This is a **warning**, not an abort. The vector-centred pipeline
continues; only the case-context report section is empty.

**Diagnose.** If you *expected* case data:

```r
DBI::dbGetQuery(con, "SELECT COUNT(*) FROM fact_disease_case")
DBI::dbGetQuery(con,
  "SELECT identification_method_code, COUNT(*) FROM fact_disease_case
   GROUP BY identification_method_code")
```

If the only rows have `identification_method_code = 'ESTIMATE'` and
`include_estimated_cases: false` in `config.yml`, the query returns zero
rows by design. Set the flag to `true` if you want modelled case counts
included - but read the warning in
[Configuration](configuration.md#include_estimated_cases) first.

### A query returns far more rows than expected

**Diagnose.** This is almost always the **900-ID silent drop**. In
`db_queries.R`, `.loc_clause()` returns an empty filter when
`length(location_ids) > 900`:

```r
if (length(location_ids) > 900) {
  return(list(sql = "", params = list()))
}
```

The rationale is a safe ceiling on SQLite's `SQLITE_MAX_VARIABLE_NUMBER`.
But the consequence is that a subset request silently becomes a
full-table scan.

**Fix.** Reduce `cfg$location_ids` below 900, or split the study into
smaller regions with separate config files. If you need more than 900
locations and the query must be scoped, edit `.loc_clause()` to raise
the ceiling for your SQLite build, or migrate to Postgres where the
parameter limit is much higher (65,535).

### A query returns fewer rows than expected

**Diagnose.** All fact queries join to `dim_time` via `.year_filter()`.
If `dim_time` doesn't cover the years you asked for, the join drops
everything outside the covered range.

```r
DBI::dbGetQuery(con,
  "SELECT MIN(date), MAX(date), COUNT(*) FROM dim_time")
```

**Fix.** Extend `dim_time` on the Python side. Run the Python helper
directly:

```python
from main import init_dim_time
init_dim_time(start_date="1900-01-01", end_date="2030-12-31")
```

---

## Analysis failures

### `analyze_*()` returns `list(error = "...")`

Most `analyze_*` functions return an error list rather than raising when
their input is empty or degenerate. This is **by design** - the report
prints the error as an italic note and continues.

To see the error and decide whether it's expected:

```r
targets::tar_load(vector_seasonal)
str(vector_seasonal)
```

The error string tells you what happened:

| Error text | Meaning |
|---|---|
| `"No vector data available."` | `vectors_validated` was empty - should not happen, because `vectors_validated` aborts first. |
| `"Not enough data for seasonal analysis."` | Fewer than two rows in the input. |
| `"No resistance data available."` | `fact_insecticide_resistance` is empty for this study area. |
| `"No bionomics data available."` | `fact_bionomics` is empty for this study area. |
| `"No overlapping dates between vector and climate data."` | The two series have no dates in common after aggregation. |
| `"value_col '...' not present in data."` | The auto-detected `value_col` is wrong; see below. |
| `"No numeric climate variables found."` | `environment` has no numeric columns after excluding `date`, `year`, `month`, `period`. |

### `analyze_vector_climate` fails on aggregation

**Symptom.**

```
Error: Unsupported aggregation: "quarterly"
```

**Diagnose.** `.floor_fun()` accepts only `"monthly"`, `"weekly"`, and
`"daily"`. The `_targets.R` sets `"monthly"`. If you edited the target,
restore the valid value.

**Fix.** Use one of the three supported strings.

### `analyze_vector_seasonal` picks the wrong `value_col`

**Diagnose.** The function auto-detects the column to summarise:

1. `"count"` if present.
2. `"density_per_trap"` if present.
3. The first column of the input.

If neither `count` nor `density_per_trap` exists, the third fallback may
pick the wrong column (e.g. `year`, `month`, or `species_name`).

**Fix.** Pass `value_col` explicitly:

```r
analyze_vector_seasonal(vectors_validated, value_col = "my_column")
```

Or, for the pipeline path, edit the target in `_targets.R`.

---

## Figure failures

### A figure is missing

**Diagnose.** Check `tar_meta` for warnings on the corresponding `fig_*`
target:

```r
targets::tar_meta(fig_vector_climate, fields = c("warnings", "error", "time"))
```

Plot functions warn and return `invisible(character())` on error. They
never abort the pipeline.

To reproduce the warning directly:

```r
targets::tar_load(vector_climate)
plot_vector_climate(vector_climate, "test", tempdir())
```

### `Could not plot variable t2m: ...`

**Symptom.** This warning appears inside `plot_vector_climate` during a
run. Other variables' figures still write.

**Diagnose.** The per-variable plot failed for one climate variable. Run
the sub-function directly to see the full error:

```r
targets::tar_load(vector_climate)
data-analysis/R/analysis_vector_climate.R::.plot_one_variable(
  result      = vector_climate,
  var         = "t2m",
  study_name  = "test",
  out_dir     = tempdir()
)
```

Common causes:

- Fewer than two rows of data after the join for that variable.
- All values of that variable are `NaN` or `NA`.
- The `seasonal` table lacks a row for that variable.

### A figure is blank

**Symptom.** The PNG exists but contains only axes or is entirely white.

**Diagnose.** Check the underlying analysis result:

```r
targets::tar_load(vector_seasonal)
vector_seasonal$composition      # should have > 0 rows
vector_seasonal$monthly          # should have > 0 rows
```

If the analysis result is empty, the plot function renders a blank
theme-void placeholder. That's intentional - the report shows the
placeholder alongside an explanatory note.

If the analysis result is non-empty but the figure is blank, the issue
is inside `ggplot2` - inspect interactively:

```r
p <- plot_vector_seasonal(vector_seasonal, "test", tempdir())
```

### The DLNM plot is empty {#dlnm-plot-is-empty}

**Symptom.** `plot_dlnm()` writes a 0-byte or blank PNG.

**Diagnose.** `dlnm::plot.crosspred()` is a **base R** plotting function.
`ggplot2::ggsave()` cannot capture base plots; it captures the current
`ggplot2` device, which is empty.

**Fix.** Wrap the base plot in a device:

```r
png(path, width = 8, height = 5, units = "in", res = 300)
dlnm::plot.crosspred(dlnm_fit$pred_temp, "overall")
dev.off()
```

This module is a scaffold and not wired into `_targets.R`. Until it's
activated, this bug is latent.

### `plot_seasonal` returns an empty character vector

**Diagnose.** `plot_seasonal()` returns `invisible(NULL)` - not
`invisible(character())` - when the result carries an error. It does
not warn.

```r
targets::tar_load(seasonal)
str(seasonal)   # check for $error
```

**Fix.** The report's section for case data checks
`isTRUE(res$cases_available)` before rendering, so a `NULL` return from
`plot_seasonal` is benign. If you want a warning, add one.

---

## Report failures

### Quarto render fails

**Symptom.** `tar_make()` errors on `qa_report` or `analysis_report`.

**Diagnose.** `tar_quarto(quiet = FALSE)` prints Quarto's own output.
Read it - the error is usually specific:

- **"Object not found"** - the sanitised parameter is missing a field.
  Check whether the `.qmd` references `res$something` that isn't in
  `analysis_results`.
- **"cannot open the connection"** - a figure path doesn't exist. This
  happens when `plot_vector_climate` produced files under a different
  `study_name` than the report is looking for.
- **"no such column"** - a table in `results$qa$tables` has fewer
  columns than the report expects.

**Fix.** Reproduce the render interactively:

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

The error message is clearer here than under `tar_make()`.

### Quarto errors on a parameter round-trip

**Symptom.** Cryptic error about a YAML scalar or an unparsable parameter.

**Diagnose.** Quarto's YAML 1.2 parameter round-trip cannot represent R
`NA`. `sanitize_for_quarto()` replaces `NA` recursively, but if you
added a new field to `analysis_results` that the sanitiser doesn't
handle - a raw `call` object, a function, an S4 object - the
serialisation will fail.

**Fix.** Simplify the object you pass. If it must contain a complex
structure, serialise it yourself (e.g. `readRDS()` of a file the report
reads) instead of passing it as a parameter.

### A figure renders as a broken image link

**Symptom.** The HTML report shows an `<img>` with a broken source.

**Diagnose.** The report loads two categories of figures:

- Sections 3, 5, 6, 7 read `results$fig_*` (the file paths returned by
  `plot_*`).
- Section 4 reconstructs paths from `cfg$study_name` and the climate
  variable name.

If a file is missing, the report prints an italic note instead of a
broken link - *unless* the file exists at render time but was deleted
before the browser loads it. That happens if you `tar_destroy()` after
rendering.

**Fix.** Re-render after any cache operation:

```r
targets::tar_make(names = "analysis_report")
```

---

## Cache issues

### `targets` re-runs everything on every `tar_make()`

**Diagnose.** Two common causes.

1. **A non-deterministic target.** If a target's value changes between
   runs even when its inputs don't, `targets` will re-run its downstream
   targets. Common culprits: `Sys.time()`, `sample()` without a seed,
   `tempfile()`.

   ```r
   targets::tar_outdated()
   targets::tar_meta(fields = c("name", "time", "warnings", "error"))
   ```

   A target with a very recent `time` value, repeatedly, is the
   culprit.

2. **The database changed but targets doesn't know.** `targets` does
   not hash the CDM file. If the underlying database was rewritten by
   the Python ETL, the cached extraction targets are stale - but
   `targets` has no way to detect this.

**Fix.** For (1), remove the non-determinism. For (2), see
*The pipeline returns stale results* below.

### The pipeline returns stale results

**Symptom.** You re-ran the ETL and picked up new data, but the R
figures are unchanged.

**Diagnose.** `targets` has no dependency on the database file. The
extraction targets return their cached values.

**Fix (manual).** After re-running the ETL:

```r
targets::tar_invalidate(c(
  locations, vectors, environment, resistance, bionomics, cases, burden
))
targets::tar_make()
```

**Fix (automatic).** See
[Pipeline -> Database invalidation](pipeline.md#database-invalidation)
for the `db_hash` target pattern. It requires touching every query
function, but once done it eliminates this class of bug.

### `targets` cache is corrupted

**Symptom.** `tar_make()` errors with a message about a `qs` file, or
about a target whose value can't be read.

**Diagnose.** The `_targets/` store has been partially deleted, or the
`format` option changed without invalidation.

**Fix.**

```r
targets::tar_destroy()
targets::tar_make()
```

This wipes and rebuilds everything. On a large database it takes as
long as the initial run.

### `tar_make()` fails on a single target

**Diagnose.** Reproduce the target's command interactively. The command
is shown by:

```r
targets::tar_manifest(seasonal)
```

Or run the function directly with its cached inputs:

```r
targets::tar_load(cases_validated)
seasonal <- analyze_seasonal(cases_validated)
```

The error message is clearer here than under `tar_make()` because you're
not running inside a child R session.

**Fix.** Depends on the underlying cause. Once fixed, re-run
`targets::tar_make()`; only the affected target and its downstream
dependencies will re-run.

---

## Scaffold bugs

The three scaffold modules (`analysis_glm.R`, `analysis_dlnm.R`,
`analysis_inla.R`) are not wired into the pipeline and are not covered
by the QA or analysis reports. Each has a known issue.

### `analyze_inla`: two `f(month_id, ...)` terms {#inla}

**Symptom.**

```
Error: The same variable 'month_id' is used in multiple f() terms.
```

**Diagnose.** The formula declares:

```r
case_count ~ t2m + tp +
  f(month_id,    model = "rw1") +
  f(location_id, model = "bym2", graph = g, scale.model = TRUE) +
  f(month_id,    model = "ar1")
```

INLA does not allow two `f()` terms on the same variable in one
formula.

**Fix.** Pick one temporal model. If you want both a smooth seasonal
effect and an AR(1) residual, split the data into a seasonal term
(`sin(2*pi*month/12)`, `cos(2*pi*month/12)`) and an AR(1) process on the
residual index:

```r
case_count ~ t2m + tp +
  sin(2 * pi * month_id / 12) + cos(2 * pi * month_id / 12) +
  f(location_id, model = "bym2", graph = g, scale.model = TRUE) +
  f(time_index,  model = "ar1")
```

### `analyze_glm`: `dispersion` is `1/theta`

**Symptom.** `result$dispersion` is very close to `1` for most fits.

**Diagnose.** `summary(m_nb)$dispersion` on a `MASS::glm.nb` object is
`1/theta`, not a separate dispersion estimate. As `theta` goes to
infinity, the negative-binomial reduces to Poisson and `1/theta` goes to
zero, but `summary()` clamps it.

**Fix.** Use `m_nb$theta` directly (it's already returned as
`result$theta`). Ignore `result$dispersion`.

---

## Windows specifics

### Path separators

`fs` and `here` handle path separators. You should not need to escape
backslashes in `config.yml`. Use forward slashes even on Windows:

```yaml
database: "../data-integration/vbd_cdm.db"   # works on Windows
```

### CRLF line endings

If you edit `config.yml` on Windows with an editor that writes CRLF, the
`config` package parses it correctly. No issue.

If you generate a mapping CSV or vocabulary CSV on Windows and feed it to
the Python side, ensure the CSV is written with `newline=''` to avoid
double-escaped line endings. The existing files in this project are
LF-only.

### Rtools

`renv::restore()` on Windows requires Rtools for any package with
compiled code (`RPostgres`, `RSQLite`, `glmmTMB`, `TMB`, `sf`, `xml2`,
and their transitive dependencies). Install Rtools matching your R minor
version from the CRAN Rtools page.

---

## Getting more information

### Inspect a target

```r
targets::tar_load(vector_climate)    # loads into the session
str(vector_climate, max.level = 1)

targets::tar_read(vector_climate)    # returns without loading
```

### Inspect every target's metadata

```r
targets::tar_meta()
```

Columns include: `name`, `type`, `data`, `command`, `depend`,
`seed`, `path`, `time`, `size`, `bytes`, `format`, `repository`,
`iteration`, `parent`, `children`, `warnings`, `error`.

### Inspect the raw database

```r
con <- cdm_connect(config::get("database"))
DBI::dbListTables(con)

DBI::dbGetQuery(con, "SELECT COUNT(*) FROM fact_vector_observation")
DBI::dbGetQuery(con, "SELECT COUNT(*) FROM dim_location")
DBI::dbGetQuery(con, "SELECT COUNT(*) FROM dim_time")

DBI::dbDisconnect(con)
```

### Inspect the QA report

The QA report is designed specifically for diagnosing data-availability
issues. When in doubt, regenerate it:

```r
targets::tar_make(names = "qa_report")
browseURL("outputs/analysis/reports/qa_report.html")
```

It shows row counts, species presence, filter coverage, environment
variables present, and monthly case counts - the five things you need
to know before debugging an analysis.

### Ask for help

If you're still stuck, include the output of:

```r
sessionInfo()
targets::tar_meta(fields = c("name", "error", "warnings", "time"))
targets::tar_manifest()
```

along with the `config.yml` in use and the exact command that failed.