# Configuration

All configuration lives in `config.yml` at the project root. The file has
**two top-level blocks**:

- `vbd-cdm-analysis:` — what to analyse.
- `database:` — where the CDM lives.

The file is read directly by `yaml::read_yaml()` via helpers defined in
`R/config.R`:

| Helper | Used for |
|---|---|
| `read_cfg(key)` | A required key from `vbd-cdm-analysis:`. Aborts if missing. |
| `read_cfg_opt(key, default)` | An optional key. Returns `default` on failure. |
| `read_db_block()` | The whole `database:` block, resolved by `resolve_database()`. |

> **The `config` package is not used.** Its `config::get()` requires a
> top-level `default:` fallback block, which conflicts with the two-block
> layout the project needs. Do not add `default:` back.

> **One file, one study.** `config.yml` describes a single study. Running
> two study areas simultaneously means two project checkouts, two
> databases, and two `targets` caches. There is no built-in multi-study
> support.

---

## Full reference

```yaml
vbd-cdm-analysis:
  study_name: "Case Study"

  # countries: ["ALL"]            # every country in dim_location
  # countries: ["Kenya"]          # name or ISO alpha-2
  countries: ["KE"]

  # locations: ["ALL"]                  # every location inside the country filter
  # locations: ["Siaya County"]         # exact or substring match on dim_location.name
  locations: ["ALL"]

  start_year: 2019
  end_year:   2025

  # diseases: ["ALL"]                     # no disease filter
  diseases: ["MAL", "DENGUE"]             # dim_disease.disease_code values

  include_estimated_cases: true

  env_variables: ["t2m", "tp"]

  # species_filter: ["All"]                                  # every species
  species_filter: ["Anopheles gambiae", "Anopheles funestus"]

  max_lag_months: 6
  output_dir: "outputs/analysis"

database:
  # Uncomment to use PostgreSQL. If both blocks are present, postgres wins.
  # postgres:
  #   host:     "db-host"
  #   port:     "5432"
  #   username: "your-username"
  #   password: "your-password"
  #   dbname:   "vbd_cdm"

  sqlite:
    db_path: "../data-integration/vbd_cdm.db"
```

Every key shown above is passed to `new_analysis_config()` at the top of
`_targets.R`. The YAML key name is always the same as the field name on
`cfg` — there is no renaming layer.

---

## Key-by-key notes

### `study_name`

Used verbatim in figure titles and file names, and in the report title.
Keep it short, human-readable, and filesystem-safe. Avoid `/`, `:`, and
whitespace.

Example file names produced with the default value:

```
outputs/analysis/figures/Case_Study_vector_seasonal.png
outputs/analysis/figures/Case_Study_vector_climate_t2m.png
outputs/analysis/figures/Case_Study_vector_climate_tp.png
outputs/analysis/figures/Case_Study_resistance.png
outputs/analysis/figures/Case_Study_bionomics.png
outputs/analysis/figures/Case_Study_seasonal.png
```

### `countries`

A list of ISO alpha-2 codes **or** full country names. Passed to
`resolve_location_ids(countries = …)`, which uppercases and matches
against `dim_location.country_code` exactly.

- `["ALL"]`, `["all"]`, `["*"]`, `[]`, and a missing key all mean "no
  country filter". `normalize_all_list()` collapses them to `NULL`.
- Mixing `"ALL"` with explicit values (`["ALL", "KE"]`) aborts.

### `locations`

A list of location names. Each element matches `dim_location.name`
**exactly or as a substring** (the SQL is `name = ? OR name LIKE '%…%'`).

- `["ALL"]` (the default) means "every location inside the country
  filter".
- Multiple elements are OR-ed together.

**Abort conditions:**

- Zero rows match -> the pipeline aborts, suggesting you set
  `location_ids` at the query level. (See *Troubleshooting*.)
- More than 20 rows match -> a warning. This usually means the ETL merged
  location labels and your substring now matches the whole continent.

### `start_year` and `end_year`

Inclusive. Applied to `dim_time.year` via the shared `.year_filter()`
helper in `db_queries.R`. Every fact query that joins `dim_time` respects
them.

The resolver does **not** verify that data exist in the range. Check the
QA report to see actual coverage.

### `diseases`

A list of `dim_disease.disease_code` values. Passed to
`query_cases()` and `query_burden()` via `.disease_filter()`.

- `["ALL"]` (or `[]`, or absent) means "no disease filter".
- It does **not** affect vector, environment, resistance, or bionomics
  queries — those tables are disease-agnostic in the CDM.

If your study is malaria-only and the disease dimension stores `"MAL"`,
use `diseases: ["MAL"]`. If it stores the long name, use that.

### `include_estimated_cases`

Boolean. Controls whether `query_cases()` includes rows with
`identification_method_code = 'ESTIMATE'`.

- **`false` (default)** — the filter is `AND c.identification_method_code
  <> 'ESTIMATE'`, so only observed cases are returned.
- **`true`** — no filter; estimated rows are included.

The `'ESTIMATE'` rows are synthesised on the Python side by `fix_cases.py`
and `consolidation._expand_burden_to_daily()` from yearly burden
estimates. They are not observations.

