# Configuration

All configuration lives in `config.yml` at the project root. The file is
read by the `config` package under the top-level key `default:`, and the
resolved values become the `cfg` object that every target in the pipeline
receives.

> **One file, one study.** `config.yml` describes a single study.
> Running two study areas simultaneously means two project checkouts, two
> databases, and two `targets` caches. There is no built-in
> multi-study support.

---

## Full reference

```yaml
default:
  study_name: "Pan_African_Anopheles"   # label used in figure titles and filenames
  location:   "ALL"                     # location name or "ALL"
  country:    "ALL"                     # ISO alpha-2 code or "ALL"
  location_ids: []                      # explicit IDs; overrides location/country

  start_year: 2019
  end_year:   2025
  disease_code: null                    # e.g. "MAL"; null = no filter
  include_estimated_cases: false        # include rows with identification_method_code='ESTIMATE'

  env_variables: ["t2m", "tp"]          # fact_environment.variable values

  species_filter:                       # case-insensitive substrings
    - "gambiae"
    - "coluzzii"
    - "arabiensis"
    - "funestus"
    - "melas"
    - "quadriannulatus"

  max_lag_months: 6                     # max lag for lag-correlation analysis
  database: "../data-integration/vbd_cdm.db"
  output_dir: "outputs/analysis"
```

Every key shown above is passed to `new_analysis_config()` at the top of
`_targets.R`. The function name of the config key is not always the same
as the field name on `cfg` — see the notes below.

---

## Key-by-key notes

### `study_name`

Used verbatim in figure titles and file names, and in the report title.
Keep it short, human-readable, and filesystem-safe. Avoid `/`, `:`, and
whitespace.

Example file names produced with the default value:

```
outputs/analysis/figures/Pan_African_Anopheles_vector_seasonal.png
outputs/analysis/figures/Pan_African_Anopheles_vector_climate_t2m.png
outputs/analysis/figures/Pan_African_Anopheles_vector_climate_tp.png
outputs/analysis/figures/Pan_African_Anopheles_resistance.png
outputs/analysis/figures/Pan_African_Anopheles_bionomics.png
outputs/analysis/figures/Pan_African_Anopheles_seasonal.png
```

### `location` and `country`

Name-based resolution. `resolve_location_ids()` runs:

```sql
SELECT location_id, name, country_code, admin_level
FROM dim_location
WHERE country_code = ?            -- skipped if country = 'ALL'
  AND (name = ? OR name LIKE ?)   -- skipped if location = 'ALL'
ORDER BY name
```

where the `LIKE` pattern is `%<location>%`.

**Behaviour:**

- If both are `"ALL"`, every location in `dim_location` is selected.
- If only `country` is set, every location in that country is selected.
- If `location` is set, it matches exactly **or** as a substring.

**Abort conditions:**

- Zero rows match → the pipeline aborts with a suggestion to set
  `location_ids`.
- More than 20 rows match → a warning is emitted. This usually means the
  ETL merged location labels and your substring now matches the whole
  continent. See [Troubleshooting](troubleshooting.md).

### `location_ids`

An explicit list of integer `location_id` values. When non-empty, this
takes priority over `location` and `country` — those fields are ignored
entirely.

**Behaviour:**

- Aborts if *none* of the IDs exist in `dim_location`.
- Warns if *some* IDs are missing, and continues with the ones that do.
- An empty list (`[]`) is treated as `null`, i.e. falls back to name-based
  resolution.

Resolve IDs with:

```sql
SELECT location_id, name, country_code, admin_level
FROM dim_location
WHERE country_code = 'KE'
ORDER BY name;
```

### `start_year` and `end_year`

Inclusive. Applied to `dim_time.year` via the shared `.year_filter()`
helper in `db_queries.R`. Every fact query that joins `dim_time` respects
them.

The resolver does **not** verify that data exist in the range. If
`fact_vector_observation` starts in 2020 and you set `start_year: 2019`,
you will silently get 2020 onwards. Check the QA report to see the actual
coverage.

### `disease_code`

A `dim_disease.disease_code` value, or `null`. When non-null:

- `query_cases()` filters `fact_disease_case.disease_code = ?`.
- `query_burden()` filters `fact_disease_burden.disease_code = ?`.

It does **not** affect vector, environment, resistance, or bionomics
queries — those tables are disease-agnostic in the CDM.

Set to `null` if you want the disease dimension to be ignored.

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
(`SUPPORTED_ENV_VARIABLES`), which currently includes:

```
t2m, tp, d2m, rh, ndvi, evi, lst_day, lst_night,
temperature_mean, temperature_min, temperature_max,
rainfall_total, relative_humidity, aridity_index,
potential_breeding_sites
```

Adding a variable that exists in the database but is not in this list
works fine on the R side — the query simply returns whatever is there.
The list exists to document what the ETL is expected to write.

To check what's actually in the database:

```sql
SELECT DISTINCT variable FROM fact_environment ORDER BY variable;
```

Adding a new variable end-to-end requires coordination:

1. The Python ETL writes it into `fact_environment`.
2. It is added to `SUPPORTED_ENV_VARIABLES` in `database/schemas.py` (for
   documentation and validation).
3. It is added to `env_variables` in this file.
4. Optionally, the analysis tolerates the new variable — `analyze_vector_climate()`
   auto-detects numeric columns, so no code change is needed.

### `species_filter`

A list of substrings, matched **case-insensitively** against two columns
in `query_vectors()`:

- `dim_vector.species_name`
- `fact_vector_observation.vector_code`

Each list element becomes a `LIKE '%<value>%'` pattern. Multiple elements
are OR-ed together.

**Consequences:**

- `"gambiae"` matches `"Anopheles gambiae"`, `"An. gambiae s.s."`, and
  any `vector_code` containing that substring.
- Overly broad substrings (`"a"`, `"spp"`) will match nearly everything.
- An empty list or `null` disables the filter entirely — all species are
  returned.

The QA report includes a **Species filter coverage** table showing which
filter values matched at least one row. If a filter value is silently
unmatched, this table is how you find out.

### `max_lag_months`

Integer. Passed to `analyze_vector_climate()` as `max_lag`, which caps
the lag-correlation curve. Lag is measured in the aggregation unit — with
the default `"monthly"` aggregation, `max_lag_months = 6` produces lag
correlations at lags 0 through 6 months.

Six months is a reasonable default for *Anopheles*: it covers the
sporogonic cycle (roughly 10–14 days at tropical temperatures) plus
vector development and population dynamics. Larger values produce more
lag points but fewer paired observations at the deepest lags, so the
confidence intervals widen quickly.

### `database`

Path to the SQLite database. Resolved relative to the project root via
`here::here()`, **not** the current working directory. So the default
`"../data-integration/vbd_cdm.db"` means
`<repo>/data-integration/vbd_cdm.db`, regardless of where you invoke R
from.

The value is stored on `cfg` as `db_path`, not `database`. This is a
naming mismatch inherited from the config layer; it's harmless but worth
knowing if you inspect `cfg`.

An absolute path also works. See [Environment variables](#environment-variables)
for how `DATABASE_URL` interacts with this.

### `output_dir`

Where figures, data, and reports are written. Created by `_targets.R` if
missing, along with the `figures/`, `data/`, and `reports/` subdirectories.

If omitted, `_targets.R` falls back to `"outputs/analysis"`.

The path is relative to the project root. All plot functions receive
`fig_dir <- file.path(cfg$output_dir, "figures")` and write there.

---

## Environment variables

These override or supplement `config.yml`. They are read by
`cdm_connect()` in `R/db.R`.

### `DATABASE_URL`

Standard SQLAlchemy/DBI-style URL. Checked **after** the explicit
`db_path` from `config.yml` and **before** `SQL_LITE_DB_NAME`.

Supported schemes:

- `sqlite:///absolute/path/to/vbd_cdm.db`
- `postgresql://user:password@host:port/dbname`

Example:

```bash
export DATABASE_URL="postgresql://analyst:pw@db.example.org:5432/vbd_cdm"
Rscript -e 'targets::tar_make()'
```

If `config.yml` sets `database: "../data-integration/vbd_cdm.db"` and
`DATABASE_URL` is also set, `config.yml` wins, because `db_path` is the
first argument passed to `cdm_connect()`. To prefer `DATABASE_URL`,
remove the `database` key from `config.yml`, or set it to an empty string.

### `SQL_LITE_DB_NAME`

Filename only, e.g. `vbd_cdm.db`. Checked as a last resort. The filename
is looked up in two places:

1. The project root (`data-analysis/vbd_cdm.db`).
2. The project's parent directory (`vbd_cdm.db`, i.e. the sibling of
   `data-analysis/`).

The default is `vbd_cdm.db`. Note the misspelling — `SQL_LITE_DB_NAME`,
not `SQLITE_DB_NAME`. The Python side uses the same spelling, so they
agree, but the typo is easy to misremember.

### `R_CONFIG_ACTIVE`

