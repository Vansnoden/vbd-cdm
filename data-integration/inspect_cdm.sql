-- =============================================================
-- CDM inspection: current state of the database
-- =============================================================

.headers on
.mode column
.width 30 40

.print ''
.print '============================================================'
.print '0. QUARANTINE TABLES (rows removed during FK alignment)'
.print '============================================================'
.print 'If any table is listed below, some source produced orphaned'
.print 'foreign keys. Inspect each with the companion shell snippet.'
.print '============================================================'
SELECT name AS quarantine_table,
       (SELECT COUNT(*) FROM sqlite_master sm2
        WHERE sm2.name = sm.name) AS exists_flag
FROM sqlite_master sm
WHERE type = 'table' AND name LIKE '_quarantine_%'
ORDER BY name;

.print ''
.print '============================================================'
.print '1. ROW COUNTS PER TABLE'
.print '============================================================'
SELECT 'dim_location'             AS table_name, COUNT(*) AS rows FROM dim_location
UNION ALL SELECT 'dim_vector',                     COUNT(*) FROM dim_vector
UNION ALL SELECT 'dim_disease',                    COUNT(*) FROM dim_disease
UNION ALL SELECT 'dim_insecticide',                COUNT(*) FROM dim_insecticide
UNION ALL SELECT 'dim_time',                       COUNT(*) FROM dim_time
UNION ALL SELECT 'fact_disease_case',              COUNT(*) FROM fact_disease_case
UNION ALL SELECT 'fact_vector_observation',        COUNT(*) FROM fact_vector_observation
UNION ALL SELECT 'fact_environment',               COUNT(*) FROM fact_environment
UNION ALL SELECT 'fact_intervention',              COUNT(*) FROM fact_intervention
UNION ALL SELECT 'fact_insecticide_resistance',    COUNT(*) FROM fact_insecticide_resistance
UNION ALL SELECT 'fact_disease_burden',            COUNT(*) FROM fact_disease_burden
UNION ALL SELECT 'fact_socioeconomic_condition',   COUNT(*) FROM fact_socioeconomic_condition;

.print ''
.print '============================================================'
.print '1b. ROW COUNTS — REMAINING TABLES'
.print '============================================================'
SELECT 'dim_source'                     AS table_name, COUNT(*) AS rows FROM dim_source
UNION ALL SELECT 'dim_identification_method',        COUNT(*) FROM dim_identification_method
UNION ALL SELECT 'dim_resistance_test_type',         COUNT(*) FROM dim_resistance_test_type
UNION ALL SELECT 'dim_socioeconomic_indicator',      COUNT(*) FROM dim_socioeconomic_indicator
UNION ALL SELECT 'dim_concept',                      COUNT(*) FROM dim_concept
UNION ALL SELECT 'dim_vocabulary',                   COUNT(*) FROM dim_vocabulary
UNION ALL SELECT 'fact_concept_map',                 COUNT(*) FROM fact_concept_map
UNION ALL SELECT 'fact_bionomics',                   COUNT(*) FROM fact_bionomics
UNION ALL SELECT 'fact_health_facility',             COUNT(*) FROM fact_health_facility;

.print ''
.print '============================================================'
.print '1c. fact_disease_burden BY YEAR (expect 158 pixels × 6 years)'
.print '============================================================'
SELECT year,
       COUNT(*)                      AS n_rows,
       COUNT(DISTINCT location_id)   AS n_pixels,
       ROUND(AVG(incidence_rate), 6) AS mean_incidence,
       ROUND(MIN(incidence_rate), 6) AS min_incidence,
       ROUND(MAX(incidence_rate), 6) AS max_incidence
FROM fact_disease_burden
GROUP BY year
ORDER BY year;

.print ''
.print '============================================================'
.print '2. LOCATION LANDSCAPE - admin_level distribution'
.print '============================================================'
SELECT COALESCE(admin_level, '(null)') AS admin_level,
       COUNT(*) AS n_locations
FROM dim_location
GROUP BY admin_level
ORDER BY n_locations DESC;

.print ''
.print '============================================================'
.print '3. LOCATION LANDSCAPE - country distribution'
.print '============================================================'
SELECT COALESCE(country_code, '(null)') AS country_code,
       COALESCE(country_name, '(null)') AS country_name,
       COUNT(*) AS n_locations
FROM dim_location
GROUP BY country_code, country_name
ORDER BY n_locations DESC;