**Only set this to `true` if you understand the difference.** Mixing
observed and modelled case counts in the same figure produces a series
that looks continuous but is not.

The value is coerced with `isTRUE()`, so any truthy value works, but
stick to `true` / `false`.

### `env_variables`

A list of `fact_environment.variable` values. Passed as-is to
`query_environment()`, which uses it in an `IN (...)` clause, then pivots
the long-format result to wide format.

**The variable names must match exactly what the ETL wrote.** The Python
side recognises the names listed in `database/schemas.py`
(`SUPPORTED_ENV_VARIABLES`):

```
t2m, tp, d2m, rh, ndvi, evi, lst_day, lst_night,
temperature_mean, temperature_min, temperature_max,
rainfall_total, relative_humidity, aridity_index,
potential_breeding_sites
```

To check what's actually in the database:

```sql
SELECT DISTINCT variable FROM fact_environment ORDER BY variable;
```

### `species_filter`

A list of species names, matched **exactly, case-insensitively**, against
`dim_vector.species_name` in `query_vectors()`.

- Each element must match exactly one species name in `dim_vector`. If
  any element matches nothing, the query aborts with the list of what's
  actually available. This is deliberate: silent non-matches were the
  single most common source of empty outputs.
- `["All"]`, `[]`, or a missing key disables the filter entirely.
- The canonical form is the full binomial (`"Anopheles gambiae"`), because
  the Python consolidation step runs `_normalize_species_name()` on the
  ETL side before writing `dim_vector`. The names in the DB are the
  canonicalised forms, not the raw source labels.

The QA report includes a **Species filter coverage** table showing which
filter values matched at least one row.

### `max_lag_months`

Integer. Passed to `analyze_vector_climate()` as `max_lag`, which caps
the lag-correlation curve. Lag is measured in the aggregation unit —
with the default `"monthly"` aggregation, `max_lag_months = 6` produces
lag correlations at lags 0 through 6 months.

Six months is a reasonable default for *Anopheles*: it covers the
sporogonic cycle (roughly 10–14 days at tropical temperatures) plus
vector development and population dynamics.

### `output_dir`

Where figures, data, and reports are written. Created by `_targets.R` if
missing, along with `figures/`, `data/`, and `reports/` subdirectories.

If omitted, `_targets.R` falls back to `"outputs/analysis"`.

---

## The `database:` block

Read as a whole by `read_db_block()` and resolved by
`resolve_database()` into `list(db_path = …, db_url = …)`.

Resolution priority:

1. **`postgres:`** — if present and complete (`host`, `port`, `username`,
   `password`; `dbname` defaults to `"vbd_cdm"`), builds a
   `postgresql://user:pw@host:port/dbname` URL. `db_url` is set;
   `db_path` is `NULL`.
2. **`sqlite:`** — `db_path` from `sqlite.db_path`. `db_url` is `NULL`.
3. **`DATABASE_URL` env var** — fallback when neither block is set.
4. **Neither** — both fields are `NULL`, and `cdm_connect()` falls back
   to `SQL_LITE_DB_NAME`.

If both `postgres:` and `sqlite:` are present, postgres wins.

The SQLite path is resolved relative to the project root via
`here::here()`, **not** the working directory. So
`"../data-integration/vbd_cdm.db"` means
`<repo>/data-integration/vbd_cdm.db` regardless of where R is launched.

### `database.postgres`

| Key | Required | Notes |
|---|---|---|
| `host` | yes | |
| `port` | yes | String is fine; it is interpolated as-is. |
| `username` | yes | |
| `password` | yes | |
| `dbname` | no | Defaults to `"vbd_cdm"`. |

Password redaction: the QA report shows the URL with the password
component replaced by `***`.

### `database.sqlite`

| Key | Required | Notes |
|---|---|---|
| `db_path` | yes | Relative to the project root, or absolute. |

---

## Environment variables

These override or supplement `config.yml`. They are read by
`cdm_connect()` in `R/db.R`.

### `DATABASE_URL`

Standard SQLAlchemy/DBI-style URL. Checked **after** an explicit
`db_path` or `db_url` from `config.yml` and **before** `SQL_LITE_DB_NAME`.

Supported schemes:

- `sqlite:///absolute/path/to/vbd_cdm.db`
- `postgresql://user:password@host:port/dbname`

Example:

```bash
export DATABASE_URL="postgresql://analyst:pw@db.example.org:5432/vbd_cdm"
Rscript -e 'targets::tar_make()'
```

If `config.yml` sets either a sqlite or postgres block, `config.yml`
wins. To prefer `DATABASE_URL`, remove the `database:` block entirely,
or set it to an empty mapping.

### `SQL_LITE_DB_NAME`

Filename only, e.g. `vbd_cdm.db`. Checked as a last resort. The filename
is looked up in two places:

1. `<project_root>/<db_name>` — i.e. `data-analysis/vbd_cdm.db`
2. `<project_root>/../<db_name>` — i.e. `<repo>/vbd_cdm.db`

