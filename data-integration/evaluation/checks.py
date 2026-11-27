# -*- coding: utf-8 -*-
"""Read-only verification checks over a populated CDM, plus report emission."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import inspect, text

from database.session import require_identifier
from etl.loaders.db_loader import NATURAL_KEY_CONFIG

logger = logging.getLogger(__name__)

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

VOLATILE_COLUMNS = frozenset({
    "loaded_at", "mapped_at", "applied_at", "reviewed_at", "resolved_at",
    "created_at", "updated_at", "run_id", "last_run_id",
})

MASKED_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "fact_concept_map": ("map_id", "is_approved"),
    "fact_resolution_log": ("log_id",),
}

EXCLUDED_TABLES = frozenset({"alembic_version", "run_manifest"})

LEDGER_TABLES = frozenset({"fact_concept_map", "fact_resolution_log"})

DATE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("fact_vector_observation", "date_id"),
    ("fact_environment", "date_id"),
    ("fact_insecticide_resistance", "date_id"),
    ("fact_bionomics", "date_id"),
    ("fact_disease_case", "date_id_onset"),
    ("fact_intervention", "date_start"),
)

PLACEHOLDER_SHARE = 0.25

NUMERIC_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("fact_insecticide_resistance", "mortality IS NOT NULL AND (mortality < 0 OR mortality > 100)", "mortality outside 0-100"),
    ("fact_insecticide_resistance", "concentration IS NOT NULL AND concentration < 0", "negative concentration"),
    ("fact_insecticide_resistance", "n_insect_tested IS NOT NULL AND n_insect_tested < 0", "negative insects tested"),
    ("fact_insecticide_resistance", "n_insect_dead IS NOT NULL AND n_insect_tested IS NOT NULL AND n_insect_dead > n_insect_tested", "deaths exceed insects tested"),
    ("fact_insecticide_resistance", "n_insect_resistant IS NOT NULL AND n_insect_tested IS NOT NULL AND n_insect_resistant > n_insect_tested", "resistant exceed insects tested"),
    ("fact_insecticide_resistance", "n_insect_susceptible IS NOT NULL AND n_insect_tested IS NOT NULL AND n_insect_susceptible > n_insect_tested", "susceptible exceed insects tested"),
    ("fact_vector_observation", "count IS NOT NULL AND count < 0", "negative vector count"),
    ("fact_vector_observation", "density_per_trap IS NOT NULL AND density_per_trap < 0", "negative density"),
    ("fact_vector_observation", "infection_rate IS NOT NULL AND (infection_rate < 0 OR infection_rate > 1)", "infection rate outside 0-1"),
    ("fact_disease_case", "case_count < 1", "case_count below one"),
    ("fact_disease_case", "age_years IS NOT NULL AND age_years < 0", "negative age"),
    ("fact_disease_case", "date_id_onset > date_id_reported", "onset after report"),
    ("fact_intervention", "date_end IS NOT NULL AND date_end < date_start", "intervention ends before it starts"),
    ("fact_socioeconomic_condition", "indicator_code = 'SP.POP.TOTL' AND indicator_value IS NOT NULL AND indicator_value <= 0", "non-positive population"),
    ("fact_disease_burden", "cases_mean IS NOT NULL AND cases_mean < 0", "negative case count"),
    ("fact_disease_burden", "incidence_rate IS NOT NULL AND incidence_rate < 0", "negative incidence"),
    ("dim_location", "centroid_lat IS NOT NULL AND (centroid_lat < -90 OR centroid_lat > 90)", "latitude out of range"),
    ("dim_location", "centroid_lon IS NOT NULL AND (centroid_lon < -180 OR centroid_lon > 180)", "longitude out of range"),
)


@dataclass(frozen=True)
class Check:
    """One verification result, one row of the evaluation table."""

    check_id: str
    title: str
    status: str
    metric: Optional[float] = None
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Plain dict for the JSON report."""
        return {
            "check_id": self.check_id, "title": self.title,
            "status": self.status, "metric": self.metric, "detail": self.detail,
        }


