# -*- coding: utf-8 -*-
"""Read-only database introspection: schema summary, samples, metadata.

Every table name goes through ``require_identifier`` before it is interpolated
into SQL. Before this revision the module built statements with f-strings and a
bare ``except:`` per table, so a quoting error was reported as "row_count_error"
rather than raised, and the size probes silently returned nothing.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from database.session import DATABASE_URL, engine, require_identifier

logger = logging.getLogger(__name__)


def _qualified(table: str, schema_name: Optional[str]) -> str:
    """A safe, quoted ``schema.table`` (or ``table``) for interpolation."""
    name = require_identifier(table, what="table name")
    if not schema_name:
        return name
    return f"{require_identifier(schema_name, what='schema name')}.{name}"


def _table_size(conn, table: str, schema_name: Optional[str]) -> Optional[str]:
    """Human-readable on-disk size, for the dialects that expose one."""
    if DATABASE_URL.startswith("postgresql"):
        try:
            return conn.execute(
                text("SELECT pg_size_pretty(pg_total_relation_size(:t))"),
                {"t": _qualified(table, schema_name)},
            ).scalar()
        except SQLAlchemyError as exc:
            logger.debug("no size for %s: %s", table, exc)
            return None
    if DATABASE_URL.startswith("mysql"):
        try:
            return conn.execute(
                text(
                    "SELECT ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                ),
                {"t": table},
            ).scalar()
        except SQLAlchemyError as exc:
            logger.debug("no size for %s: %s", table, exc)
            return None
    return None


def schema_summary_to_csv(
    output_csv: Optional[str] = "outputs/data/schema_summary.csv",
    schema_name: Optional[str] = None,
) -> pd.DataFrame:
    """One row per table: columns, indexes, foreign keys, rows, size."""
    inspector = inspect(engine)
    tables = inspector.get_table_names(schema=schema_name)
    rows: List[Dict[str, Any]] = []

    with engine.connect() as conn:
        for table in tables:
            entry: Dict[str, Any] = {
                "table_name": table,
                "schema": schema_name or "public",
                "column_count": len(inspector.get_columns(table, schema=schema_name)),
                "index_count": len(inspector.get_indexes(table, schema=schema_name)),
                "foreign_key_count": len(
                    inspector.get_foreign_keys(table, schema=schema_name)),
            }
            try:
                entry["row_count"] = int(
                    conn.execute(text(f"SELECT COUNT(*) FROM {_qualified(table, schema_name)}")
                                 ).scalar() or 0)
            except SQLAlchemyError as exc:
                entry["row_count"] = None
                entry["row_count_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("cannot count %s: %s", table, exc)
            entry["total_size"] = _table_size(conn, table, schema_name)
            rows.append(entry)

    frame = pd.DataFrame(rows)
    if output_csv:
        os.makedirs(os.path.dirname(output_csv) or ".", exist_ok=True)
        frame.to_csv(output_csv, index=False)
        logger.info("schema summary: %d table(s) -> %s", len(frame), output_csv)
    return frame


def detailed_table_info(table_name: str, schema_name: Optional[str] = None,
                        output_csv: Optional[str] = None) -> pd.DataFrame:
    """Column-level metadata for one table."""
    frame = pd.DataFrame(
        inspect(engine).get_columns(table_name, schema=schema_name))
    if "type" in frame.columns:
        frame["data_type"] = frame["type"].astype(str)
        frame = frame.drop(columns=["type"])
    if output_csv:
        os.makedirs(os.path.dirname(output_csv) or ".", exist_ok=True)
        frame.to_csv(output_csv, index=False)
        logger.info("table info for %r -> %s", table_name, output_csv)
    return frame


def compare_schemas(output_csv: str = "schema_comparison.csv") -> pd.DataFrame:
    """Diff the current schema against the last saved baseline."""
    current = schema_summary_to_csv(output_csv=None)
    baseline_path = "schema_summary_previous.csv"
    if not os.path.exists(baseline_path):
        current.to_csv(baseline_path, index=False)
        logger.info("no previous schema; saved the current one as the baseline")
        return current

    previous = pd.read_csv(baseline_path)
    comparison = current.merge(previous, on="table_name",
                              suffixes=("_current", "_previous"))
    comparison["row_count_change"] = (comparison["row_count_current"]
                                      - comparison["row_count_previous"])
    comparison["has_changed"] = (
        (comparison["row_count_change"] != 0)
        | (comparison["column_count_current"] != comparison["column_count_previous"])
    )
    os.makedirs(os.path.dirname(output_csv) or ".", exist_ok=True)
    comparison.to_csv(output_csv, index=False)
    logger.info("schema comparison -> %s", output_csv)
    return comparison


def export_all_table_samples(output_dir: str = "table_samples",
                             rows_per_table: int = 100) -> Dict[str, int]:
    """Write ``<table>_sample.csv`` for every table. Returns rows per table."""
    os.makedirs(output_dir, exist_ok=True)
    written: Dict[str, int] = {}
    with engine.connect() as conn:
        for table in inspect(engine).get_table_names():
            try:
                frame = pd.read_sql(
                    text(f"SELECT * FROM {_qualified(table, None)} "
                         f"LIMIT {int(rows_per_table)}"), conn)
            except (SQLAlchemyError, ValueError) as exc:
                logger.warning("cannot sample %s: %s", table, exc)
                continue
            frame.to_csv(os.path.join(output_dir, f"{table}_sample.csv"),
                         index=False)
            written[table] = len(frame)
    logger.info("samples for %d table(s) -> %s", len(written), output_dir)
    return written


def get_database_metadata() -> pd.DataFrame:
    """Primary keys, foreign keys and unique constraints per table."""
    inspector = inspect(engine)
    rows: List[Dict[str, Any]] = []
    for table in inspector.get_table_names():
        indexes = inspector.get_indexes(table)
        rows.append({
            "table_name": table,
            "primary_key": inspector.get_pk_constraint(table).get(
                "constrained_columns", []),
            "foreign_keys": [
                (fk["constrained_columns"], fk["referred_table"],
                 fk["referred_columns"])
                for fk in inspector.get_foreign_keys(table)],
            "indexes": [index["column_names"] for index in indexes],
            "unique_constraints": [index["column_names"] for index in indexes
                                   if index.get("unique", False)],
        })
    return pd.DataFrame(rows)