# Data access

The data-access layer is two files:

- `R/db.R` — connection handling and the generic query runner.
- `R/db_queries.R` — every SQL statement in the project.

Nothing else in the R side should contain SQL. If you need a new query,
add it to `db_queries.R` and call it from a `targets` extraction target.

---

## Connecting

`cdm_connect()` resolves a database in this priority order:

1. The `db_path` argument, if non-`NULL` and non-empty.
2. The `DATABASE_URL` environment variable.
3. The `SQL_LITE_DB_NAME` environment variable (default `"vbd_cdm.db"`).

```r
source("R/db.R")

# Explicit path
con <- cdm_connect("../data-integration/vbd_cdm.db")

# From DATABASE_URL if set, otherwise SQL_LITE_DB_NAME, otherwise error
con <- cdm_connect()

DBI::dbDisconnect(con)
```

The return value is a `DBIConnection` (SQLite or PostgreSQL, depending on
the URL scheme). Close it with `DBI::dbDisconnect()` when you're done.

### Path resolution

A relative `db_path` is resolved with `here::here()`, which anchors to
the project root (the directory containing `DESCRIPTION`). So
`cdm_connect("../data-integration/vbd_cdm.db")` resolves to
`<repo>/data-integration/vbd_cdm.db`, regardless of the working
directory when R was launched.

An absolute `db_path` is used as-is.

If `db_path` is `NULL` or empty and `DATABASE_URL` is unset,
`cdm_connect()` looks for `SQL_LITE_DB_NAME` in two places:

1. `<project_root>/<db_name>` — i.e. `data-analysis/vbd_cdm.db`
2. `<project_root>/../<db_name>` — i.e. `<repo>/vbd_cdm.db`

It aborts with a listed search path if neither exists.

### Supported URL schemes

`DATABASE_URL` accepts:

- `sqlite:///absolute/path/to/vbd_cdm.db` — SQLite
- `postgresql://user:password@host:port/dbname` — PostgreSQL

Any other scheme aborts with `Unsupported DATABASE_URL scheme`.

### Common connection failure

```
Error: Database not found at '/path/to/vbd_cdm.db'.
```

