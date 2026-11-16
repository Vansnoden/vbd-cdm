#!/usr/bin/env bash
# CLI wrapper for burden expansion (delegates to utilities.consolidation).
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd)"
cd "$ROOT"

exec python3 - "$@" <<'PY'
import argparse
from utilities.consolidation import DataConsolidator

p = argparse.ArgumentParser(description="Expand disease burden data into daily cases.")
p.add_argument("--force",     action="store_true",
               help="Delete and re-insert existing ESTIMATE rows.")
p.add_argument("--seed",      type=int,   default=42,
               help="Random seed (default: 42).")
p.add_argument("--variation", type=float, default=0.25,
               help="Base log-normal variation (default: 0.25).")
args = p.parse_args()

c = DataConsolidator()
try:
    c.expand_burden_to_daily(
        force=args.force,
        seed=args.seed,
        base_variation=args.variation,
    )
finally:
    c.close()
PY