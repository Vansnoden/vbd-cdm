# Data access

The data-access layer is two files:

- `R/db.R` — connection handling and the generic query runner.
- `R/db_queries.R` — every SQL statement in the project.

Nothing else in the R side should contain SQL. If you need a new query,
add it to `db_queries.R` and call it from a `targets` extraction target.

---

## Connecting

`cdm_connect()` resolves a database in this priority order:

1. The `db_url` argument, if non-`NULL` and non-empty. `postgresql://` and
   `sqlite:///` schemes are supported.
2. The `db_path` argument, if non-`NULL` and non-empty.
3. The `DATABASE_URL` environment variable.
4. The `SQL_LITE_DB_NAME` environment variable (default `"vbd_cdm.db"`).

```r
source("R/db.R")

# Explicit SQLite path
con <- cdm_connect("../data-integration/vbd_cdm.db")

# Explicit Postgres URL
con <- cdm_connect(db_url = "postgresql://user:pw@host:5432/vbd_cdm")

# From DATABASE_URL if set, otherwise SQL_LITE_DB_NAME, otherwise error
con <- cdm_connect()

DBI::dbDisconnect(con)
```

The return value is a `DBIConnection` (SQLite or PostgreSQL). Close it
with `DBI::dbDisconnect()` when you're done.

### Dialect detection

`cdm_backend(db_path, db_url)` returns `"sqlite"` or `"postgresql"`,
mirroring `cdm_connect()`'s resolution order. It is used internally by
`db_queries.R` to pick the correct JSON-array SQL function for the
location filter.

```r
cdm_backend()                                    # → "sqlite" (default)
cdm_backend(db_url = "postgresql://…")           # → "postgresql"
```

### Path resolution

A relative `db_path` is resolved with `here::here()`, which anchors to
the project root (the directory containing `DESCRIPTION`). So
`cdm_connect("../data-integration/vbd_cdm.db")` resolves to
`<repo>/data-integration/vbd_cdm.db`, regardless of the working
directory when R was launched.

An absolute `db_path` is used as-is.

If both `db_path` and `db_url` are `NULL` and `DATABASE_URL` is unset,
`cdm_connect()` looks for `SQL_LITE_DB_NAME` in two places:

1. `<project_root>/<db_name>` — i.e. `data-analysis/vbd_cdm.db`
2. `<project_root>/../<db_name>` — i.e. `<repo>/vbd_cdm.db`

It aborts with a listed search path if neither exists.

### Supported URL schemes

`db_url` and `DATABASE_URL` accept:

- `sqlite:///absolute/path/to/vbd_cdm.db` — SQLite (three slashes)
- `postgresql://user:password@host:port/dbname` — PostgreSQL

Any other scheme aborts with `Unsupported DATABASE_URL scheme`.

### Common connection failure

```
Error: Database not found at '/path/to/vbd_cdm.db'.
```