The `database` key in `config.yml` points somewhere that doesn't exist.
Check the path, or run the Python ingestion pipeline first to create the
database. See [Configuration → `database`](configuration.md#database).

---

## Running a query

`cdm_query()` is the generic runner:

```r
cdm_query(sql, params = NULL, con = NULL, db_path = NULL)
```

- Returns a `tibble`.
- `params` may be `NULL`, a `list()`, or a named list. An empty list is
  treated as `NULL`, so `cdm_query(sql, list())` is safe.
- If `con` is `NULL`, a new connection is opened and closed for that one
  call. If you're running several queries in a row, pass `con` explicitly
  to reuse the connection.

```r
# One-off
df <- cdm_query("SELECT COUNT(*) AS n FROM fact_vector_observation")

# Parameterised
df <- cdm_query(
  "SELECT * FROM dim_location WHERE country_code = ?",
  list("KE")
)

# Multiple queries, shared connection
con <- cdm_connect()
on.exit(DBI::dbDisconnect(con), add = TRUE)
a <- cdm_query("SELECT ...", con = con)
b <- cdm_query("SELECT ...", con = con)
```

Note: `cdm_query()` is `SELECT`-only in intent but not enforced. If you
pass an `UPDATE` or `DELETE` it will execute. Don't.

---

## Location resolution

```r
resolve_location_ids(
  location     = "Siaya",
  country      = "KE",
  db_path      = NULL,
  explicit_ids = NULL
)
```

Returns a tibble with columns `location_id`, `name`, `country_code`,
`admin_level`.

Three resolution modes:

### Explicit IDs

When `explicit_ids` is non-`NULL` and non-empty, it takes priority over
`location` and `country`, which are ignored.

```r
locs <- resolve_location_ids(
  location     = "ALL",
  country      = "ALL",
  explicit_ids = c(12, 47, 83)
)
```

Behaviour:

- Aborts if *none* of the IDs exist in `dim_location`.
- Warns and continues with the ones that do, if *some* are missing.

### Name / country

When `explicit_ids` is `NULL` or empty, the resolver builds a `WHERE`
clause from `location` and `country`:

- `country = "ALL"` skips the country filter.
- `location = "ALL"` skips the name filter.
- Otherwise, `country` matches `dim_location.country_code` exactly, and
  `location` matches `dim_location.name` exactly **or** as a `LIKE
  '%<value>%'` substring.

Behaviour:

- Aborts if zero rows match.
- Warns if more than 20 rows match. That usually means the ETL merged
  labels and your substring now matches the whole continent.

### All locations

With `location = "ALL"` and `country = "ALL"`, every row in
`dim_location` is returned. This is a legitimate setting for a
continent-wide study, but be aware that downstream vector–climate
aggregation will produce a *continental mean* rather than a per-site
series. See *What the queries don't do* below.

---

## The query catalogue

`R/db_queries.R` exposes one function per CDM fact table, plus the
location resolver. Every function takes a `cfg` object and an integer
vector of `location_id`s. Passing `NULL` or an empty vector means "all
locations".

| Function | Returns | Notes |
|---|---|---|
| `resolve_location_ids(location, country, db_path, explicit_ids)` | `location_id`, `name`, `country_code`, `admin_level` | Returns tibble. Aborts on zero matches. |
| `query_cases(cfg, location_ids)` | `date`, `year`, `month`, `week`, `season`, `case_count` | Daily aggregated counts. Excludes `'ESTIMATE'` rows unless `cfg$include_estimated_cases`. |
| `query_vectors(cfg, location_ids, species_filter)` | `date`, `year`, `month`, `species_name`, `vector_code`, `count`, `density_per_trap`, `infection_rate` | **No `location_id` in the output.** Species filter is case-insensitive substring. |
| `query_environment(cfg, location_ids)` | `date`, `year`, `month`, plus one column per variable in `cfg$env_variables` | **Wide format, not long.** **No `location_id`.** See caveat below. |
| `query_interventions(cfg, location_ids)` | `intervention_id`, `intervention_type`, `coverage`, `date`, `year`, `month`, `disease_name`, `disease_code` | Not currently wired into the pipeline. |
| `query_resistance(cfg, location_ids)` | `resistance_id`, `location_id`, `date`, `year`, `month`, `species_name`, `insecticide_name`, `chemical_class`, `test_type_name`, `vector_code`, `insecticide_code`, `test_type_code`, `concentration`, `exposure_time`, `n_insect_tested`, `n_insect_dead`, `mortality`, `resistance_status` | Full width. `mortality` is a percentage in `[0, 100]`. |
| `query_bionomics(cfg, location_ids)` | `bionomics_id`, `location_id`, `date`, `year`, `month`, `species_name`, `vector_code`, `biting_rate`, `feeding_preference`, `biting_location`, `resting_behavior`, `larval_site_type` | Full width. |
| `query_burden(cfg, location_ids)` | `year`, `incidence_rate`, `prevalence_rate`, `mortality_rate`, `cases_mean`, `disease_name`, `disease_code`, `location_name` | **No `location_id`, only `location_name`.** Yearly. |
| `query_socioeconomic(cfg, location_ids)` | `year`, `indicator_value`, `indicator_name`, `indicator_code`, `unit`, `location_name` | Not currently wired into the pipeline. |

### Which are actually used by the pipeline

`_targets.R` calls:

- `resolve_location_ids` → `locations`
- `query_vectors` → `vectors`
- `query_environment` → `environment`
- `query_resistance` → `resistance`
- `query_bionomics` → `bionomics`
- `query_cases` → `cases`
- `query_burden` → `burden`

`query_interventions` and `query_socioeconomic` exist and are tested
against the schema, but are not called. If you need them, add a target
to `_targets.R`.

---

## The 900-ID limit

`.loc_clause()` builds the `IN (...)` fragment used by every fact query.
Its behaviour when `location_ids` is large is important:

```r
.loc_clause <- function(location_ids, alias = NULL, column = "location_id") {
  if (is.null(location_ids) || length(location_ids) == 0) {
    return(list(sql = "", params = list()))
  }
  if (length(location_ids) > 900) {
    return(list(sql = "", params = list()))
  }
  # ... build the clause
}
```

If you pass **more than 900** location IDs, the filter is silently
dropped. Every fact query then returns rows for **all** locations in the
database, not just the ones you asked for.

The rationale (from the docstring) is that SQLite's
`SQLITE_MAX_VARIABLE_NUMBER` varies between builds, and 900 is a safe
ceiling. Semantically, "all locations" and "no filter" are identical —
but the caller may have intended a subset.

**Practical consequence:** a study area with more than 900 grid cells
will silently fan out to the entire `dim_location` table. If you set
`location_ids` explicitly and expect a subset, verify the row count in
the QA report before trusting downstream figures.

---

## What the queries don't do

### No per-site climate join

`query_environment()` aggregates across whatever `location_id`s match,
via `dplyr::pivot_wider(values_fn = mean)`. When `location_ids` is
`NULL`, or longer than 900 (see above), this becomes a **continental
mean** — a placeholder series, not a physically meaningful per-site
climate trace.

The query returns **no `location_id` column**, so the aggregation is
opaque to the caller.

Per-site climate is possible via `R/spatial_join.R::attach_grid_cells()`,
which maps each vector site to its nearest environment grid cell by
centroid proximity, then joins on that. But:

- `attach_grid_cells()` expects its input to carry a `location_id`
  column.
- `query_vectors()` does **not** return `location_id`.

So the function, as currently written, cannot be applied to
`query_vectors()` output directly. This is a known gap. To use it, you'd
need a variant of `query_vectors()` that preserves `location_id`, or a
different join key.

### No species-filter pushdown to environment

`query_environment()` accepts `cfg` but only reads `cfg$env_variables`
and `cfg$db_path`. Species filters are not applied, and are not
applicable — `fact_environment` is species-agnostic.

### No time-series decomposition or lagging

Every query returns rows as stored. If you need a monthly series, lagged
values, or seasonal aggregation, do it in the analysis layer. This keeps
the queries simple and cacheable.

### No fallback for missing variables

If `cfg$env_variables` contains a name that does not exist in
`fact_environment`, the query returns zero rows for it — the pivot_wider
then produces a column of all `NA`. The pipeline's validation target
aborts when the environment result is empty, but a partial miss (some
variables present, some not) is not caught.

Verify with:

```sql
SELECT variable, COUNT(*) FROM fact_environment GROUP BY variable;
```

### No date normalisation beyond `as.Date()`

Every query converts `date` from the CDM's `YYYY-MM-DD` string to an
R `Date`. Time zones are not considered; all dates are treated as UTC.

---

## Querying the DB directly

For ad-hoc exploration, bypass the query catalogue:

```r
con <- cdm_connect()

# Which species are present?
DBI::dbGetQuery(con, "
  SELECT v.species_name, COUNT(*) AS n
  FROM fact_vector_observation o
  LEFT JOIN dim_vector v ON o.vector_code = v.vector_code
  GROUP BY v.species_name
  ORDER BY n DESC
")

# What environment variables exist?
DBI::dbGetQuery(con, "
  SELECT variable, COUNT(*) AS n
  FROM fact_environment
  GROUP BY variable
  ORDER BY variable
")

# What does the time dimension actually cover?
DBI::dbGetQuery(con, "
  SELECT MIN(year) AS first_year, MAX(year) AS last_year, COUNT(*) AS n_days
  FROM dim_time
")

DBI::dbDisconnect(con)
```

If you find yourself running the same query more than twice, add it to
`R/db_queries.R`. That way it becomes cacheable by `targets` and
reusable from reports.

---

## Reminders from the CDM contract

These are the conventions that are easy to get wrong when writing raw
SQL. They are enforced by the Python ETL, not by the database schema, so
nothing will stop you from misreading them.

- **`mortality` is a percentage in `[0, 100]`.** Not a proportion.
  `98` means 98% mortality (susceptible).
- **`infection_rate` is a proportion in `[0, 1]`.** Not a percentage.
  This asymmetry is intentional but is a frequent source of bugs.
- **Rows with `identification_method_code = 'ESTIMATE'` are synthetic.**
  They are generated by `fix_cases.py` from yearly burden estimates. The
  R pipeline excludes them unless `include_estimated_cases: true`.
- **Null sentinels.** Nullable columns in natural keys store `''` (empty
  string) or `-1.0` instead of `NULL`, because SQL treats NULLs as
  distinct in unique indexes. Columns affected:

  | Table | Column | Sentinel |
  |---|---|---|
  | `fact_vector_observation` | `life_stage`, `sex`, `collection_method` | `''` |
  | `fact_insecticide_resistance` | `concentration` | `-1.0` |
  | `fact_intervention` | `intervention_type` | `''` |
  | `fact_disease_case` | `case_status`, `age_group`, `sex` | `''` |

  In R, treat `''` and `-1.0` as `NA` for analysis purposes.

- **`dim_time.date_id` is a string, not a `Date`.** Fact tables join to
  it via `date_id`. If you cast it to `Date` for a comparison, you lose
  the index. Let the query planner use the string column directly.

- **Grid cells are not real locations.** Rows in `dim_location` with
  `admin_level = 'grid'` are environment pixels. They have
  `location_code` values starting with `GRID_`, and their `name` may be
  shared across many pixels. Never treat them as administrative units.

See the full [CDM contract](../../data-integration/docs/cdm_contract.md)
for schema-level detail.