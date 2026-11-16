#!/usr/bin/env bash
# Shell entry point for the matplotlib exploration script.
# The Python source stays next to this file because matplotlib has no shell equivalent.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
PY="$SCRIPT_DIR/explore_data.py"

[[ -f "$PY" ]] || { echo "Missing $PY — place explore_data.py next to this wrapper." >&2; exit 1; }

exec python3 "$PY" "$@"