.print ''
.print '============================================================'
.print '4. LOCATIONS THAT HOLD DATA (cases / vectors / env / int)'
.print '============================================================'
SELECT l.location_id,
       substr(l.name, 1, 32)          AS name,
       COALESCE(l.admin_level, '')    AS admin,
       COALESCE(l.country_code, '')   AS cc,
       (SELECT COUNT(*) FROM fact_disease_case       c WHERE c.location_id = l.location_id) AS n_cases,
       (SELECT COUNT(*) FROM fact_vector_observation v WHERE v.location_id = l.location_id) AS n_vec,
       (SELECT COUNT(*) FROM fact_environment        e WHERE e.location_id = l.location_id) AS n_env,
       (SELECT COUNT(*) FROM fact_intervention       i WHERE i.location_id = l.location_id) AS n_int
FROM dim_location l
WHERE EXISTS (SELECT 1 FROM fact_disease_case       c WHERE c.location_id = l.location_id)
   OR EXISTS (SELECT 1 FROM fact_vector_observation v WHERE v.location_id = l.location_id)
   OR EXISTS (SELECT 1 FROM fact_environment        e WHERE e.location_id = l.location_id)
   OR EXISTS (SELECT 1 FROM fact_intervention       i WHERE i.location_id = l.location_id)
ORDER BY n_cases DESC, n_vec DESC, n_env DESC;

.print ''
.print '============================================================'
.print '5. VECTOR SPECIES INVENTORY (across all locations)'
.print '============================================================'
SELECT v.species_name, COUNT(*) AS n_records,
       COUNT(DISTINCT o.location_id) AS n_locations,
       MIN(t.date) AS date_min, MAX(t.date) AS date_max
FROM fact_vector_observation o
JOIN dim_vector v ON o.vector_code = v.vector_code
JOIN dim_time   t ON o.date_id    = t.date_id
GROUP BY v.species_name
ORDER BY n_records DESC;

.print ''
.print '============================================================'
.print '6. GAMBIAE-COMPLEX ROWS, GROUPED BY LOCATION'
.print '============================================================'
SELECT l.location_id,
       substr(l.name, 1, 40)  AS name,
       COALESCE(l.country_code, '') AS cc,
       v.species_name,
       COUNT(*) AS n
FROM fact_vector_observation o
JOIN dim_vector   v ON o.vector_code = v.vector_code
JOIN dim_location l ON o.location_id = l.location_id
WHERE LOWER(v.species_name) LIKE '%gambiae%'
   OR LOWER(v.species_name) LIKE '%coluzzii%'
   OR LOWER(v.species_name) LIKE '%melas%'
   OR LOWER(v.species_name) LIKE '%quadriannulatus%'
GROUP BY l.location_id, l.name, l.country_code, v.species_name
ORDER BY n DESC;

.print ''
.print '============================================================'
.print '7. ENVIRONMENT - variable inventory'
.print '============================================================'
SELECT e.variable,
       COUNT(*) AS n_records,
       COUNT(DISTINCT e.location_id) AS n_locations,
       MIN(t.date) AS date_min, MAX(t.date) AS date_max
FROM fact_environment e
JOIN dim_time t ON e.date_id = t.date_id
GROUP BY e.variable
ORDER BY n_records DESC;

.print ''
.print '============================================================'
.print '8. CASES - location and disease coverage'
.print '============================================================'
SELECT l.location_id,
       substr(l.name, 1, 40) AS name,
       d.disease_code,
       COUNT(*) AS n,
       MIN(t.date) AS date_min, MAX(t.date) AS date_max
FROM fact_disease_case c
JOIN dim_location l ON c.location_id       = l.location_id
JOIN dim_disease  d ON c.disease_code      = d.disease_code
JOIN dim_time     t ON c.date_id_reported  = t.date_id
GROUP BY l.location_id, l.name, d.disease_code
ORDER BY n DESC;

.print ''
.print '============================================================'
.print '9. INTERVENTIONS'
.print '============================================================'
SELECT l.location_id,
       substr(l.name, 1, 40) AS name,
       i.intervention_type,
       i.coverage,
       MIN(t.date) AS date_min, MAX(t.date) AS date_max,
       COUNT(*) AS n
FROM fact_intervention i
JOIN dim_location l ON i.location_id = l.location_id
JOIN dim_time     t ON i.date_start  = t.date_id
GROUP BY l.location_id, l.name, i.intervention_type, i.coverage
ORDER BY n DESC;

.print ''
.print '============================================================'
.print '9b. INSECTICIDE RESISTANCE SUMMARY'
.print '============================================================'
SELECT
    i.insecticide_name,
    t.test_type_name,
    COUNT(*)                      AS n_tests,
    COUNT(DISTINCT r.location_id) AS n_locations,
    ROUND(AVG(r.mortality), 2)    AS mean_mortality,
    SUM(CASE WHEN r.resistance_status = 'Resistant'            THEN 1 ELSE 0 END) AS n_resistant,
    SUM(CASE WHEN r.resistance_status = 'Suspected Resistance' THEN 1 ELSE 0 END) AS n_suspected,
    SUM(CASE WHEN r.resistance_status = 'Susceptible'          THEN 1 ELSE 0 END) AS n_susceptible
