#!/usr/bin/env bash
# Curated-grid ER diagram, built from SQLite pragmas (no Python / SQLAlchemy).
set -euo pipefail

DB="${1:-${DATABASE_URL:-vbd_cdm.db}}"
DB="${DB#sqlite:///}"
OUT="${2:-er_diagram_publication.png}"

[[ -f "$DB" ]] || { echo "DB not found: $DB" >&2; exit 1; }
command -v dot     >/dev/null 2>&1 || { echo "graphviz 'dot' required"     >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 required"            >&2; exit 1; }

temp_dot=$(mktemp --suffix=.dot)
trap 'rm -f "$temp_dot"' EXIT

# Fixed grid layout — mirrors generate_er_neo.py.
grid_rows=(
    "dim_location dim_time dim_source"
    "dim_disease dim_identification_method dim_socioeconomic_indicator"
    "fact_disease_case fact_disease_burden fact_health_facility"
    "fact_intervention fact_environment fact_socioeconomic_condition"
    "dim_vector dim_insecticide dim_resistance_test_type"
    "fact_vector_observation fact_bionomics fact_insecticide_resistance"
)

table_exists() {
    [[ -n "$(sqlite3 "$DB" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$1';")" ]]
}

{
    cat <<'HEADER'
digraph ERD {
    rankdir=TB;
    newrank=true;
    graph [
        splines=ortho,
        nodesep=0.55,
        ranksep=1.0,
        fontname="Helvetica",
        dpi=300
    ];
    node [
        shape=rectangle,
        style=filled,
        fillcolor="#F8F9FA",
        color="#333333",
        fontname="Helvetica-Bold",
        fontsize=8,
        penwidth=1.0,
        margin="0.12,0.06"
    ];
    edge [
        color="#666666",
        arrowsize=0.5,
        penwidth=0.8,
        fontname="Helvetica",
        fontsize=7
    ];

HEADER

    # 1. Nodes
    while IFS= read -r tbl; do
        [[ -z "$tbl" ]] && continue
        pks=$(sqlite3 "$DB" "PRAGMA table_info('$tbl');" \
              | awk -F'|' '$6 > 0 { printf "%s%s", (sep?"":""), $2; sep=", " }')
        if [[ -n "$pks" ]]; then
            label="${tbl}\\n(${pks})"
        else
            label="$tbl"
        fi
        if [[ "$tbl" == fact_* ]]; then fill="#E9ECEF"; else fill="#FFFFFF"; fi
        printf '    "%s" [label="%s", fillcolor="%s"];\n' "$tbl" "$label" "$fill"
    done < <(sqlite3 "$DB" \
        "SELECT name FROM sqlite_master WHERE type='table' AND name!='alembic_version' ORDER BY name;")

    echo

    # 2. Rank rows + invisible ordering edges
    for row in "${grid_rows[@]}"; do
        present=()
        for t in $row; do table_exists "$t" && present+=("$t"); done
        (( ${#present[@]} == 0 )) && continue
        printf '    { rank=same; %s }\n' "${present[*]}"
        for ((i=0; i<${#present[@]}-1; i++)); do
            printf '    "%s" -> "%s" [style=invis, constraint=false];\n' \
                   "${present[i]}" "${present[i+1]}"
        done
    done

    echo

    # 3. Real FK edges
    while IFS= read -r tbl; do
        [[ -z "$tbl" ]] && continue
        sqlite3 "$DB" "PRAGMA foreign_key_list('$tbl');" \
            | awk -F'|' '$3 != "" && $3 != "alembic_version" { print $3 }' \
            | sort -u \
            | while IFS= read -r target; do
                printf '    "%s" -> "%s";\n' "$target" "$tbl"
              done
    done < <(sqlite3 "$DB" \
        "SELECT name FROM sqlite_master WHERE type='table' AND name!='alembic_version' ORDER BY name;")

    echo '}'
} > "$temp_dot"

dot -Tpng "$temp_dot" -o "$OUT"
echo "✅ ER diagram (curated grid) saved to $OUT"