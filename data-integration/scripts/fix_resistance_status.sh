#!/usr/bin/env bash
# Recompute mortality and resistance_status from n_insect_dead / n_insect_tested.
set -euo pipefail

DB="${1:-${DATABASE_URL:-vbd_cdm.db}}"
DB="${DB#sqlite:///}"

[[ -f "$DB" ]] || { echo "DB not found: $DB" >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 required" >&2; exit 1; }

updated=$(sqlite3 "$DB" <<'SQL'
UPDATE fact_insecticide_resistance
SET
    mortality = ROUND((n_insect_dead * 100.0) / n_insect_tested, 2),
    resistance_status = CASE
        WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 98 THEN 'Susceptible'
        WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 90 THEN 'Suspected Resistance'
        ELSE 'Resistant'
    END
WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
  AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0;
SELECT changes();
SQL
)

echo "Updated $updated records"
echo
echo "Distribution:"
sqlite3 -header -column "$DB" "
    SELECT resistance_status, COUNT(*) AS n
    FROM fact_insecticide_resistance
    WHERE resistance_status IS NOT NULL
    GROUP BY resistance_status
    ORDER BY n DESC;"