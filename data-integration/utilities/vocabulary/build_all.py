# -*- coding: utf-8 -*-
"""Build every vocabulary CSV the registry needs, into the pack directories.
Usage:
    python -m utilities.vocabulary.build_all --gpkg /data/gadm41_KEN.gpkg \\
                                             --obo /data/idomal.obo
    python -m utilities.vocabulary.build_all --gpkg ... --obo ... --ncbi
    python -m utilities.vocabulary.build_all --status
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_ROOT = PROJECT_ROOT / "vocabularies" / "registry"

PACK_TARGETS: Dict[str, Dict[str, str]] = {
    "gadm":  {"domain": "location", "builder": "locations",
              "vocabulary_id": "gadm@4.1"},
    "idomal": {"domain": "disease", "builder": "diseases",
               "vocabulary_id": "idomal@2024-05"},
    "ncbi":  {"domain": "vector",  "builder": "species",
              "vocabulary_id": "ncbi@2024-11"},
}


def destination(pack_id: str) -> Path:
    """Where ``pack_id``'s concepts CSV belongs."""
    return REGISTRY_ROOT / pack_id / "concepts.csv"


def status() -> Dict[str, Dict[str, Any]]:
    """Which packs have a CSV, how big, and what it holds. Read-only."""
    report: Dict[str, Dict[str, Any]] = {}
    for pack_id in sorted(PACK_TARGETS):
        path = destination(pack_id)
        entry: Dict[str, Any] = {"path": str(path), "exists": path.is_file()}
        if path.is_file():
            entry["bytes"] = path.stat().st_size
            try:
                import pandas as pd
                frame = pd.read_csv(path, usecols=lambda c: c in
                                    ("concept_code", "concept_name"))
                entry["concepts"] = int(len(frame))
                entry["blank_codes"] = int(
                    frame.get("concept_code", pd.Series(dtype=str)).isna().sum())
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"
        report[pack_id] = entry
    return report


def log_status(report: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    report = report or status()
    logger.info("=" * 62)
    logger.info("VOCABULARY CSV STATUS")
    logger.info("=" * 62)
    for pack_id in sorted(report):
        entry = report[pack_id]
        if not entry["exists"]:
            logger.info("  %-8s MISSING   %s", pack_id, entry["path"])
            continue
        logger.info("  %-8s %7d concept(s)  %8.1f KB  %s", pack_id,
                    entry.get("concepts", 0), entry.get("bytes", 0) / 1024.0,
                    entry["path"])
        if entry.get("blank_codes"):
            logger.warning("    %d row(s) have no concept_code and will be "
                           "skipped on load", entry["blank_codes"])
        if entry.get("error"):
            logger.error("    unreadable: %s", entry["error"])
    logger.info("=" * 62)


def build_locations(gpkg: Path, pack_id: str = "gadm",
                    country_codes: Optional[List[str]] = None) -> int:
    """Boundary concepts from a GADM GeoPackage. Returns rows written."""
    from utilities.vocabulary.locations import build_location_vocabulary

    if not gpkg.is_file():
        raise FileNotFoundError(f"boundary file not found: {gpkg}")
    target = destination(pack_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = build_location_vocabulary(
        str(gpkg), str(target), country_codes=country_codes)
    logger.info("  %s: %d location concept(s) -> %s", pack_id, len(frame), target)
    return len(frame)


def build_diseases(obo: Path, pack_id: str = "idomal",
                   vocabulary_id: str = "IDOMAL") -> int:
    """Disease concepts from an OBO ontology. Returns rows written."""
    from utilities.vocabulary.diseases import build_disease_vocabulary

    if not obo.is_file():
        raise FileNotFoundError(f"ontology file not found: {obo}")
    target = destination(pack_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = build_disease_vocabulary(
        obo, target, vocabulary_id=vocabulary_id,
        ontology_prefix=f"{vocabulary_id}:")
    logger.info("  %s: %d disease concept(s) -> %s", pack_id, len(frame), target)
    return len(frame)


def build_species(pack_id: str = "ncbi", genera: Optional[List[str]] = None,
                  email: str = "you@example.com") -> int:
    """Vector species from NCBI Taxonomy. Requires network access."""
    from utilities.vocabulary.species import build_species_vocabulary

    target = destination(pack_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = build_species_vocabulary(
        str(target), genera=genera, email=email)
    logger.info("  %s: %d species concept(s) -> %s", pack_id, len(frame), target)
    return len(frame)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m utilities.vocabulary.build_all",
        description="Build the vocabulary CSVs the registry loads.")
    parser.add_argument("--pack", action="append", choices=sorted(PACK_TARGETS),
                        help="build only this pack (repeatable)")
    parser.add_argument("--gpkg", type=Path, default=None,
                        help="GADM GeoPackage, e.g. gadm41_KEN.gpkg")
    parser.add_argument("--obo", type=Path, default=None,
                        help="IDOMAL OBO file, e.g. idomal.obo")
    parser.add_argument("--ncbi", action="store_true",
                        help="fetch vector species from NCBI Taxonomy (network)")
    parser.add_argument("--country", action="append", default=None,
                        help="GID_0 filter for the boundary build, e.g. KEN")
    parser.add_argument("--email", default="you@example.com",
                        help="contact address NCBI requires")
    parser.add_argument("--status", action="store_true",
                        help="report which CSVs exist and exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level), stream=sys.stdout,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    if args.status:
        log_status()
        return 0

    wanted = set(args.pack or PACK_TARGETS)
    failures = 0

    if "gadm" in wanted:
        if args.gpkg is None:
            logger.warning("  gadm: skipped, no --gpkg given")
        else:
            try:
                build_locations(args.gpkg, country_codes=args.country)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.error("  gadm: build failed: %s: %s",
                             type(exc).__name__, exc)

    if "idomal" in wanted:
        if args.obo is None:
            logger.warning("  idomal: skipped, no --obo given")
        else:
            try:
                build_diseases(args.obo)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.error("  idomal: build failed: %s: %s",
                             type(exc).__name__, exc)

    if "ncbi" in wanted:
        if not args.ncbi:
            logger.warning("  ncbi: skipped, pass --ncbi to fetch from NCBI")
        else:
            try:
                build_species(email=args.email)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.error("  ncbi: build failed: %s: %s",
                             type(exc).__name__, exc)

    log_status()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PACK_TARGETS",
    "build_diseases",
    "build_locations",
    "build_species",
    "destination",
    "log_status",
    "main",
    "status",
]