# The VBD CDM contract

> **Status:** stable as of schema revision `e7fb3ebe7463`.
> Any change to the tables, columns, unique constraints, or value domains
> described here requires a coordinated migration *and* a matching update
> to `data-analysis/docs/data-access.md`.

## 1. Purpose

The CDM is a **vector-centred** data warehouse for *Anopheles* and other
VBD-relevant arthropod surveillance. The single most important design rule:

> **Vectors are the study subject. Cases, environment, resistance, and
> bionomics are all interpreted in relation to them.**

This is why `fact_vector_observation` is the largest fact table, why the
ingestion pipeline runs even when `fact_disease_case` is empty, and why the
R analysis pipeline (`data-analysis/_targets.R`) treats case data as
*contextual* rather than *primary*.

## 2. Storage

| Environment   | URL                      | Notes                                                  |
|---------------|--------------------------|--------------------------------------------------------|
| Development   | `sqlite:///./vbd_cdm.db` | Default. Column types are permissive.                  |
| Production    | `postgresql://...`       | URL from `DATABASE_URL`. Uses `COPY` for bulk upserts. |

All timestamps are UTC. All dates are stored twice:

- as a `Date` column (`dim_time.date`) for SQL-level range filters, and
- as a **`YYYY-MM-DD` string FK** (`dim_time.date_id`) on every fact table,
  so joins never rely on dialect-specific date casting.

## 3. Grain

| Table                          | Grain                                                                   |
|--------------------------------|-------------------------------------------------------------------------|
| `fact_vector_observation`      | (location, day, species, source, life_stage, sex, collection_method)    |
| `fact_environment`             | (location, day, variable)                                               |
| `fact_insecticide_resistance`  | (location, day, species, insecticide, test_type, source, concentration) |
| `fact_bionomics`               | (location, day, species, source)                                        |
| `fact_disease_case`            | (location, onset_date, disease, method, source, status, age_group, sex) |
| `fact_disease_burden`          | (location, year, disease, pathogen_species)                             |
| `fact_intervention`            | (location, start_date, disease, source, type)                           |
| `fact_socioeconomic_condition` | (location, year, indicator)                                             |
| `fact_health_facility`         | `facility_code`                                                         |

## 4. Nullable natural-key columns

SQL treats `NULL` as **distinct** in unique indexes. Four fact tables have
nullable columns in their natural key. The ETL loader replaces these with
*empty-string sentinels* before insert:

| Table                         | Column                                   | Sentinel   |
|-------------------------------|------------------------------------------|------------|
| `fact_vector_observation`     | `life_stage`, `sex`, `collection_method` | `''`       |
| `fact_insecticide_resistance` | `concentration`                          | `-1.0`     |
| `fact_intervention`           | `intervention_type`                      | `''`       |
| `fact_disease_case`           | `case_status`, `age_group`, `sex`        | `''`       |

If you query these tables directly, treat `''` and `-1.0` as synonyms for
NULL. `data-analysis/R/db_queries.R` already does this.

`fact_environment` and `fact_disease_burden` have no nullable columns in
their natural keys, so no sentinel applies.

## 5. Reserved identifiers

| Identifier                                           | Meaning                                                                                                                                  |
|------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------|
| `dim_source.source_code = 'ESTIMATE'`                | Rows synthesised by `fix_cases.py` from burden estimates. The R analysis pipeline excludes these unless `include_estimated_cases: true`. |
| `dim_identification_method.method_code = 'ESTIMATE'` | Same.                                                                                                                                    |
| `location_code` starting with `GRID_`                | Environment grid cells. Not real administrative units. Never merged by consolidation.                                                    |
| `admin_level = 'grid'`                               | Marker set on grid-cell rows by the environment ETL.                                                                                     |

## 6. Value domains

### `fact_insecticide_resistance.mortality`

