# -*- coding: utf-8 -*-
"""Load the shipped vocabularies, then align stored values to them.

The value-alignment chain, in the order it must run:

1. :func:`load_shipped_vocabularies` — ``dim_concept`` is written only by
   ``VocabularyLoader.load_csv``, and nothing called it. Reconciliation
   therefore ran against an empty dictionary and marked every value
   ``unresolved``.
2. ``DataConsolidator.standardize_vocabularies`` — matches values and writes
   ``concept_id`` / ``standard_code`` on a dimension, or a decision row in
   ``fact_concept_map`` for a fact column.
3. :func:`align_dimension_values` — rewrites a dimension's label column to the
   canonical concept name, so two rows that resolved to one concept stop
   carrying two spellings even before the merge pass groups them.
4. :func:`align_fact_values` — the same rewrite for a fact table's free-text
   column, which has no ``concept_id`` and is joined through
   ``fact_concept_map`` instead.

:func:`describe_target` and :func:`require_data` exist because the first
question a zero-everywhere report must answer is whether it read the database
the pipeline wrote. ``database.session`` fixes ``DATABASE_URL`` at import time,
so an entry point that does not load ``.env`` first resolves the fallback and
SQLite silently creates an empty file with the full schema.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Loaded before anything that touches `database`: session.py resolves its URL at
# import time and a later load_dotenv() is a no-op for this process.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001 - python-dotenv is optional here
    pass

from sqlalchemy import text

from utilities.vocabulary.field_catalog import (
    can_stamp,
    fact_value_fields,
    iter_bindings,
    resolve_field,
)

logger = logging.getLogger(__name__)

MAX_IN_VARS = 900

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_ROOT = PROJECT_ROOT / "vocabularies" / "registry"
VOCABULARY_ROOT = PROJECT_ROOT / "vocabularies"

#: Dimension label columns that are never rewritten to the canonical name.
#:
#: ``dim_location.name`` is here for a specific reason:
#: ``resolve_parent_locations`` stamps a ``standard_code`` on **every** row whose
#: centroid falls inside a boundary polygon, including survey sites and grid
#: cells. A concept-driven rename would therefore overwrite a site's own name
#: with its district's, destroying the identity the site exists to carry. A
#: location's name is its own; its boundary membership is ``standard_code``.
NEVER_RENAME = frozenset({"dim_location.name"})


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text_value = str(value).strip()
    return "" if text_value.lower() in ("nan", "nat", "<na>", "none") else text_value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rewritable_fields() -> tuple[str, ...]:
    """Every catalog field whose value may be replaced by the concept name.

    All text fields except :data:`NEVER_RENAME`. Dimension fields are rewritten
    from their own ``concept_id``; fact fields are rewritten through
    ``fact_concept_map``.
    """
    return tuple(
        binding.target_field for binding in iter_bindings()
        if binding.value_type == "text"
        and binding.target_field not in NEVER_RENAME
    )


# ---------------------------------------------------------------------------
# Which database is this
# ---------------------------------------------------------------------------

def describe_target(session) -> Dict[str, Any]:
    """The resolved URL, file and row counts this process is actually reading."""
    from database.session import DATABASE_URL, engine

    info: Dict[str, Any] = {
        "database_url": DATABASE_URL,
        "file": engine.url.database,
        "driver": engine.url.drivername,
    }
    for key, sql in (
        ("sources_loaded", "SELECT COUNT(*) FROM dim_source"),
        ("locations", "SELECT COUNT(*) FROM dim_location"),
        ("vectors", "SELECT COUNT(*) FROM dim_vector"),
        ("concepts", "SELECT COUNT(*) FROM dim_concept"),
    ):
        try:
            info[key] = int(session.execute(text(sql)).scalar() or 0)
        except Exception:  # noqa: BLE001 - schema may be absent
            info[key] = None
    return info


def require_data(session, *, allow_empty: bool = False) -> Dict[str, Any]:
    """Abort unless the database this process is reading holds data.

    ``dim_location`` is the probe: every ingest writes some, so zero means a
    different (or brand-new) database. Aborting with the URL beats emitting a
    report of zeros, which reads as "nothing resolved" when the truth is
    "nothing was looked at".
    """
    info = describe_target(session)
    empty = (info.get("locations") or 0) == 0 and (info.get("sources_loaded") or 0) == 0
    if empty and not allow_empty:
        raise SystemExit(
            f"this process is reading {info['database_url']!r} "
            f"(file {info['file']!r}) and it holds no dim_location and no "
            f"dim_source rows.\n"
            f"That is not a vocabulary problem: it is not the database the "
            f"pipeline wrote to.\n"
            f"  * run from the directory `run.sh` uses, with its environment: "
            f"`set -a; . ./.env; set +a`, or\n"
            f"  * point this command at the right file: DATABASE_URL="
            f"sqlite:////absolute/path/to/vbd_cdm.db\n"
            f"Use --allow-empty to report against an empty database anyway."
        )
    return info


# ---------------------------------------------------------------------------
# Loading vocabulary CSVs
# ---------------------------------------------------------------------------

def _csv_candidates(pack: Any) -> List[Path]:
    """CSVs that could hold ``pack``'s concepts, in a fixed order.

    Filesystem-based because a ``concepts_csv`` manifest key cannot be added
    without editing ``registry.REQUIRED_KEYS``. Conventions, in order:
    ``vocabularies/registry/<pack_id>/*.csv``, ``vocabularies/<pack_id>.csv``,
    ``vocabularies/<pack_id>.<version>.csv``.
    """
    pack_id = _clean(getattr(pack, "pack_id", ""))
    version = _clean(getattr(pack, "version", ""))
    if not pack_id:
        return []

    found: List[Path] = []
    pack_dir = REGISTRY_ROOT / pack_id
    if pack_dir.is_dir():
        found.extend(sorted(p for p in pack_dir.glob("*.csv") if p.is_file()))
    for name in (f"{pack_id}.csv", f"{pack_id}.{version}.csv" if version else ""):
        if not name:
            continue
        candidate = VOCABULARY_ROOT / name
        if candidate.is_file():
            found.append(candidate)

    seen: set = set()
    ordered: List[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(path)
    return ordered


def _stored_checksum(session, vocabulary_id: str) -> Optional[str]:
    try:
        row = session.execute(text(
            "SELECT checksum FROM dim_vocabulary WHERE vocabulary_id = :v"),
            {"v": vocabulary_id}).first()
    except Exception as exc:  # noqa: BLE001 - pre-migration database
        logger.debug("could not read dim_vocabulary.checksum for %r: %s",
                     vocabulary_id, exc)
        return None
    return None if row is None else _clean(row[0]) or None


def _concept_count(session, vocabulary_id: str) -> int:
    try:
        return int(session.execute(text(
            "SELECT COUNT(*) FROM dim_concept WHERE vocabulary_id = :v"),
            {"v": vocabulary_id}).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not count dim_concept for %r: %s", vocabulary_id, exc)
        return 0


def load_shipped_vocabularies(session, registry: Any, *,
                              dry_run: bool = False) -> Dict[str, Any]:
    """Load every CSV that ships with a concept pack into ``dim_concept``.

    Idempotent by checksum: a pack whose ``dim_vocabulary.checksum`` already
    matches its CSV is skipped, so a re-run costs one hash and no writes. A pack
    with no CSV is reported, not fatal — the registry may describe a vocabulary
    the operator has not built yet.
    """
    result: Dict[str, Any] = {"packs": {}, "loaded": 0, "skipped": 0,
                              "no_csv": 0, "errors": 0, "concepts": 0}
    if registry is None:
        logger.error("  no registry: no vocabulary can be loaded, so nothing "
                     "downstream will resolve.")
        result["errors"] = 1
        return result

    from utilities.vocabulary.loader import VocabularyLoader

    loader = VocabularyLoader(session)

    for pack in registry.packs():
        vocabulary_id = getattr(pack, "vocabulary_id", "")
        entry: Dict[str, Any] = {
            "pack_id": getattr(pack, "pack_id", "?"),
            "vocabulary_id": vocabulary_id,
            "domain": getattr(pack, "domain", "?"),
            "concepts_before": _concept_count(session, vocabulary_id),
        }
        result["packs"][entry["pack_id"]] = entry

        candidates = _csv_candidates(pack)
        if not candidates:
            entry["status"] = "no_csv"
            result["no_csv"] += 1
            logger.warning(
                "  %s (%s): no vocabulary CSV. Looked for %s/%s/*.csv and "
                "%s/%s.csv. Build one with "
                "`python -m utilities.vocabulary.build_all --pack %s`. Every "
                "value in domain %r stays unresolved until then.",
                entry["pack_id"], vocabulary_id, REGISTRY_ROOT, entry["pack_id"],
                VOCABULARY_ROOT, entry["pack_id"], entry["pack_id"], entry["domain"])
            continue

        csv_path = candidates[0]
        entry["csv"] = str(csv_path)
        if len(candidates) > 1:
            logger.info("  %s: %d candidate CSV(s), loading %s",
                        entry["pack_id"], len(candidates), csv_path.name)

        try:
            digest = _sha256(csv_path)
        except OSError as exc:
            entry["status"] = "error"
            entry["reason"] = f"{type(exc).__name__}: {exc}"
            result["errors"] += 1
            logger.error("  %s: cannot hash %s: %s", entry["pack_id"], csv_path, exc)
            continue

        stored = _stored_checksum(session, vocabulary_id)
        if stored and stored == digest and entry["concepts_before"] > 0:
            entry["status"] = "skipped"
            entry["concepts"] = entry["concepts_before"]
            result["skipped"] += 1
            logger.info("  %s: already loaded (%d concept(s), checksum %s…)",
                        entry["pack_id"], entry["concepts_before"], digest[:12])
            continue

        try:
            written = loader.load_csv(csv_path, pack, checksum=digest,
                                      dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - one bad CSV must not stop the rest
            entry["status"] = "error"
            entry["reason"] = f"{type(exc).__name__}: {exc}"
            result["errors"] += 1
            logger.error("  %s: loading %s failed: %s: %s",
                         entry["pack_id"], csv_path.name, type(exc).__name__, exc)
            continue

        after = _concept_count(session, vocabulary_id)
        entry["status"] = "dry_run" if dry_run else "loaded"
        entry["concepts"] = after
        entry["written"] = written
        if not dry_run:
            result["loaded"] += 1
            result["concepts"] += after
        if after == 0 and not dry_run:
            result["errors"] += 1
            logger.error(
                "  %s: loaded %s but dim_concept still holds 0 row(s) for %r. "
                "Check that the CSV carries concept_code and concept_name, and "
                "that the pack's domain matches the rows.",
                entry["pack_id"], csv_path.name, vocabulary_id)

    logger.info("  vocabularies: %d loaded (%d concept(s)), %d already present, "
                "%d without a CSV, %d error(s)",
                result["loaded"], result["concepts"], result["skipped"],
                result["no_csv"], result["errors"])
    return result


# ---------------------------------------------------------------------------
# Rewriting values
# ---------------------------------------------------------------------------

def align_dimension_values(session, registry: Any, *,
                           dry_run: bool = False) -> Dict[str, Any]:
    """Rewrite a dimension's label column to the concept's canonical name.

    Runs after reconciliation, so ``concept_id`` is populated only on rows whose
    decision was ``auto_accepted``; a row awaiting review has no ``concept_id``
    and is left exactly as the source sent it.

    The source spelling is not lost: ``fact_concept_map`` keeps
    ``(source_table, source_column, source_value, concept_code, match_method,
    match_score)``, so the rewrite is auditable and reversible.

    Idempotent: the predicate requires the stored label to differ from the
    concept name, so a second run reports 0 and writes nothing.
    """
    result: Dict[str, Any] = {"fields": {}, "updated": 0, "dry_run": bool(dry_run)}

    for binding in iter_bindings():
        field = binding.target_field
        if binding.value_type != "text" or not can_stamp(field):
            continue
        if field in NEVER_RENAME:
            result["fields"][field] = {"status": "kept",
                                       "reason": "identity is not a label"}
            continue

        table = binding.dimension_table
        column = field.split(".", 1)[1]
        predicate = (
            f"t.concept_id IS NOT NULL AND EXISTS ("
            f"  SELECT 1 FROM dim_concept c WHERE c.concept_id = t.concept_id "
            f"  AND c.concept_name IS NOT NULL AND TRIM(c.concept_name) <> '' "
            f"  AND c.concept_name <> COALESCE(t.{column}, ''))"
        )
        entry: Dict[str, Any] = {"table": table, "column": column}

        try:
            pending = int(session.execute(text(
                f"SELECT COUNT(*) FROM {table} t WHERE {predicate}")).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "error"
            entry["reason"] = f"{type(exc).__name__}: {exc}"
            result["fields"][field] = entry
            continue

        entry["pending"] = pending
        if not pending:
            entry["status"] = "nothing_to_do"
            result["fields"][field] = entry
            continue
        if dry_run:
            entry["status"] = "dry_run"
            result["fields"][field] = entry
            continue

        try:
            written = int(session.execute(text(
                f"UPDATE {table} SET {column} = ("
                f"  SELECT c.concept_name FROM dim_concept c "
                f"  WHERE c.concept_id = {table}.concept_id) "
                f"WHERE {predicate.replace('t.', f'{table}.')}"
            )).rowcount or 0)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            entry["status"] = "error"
            entry["reason"] = f"{type(exc).__name__}: {exc}"
            logger.error("  %s: could not align %s: %s", table, column, exc)
            result["fields"][field] = entry
            continue

        entry["status"] = "ok"
        entry["updated"] = written
        result["updated"] += written
        logger.info("  %s.%s: %d row(s) renamed to the canonical concept name",
                    table, column, written)
        result["fields"][field] = entry

    logger.info("  dimension values: %d row(s) rewritten across %d field(s)%s",
                result["updated"], len(result["fields"]),
                " (dry run)" if dry_run else "")
    return result


def align_fact_values(session, registry: Any, *,
                      dry_run: bool = False) -> Dict[str, Any]:
    """Rewrite a fact table's free-text column to the canonical concept name.

    A fact table has no ``concept_id``, so the decision lives only in
    ``fact_concept_map``. The join is therefore
    ``fact_concept_map.source_value = the table's own column``, restricted to
    ``decision = 'auto_accepted'`` and to the vocabulary that produced it.

    Nothing is rewritten when no pack governs the column, which is the honest
    outcome: it means no vocabulary has an opinion about ``life_stage`` yet.
    """
    result: Dict[str, Any] = {"fields": {}, "updated": 0,
                              "ungoverned": [], "dry_run": bool(dry_run)}

    for field in fact_value_fields():
        if field in NEVER_RENAME:
            continue
        binding = resolve_field(field)
        table = binding.dimension_table
        column = field.split(".", 1)[1]
        entry: Dict[str, Any] = {"table": table, "column": column}

        try:
            governed = int(session.execute(text(
                "SELECT COUNT(*) FROM fact_concept_map "
                "WHERE source_table = :t AND source_column = :c "
                "AND decision = 'auto_accepted' AND concept_code IS NOT NULL"
            ), {"t": table, "c": column}).scalar() or 0)
        except Exception as exc:  # noqa: BLE001 - pre-migration database
            entry["status"] = "error"
            entry["reason"] = f"{type(exc).__name__}: {exc}"
            result["fields"][field] = entry
            continue

        if not governed:
            entry["status"] = "ungoverned"
            result["ungoverned"].append(field)
            result["fields"][field] = entry
            continue

        lookup = (
            "SELECT c.concept_name FROM fact_concept_map m "
            "JOIN dim_concept c ON c.concept_code = m.concept_code "
            "AND c.vocabulary_id = m.vocabulary_id "
            "WHERE m.source_table = :t AND m.source_column = :c "
            "AND m.decision = 'auto_accepted' AND m.concept_code IS NOT NULL "
            f"AND m.source_value = {table}.{column} "
            "AND c.concept_name IS NOT NULL AND TRIM(c.concept_name) <> '' "
            f"AND c.concept_name <> {table}.{column} "
            "LIMIT 1"
        )
        exists = (
            "SELECT 1 FROM fact_concept_map m "
            "WHERE m.source_table = :t AND m.source_column = :c "
            "AND m.decision = 'auto_accepted' AND m.concept_code IS NOT NULL "
            f"AND m.source_value = {table}.{column}"
        )
        params = {"t": table, "c": column}

        try:
            pending = int(session.execute(text(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL "
                f"AND EXISTS ({exists})"
            ), params).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "error"
            entry["reason"] = f"{type(exc).__name__}: {exc}"
            result["fields"][field] = entry
            continue

        entry["pending"] = pending
        if not pending:
            entry["status"] = "nothing_to_do"
            result["fields"][field] = entry
            continue
        if dry_run:
            entry["status"] = "dry_run"
            result["fields"][field] = entry
            continue

        try:
            written = int(session.execute(text(
                f"UPDATE {table} SET {column} = ({lookup}) "
                f"WHERE {column} IS NOT NULL AND EXISTS ({exists})"
            ), params).rowcount or 0)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            entry["status"] = "error"
            entry["reason"] = f"{type(exc).__name__}: {exc}"
            logger.error("  %s: could not align %s: %s", table, column, exc)
            result["fields"][field] = entry
            continue

        entry["status"] = "ok"
        entry["updated"] = written
        result["updated"] += written
        logger.info("  %s.%s: %d value(s) rewritten to the canonical concept name",
                    table, column, written)
        result["fields"][field] = entry

    if result["ungoverned"]:
        logger.info("  %d fact column(s) have no governing vocabulary and were "
                    "left unchanged: %s", len(result["ungoverned"]),
                    ", ".join(result["ungoverned"]))
    logger.info("  fact values: %d row(s) rewritten across %d field(s)%s",
                result["updated"],
                sum(1 for e in result["fields"].values() if e.get("status") == "ok"),
                " (dry run)" if dry_run else "")
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _distinct_count(session, table: str, column: str) -> int:
    try:
        return int(session.execute(text(
            f"SELECT COUNT(DISTINCT {column}) FROM {table} "
            f"WHERE {column} IS NOT NULL AND TRIM(CAST({column} AS TEXT)) <> ''"
        )).scalar() or 0)
    except Exception:  # noqa: BLE001 - column may not exist yet
        return 0


def alignment_report(session, registry: Any) -> Dict[str, Any]:
    """Per-field reconciliation state, plus every field no pack governs.

    Read-only. This separates the three failure modes: a pack with 0 concepts
    (never loaded), a field with values and 0 ``auto_accepted`` (loaded but not
    matching), and a table with 0 rows (wrong database — see
    :func:`describe_target`).
    """
    report: Dict[str, Any] = {"vocabularies": [], "fields": [],
                              "ungoverned": []}

    bound: set = set()
    if registry is not None:
        for pack in registry.packs():
            vocabulary_id = getattr(pack, "vocabulary_id", "")
            report["vocabularies"].append({
                "pack_id": getattr(pack, "pack_id", "?"),
                "vocabulary_id": vocabulary_id,
                "domain": getattr(pack, "domain", "?"),
                "concepts": _concept_count(session, vocabulary_id),
                "fuzzy_threshold": getattr(pack, "fuzzy_threshold", None),
                "normalizer_chain": list(getattr(pack, "normalizer_chain", ()) or ()),
            })
        bound = set(registry.field_bindings())

    for binding in iter_bindings():
        field = binding.target_field
        table = binding.dimension_table
        column = field.split(".", 1)[1]
        row: Dict[str, Any] = {"field": field, "domain": binding.domain,
                               "table": table, "column": column,
                               "stampable": can_stamp(field),
                               "governed": field in bound}
        try:
            row["rows"] = int(session.execute(text(
                f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
            report["fields"].append(row)
            continue
        row["distinct_values"] = _distinct_count(session, table, column)

        if field not in bound:
            row.update({"auto_accepted": 0, "needs_review": 0, "unresolved": 0,
                        "with_concept": 0, "unresolved_samples": []})
            if row["distinct_values"]:
                report["ungoverned"].append(
                    {"field": field, "distinct_values": row["distinct_values"]})
            report["fields"].append(row)
            continue

        decisions = {"auto_accepted": 0, "needs_review": 0, "unresolved": 0,
                     "other": 0}
        samples: List[str] = []
        try:
            for decision, count in session.execute(text(
                "SELECT COALESCE(decision, '(none)') AS d, COUNT(*) "
                "FROM fact_concept_map WHERE source_table = :t "
                "AND source_column = :c GROUP BY COALESCE(decision, '(none)')"
            ), {"t": table, "c": column}).fetchall():
                key = _clean(decision)
                decisions[key if key in decisions else "other"] += int(count)
            samples = [
                _clean(value) for (value,) in session.execute(text(
                    "SELECT source_value FROM fact_concept_map "
                    "WHERE source_table = :t AND source_column = :c "
                    "AND (decision IS NULL OR decision <> 'auto_accepted') "
                    "ORDER BY map_id LIMIT 5"),
                {"t": table, "c": column}).fetchall()
                if _clean(value)
            ]
        except Exception as exc:  # noqa: BLE001
            row["decision_error"] = f"{type(exc).__name__}: {exc}"

        row.update(decisions)
        row["unresolved_samples"] = samples
        if can_stamp(field):
            try:
                row["with_concept"] = int(session.execute(text(
                    f"SELECT COUNT(*) FROM {table} WHERE concept_id IS NOT NULL"
                )).scalar() or 0)
            except Exception:  # noqa: BLE001
                row["with_concept"] = 0
        else:
            row["with_concept"] = 0
        report["fields"].append(row)

    return report


def log_report(report: Dict[str, Any],
               target: Optional[Dict[str, Any]] = None) -> None:
    """Write :func:`alignment_report` to the logger, human-readable."""
    logger.info("=" * 74)
    logger.info("VOCABULARY ALIGNMENT REPORT")
    logger.info("=" * 74)
    if target:
        logger.info("  database: %s", target["database_url"])
        logger.info("  file:     %s", target["file"] or "(in memory)")
        logger.info("  rows:     %s location(s), %s source(s), %s vector(s), "
                    "%s concept(s)", target.get("locations"),
                    target.get("sources_loaded"), target.get("vectors"),
                    target.get("concepts"))
        logger.info("-" * 74)

    for pack in report["vocabularies"]:
        logger.info("  pack %-12s %-18s concepts=%-7d threshold=%s chain=%s",
                    pack["pack_id"], pack["vocabulary_id"], pack["concepts"],
                    pack["fuzzy_threshold"], ",".join(pack["normalizer_chain"]))
    logger.info("-" * 74)
    logger.info("  %-46s %6s %7s %6s %6s %6s", "field", "rows", "distinct",
                "auto", "review", "unres")
    for row in report["fields"]:
        marker = "" if row["governed"] else "  [no vocabulary]"
        logger.info("  %-46s %6d %7d %6d %6d %6d%s",
                    row["field"], row.get("rows", 0), row.get("distinct_values", 0),
                    row.get("auto_accepted", 0), row.get("needs_review", 0),
                    row.get("unresolved", 0), marker)
    if report["ungoverned"]:
        logger.info("-" * 74)
        logger.info("  %d field(s) hold values no vocabulary governs; they are "
                    "merged on a normalised key only:", len(report["ungoverned"]))
        for entry in report["ungoverned"]:
            logger.info("    %-46s %d distinct", entry["field"],
                        entry["distinct_values"])
    logger.info("=" * 74)


def align_all(session, registry: Any, *, dry_run: bool = False,
              report_only: bool = False,
              allow_empty: bool = False) -> Dict[str, Any]:
    """Load, rewrite dimensions, rewrite fact values, report — in that order."""
    target = require_data(session, allow_empty=allow_empty)
    result: Dict[str, Any] = {"dry_run": bool(dry_run), "target": target}
    if report_only:
        result["report"] = alignment_report(session, registry)
        log_report(result["report"], target)
        return result

    result["load"] = load_shipped_vocabularies(session, registry, dry_run=dry_run)
    result["align"] = align_dimension_values(session, registry, dry_run=dry_run)
    result["align_facts"] = align_fact_values(session, registry, dry_run=dry_run)
    result["report"] = alignment_report(session, registry)
    log_report(result["report"], target)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m utilities.vocabulary.alignment",
        description="Load the shipped vocabularies and align stored values.")
    parser.add_argument("--registry", type=Path, default=REGISTRY_ROOT)
    parser.add_argument("--load", action="store_true",
                        help="load the vocabulary CSVs into dim_concept")
    parser.add_argument("--align", action="store_true",
                        help="rewrite label and fact-value columns to the "
                             "canonical concept name")
    parser.add_argument("--report", action="store_true",
                        help="print the reconciliation report (default action)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="report against a database with no rows instead of "
                             "refusing")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level), stream=sys.stdout,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    from database.session import SessionLocal, resolve_database_url
    from utilities.vocabulary.registry import VocabularyRegistry

    logger.info("database url: %s", resolve_database_url())

    if not args.registry.is_dir():
        logger.error("no registry at %s", args.registry)
        return 2
    registry = VocabularyRegistry.load_from(args.registry)

    session = SessionLocal()
    try:
        if not (args.load or args.align):
            result = align_all(session, registry, report_only=True,
                               allow_empty=args.allow_empty)
        else:
            result = {"dry_run": bool(args.dry_run)}
            target = require_data(session, allow_empty=args.allow_empty)
            result["target"] = target
            if args.load:
                result["load"] = load_shipped_vocabularies(
                    session, registry, dry_run=args.dry_run)
            if args.align:
                result["align"] = align_dimension_values(
                    session, registry, dry_run=args.dry_run)
                result["align_facts"] = align_fact_values(
                    session, registry, dry_run=args.dry_run)
            result["report"] = alignment_report(session, registry)
            log_report(result["report"], target)
    finally:
        session.close()

    errors = int((result.get("load") or {}).get("errors", 0))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NEVER_RENAME",
    "align_all",
    "align_dimension_values",
    "align_fact_values",
    "alignment_report",
    "describe_target",
    "load_shipped_vocabularies",
    "log_report",
    "main",
    "require_data",
    "rewritable_fields",
]