The `database:` block in `config.yml` points somewhere that doesn't
exist. Check the path, or run the Python ingestion pipeline first to
create the database. See [Configuration → `database`](configuration.md#the-database-block).

---

## Running a query

`cdm_query()` is the generic runner:

```r
cdm_query(sql, params = NULL, con = NULL, db_path = NULL, db_url = NULL)
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

`cdm_query()` is `SELECT`-only in intent but not enforced. Don't pass
`UPDATE` or `DELETE`.

There is an internal helper `cdm_query_cfg(cfg, sql, params)` that
short-circuits the dialect resolution by using `cfg$db_path` and
`cfg$db_url`. Every query in `db_queries.R` calls it.

---

## Location resolution

```r
resolve_location_ids(
  countries = NULL,
  locations = NULL,
  db_path   = NULL,
  db_url    = NULL
)
```

Returns a tibble with columns `location_id`, `name`, `country_code`,
`admin_level`.

**Arguments:**

- `countries` — character vector of ISO alpha-2 codes (or full names,
  which will be uppercased and then matched literally — see the pitfall
  in *Configuration*). `"ALL"`, `[]`, and `NULL` all skip the country
  filter.
- `locations` — character vector of location names. Each element matches
  `dim_location.name` exactly or as a substring.
- `db_path` / `db_url` — same semantics as `cdm_connect()`.

**Resolution modes:**

- Both filters empty → every row in `dim_location`.
- Only `countries` set → every location in those countries.
- Only `locations` set → every location whose name matches, continent-wide.
- Both set → AND-ed.

**Abort / warning behaviour:**

- Zero rows match → abort, with the resolved filter values printed.
- More than 20 rows match → warning. The pipeline continues.

The historical `explicit_ids` argument is **gone**. There is no way to
pass a literal vector of `location_id`s through `config.yml`. If you
need to pin a study area to specific IDs, use `locations` with a name
that matches exactly one row, or query the DB directly and edit the
`locations` query in `db_queries.R`.

---

## The query catalogue

`R/db_queries.R` exposes one function per CDM fact table, plus the
location resolver. Every function takes a `cfg` object and an integer
vector of `location_id`s. Passing `NULL` or an empty vector means "all
locations".

| Function | Returns | Notes |
|---|---|---|
| `resolve_location_ids(countries, locations, db_path, db_url)` | `location_id`, `name`, `country_code`, `admin_level` | Aborts on zero matches. |
| `query_cases(cfg, location_ids)` | `date`, `year`, `month`, `week`, `season`, `case_count` | Daily aggregated counts. Excludes `'ESTIMATE'` rows unless `cfg$include_estimated_cases`. |
| `query_vectors(cfg, location_ids, species_filter)` | `date`, `year`, `month`, `species_name`, `vector_code`, `count`, `density_per_trap`, `infection_rate` | **No `location_id` in the output.** Species filter is exact, case-insensitive match. |
| `query_environment(cfg, location_ids)` | `date`, `year`, `month`, plus one column per variable in `cfg$env_variables` | **Wide format, not long.** **No `location_id`.** |
| `query_resistance(cfg, location_ids)` | Full width including all VGSC counts and `source_code` | `mortality` is a percentage in `[0, 100]`. |
| `query_bionomics(cfg, location_ids)` | `bionomics_id`, `location_id`, `date`, `year`, `month`, `species_name`, `vector_code`, `biting_rate`, `feeding_preference`, `biting_location`, `resting_behavior`, `larval_site_type` | Full width. |
| `query_burden(cfg, location_ids)` | `year`, `incidence_rate`, `prevalence_rate`, `mortality_rate`, `cases_mean`, `disease_name`, `disease_code`, `location_name` | **No `location_id`, only `location_name`.** Yearly. |
| `query_interventions(cfg, location_ids)` | `intervention_id`, `intervention_type`, `coverage`, `date`, `year`, `month`, `disease_name`, `disease_code` | Not wired into the pipeline. |
| `query_socioeconomic(cfg, location_ids)` | `year`, `indicator_value`, `indicator_name`, `indicator_code`, `unit`, `location_name` | Not wired into the pipeline. |

### Which are actually used by the pipeline

`_targets.R` calls:

- `resolve_location_ids` → `locations`
- `query_vectors` → `vectors`
- `query_environment` → `environment`
- `query_resistance` → `resistance`
- `query_bionomics` → `bionomics`
- `query_cases` → `cases`
- `query_burden` → `burden`

`query_interventions` and `query_socioeconomic` exist but are not called.

---

## The location filter

`.loc_clause()` builds the `IN (...)` fragment used by every fact query.
Unlike earlier iterations, it imposes **no upper bound** on the number
of IDs. The full set is JSON-encoded into a single parameter and
expanded server-side:

- SQLite: `SELECT value FROM json_each(?)`
- PostgreSQL: `SELECT value::int FROM jsonb_array_elements_text(?::jsonb)`

The dialect is selected by `cdm_backend()`, which is passed through
`query_*()` via the `backend` argument to `.loc_clause()`.

**Practical consequence:** a study area with any number of grid cells
will produce a correctly scoped query. There is no silent fan-out to the
whole `dim_location` table. If a query returns far more rows than
expected, the cause is elsewhere (empty `countries`/`locations` in
`config.yml`, or a species/disease filter that matched everything).

---

## What the queries don't do

### No per-site climate join

`query_environment()` aggregates across whatever `location_id`s match,
via `dplyr::pivot_wider(values_fn = mean)`. When `location_ids` is
`NULL`, this becomes a **continental mean** — a placeholder series, not
a physically meaningful per-site climate trace.

The query returns **no `location_id` column**, so the aggregation is
opaque to the caller.

Per-site climate is possible via `R/spatial_join.R::attach_grid_cells()`,
which maps each vector site to its nearest environment grid cell by
centroid proximity. But:

- `attach_grid_cells()` expects its input to carry a `location_id`
  column.
- `query_vectors()` does **not** return `location_id`.

So the function cannot be applied to `query_vectors()` output directly.
This is a known gap. To use it, you'd need a variant of
`query_vectors()` that preserves `location_id`, or a different join key.

### No species-filter pushdown to environment

`query_environment()` accepts `cfg` but only reads `cfg$env_variables`
and `cfg$db_path`/`cfg$db_url`. Species filters are not applied, and are
not applicable — `fact_environment` is species-agnostic.

### No time-series decomposition or lagging

Every query returns rows as stored. If you need a monthly series, lagged
values, or seasonal aggregation, do it in the analysis layer.

### No fallback for missing variables

If `cfg$env_variables` contains a name that does not exist in
`fact_environment`, the query returns zero rows for it — the
`pivot_wider` then produces a column of all `NA`. The pipeline's
`environment_validated` target aborts when the **entire** environment
result is empty, but a partial miss (some variables present, some not)
is not caught.

Verify with:

```sql
SELECT variable, COUNT(*) FROM fact_environment GROUP BY variable;
```

### No date normalisation beyond `as.Date()`

Every query converts `date` from the CDM's `YYYY-MM-DD` string to an R
`Date`. Time zones are not considered; all dates are treated as UTC.

---

## Querying the DB directly

For ad-hoc exploration, bypass the query catalogue:

```r
con <- cdm_connect()

DBI::dbGetQuery(con, "
  SELECT v.species_name, COUNT(*) AS n
  FROM fact_vector_observation o
  LEFT JOIN dim_vector v ON o.vector_code = v.vector_code
  GROUP BY v.species_name
  ORDER BY n DESC
")

DBI::dbGetQuery(con, "
  SELECT variable, COUNT(*) AS n
  FROM fact_environment
  GROUP BY variable
  ORDER BY variable
")

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

- **`mortality` is a percentage in `[0, 100]`.** Not a proportion.
  `98` means 98% mortality (susceptible).
- **`infection_rate` is a proportion in `[0, 1]`.** Not a percentage.
- **Rows with `identification_method_code = 'ESTIMATE'` are synthetic.**
  Excluded by default; include them with `include_estimated_cases: true`.
- **Null sentinels.** Nullable columns in natural keys store `''` or
  `-1.0` instead of `NULL`:

  | Table | Column | Sentinel |
  |---|---|---|
  | `fact_vector_observation` | `life_stage`, `sex`, `collection_method` | `''` |
  | `fact_insecticide_resistance` | `concentration` | `-1.0` |
  | `fact_intervention` | `intervention_type` | `''` |
  | `fact_disease_case` | `case_status`, `age_group`, `sex` | `''` |

  In R, treat `''` and `-1.0` as `NA` for analysis purposes.

- **`dim_time.date_id` is a string, not a `Date`.** Fact tables join to
  it via `date_id`. Let the query planner use the string column directly.

- **Grid cells are not real locations.** Rows in `dim_location` with
  `admin_level = 'grid'` are environment pixels. Never treat them as
  administrative units.