The default is `vbd_cdm.db`. Note the misspelling — `SQL_LITE_DB_NAME`,
not `SQLITE_DB_NAME`. The Python side uses the same spelling.

> `R_CONFIG_ACTIVE` is **not** supported. The `config` package is not
> used anywhere in this pipeline, so there is no multi-environment
> switcher. To run against two databases, use two checkouts (or the
> `DATABASE_URL` env var).

---

## Config invalidation

`cfg` is constructed once, at the top of `_targets.R`, and referenced by
nearly every target in the pipeline. `targets` hashes `cfg` as a global
object, so **any change to any key invalidates every target that
references `cfg`**.

Which targets reference `cfg`:

| Target | Reads `cfg`? |
|---|---|
| `locations` | yes |
| `vectors`, `environment`, `resistance`, `bionomics`, `cases`, `burden` | yes |
| `qa` | yes |
| `vector_seasonal` | no — depends only on `vectors_validated` |
| `vector_climate` | yes — reads `cfg$env_variables` and `cfg$max_lag_months` |
| `resistance_summary`, `bionomics_summary` | no |
| `seasonal` | no |
| All `fig_*` targets | yes — read `cfg$study_name` and `fig_dir` |
| `analysis_results` | yes |
| Reports | yes (via sanitised `cfg`) |

**Practical consequence.** Changing `study_name` invalidates every target
shown above, including the extraction queries. Even though `study_name`
has no effect on the SQL, the fact that `cfg` changed means everything
downstream re-runs.

If this becomes a problem, the fix is to split `cfg` into per-consumer
targets (`cfg_study_name`, `cfg_env_variables`, `cfg_db_path`, …) so
that changing `study_name` invalidates only the targets that actually
read it. This is not currently done.

**For now:** expect a config change to trigger a full pipeline rebuild.

---

## Validating a config change

Before running the pipeline, sanity-check the config:

```r
source("R/config.R")
source("R/db.R")
source("R/db_queries.R")

db <- resolve_database(read_db_block())

cfg <- new_analysis_config(
  study_name    = read_cfg("study_name"),
  countries     = read_cfg_opt("countries"),
  locations     = read_cfg_opt("locations"),
  start_year    = read_cfg("start_year"),
  end_year      = read_cfg("end_year"),
  diseases      = read_cfg_opt("diseases"),
  env_variables = read_cfg("env_variables"),
  max_lag_months = read_cfg("max_lag_months"),
  db_path       = db$db_path,
  db_url        = db$db_url
)

# 1. Does the DB connect?
con <- cdm_connect(cfg$db_path, cfg$db_url)
DBI::dbDisconnect(con)

# 2. Do the locations resolve?
locs <- resolve_location_ids(
  countries = cfg$countries,
  locations = cfg$locations,
  db_path   = cfg$db_path,
  db_url    = cfg$db_url
)
print(locs)

# 3. Do the environment variables exist?
con <- cdm_connect(cfg$db_path, cfg$db_url)
DBI::dbGetQuery(con,
  paste0("SELECT DISTINCT variable FROM fact_environment WHERE variable IN (",
         paste(sprintf("'%s'", cfg$env_variables), collapse = ","), ")"))
DBI::dbDisconnect(con)

# 4. Do the species names exist?
con <- cdm_connect(cfg$db_path, cfg$db_url)
DBI::dbGetQuery(con, "SELECT DISTINCT species_name FROM dim_vector ORDER BY 1")
DBI::dbDisconnect(con)
```

Steps 3 and 4 must return at least one row per requested value, or the
pipeline will abort at the extraction stage.

---

## Common pitfalls

**`countries: ["Kenya"]` with `locations: ["ALL"]`.** The name filter is
resolved against `dim_location.country_code` **exactly** after
uppercasing. If the DB stores `"KE"` but you wrote `"Kenya"`, that's fine
— the resolver also accepts full country names via `get_iso_country_code()`
on the Python side, but **not** on the R side. The R-side resolver runs
`toupper()` on each element. So `["Kenya"]` becomes `["KENYA"]` and will
match only a `country_code` of `"KENYA"`, which is not a valid ISO
alpha-2. Use `["KE"]`.

**`start_year: 2019` on a database that starts in 2020.** No error, no
warning. The extraction simply returns 2020-onwards. Check the QA report.

**`env_variables: ["temperature"]` on a database that stores `t2m`.** The
query returns zero rows and `query_environment()` aborts with a message
about the location and the requested variables. Run:

```sql
SELECT DISTINCT variable FROM fact_environment;
```

**`diseases: ["malaria"]` instead of `["MAL"]`.** The filter matches
`dim_disease.disease_code` **exactly**. Run:

```sql
SELECT disease_code, disease_name FROM dim_disease;
```

**`species_filter: ["gambiae"]`.** The R side does **exact,
case-insensitive** matching, not substring. `"gambiae"` will not match
`"Anopheles gambiae"`. Use the full canonical name. The query aborts
with a list of valid names if any filter element is unknown.

**Empty `species_filter: []`.** Treated as `null`, i.e. no filter. All
species are returned.