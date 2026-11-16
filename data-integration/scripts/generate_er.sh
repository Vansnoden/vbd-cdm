#!/usr/bin/env bash
# ER diagram using ERAlchemy's auto layout + Graphviz unflatten for a compact grid.
set -euo pipefail

DB="${1:-${DATABASE_URL:-vbd_cdm.db}}"
DB="${DB#sqlite:///}"
OUT="${2:-er_diagram.png}"

command -v eralchemy >/dev/null 2>&1 || { echo "Install: pip install eralchemy" >&2; exit 1; }
command -v dot       >/dev/null 2>&1 || { echo "Install graphviz"           >&2; exit 1; }

temp_dot=$(mktemp --suffix=.dot)
temp_flat=$(mktemp --suffix=.dot)
trap 'rm -f "$temp_dot" "$temp_flat"' EXIT

# 1. Base DOT from ERAlchemy
eralchemy -i "sqlite:///$DB" -o "$temp_dot"

# 2. Strip any existing rankdir setting
sed -i -E 's/rankdir[[:space:]]*=[[:space:]]*["'\'']?[A-Za-z]+["'\'']?;?//g' "$temp_dot"

# 3. Inject A4-friendly graph attributes right after "digraph {"
attrs='    graph [
        ratio="fill",
        size="8.0,11.0!",
        dpi=300,
        nodesep=0.4,
        ranksep=0.5,
        overlap=false,
        splines=polyline,
        pack=true,
        packmode="node"
    ];
    node [fontsize=10];'
awk -v a="$attrs" '/^digraph[[:space:]]*\{/ { print; print a; next } 1' \
    "$temp_dot" > "$temp_dot.new" && mv "$temp_dot.new" "$temp_dot"

# 4. Balance vertical stacks into a 3-wide grid (optional)
if command -v unflatten >/dev/null 2>&1; then
    unflatten -f -l3 -c3 "$temp_dot" -o "$temp_flat"
    input="$temp_flat"
else
    input="$temp_dot"
fi

# 5. Render
dot -Tpng "$input" -o "$OUT"
echo "✅ Grid-optimized ER diagram saved to $OUT"