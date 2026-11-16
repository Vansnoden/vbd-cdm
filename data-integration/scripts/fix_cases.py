#!/usr/bin/env python3
"""
fix_cases.py - CLI wrapper for burden expansion.

The implementation lives in
utilities.consolidation.DataConsolidator.expand_burden_to_daily().

This script exists only for backward compatibility with the previous
command-line interface. It is safe to call, and safe to call twice:

    - Without --force, burden records that already have ESTIMATE rows
      are skipped.
    - With --force, existing ESTIMATE rows are deleted and re-inserted.

    The random seed is combined with (location_id, year), so the same
    (location, year) produces the same daily series across runs,
    regardless of how many other locations are processed in between.
"""

import sys
import logging
import argparse
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from utilities.consolidation import DataConsolidator  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expand disease burden data into daily case records."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Delete and re-insert existing ESTIMATE rows for each burden record.",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed (default: 42).",
    )
    parser.add_argument(
        "--variation", type=float, default=0.25,
        help="Base log-normal variation (0–1, default: 0.25).",
    )
    args = parser.parse_args()

    consolidator = DataConsolidator()
    try:
        consolidator.expand_burden_to_daily(
            force=args.force,
            seed=args.seed,
            base_variation=args.variation,
        )
    finally:
        consolidator.close()


if __name__ == "__main__":
    main()