Set by the `config` package. When unset (the normal case),
`config::get("key")` reads from the `default:` block in `config.yml`.
When set to a value, `config::get()` reads from a same-named top-level
block instead.

For example, to run the pipeline with a production database:

```yaml
default:
  database: "../data-integration/vbd_cdm.db"
  # ... other defaults

production:
  database: "/var/lib/vbd_cdm/vbd_cdm.db"
  start_year: 2015
```

```bash
export R_CONFIG_ACTIVE=production
Rscript -e 'targets::tar_make()'
```

Anything not overridden in the active block falls back to `default`. This
is useful for running the same pipeline against different databases
without editing `config.yml`.

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
| `vectors`, `environment`, `resistance`, `bionomics`, `cases`, `burden` | yes (via `cfg` passed to the query functions) |
| `qa` | yes |
| `vector_seasonal` | no — depends only on `vectors_validated` |
| `vector_climate` | yes — reads `cfg$env_variables` and `cfg$max_lag_months` |
| `resistance_summary`, `bionomics_summary` | no |
| `seasonal` | no |
| All `fig_*` targets | yes — read `cfg$study_name` and `fig_dir` |
| `analysis_results` | yes |
| Reports | yes (via sanitised `cfg`) |

**Practical consequence.** Changing `study_name` from
`"Pan_African_Anopheles"` to something else invalidates every target
shown above, including the extraction queries. Even though
`study_name` has no effect on the SQL, the fact that `cfg` changed means
everything downstream re-runs.

If this becomes a problem, the fix is to split `cfg` into per-consumer
targets (`cfg_study_name`, `cfg_env_variables`, `cfg_db_path`, …) so
that changing `study_name` invalidates only the targets that actually
read it. This is not currently done.

**For now:** expect a config change to trigger a full pipeline rebuild.
On a large database, that's the difference between a 30-second run and a
30-minute one.

---

## Validating a config change

Before running the pipeline, sanity-check the config with a small script:

```r
source("R/config.R")
source("R/db.R")
source("R/db_queries.R")

cfg <- new_analysis_config(
  study_name = config::get("study_name"),
  location   = config::get("location"),
  country    = config::get("country"),
  start_year = config::get("start_year"),
  end_year   = config::get("end_year"),
  db_path    = config::get("database")
)

# 1. Does the DB connect?
con <- cdm_connect(cfg$db_path)
DBI::dbDisconnect(con)

# 2. Do the locations resolve?
locs <- resolve_location_ids(cfg$location, cfg$country, db_path = cfg$db_path)
print(locs)

# 3. Do the environment variables exist?
con <- cdm_connect(cfg$db_path)
DBI::dbGetQuery(con,
  paste0("SELECT DISTINCT variable FROM fact_environment WHERE variable IN (",
         paste(sprintf("'%s'", cfg$env_variables), collapse = ","),
         ")"))
DBI::dbDisconnect(con)
```

If step 2 aborts, the location name or country code is wrong. If step 3
returns fewer rows than `length(cfg$env_variables)`, one or more of the
requested variables is not present in the database.

---

## Common pitfalls

**`location: "Kenya"` with `country: "ALL"`.** The name filter matches
any location whose name *contains* `"Kenya"`. If the ETL stored a
`"Kenya - Siaya"` label, that match is intentional. If it stored
`"Western Kenya"` on 200 rows, the resolver will warn about the fan-out
and proceed. Set `location_ids` explicitly to avoid the ambiguity.

**`start_year: 2019` on a database that starts in 2020.** No error, no
warning. The extraction simply returns 2020-onwards. Check the QA report
to see the actual coverage.

**`env_variables: ["temperature"]` on a database that stores `t2m`.** The
query returns zero rows, and `query_environment()` aborts the pipeline
with a message about the location and the requested variables. Run:

```sql
SELECT DISTINCT variable FROM fact_environment;
```

to see the correct names.

**`disease_code: "malaria"` instead of `"MAL"`.** The filter matches
`dim_disease.disease_code` exactly. `"malaria"` will match zero rows if
the code is `"MAL"`. Run:

```sql
SELECT disease_code, disease_name FROM dim_disease;
```

**`location_ids: ["1", "2"]` (strings).** The resolver calls
`as.integer(unlist(...))`, so numeric strings are accepted. But if a
value can't be coerced to an integer (`"KE-001"`), it becomes `NA`, and
the query will pass `NA` to SQLite, which will return zero rows without
error. Keep `location_ids` as integers.

**Empty `species_filter: []`.** Treated as `null`, i.e. no filter. All
species are returned. This is usually what you want for a first-pass QA
run, but it can be surprising if you expected an empty result.