- Stored as a **percentage** in `[0, 100]`, not a proportion.
- Computed as `(n_insect_dead / n_insect_tested) * 100`.
- WHO thresholds: `>= 98` susceptible, `90-97.99` suspected, `< 90` resistant.

### `fact_vector_observation.infection_rate`

- Stored as a **proportion** in `[0, 1]`. *Not* the same convention as
  `mortality`. This asymmetry is deliberate (it matches how the field data
  were originally reported) but is a frequent source of bugs. Always check
  the column name before converting.

### `fact_environment.variable`

Free text. The R analysis pipeline only knows about variables registered in
`SUPPORTED_ENV_VARIABLES` (see `database/schemas.py`). As of the current
schema, that list is:

```default
2t, t2m, tp, d2m, rh, ndvi, evi, lst_day, lst_night,
temperature_mean, temperature_min, temperature_max,
rainfall_total, relative_humidity, aridity_index,
potential_breeding_sites
```

**The ERA5 short name is preserved verbatim.** Temperature from ERA5-Land
files is stored as `2t`, not renamed to `t2m`. Both `2t` and `t2m` are
listed because different ingestion paths (raw ERA5 `.nc`, clipped files
with `--rename-t2m`) can produce either. Downstream code must treat them as
the same physical quantity.

Adding a new variable requires:

1. Registering it in `SUPPORTED_ENV_VARIABLES`.
2. Adding it to `config.yml` `env_variables` on the R side.
3. Optionally extending `data-analysis/R/db_queries.R::query_environment`.

## 7. Upsert semantics — the loader is a *partial* update

This is a property of the ETL, not of the schema, but it is easy to get
wrong when you reason about the database, so it belongs here.

When a source upserts at a natural key that already exists, **only the
columns that source actually populates are updated.** Columns the source
does not touch are left at their existing values.

Concretely: the six MAP burden sources (`map_pf_*`, `map_pv_*`) share the
same natural key `(location_id, year, disease_code, pathogen_species)`.
Each writes exactly one value column — `cases_mean`, `incidence_rate`, or
`prevalence_rate`. Before the loader fix, the last source to run would wipe
the other two columns to NULL. After the fix, each source touches only its
own column, so all three coexist for the same `(location, year, disease, pathogen)`.

The rule the loader applies is: a column participates in the `ON CONFLICT DO UPDATE SET` clause only if the incoming batch has at least one non-null
value for it. If a source is entirely null in a column for a given batch,
that column is left alone.

**Consequence for contract authors.** A new source that legitimately needs
to write NULL into an existing column (to clear it) cannot do so through
the normal upsert path. That is a known limitation; if it ever becomes
necessary, add a per-mapping flag (e.g. `null_authoritative`) and extend
`_upsert_sqlite` / `_upsert_postgresql` / `_copy_upsert_postgresql` to
include such columns unconditionally.

## 8. What the contract does *not* guarantee

- **Location coverage.** A `location_id` present in `dim_location` is not
  guaranteed to have rows in every fact table.
- **Populated `fact_disease_case`.** Many deployments have zero observed
  case rows. See `data-analysis/docs/troubleshooting.md`.
- **Non-null `concept_id`.** Vocabulary standardisation is optional; the FK
  is nullable.
- **Populated `fact_disease_burden` value columns.** When a MAP raster is
  entirely nodata over the study area, the ingest still produces grid-cell
  rows with all three value columns NULL. Filter on `> 0` before using.

## 9. Change procedure

1. Edit `database/models.py`.
2. `alembic revision --autogenerate -m "..."`.
3. **Inspect the generated migration.** Alembic does not detect:
   - changes to `NATURAL_KEY_CONFIG` in `etl/loaders/db_loader.py`,
   - changes to unique constraints that are only enforced via `UniqueConstraint`,
   - renames vs. drop+create.
4. Run `python -m scripts.verify_country_codes --verbose` and
   `python explore_data.py` to confirm nothing regressed.
5. Update this file.