FROM fact_insecticide_resistance r
JOIN dim_insecticide           i ON r.insecticide_code = i.insecticide_code
JOIN dim_resistance_test_type  t ON r.test_type_code   = t.test_type_code
GROUP BY i.insecticide_name, t.test_type_name
ORDER BY n_tests DESC;

.print ''
.print '============================================================'
.print '9c. SOCIOECONOMIC INDICATOR COVERAGE'
.print '============================================================'
SELECT indicator_code,
       COUNT(*)                    AS n_rows,
       COUNT(DISTINCT year)        AS n_years,
       COUNT(DISTINCT location_id) AS n_locations
FROM fact_socioeconomic_condition
GROUP BY indicator_code
ORDER BY n_rows DESC
LIMIT 30;

.print ''
.print '============================================================'
.print '10. LOCATIONS MATCHING SIAYA (any variant)'
.print '============================================================'
SELECT location_id, name, location_code,
       centroid_lat, centroid_lon,
       country_code, country_name, admin_level
FROM dim_location
WHERE LOWER(name)     LIKE '%siaya%'
   OR LOWER(name)     LIKE '%bondo%'
   OR LOWER(name)     LIKE '%rarieda%'
   OR LOWER(name)     LIKE '%ugenya%'
   OR LOWER(name)     LIKE '%gem%'
   OR LOWER(name)     LIKE '%alego%'
   OR LOWER(name)     LIKE '%yala%'
   OR LOWER(location_code) LIKE '%siaya%'
ORDER BY name;

.print ''
.print '============================================================'
.print '11. ORPHAN CHECK - facts referencing missing dim rows'
.print '============================================================'
SELECT 'cases: location_id' AS check_name, COUNT(*) AS orphans
  FROM fact_disease_case c
  LEFT JOIN dim_location l ON c.location_id = l.location_id
  WHERE l.location_id IS NULL
UNION ALL
SELECT 'vectors: location_id', COUNT(*)
  FROM fact_vector_observation v
  LEFT JOIN dim_location l ON v.location_id = l.location_id
  WHERE l.location_id IS NULL
UNION ALL
SELECT 'env: location_id', COUNT(*)
  FROM fact_environment e
  LEFT JOIN dim_location l ON e.location_id = l.location_id
  WHERE l.location_id IS NULL
UNION ALL
SELECT 'vectors: vector_code', COUNT(*)
  FROM fact_vector_observation v
  LEFT JOIN dim_vector d ON v.vector_code = d.vector_code
  WHERE d.vector_code IS NULL
UNION ALL
SELECT 'cases: date_id_reported', COUNT(*)
  FROM fact_disease_case c
  LEFT JOIN dim_time t ON c.date_id_reported = t.date_id
  WHERE t.date_id IS NULL;

.print ''
.print '============================================================'
.print '12. SAMPLE dim_location ROWS (first 40 alphabetically)'
.print '============================================================'
SELECT location_id, substr(name, 1, 40) AS name, location_code,
       country_code, admin_level
FROM dim_location
ORDER BY name
LIMIT 40;

.print ''
.print '============================================================'
.print '13. SOURCE DISTRIBUTION ACROSS FACT TABLES'
.print '============================================================'
SELECT 'fact_vector_observation'         AS fact_table, source_code, COUNT(*) AS n
  FROM fact_vector_observation GROUP BY source_code
UNION ALL SELECT 'fact_disease_burden',                source_code, COUNT(*)
  FROM fact_disease_burden GROUP BY source_code
UNION ALL SELECT 'fact_disease_case',                  source_code, COUNT(*)
  FROM fact_disease_case GROUP BY source_code
UNION ALL SELECT 'fact_environment',                   source_code, COUNT(*)
  FROM fact_environment GROUP BY source_code
UNION ALL SELECT 'fact_insecticide_resistance',        source_code, COUNT(*)
  FROM fact_insecticide_resistance GROUP BY source_code
UNION ALL SELECT 'fact_bionomics',                     source_code, COUNT(*)
  FROM fact_bionomics GROUP BY source_code
UNION ALL SELECT 'fact_intervention',                  source_code, COUNT(*)
  FROM fact_intervention GROUP BY source_code
UNION ALL SELECT 'fact_socioeconomic_condition',       source_code, COUNT(*)
  FROM fact_socioeconomic_condition GROUP BY source_code
UNION ALL SELECT 'fact_health_facility',               source_code, COUNT(*)
  FROM fact_health_facility GROUP BY source_code
ORDER BY fact_table, n DESC;

.print ''
.print '============================================================'
.print 'DONE.'
.print '============================================================'