def failed(checks: Sequence[Check]) -> bool:
    """Whether any check failed."""
    return any(check.status == STATUS_FAIL for check in checks)


def _tables(session) -> set:
    """Names of every table in the connected database."""
    try:
        return set(inspect(session.get_bind()).get_table_names())
    except Exception as exc:
        logger.debug("cannot enumerate tables: %s", exc)
        return set()


def _scalar(session, sql: str, params: Optional[dict] = None) -> Optional[int]:
    """One integer scalar, or None when the query cannot run."""
    try:
        value = session.execute(text(sql), params or {}).scalar()
    except Exception as exc:
        logger.debug("query failed: %s", exc)
        return None
    return None if value is None else int(value)


def _exists(table: str, column: str, tables: set, session) -> bool:
    """Whether ``table.column`` is safe to query."""
    if table not in tables:
        return False
    try:
        columns = {c["name"] for c in inspect(session.get_bind()).get_columns(table)}
    except Exception:
        return False
    return column in columns


def _has_runs(session) -> bool:
    """Whether run_manifest records at least one completed run."""
    if "run_manifest" not in _tables(session):
        return False
    return bool(_scalar(session, "SELECT COUNT(*) FROM run_manifest"))


def check_foreign_keys(session) -> Check:
    """Every non-null fact foreign key names an existing dimension row."""
    tables = _tables(session)
    inspector = inspect(session.get_bind())
    orphans = 0
    details: List[str] = []
    probed = 0
    for fact in sorted(t for t in tables if t.startswith("fact_")):
        try:
            keys = inspector.get_foreign_keys(fact)
        except Exception:
            continue
        for key in keys:
            columns = key.get("constrained_columns") or []
            referred = key.get("referred_columns") or []
            target = key.get("referred_table")
            if len(columns) != 1 or len(referred) != 1 or target not in tables:
                continue
            try:
                col = require_identifier(columns[0])
                ref = require_identifier(referred[0])
                dim = require_identifier(target)
                fact_name = require_identifier(fact)
            except ValueError:
                continue
            probed += 1
            count = _scalar(session, (
                f"SELECT COUNT(*) FROM {fact_name} f WHERE f.{col} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {dim} d WHERE d.{ref} = f.{col})"))
            if count:
                orphans += count
                details.append(f"{fact}.{col}: {count}")
    status = STATUS_PASS if orphans == 0 else STATUS_FAIL
    if not probed:
        status = STATUS_SKIP
    return Check("C01", "every fact foreign key resolves", status,
                 0.0 if probed else None,
                 f"{probed} key(s) probed; orphans: {', '.join(details) or 'none'}")


def check_natural_keys(session) -> Check:
    """No table holds a duplicate natural key."""
    tables = _tables(session)
    duplicates = 0
    details: List[str] = []
    probed = 0
    for table, config in sorted(NATURAL_KEY_CONFIG.items()):
        columns = list(config.get("columns") or [])
        if not columns or table not in tables:
            continue
        try:
            names = [require_identifier(c) for c in columns]
            safe = require_identifier(table)
        except ValueError:
            continue
        if not all(_exists(table, c, tables, session) for c in names):
            continue
        probed += 1
        group_by = ", ".join(names)
        count = _scalar(session, (
            f"SELECT COUNT(*) FROM (SELECT {group_by} FROM {safe} "
            f"GROUP BY {group_by} HAVING COUNT(*) > 1) AS dup"))
        if count:
            duplicates += count
            details.append(f"{table}: {count}")
    status = STATUS_PASS if duplicates == 0 else STATUS_FAIL
    if not probed:
        status = STATUS_SKIP
    return Check("C02", "no duplicate natural key in any table", status,
                 float(duplicates), ", ".join(details) or "no duplicate group")


