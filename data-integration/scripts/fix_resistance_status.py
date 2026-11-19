# -*- coding: utf-8 -*-
"""
fix_resistance_status.py - recompute resistance_status for stored bioassays.

This is a **thin CLI over the one implementation**. It contains no counting
logic and no classification rule of any kind: it opens a session, calls
``DataConsolidator._compute_resistance_status``, and reports. Before this
revision the script carried its own copy of the four passes, including its own
copies of the mortality cut-offs, which meant the pipeline and the repair tool
could disagree about what "Susceptible" means — and there was no way to tell
which of them had written a given row.

The cut-offs themselves live in
``vocabularies/registry/who-resistance/manifest.yaml``; see
``utilities.consolidation._resistance_thresholds``. Nothing in this file names
a threshold.

Usage::

    python scripts/fix_resistance_status.py
    python scripts/fix_resistance_status.py --verify-only
    python scripts/fix_resistance_status.py --registry vocabularios/registry

Exit status
-----------
0   the pass ran (or the verification completed)
1   the session or the computation raised; the message is on stderr
2   the script was asked to run and no registry could be loaded

Why it reuses the consolidator's session factory
------------------------------------------------
``DataConsolidator()`` with no argument opens its own ``SessionLocal()``, which
is bound to ``DATABASE_URL`` at import time — exactly what this script wants,
and exactly what a repair tool must not get wrong by inventing a second engine.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow `python scripts/fix_resistance_status.py` from the repository root
# without installing the package. Inserted before the application imports, and
# only the project root: scripts/ itself is not a package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from sqlalchemy import text  # noqa: E402

from utilities.consolidation import (  # noqa: E402
    DataConsolidator,
    _resistance_thresholds,
)

from etl.transformers.concept_resolver import RESISTANCE_BANDS

logger = logging.getLogger(__name__)


def _verify(session, susceptible, suspected) -> int:
    """Report the stored classification distribution. Returns an exit status.

    Read-only. A status that contradicts the cut-offs is reported as an ERROR
    rather than corrected: rewriting it is what ``--verify-only`` exists to
    avoid, and the caller asked to look, not to change.

    The three band labels are read from
    ``etl.transformers.concept_resolver.RESISTANCE_BANDS`` and bound as
    parameters. Before this revision they were three literal strings inside the
    mismatch query, which meant the tool that *checks* the classification could
    disagree with the tool that *writes* it, with no way to tell which of the two
    had spelled a band differently (review §6.10).

    Parameters
    ----------
    session : sqlalchemy.orm.Session
    susceptible : float
        The susceptible cut-off, as a mortality percentage.
    suspected : float
        The suspected-resistance cut-off, as a mortality percentage.

    Returns
    -------
    int
        0 when the stored distribution is consistent with the cut-offs, 1 when
        it is not.
    """
    susceptible_label = RESISTANCE_BANDS["susceptible"]
    suspected_label = RESISTANCE_BANDS["suspected"]
    resistant_label = RESISTANCE_BANDS["resistant"]

    rows = session.execute(text("""
        SELECT resistance_status, COUNT(*) AS n
        FROM fact_insecticide_resistance
        WHERE resistance_status IS NOT NULL AND resistance_status != ''
        GROUP BY resistance_status
        ORDER BY resistance_status
    """)).fetchall()

    unclassified = session.execute(text("""
        SELECT COUNT(*) FROM fact_insecticide_resistance
        WHERE resistance_status IS NULL OR resistance_status = ''
    """)).scalar() or 0

    total = session.execute(
        text("SELECT COUNT(*) FROM fact_insecticide_resistance")
    ).scalar() or 0

    print(
        f"cut-offs in force: {susceptible_label} >= {susceptible:g}, "
        f"{suspected_label} >= {suspected:g} (mortality %)"
    )
    print(f"rows: {total}, classified: {total - unclassified}, "
          f"unclassified: {unclassified}")

    if not rows:
        print("no classified rows")
        return 0

    known = {susceptible_label, suspected_label, resistant_label}
    for status, count in rows:
        marker = "" if status in known else "   <- not a band this tool writes"
        print(f"  {status!r:<24} {count}{marker}")

    mismatches = session.execute(text("""
        SELECT COUNT(*) FROM fact_insecticide_resistance
        WHERE mortality IS NOT NULL
          AND resistance_status IS NOT NULL AND resistance_status != ''
          AND (
                (mortality >= :susceptible
                 AND resistance_status <> :susceptible_label)
             OR (mortality <  :susceptible AND mortality >= :suspected
                 AND resistance_status <> :suspected_label)
             OR (mortality <  :suspected
                 AND resistance_status <> :resistant_label)
          )
    """), {
        "susceptible": susceptible,
        "suspected": suspected,
        "susceptible_label": susceptible_label,
        "suspected_label": suspected_label,
        "resistant_label": resistant_label,
    }).scalar() or 0

    if mismatches:
        print(
            f"ERROR: {mismatches} row(s) carry a resistance_status that "
            f"contradicts their mortality against the cut-offs in force. "
            f"Re-run without --verify-only to recompute them."
        )
        return 1

    print("every classified row agrees with the cut-offs in force")
    return 0
    

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute fact_insecticide_resistance.mortality and "
            "resistance_status from whichever counts each row carries."
        ),
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="report the stored classification without changing anything",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default: INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stdout,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    load_dotenv()

    # Resolve the cut-offs before opening a consolidator, so a missing registry
    # is reported as a packaging problem rather than as a database error three
    # frames down.
    try:
        susceptible, suspected = _resistance_thresholds()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    consolidator = None
    try:
        consolidator = DataConsolidator()

        if args.verify_only:
            return _verify(consolidator.session, susceptible, suspected)

        consolidator._compute_resistance_status()
        return _verify(consolidator.session, susceptible, suspected)

    except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not trace
        logger.error("resistance-status pass failed: %s: %s",
                     type(exc).__name__, exc)
        return 1
    finally:
        if consolidator is not None:
            consolidator.close()


if __name__ == "__main__":
    raise SystemExit(main())