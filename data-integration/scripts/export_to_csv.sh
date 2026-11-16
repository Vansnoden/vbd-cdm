#!/usr/bin/env bash
# Stream every fact table (with dims pre-joined) into per-table CSVs and zip them.
# Peak memory is independent of DB size — sqlite3 streams row by row.
#
# Usage:
#   export_to_csv.sh [--db DB] [--out PREFIX] [--limit N]
#
# --out PREFIX   Writes PREFIX.zip (default: outputs/cdm_export).
# --limit N      Cap rows per fact table (for smoke tests).
set -euo pipefail

DB="${DATABASE_URL:-vbd_cdm.db}"; DB="${DB#sqlite:///}"
OUT="outputs/cdm_export"
LIMIT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)    DB="$2"; DB="${DB#sqlite:///}"; shift 2 ;;
        --out)   OUT="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --per-table) shift ;;              # accepted for compat; this is the only mode
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

[[ -f "$DB" ]] || { echo "DB not found: $DB" >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 required" >&2; exit 1; }

LIM_CLAUSE=""
[[ -n "$LIMIT" ]] && LIM_CLAUSE=" LIMIT $LIMIT"

WORK_DIR="$(dirname "$OUT")/$(basename "$OUT")_tmp"
rm -rf "$WORK_DIR"; mkdir -p "$WORK_DIR"

export_one() {
    local name="$1" sql="$2" path n
    path="$WORK_DIR/$name.csv"
    n=$(sqlite3 "$DB" "SELECT COUNT(*) FROM ( $sql $LIM_CLAUSE );" 2>/dev/null || echo "?")
    sqlite3 -header -csv "$DB" "SELECT * FROM ( $sql $LIM_CLAUSE );" > "$path"
    printf '  %-32s %10s rows\n' "$name" "$n"
}

echo "Exporting facts → $WORK_DIR"

export_one fact_vector_observation "
    SELECT 'fact_vector_observation' AS fact_table,
           o.observation_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           t.date_id, t.date, t.year, t.month, t.week, t.season,
           NULL AS date_id_secondary,
           v.vector_code, v.species_name, v.genus, v.family,
           NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           o.life_stage, o.sex, o.collection_method,
           o.count, o.density_per_trap,
           o.tested_for_pathogen, o.infection_status, o.infection_rate
      FROM fact_vector_observation o
      LEFT JOIN dim_location l ON o.location_id = l.location_id
      LEFT JOIN dim_time     t ON o.date_id     = t.date_id
      LEFT JOIN dim_vector   v ON o.vector_code = v.vector_code
      LEFT JOIN dim_source   s ON o.source_code = s.source_code
"

export_one fact_disease_case "
    SELECT 'fact_disease_case' AS fact_table,
           c.case_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           t.date_id, t.date, t.year, t.month, t.week, t.season,
           c.date_id_reported AS date_id_secondary,
           NULL AS vector_code, NULL AS species_name, NULL AS genus, NULL AS family,
           d.disease_code, d.disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           c.identification_method_code,
           c.case_status, c.host_type,
           c.age_years, c.age_group, c.sex,
           c.outcome, c.case_count
      FROM fact_disease_case c
      LEFT JOIN dim_location l ON c.location_id   = l.location_id
      LEFT JOIN dim_time     t ON c.date_id_onset = t.date_id
      LEFT JOIN dim_disease  d ON c.disease_code  = d.disease_code
      LEFT JOIN dim_source   s ON c.source_code   = s.source_code
"

export_one fact_insecticide_resistance "
    SELECT 'fact_insecticide_resistance' AS fact_table,
           r.resistance_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           t.date_id, t.date, t.year, t.month, t.week, t.season,
           NULL AS date_id_secondary,
           v.vector_code, v.species_name, v.genus, v.family,
           NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           r.insecticide_code, i.insecticide_name, i.chemical_class,
           r.test_type_code, tt.test_type_name,
           r.concentration, r.exposure_time,
           r.n_insect_tested, r.n_insect_susceptible,
           r.n_insect_resistant, r.n_insect_dead,
           r.vgsc_n_insect_tested,
           r.vgsc995l_vgsc995l_n, r.vgsc995l_vgsc995f_n,
           r.vgsc995f_vgsc995f_n, r.vgsc995l_vgsc995s_n,
           r.vgsc995s_vgsc995s_n, r.vgsc995f_vgsc995s_n,
           r.vgsc_generation,
           r.mortality, r.resistance_status
      FROM fact_insecticide_resistance r
      LEFT JOIN dim_location             l  ON r.location_id      = l.location_id
      LEFT JOIN dim_time                 t  ON r.date_id          = t.date_id
      LEFT JOIN dim_vector               v  ON r.vector_code      = v.vector_code
      LEFT JOIN dim_insecticide          i  ON r.insecticide_code = i.insecticide_code
      LEFT JOIN dim_resistance_test_type tt ON r.test_type_code   = tt.test_type_code
      LEFT JOIN dim_source               s  ON r.source_code      = s.source_code
"

export_one fact_bionomics "
    SELECT 'fact_bionomics' AS fact_table,
           b.bionomics_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           t.date_id, t.date, t.year, t.month, t.week, t.season,
           NULL AS date_id_secondary,
           v.vector_code, v.species_name, v.genus, v.family,
           NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           b.biting_rate, b.feeding_preference, b.biting_location,
           b.resting_behavior, b.larval_site_type
      FROM fact_bionomics b
      LEFT JOIN dim_location l ON b.location_id = l.location_id
      LEFT JOIN dim_time     t ON b.date_id     = t.date_id
      LEFT JOIN dim_vector   v ON b.vector_code = v.vector_code
      LEFT JOIN dim_source   s ON b.source_code = s.source_code
"

export_one fact_intervention "
    SELECT 'fact_intervention' AS fact_table,
           i.intervention_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           t.date_id, t.date, t.year, t.month, t.week, t.season,
           i.date_end AS date_id_secondary,
           NULL AS vector_code, NULL AS species_name, NULL AS genus, NULL AS family,
           d.disease_code, d.disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           i.intervention_type, i.coverage
      FROM fact_intervention i
      LEFT JOIN dim_location l ON i.location_id         = l.location_id
      LEFT JOIN dim_time     t ON i.date_start          = t.date_id
      LEFT JOIN dim_disease  d ON i.target_disease_code = d.disease_code
      LEFT JOIN dim_source   s ON i.source_code         = s.source_code
"

export_one fact_socioeconomic_condition "
    SELECT 'fact_socioeconomic_condition' AS fact_table,
           sc.socio_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           NULL AS date_id, NULL AS date, sc.year,
           NULL AS month, NULL AS week, NULL AS season,
           NULL AS date_id_secondary,
           NULL AS vector_code, NULL AS species_name, NULL AS genus, NULL AS family,
           NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           sc.indicator_code, si.indicator_name, si.unit,
           sc.indicator_value
      FROM fact_socioeconomic_condition sc
      LEFT JOIN dim_location                l  ON sc.location_id    = l.location_id
      LEFT JOIN dim_socioeconomic_indicator si ON sc.indicator_code = si.indicator_code
      LEFT JOIN dim_source                  s  ON sc.source_code    = s.source_code
"

export_one fact_health_facility "
    SELECT 'fact_health_facility' AS fact_table,
           h.facility_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           NULL AS date_id, NULL AS date,
           NULL AS year, NULL AS month, NULL AS week, NULL AS season,
           NULL AS date_id_secondary,
           NULL AS vector_code, NULL AS species_name, NULL AS genus, NULL AS family,
           NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           h.facility_code, h.name AS facility_name,
           h.lat, h.lon, h.type, h.catchment_population
      FROM fact_health_facility h
      LEFT JOIN dim_location l ON h.location_id = l.location_id
      LEFT JOIN dim_source   s ON h.source_code = s.source_code
"

export_one fact_disease_burden "
    SELECT 'fact_disease_burden' AS fact_table,
           b.burden_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           NULL AS date_id, NULL AS date,
           b.year, NULL AS month, NULL AS week, NULL AS season,
           NULL AS date_id_secondary,
           NULL AS vector_code, NULL AS species_name, NULL AS genus, NULL AS family,
           d.disease_code, d.disease_name, b.pathogen_species,
           s.source_code, s.source_name,
           b.incidence_rate, b.prevalence_rate,
           b.mortality_rate, b.cases_mean
      FROM fact_disease_burden b
      LEFT JOIN dim_location l ON b.location_id = l.location_id
      LEFT JOIN dim_disease  d ON b.disease_code = d.disease_code
      LEFT JOIN dim_source   s ON b.source_code  = s.source_code
"

export_one fact_environment "
    SELECT 'fact_environment' AS fact_table,
           e.fact_env_id AS fact_id,
           l.location_id, l.name AS location_name,
           l.country_code, l.country_name, l.admin_level,
           l.centroid_lat, l.centroid_lon,
           t.date_id, t.date, t.year, t.month, t.week, t.season,
           NULL AS date_id_secondary,
           NULL AS vector_code, NULL AS species_name, NULL AS genus, NULL AS family,
           NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
           s.source_code, s.source_name,
           e.variable, e.value
      FROM fact_environment e
      LEFT JOIN dim_location l ON e.location_id = l.location_id
      LEFT JOIN dim_time     t ON e.date_id     = t.date_id
      LEFT JOIN dim_source   s ON e.source_code = s.source_code
"

cat > "$WORK_DIR/README.txt" <<'EOF'
CDM export (per-table mode)

Each CSV corresponds to one fact table. Dimension values are joined in;
raw FK columns are preserved. Blank cells mean the column does not apply
to that particular fact table.
EOF

ZIP="${OUT}.zip"
( cd "$WORK_DIR" && zip -q -r "$ZIP" ./* )
rm -rf "$WORK_DIR"

echo "Done: $ZIP ($(du -h "$ZIP" | cut -f1))"