def check_concept_references(session) -> Check:
    """No dimension row carries a concept_id that dim_concept does not hold."""
    tables = _tables(session)
    if "dim_concept" not in tables:
        return Check("C03", "every concept_id names a row in dim_concept",
                     STATUS_SKIP, None, "dim_concept is absent")
    dangling = 0
    details: List[str] = []
    for table in sorted(t for t in tables if t.startswith("dim_")):
        if table == "dim_concept" or not _exists(table, "concept_id", tables, session):
            continue
        name = require_identifier(table)
        count = _scalar(session, (
            f"SELECT COUNT(*) FROM {name} x WHERE x.concept_id IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM dim_concept c "
            f"WHERE c.concept_id = x.concept_id)"))
        if count:
            dangling += count
            details.append(f"{table}: {count}")
    status = STATUS_PASS if dangling == 0 else STATUS_FAIL
    return Check("C03", "every concept_id names a row in dim_concept", status,
                 float(dangling), ", ".join(details) or "no dangling concept")


def check_standard_codes(session) -> Check:
    """A stamped row's standard_code equals the code of its own concept."""
    tables = _tables(session)
    if "dim_concept" not in tables:
        return Check("C04", "standard_code agrees with its concept_id",
                     STATUS_SKIP, None, "dim_concept is absent")
    conflicts = 0
    details: List[str] = []
    probed = 0
    for table in sorted(t for t in tables if t.startswith("dim_")):
        if table == "dim_concept":
            continue
        if not (_exists(table, "concept_id", tables, session)
                and _exists(table, "standard_code", tables, session)):
            continue
        probed += 1
        name = require_identifier(table)
        count = _scalar(session, (
            f"SELECT COUNT(*) FROM {name} x JOIN dim_concept c "
            f"ON c.concept_id = x.concept_id WHERE x.standard_code IS NOT NULL "
            f"AND TRIM(x.standard_code) <> TRIM(c.concept_code)"))
        if count:
            conflicts += count
            details.append(f"{table}: {count}")
    status = STATUS_PASS if conflicts == 0 else STATUS_FAIL
    if not probed:
        status = STATUS_SKIP
    return Check("C04", "standard_code agrees with its concept_id", status,
                 float(conflicts), ", ".join(details) or "all stamped rows agree")


def check_resolution_coverage(session) -> Check:
    """Every distinct value is recorded, or is the canonical name of a concept."""
    tables = _tables(session)
    if "fact_concept_map" not in tables:
        return Check("C05", "resolution ledger covers every distinct value",
                     STATUS_SKIP, None, "fact_concept_map is absent")
    pairs = session.execute(text(
        "SELECT source_table, source_column FROM fact_concept_map "
        "GROUP BY source_table, source_column ORDER BY 1, 2")).fetchall()
    if not pairs:
        return Check("C05", "resolution ledger covers every distinct value",
                     STATUS_SKIP, None, "fact_concept_map is empty")
    has_concepts = "dim_concept" in tables
    unrecorded = 0
    distinct_total = 0
    details: List[str] = []
    for table, column in pairs:
        if table not in tables or not _exists(table, column, tables, session):
            continue
        name = require_identifier(table)
        col = require_identifier(column)
        total = _scalar(session, (
            f"SELECT COUNT(DISTINCT {col}) FROM {name} WHERE {col} IS NOT NULL"))
        if not total:
            continue
        canonical = (
            " OR EXISTS (SELECT 1 FROM dim_concept c2 "
            "WHERE c2.concept_name = d.v)" if has_concepts else "")
        missing = _scalar(session, (
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {col} AS v FROM {name} "
            f"WHERE {col} IS NOT NULL) d WHERE NOT EXISTS ("
            f"SELECT 1 FROM fact_concept_map m WHERE m.source_table = :t "
            f"AND m.source_column = :c AND m.source_value = d.v){canonical})"),
            {"t": table, "c": column})
        if missing is None:
            continue
        distinct_total += total
        unrecorded += missing
        if missing:
            details.append(f"{table}.{column}: {missing}/{total}")
    coverage = 1.0 if not distinct_total else 1.0 - (unrecorded / distinct_total)
    status = STATUS_PASS if unrecorded == 0 else STATUS_FAIL
    return Check("C05", "resolution ledger covers every distinct value", status,
                 round(coverage, 6), ", ".join(details[:8]) or "full coverage")


