#!/usr/bin/env bash
# Summarise every _quarantine_* table present in vbd_cdm.db.
# Prints nothing if none exist.

DB="${1:-vbd_cdm.db}"

tables=$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_quarantine_%';")

if [ -z "$tables" ]; then
  echo "No quarantine tables — no source produced orphaned FKs."
  exit 0
fi

for t in $tables; do
  echo ""
  echo "======================================================"
  echo "QUARANTINE: $t"
  echo "======================================================"
  sqlite3 -header -column "$DB" "
    SELECT _orphan_fk_col                  AS fk_col,
           _orphan_value                   AS orphan_value,
           COUNT(*)                        AS n_rows,
           MIN(_quarantined_at)            AS first_seen,
           MAX(_quarantined_at)            AS last_seen
    FROM $t
    GROUP BY _orphan_fk_col, _orphan_value
    ORDER BY n_rows DESC
    LIMIT 20;"
done