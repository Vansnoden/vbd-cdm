#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate a CDM database: checks, content digest, determinism, reports."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from evaluation.checks import (
    content_digest, failed, governed_summary, run_checks, write_reports,
)

logger = logging.getLogger("evaluate")


def _session(url: Optional[str]) -> Session:
    """A session on ``url``, or on the project's configured database."""
    if url:
        return Session(create_engine(url))
    from database.session import SessionLocal
    return SessionLocal()


def _load_audit(path: Optional[str]) -> Optional[dict]:
    """Read an ingest ledger written by ``AuditLedger.write``."""
    if not path:
        return None
    candidate = Path(str(path))
    if not candidate.is_file():
        logger.warning("ingest ledger not found: %s", candidate)
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ingest ledger unreadable: %s", exc)
        return None


def command_check(args) -> int:
    """Run every check and write the reports. Returns an exit status."""
    session = _session(args.url)
    try:
        audit = _load_audit(args.audit)
        checks = run_checks(session, audit)
        coverage = governed_summary(session)
        run_id = "" if not audit else str(audit.get("run_id") or "")
        paths = write_reports(checks, args.out, run_id=run_id, audit=audit,
                              coverage=coverage)
    finally:
        session.close()

    for check in checks:
        logger.info("%-4s %-46s %s", check.check_id, check.title, check.status)
    logger.info("reports: %s", ", ".join(paths.values()))
    if failed(checks):
        logger.error("one or more checks FAILED")
        return 1
    return 0


def command_digest(args) -> int:
    """Print the CDM content digest as JSON."""
    session = _session(args.url)
    try:
        payload = content_digest(session)
    finally:
        session.close()
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        logger.info("digest %s written to %s", payload["digest"][:16], target)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_compare(args) -> int:
    """Compare two digest JSON files or two databases. Returns an exit status."""
    def _load(source: str) -> dict:
        candidate = Path(str(source))
        if candidate.is_file() and candidate.suffix == ".json":
            return json.loads(candidate.read_text(encoding="utf-8"))
        session = _session(source)
        try:
            return content_digest(session)
        finally:
            session.close()

    left = _load(args.a)
    right = _load(args.b)
    if left["digest"] == right["digest"]:
        logger.info("identical: digest %s across %d table(s)",
                    left["digest"][:16], len(left["tables"]))
        return 0
    difference = 0
    for table in sorted(set(left["tables"]) | set(right["tables"])):
        if left["tables"].get(table) != right["tables"].get(table):
            difference += 1
            logger.error("differs: %s (rows %s vs %s)", table,
                         left["rows"].get(table), right["rows"].get(table))
    logger.error("%d table(s) differ; the two runs are NOT reproducible", difference)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    """Parse the CLI and dispatch. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="evaluate.py",
        description="Verify a CDM database and produce the evaluation table.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="run every verification check")
    check.add_argument("--url", default=None, help="SQLAlchemy URL")
    check.add_argument("--out", default="outputs/evaluation", help="report directory")
    check.add_argument("--audit", default=None, help="ingest ledger JSON")
    check.set_defaults(func=command_check)

    digest = sub.add_parser("digest", help="print the content digest")
    digest.add_argument("--url", default=None)
    digest.add_argument("--out", default=None)
    digest.set_defaults(func=command_digest)

    compare = sub.add_parser("compare", help="compare two digests or databases")
    compare.add_argument("--a", required=True)
    compare.add_argument("--b", required=True)
    compare.set_defaults(func=command_compare)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())