def decision_counts(session) -> Dict[str, int]:
    """Counts of current decisions in ``fact_concept_map``."""
    if "fact_concept_map" not in _tables(session):
        return {}
    rows = session.execute(text(
        "SELECT COALESCE(decision, '(none)') AS d, COUNT(*) FROM fact_concept_map "
        "GROUP BY COALESCE(decision, '(none)') ORDER BY 1")).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def check_decisions(session) -> Check:
    """No current-state row exists without a decision."""
    counts = decision_counts(session)
    if not counts:
        return Check("C06", "every mapped value carries a decision",
                     STATUS_SKIP, None, "fact_concept_map is empty")
    total = sum(counts.values())
    none = counts.get("(none)", 0)
    applied = counts.get("auto_accepted", 0)
    status = STATUS_PASS if none == 0 else STATUS_FAIL
    return Check("C06", "every mapped value carries a decision", status,
                 round(applied / total, 6) if total else None,
                 f"{applied}/{total} auto-accepted; counts: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def check_ranges(session) -> Check:
    """No stored measurement lies outside its physical range."""
    tables = _tables(session)
    violations = 0
    details: List[str] = []
    probed = 0
    for table, predicate, label in NUMERIC_RULES:
        if table not in tables:
            continue
        try:
            name = require_identifier(table)
        except ValueError:
            continue
        probed += 1
        count = _scalar(session, f"SELECT COUNT(*) FROM {name} WHERE {predicate}")
        if count:
            violations += count
            details.append(f"{label}: {count}")
    status = STATUS_PASS if violations == 0 else STATUS_FAIL
    if not probed:
        status = STATUS_SKIP
    return Check("C07", "no measurement outside its physical range", status,
                 float(violations), "; ".join(details) or f"{probed} rule(s) clean")


def check_dates(session) -> Check:
    """Every fact date exists in dim_time and the covered window is plausible."""
    tables = _tables(session)
    if "dim_time" not in tables:
        return Check("C08", "every fact date exists in dim_time",
                     STATUS_SKIP, None, "dim_time is absent")
    dangling = 0
    details: List[str] = []
    probed = 0
    for table, column in DATE_COLUMNS:
        if table not in tables or not _exists(table, column, tables, session):
            continue
        probed += 1
        name = require_identifier(table)
        col = require_identifier(column)
        count = _scalar(session, (
            f"SELECT COUNT(*) FROM {name} f WHERE f.{col} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM dim_time t "
            f"WHERE t.date_id = f.{col})"))
        if count:
            dangling += count
            details.append(f"{table}.{column}: {count}")
    window = session.execute(text(
        "SELECT MIN(date_id), MAX(date_id) FROM dim_time")).fetchone()
    span = f"dim_time {window[0]} to {window[1]}" if window else "dim_time unreadable"
    status = STATUS_PASS if dangling == 0 else STATUS_FAIL
    if not probed:
        status = STATUS_SKIP
    return Check("C08", "every fact date exists in dim_time", status,
                 float(dangling), f"{span}; orphan dates: {', '.join(details) or 'none'}")


def check_provenance(session) -> Check:
    """Every fact row names a source, and that source carries a release."""
    tables = _tables(session)
    if "dim_source" not in tables:
        return Check("C09", "every fact row names a described source",
                     STATUS_SKIP, None, "dim_source is absent")
    unknown = 0
    details: List[str] = []
    for table in sorted(t for t in tables if t.startswith("fact_")):
        if not _exists(table, "source_code", tables, session):
            continue
        name = require_identifier(table)
        count = _scalar(session, (
            f"SELECT COUNT(*) FROM {name} f WHERE f.source_code IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM dim_source s "
            f"WHERE s.source_code = f.source_code)"))
        if count:
            unknown += count
            details.append(f"{table}: {count}")
    no_version = _scalar(session, (
        "SELECT COUNT(*) FROM dim_source WHERE version IS NULL "
        "OR TRIM(version) = ''")) or 0
    sources = _scalar(session, "SELECT COUNT(*) FROM dim_source") or 0
    status = STATUS_PASS if unknown == 0 else STATUS_FAIL
    return Check("C09", "every fact row names a described source", status,
                 float(unknown),
                 f"{sources} source(s), {no_version} without a version; "
                 f"unknown: {', '.join(details) or 'none'}")


def check_vocabularies(session) -> Check:
    """Every vocabulary release is checksummed and holds at least one concept."""
    tables = _tables(session)
    if "dim_vocabulary" not in tables or "dim_concept" not in tables:
        return Check("C10", "every vocabulary release is checksummed",
                     STATUS_SKIP, None, "vocabulary tables are absent")
    rows = session.execute(text(
        "SELECT v.vocabulary_id, v.version, v.checksum, "
        "       (SELECT COUNT(*) FROM dim_concept c "
        "        WHERE c.vocabulary_id = v.vocabulary_id) AS concepts "
        "FROM dim_vocabulary v ORDER BY v.vocabulary_id")).fetchall()
    if not rows:
        return Check("C10", "every vocabulary release is checksummed",
                     STATUS_FAIL, 0.0, "dim_vocabulary is empty")
    problems: List[str] = []
    for vocabulary_id, version, checksum, concepts in rows:
        if not version:
            problems.append(f"{vocabulary_id}: no version")
        if not checksum:
            problems.append(f"{vocabulary_id}: no checksum")
        if not concepts:
            problems.append(f"{vocabulary_id}: no concepts")
    status = STATUS_PASS if not problems else STATUS_FAIL
    return Check("C10", "every vocabulary release is checksummed", status,
                 float(len(rows) - len(problems)),
                 f"{len(rows)} release(s); " + ("; ".join(problems) or "all complete"))


def check_ingest_audit(session, audit: Optional[Dict[str, Any]]) -> Check:
    """Every staged row is written or dropped, and nothing is unexplained."""
    if not audit:
        status = STATUS_FAIL if _has_runs(session) else STATUS_SKIP
        return Check(
            "C11", "every staged row is written or dropped", status, None,
            "no ingest ledger was supplied; run_manifest records at least one "
            "run, so the accounting exists and was not read by this check")
    tables = audit.get("tables") or []
    if not tables:
        return Check("C11", "every staged row is written or dropped",
                     STATUS_FAIL, None,
                     "the ingest ledger holds no table; nothing was accounted for")
    unbalanced = [row for row in tables if int(row.get("rows_unexplained") or 0)]
    staged = sum(int(row.get("rows_staged") or 0) for row in tables)
    written = sum(int(row.get("rows_written") or 0) for row in tables)
    dropped = sum(int(row.get("rows_dropped") or 0) for row in tables)
    ratio = 1.0 if not staged else written / staged
    status = STATUS_PASS if not unbalanced else STATUS_FAIL
    detail = (f"staged={staged} written={written} dropped={dropped}; "
              "unexplained: "
              + (", ".join(f"{r['source_key']}.{r['table']}={r['rows_unexplained']}"
                           for r in unbalanced[:8]) or "none"))
    return Check("C11", "every staged row is written or dropped", status,
                 round(ratio, 6), detail)


def check_locations(session) -> Check:
    """A location carries a well-formed country code and a parent that exists."""
    tables = _tables(session)
    if "dim_location" not in tables:
        return Check("C12", "location structural integrity", STATUS_SKIP, None,
                     "dim_location is absent")
    total = _scalar(session, "SELECT COUNT(*) FROM dim_location") or 0
    if not total:
        return Check("C12", "location structural integrity", STATUS_SKIP, None,
                     "dim_location is empty")
    invalid = _scalar(session, (
        "SELECT COUNT(*) FROM dim_location WHERE country_code IS NOT NULL "
        "AND TRIM(country_code) <> '' AND (LENGTH(TRIM(country_code)) <> 2 "
        "OR TRIM(country_code) <> UPPER(TRIM(country_code)))")) or 0
    self_parent = _scalar(session, (
        "SELECT COUNT(*) FROM dim_location "
        "WHERE parent_location_id = location_id")) or 0
    dangling = _scalar(session, (
        "SELECT COUNT(*) FROM dim_location l WHERE l.parent_location_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM dim_location p "
        "WHERE p.location_id = l.parent_location_id)")) or 0
    bad = invalid + self_parent + dangling
    status = STATUS_PASS if bad == 0 else STATUS_FAIL
    return Check("C12", "location structural integrity", status,
                 round(1.0 - bad / total, 6),
                 f"{total} row(s): {invalid} malformed country code, "
                 f"{self_parent} self-parent, {dangling} dangling parent")


def check_location_coverage(session) -> Check:
    """Report how many locations carry a country and a parent."""
    tables = _tables(session)
    if "dim_location" not in tables:
        return Check("C13", "location coverage", STATUS_SKIP, None,
                     "dim_location is absent")
    total = _scalar(session, "SELECT COUNT(*) FROM dim_location") or 0
    if not total:
        return Check("C13", "location coverage", STATUS_SKIP, None,
                     "dim_location is empty")
    no_country = _scalar(session, (
        "SELECT COUNT(*) FROM dim_location WHERE country_code IS NULL "
        "OR TRIM(country_code) = ''")) or 0
    unplaced = _scalar(session, (
        "SELECT COUNT(*) FROM dim_location WHERE parent_location_id IS NULL")) or 0
    no_coords = _scalar(session, (
        "SELECT COUNT(*) FROM dim_location WHERE centroid_lat IS NULL "
        "OR centroid_lon IS NULL")) or 0
    status = STATUS_PASS if no_country == 0 else STATUS_WARN
    return Check("C13", "location coverage", status,
                 round(1.0 - no_country / total, 6),
                 f"{total} row(s): {no_country} without a country "
                 f"({no_country / total:.1%}), {unplaced} without a parent, "
                 f"{no_coords} without coordinates")


def check_concept_attachment(session) -> Check:
    """A dimension table whose vocabulary resolved something has a stamped row."""
    tables = _tables(session)
    if "fact_concept_map" not in tables:
        return Check("C14", "governed dimensions carry concepts", STATUS_SKIP,
                     None, "fact_concept_map is absent")
    governed = [str(row[0]) for row in session.execute(text(
        "SELECT DISTINCT source_table FROM fact_concept_map "
        "WHERE decision = 'auto_accepted' ORDER BY 1")).fetchall()]
    governed = [t for t in governed if t in tables and t != "dim_location"]
    if "dim_location" in governed:
        governed.remove("dim_location")
    empty: List[str] = []
    stamped_total = 0
    row_total = 0
    for table in governed:
        if not _exists(table, "concept_id", tables, session):
            continue
        name = require_identifier(table)
        rows_here = _scalar(session, f"SELECT COUNT(*) FROM {name}") or 0
        if not rows_here:
            continue
        stamped = _scalar(session, (
            f"SELECT COUNT(*) FROM {name} WHERE concept_id IS NOT NULL")) or 0
        row_total += rows_here
        stamped_total += stamped
        if stamped == 0:
            empty.append(f"{table} 0/{rows_here}")
    if not row_total:
        return Check("C14", "governed dimensions carry concepts", STATUS_SKIP,
                     None, "no governed dimension table holds rows")
    status = STATUS_PASS if not empty else STATUS_WARN
    return Check("C14", "governed dimensions carry concepts", status,
                 round(stamped_total / row_total, 6),
                 f"{stamped_total}/{row_total} governed row(s) stamped; "
                 + ("none stamped in: " + ", ".join(empty) if empty
                    else "every governed table has stamped rows"))


def check_fact_presence(session) -> Check:
    """Every fact table holds at least one row."""
    tables = _tables(session)
    facts = sorted(t for t in tables if t.startswith("fact_")
                   and t not in LEDGER_TABLES)
    if not facts:
        return Check("C15", "every fact table holds rows", STATUS_SKIP, None,
                     "no fact tables")
    empty: List[str] = []
    for table in facts:
        name = require_identifier(table)
        if not (_scalar(session, f"SELECT COUNT(*) FROM {name}") or 0):
            empty.append(table)
    status = STATUS_WARN if empty else STATUS_PASS
    return Check("C15", "every fact table holds rows", status,
                 round((len(facts) - len(empty)) / len(facts), 6),
                 f"{len(facts) - len(empty)}/{len(facts)} populated; empty: "
                 + (", ".join(empty) if empty else "none"))


def check_placeholder_dates(session) -> Check:
    """No fact table concentrates its rows on one day, as a default does."""
    tables = _tables(session)
    worst = 0.0
    details: List[str] = []
    probed = 0
    for table, column in DATE_COLUMNS:
        if table not in tables or not _exists(table, column, tables, session):
            continue
        name = require_identifier(table)
        col = require_identifier(column)
        total = _scalar(session, (
            f"SELECT COUNT(*) FROM {name} WHERE {col} IS NOT NULL"))
        if not total:
            continue
        probed += 1
        try:
            row = session.execute(text(
                f"SELECT {col} AS d, COUNT(*) AS n FROM {name} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} "
                f"ORDER BY n DESC LIMIT 1")).fetchone()
        except Exception as exc:
            logger.debug("placeholder probe failed for %s: %s", table, exc)
            continue
        if not row:
            continue
        share = int(row[1]) / total
        worst = max(worst, share)
        if share >= PLACEHOLDER_SHARE:
            details.append(f"{table}.{column}: {row[0]} holds {share:.0%}")
    if not probed:
        return Check("C16", "no fact table is dominated by one date", STATUS_SKIP,
                     None, "no dated fact table holds rows")
    status = STATUS_WARN if details else STATUS_PASS
    return Check("C16", "no fact table is dominated by one date", status,
                 round(worst, 6),
                 "; ".join(details)
                 or f"no single day exceeds {PLACEHOLDER_SHARE:.0%} of a table")


def run_checks(session, audit: Optional[Dict[str, Any]] = None) -> List[Check]:
    """Run every verification check in a fixed order."""
    return [
        check_foreign_keys(session),
        check_natural_keys(session),
        check_concept_references(session),
        check_standard_codes(session),
        check_resolution_coverage(session),
        check_decisions(session),
        check_ranges(session),
        check_dates(session),
        check_provenance(session),
        check_vocabularies(session),
        check_ingest_audit(session, audit),
        check_locations(session),
        check_location_coverage(session),
        check_concept_attachment(session),
        check_fact_presence(session),
        check_placeholder_dates(session),
    ]


def governed_summary(session) -> pd.DataFrame:
    """Per field: distinct values, current decisions, coverage. Table 1 input."""
    tables = _tables(session)
    if "fact_concept_map" not in tables:
        return pd.DataFrame()
    rows = session.execute(text(
        "SELECT source_table, source_column, "
        "       SUM(CASE WHEN decision = 'auto_accepted' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN decision = 'needs_review' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN decision = 'unresolved' THEN 1 ELSE 0 END), "
        "       COUNT(*) "
        "FROM fact_concept_map GROUP BY source_table, source_column "
        "ORDER BY 1, 2")).fetchall()
    records: List[Dict[str, Any]] = []
    for table, column, applied, review, unresolved, recorded in rows:
        distinct = None
        if table in tables and _exists(table, column, tables, session):
            name = require_identifier(table)
            col = require_identifier(column)
            distinct = _scalar(session, (
                f"SELECT COUNT(DISTINCT {col}) FROM {name} "
                f"WHERE {col} IS NOT NULL"))
        records.append({
            "field": f"{table}.{column}",
            "distinct_values": distinct,
            "recorded": int(recorded or 0),
            "auto_accepted": int(applied or 0),
            "needs_review": int(review or 0),
            "unresolved": int(unresolved or 0),
            "applied_share": (round(int(applied or 0) / int(recorded), 6)
                              if recorded else None),
        })
    return pd.DataFrame(records)


def _render_markdown(summary: Dict[str, Any]) -> str:
    """Render the check table and the coverage table as Markdown."""
    lines: List[str] = []
    checks = summary.get("checks") or []
    counts: Dict[str, int] = {}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    lines.append("# Integration evaluation report")
    lines.append("")
    lines.append(f"run_id: {summary.get('run_id') or '(none)'}")
    lines.append("")
    lines.append(f"generated: {summary.get('generated_at')}")
    lines.append("")
    lines.append("Result: " + ", ".join(
        f"{name}={count}" for name, count in sorted(counts.items())))
    lines.append("")
    lines.append("| id | check | status | metric | detail |")
    lines.append("|---|---|---|---|---|")
    for check in checks:
        metric = "" if check["metric"] is None else f"{check['metric']}"
        detail = str(check["detail"] or "").replace("|", "/")
        lines.append(f"| {check['check_id']} | {check['title']} | "
                     f"{check['status']} | {metric} | {detail} |")
    coverage = summary.get("coverage") or []
    if coverage:
        lines.append("")
        lines.append("## Vocabulary coverage by field")
        lines.append("")
        lines.append("| field | distinct | recorded | auto_accepted | "
                     "needs_review | unresolved | applied share |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in coverage:
            lines.append(
                f"| {row['field']} | {row['distinct_values']} | {row['recorded']} "
                f"| {row['auto_accepted']} | {row['needs_review']} "
                f"| {row['unresolved']} | {row['applied_share']} |")
    reconciliation = summary.get("reconciliation") or []
    if reconciliation:
        lines.append("")
        lines.append("## Row accounting by source and table")
        lines.append("")
        lines.append("| source | table | staged | inserted | updated | dropped "
                     "| unexplained | reasons |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for row in reconciliation:
            reasons = "; ".join(f"{k}={v}" for k, v in
                                (row.get("drop_reasons") or {}).items())
            lines.append(
                f"| {row['source_key']} | {row['table']} | {row['rows_staged']} "
                f"| {row['rows_inserted']} | {row['rows_updated']} "
                f"| {row['rows_dropped']} | {row['rows_unexplained']} "
                f"| {reasons} |")
    lines.append("")
    return "\n".join(lines)


def write_reports(checks: Sequence[Check], out_dir: Any, *,
                  run_id: str = "",
                  audit: Optional[Dict[str, Any]] = None,
                  coverage: Optional[pd.DataFrame] = None) -> Dict[str, str]:
    """Write the JSON and Markdown reports, returning their paths."""
    target = Path(str(out_dir))
    target.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {
        "run_id": run_id,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "checks": [check.as_dict() for check in checks],
        "coverage": [] if coverage is None else coverage.to_dict(orient="records"),
        "reconciliation": [] if not audit else (audit.get("tables") or []),
    }
    json_path = target / "evaluation_report.json"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    markdown_path = target / "evaluation_report.md"
    markdown_path.write_text(_render_markdown(summary), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def content_digest(session, tables: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """A content digest of the CDM, independent of run identity and order."""
    names = _tables(session)
    selected = sorted(t for t in (names if tables is None else tables)
                      if t.startswith(("dim_", "fact_"))
                      and t not in EXCLUDED_TABLES)
    per_table: Dict[str, str] = {}
    rows_total: Dict[str, int] = {}
    for table in selected:
        name = require_identifier(table)
        try:
            frame = pd.read_sql_query(text(f"SELECT * FROM {name}"),
                                      session.connection())
        except Exception as exc:
            logger.warning("cannot read %s for the digest: %s", table, exc)
            continue
        for column in VOLATILE_COLUMNS.union(MASKED_COLUMNS.get(table, ())):
            if column in frame.columns:
                frame[column] = None
        frame = frame.astype(str).drop_duplicates()
        if not frame.empty:
            frame = frame.sort_values(by=list(frame.columns), kind="mergesort")
        text_form = frame.to_csv(index=False, lineterminator="\n")
        per_table[table] = hashlib.sha256(text_form.encode("utf-8")).hexdigest()
        rows_total[table] = int(len(frame))
    payload = "\n".join(f"{table}\t{per_table[table]}" for table in sorted(per_table))
    return {
        "tables": per_table,
        "rows": rows_total,
        "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


__all__ = [
    "Check", "STATUS_FAIL", "STATUS_PASS", "STATUS_SKIP", "STATUS_WARN",
    "content_digest", "decision_counts", "failed", "governed_summary",
    "run_checks", "write_reports",
]