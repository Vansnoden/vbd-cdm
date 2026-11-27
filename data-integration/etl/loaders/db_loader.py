# -*- coding: utf-8 -*-
"""DBLoader - upsert into the CDM, with COPY for PostgreSQL."""

from __future__ import annotations

import csv as _csv
import datetime
import logging
from dataclasses import dataclass
from io import StringIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database import models
from database.session import DATABASE_URL, SessionLocal, engine
from utilities.constants import DECISION_AUTO_ACCEPTED as _APPLIED_DECISION

logger = logging.getLogger(__name__)

NULL_SENTINEL = "\x00NULL"

NATURAL_KEY_CONFIG: Dict[str, Dict[str, Any]] = {
    "dim_location": {
        "columns": ["location_code"],
        "sentinels": {},
        "in_batch_key": ["name", "country_code", "admin_level"],
    },
    "dim_disease":                 {"columns": ["disease_code"], "sentinels": {}},
    "dim_vector":                  {"columns": ["vector_code"], "sentinels": {}},
    "dim_insecticide":             {"columns": ["insecticide_code"], "sentinels": {}},
    "dim_source":                  {"columns": ["source_code"], "sentinels": {}},
    "dim_identification_method":   {"columns": ["method_code"], "sentinels": {}},
    "dim_resistance_test_type":    {"columns": ["test_type_code"], "sentinels": {}},
    "dim_socioeconomic_indicator": {"columns": ["indicator_code"], "sentinels": {}},
    "dim_time":                    {"columns": ["date_id"], "sentinels": {}},
    "fact_environment": {
        "columns": ["location_id", "date_id", "variable", "source_code"],
        "sentinels": {},
    },
    "fact_disease_burden": {
        "columns": ["location_id", "year", "disease_code", "pathogen_species"],
        "sentinels": {"pathogen_species": ""},
        "in_batch_key": ["location_id", "year", "disease_code"],
    },
    "fact_socioeconomic_condition": {
        "columns": ["location_id", "year", "indicator_code", "source_code"],
        "sentinels": {},
    },
    "fact_health_facility": {"columns": ["facility_code"], "sentinels": {}},
    "fact_vector_observation": {
        "columns": ["location_id", "date_id", "vector_code", "source_code",
                    "life_stage", "sex", "collection_method"],
        "sentinels": {"life_stage": "", "sex": "", "collection_method": ""},
    },
    "fact_insecticide_resistance": {
        "columns": ["location_id", "date_id", "vector_code", "insecticide_code",
                    "test_type_code", "source_code", "concentration"],
        "sentinels": {},
    },
    "fact_bionomics": {
        "columns": ["location_id", "date_id", "vector_code", "source_code"],
        "sentinels": {},
    },
    "fact_intervention": {
        "columns": ["location_id", "date_start", "target_disease_code",
                    "source_code", "intervention_type"],
        "sentinels": {"intervention_type": ""},
    },
    "fact_disease_case": {
        "columns": ["location_id", "date_id_onset", "disease_code",
                    "identification_method_code", "source_code",
                    "case_status", "age_group", "sex"],
        "sentinels": {"case_status": "", "age_group": "", "sex": ""},
    },
}

_CONFLICT_WHERE: Dict[str, Any] = {
    "fact_insecticide_resistance": text("concentration IS NULL"),
}

_SQLITE_BIND_LIMIT = 30000

_COPY_NULL_MARKER = "\\N"


def _null_like(value: Any) -> bool:
    """True when a value is stored as SQL NULL, NaN included."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _constraint_reason(exc: Exception) -> str:
    """A short token naming the constraint an IntegrityError broke."""
    message = str(getattr(exc, "orig", exc)).lower()
    if "foreign key" in message:
        return "foreign_key"
    if "unique" in message:
        return "unique"
    if "not null" in message:
        return "not_null"
    if "check" in message:
        return "check"
    return type(exc).__name__


def _sum_or_null(series: pd.Series) -> Any:
    """Sum a nullable count, staying NULL when the group holds no value."""
    return pd.to_numeric(series, errors="coerce").sum(min_count=1)


def _mean_or_null(series: pd.Series) -> Any:
    """Mean of a nullable measurement, staying NULL when there is none."""
    return pd.to_numeric(series, errors="coerce").mean()


MEASURE_AGGREGATES: Dict[str, Dict[str, Any]] = {
    # Two trap collections at one site on one night are two samples, not a
    # duplicate: their counts add. Empty the dict to keep last-wins instead.
    "fact_vector_observation": {
        "count": _sum_or_null,
        "density_per_trap": _mean_or_null,
        "infection_rate": _mean_or_null,
    },
}


@dataclass(frozen=True)
class _ConflictTarget:
    """One ``ON CONFLICT`` target: its columns, plus a partial-index predicate."""

    columns: Tuple[str, ...]
    index_where: Any = None


class DBLoader:
    """Loads DataFrames into the CDM tables."""

    def __init__(self, batch_size: int = 1000, upsert: bool = True,
                 use_copy: bool = False) -> None:
        self.batch_size = batch_size
        self.upsert = upsert
        self.use_copy = use_copy
        self._table_models = self._build_model_map()
        self._db_type = self._detect_db_type()
        self._drop_hook = None

    def _build_model_map(self) -> Dict[str, Any]:
        """Map table names to SQLAlchemy model classes, by derivation."""
        model_map = {}
        for _name, obj in vars(models).items():
            if hasattr(obj, "__tablename__"):
                model_map[obj.__tablename__] = obj
        return model_map

    def _detect_db_type(self) -> str:
        """Which dialect ``DATABASE_URL`` names."""
        if DATABASE_URL.startswith("postgresql"):
            return "postgresql"
        if DATABASE_URL.startswith("sqlite"):
            return "sqlite"
        if DATABASE_URL.startswith("mysql"):
            return "mysql"
        raise ValueError(f"Unsupported database type: {DATABASE_URL}")

    def _note_drop(self, table_name: str, reason: str, count: int = 1) -> None:
        """Report rows this loader discards, through the optional hook."""
        hook = self._drop_hook
        if hook is not None and count:
            hook(table_name, reason, int(count))

    def drop(self, table_name: str, reason: str, count: int = 1) -> None:
        """Record rows discarded before this loader saw them."""
        self._note_drop(table_name, reason, count)

    def _get_primary_key_columns(self, table_name: str) -> List[str]:
        """The table's primary-key columns, in constraint order."""
        inspector = inspect(engine)
        return inspector.get_pk_constraint(table_name).get(
            "constrained_columns", []) or []

    def _get_pk_list(self, table_name: str) -> List[str]:
        """The primary-key columns, from the inspector."""
        return self._get_primary_key_columns(table_name)

    def _get_column_types(self, table_name: str) -> Dict[str, str]:
        """The table's columns mapped to their SQL type strings."""
        inspector = inspect(engine)
        return {
            col["name"]: str(col["type"])
            for col in inspector.get_columns(table_name)
        }

    def _get_conflict_targets(self, table_name: str, pk_cols: List[str],
                              batch: Optional[Sequence[Dict[str, Any]]] = None
                              ) -> List[_ConflictTarget]:
        """The ``ON CONFLICT`` target(s) a batch needs.

        Without a partial index there is one target: the declared natural key. A
        table whose key includes a nullable measurement column has a partial index
        for the NULL case, so a mixed batch is split into two targets.
        """
        config = NATURAL_KEY_CONFIG.get(table_name)
        columns = tuple(list(config["columns"]) if config and config["columns"]
                        else list(pk_cols))
        partial = _CONFLICT_WHERE.get(table_name)
        if partial is None:
            return [_ConflictTarget(columns)]

        null_columns = tuple(c for c in columns if c != "concentration")
        if batch is None:
            return [_ConflictTarget(columns)]

        has_null = any(_null_like(row.get("concentration")) for row in batch)
        has_value = any(not _null_like(row.get("concentration")) for row in batch)
        targets: List[_ConflictTarget] = []
        if has_value:
            targets.append(_ConflictTarget(columns))
        if has_null:
            targets.append(_ConflictTarget(null_columns, partial))
        return targets or [_ConflictTarget(columns)]

    def _split_by_conflict_target(self, table_name: str, pk_cols: List[str],
                                  batch: Sequence[Dict[str, Any]]
                                  ) -> List[Tuple[_ConflictTarget, List[Dict[str, Any]]]]:
        """Partition a batch so every part is served by exactly one target."""
        targets = self._get_conflict_targets(table_name, pk_cols, batch)
        if len(targets) == 1:
            return [(targets[0], list(batch))]

        null_target = next((t for t in targets if t.index_where is not None), None)
        value_target = next((t for t in targets if t.index_where is None), None)
        groups: List[Tuple[_ConflictTarget, List[Dict[str, Any]]]] = []
        if value_target is not None:
            values = [r for r in batch if not _null_like(r.get("concentration"))]
            if values:
                groups.append((value_target, values))
        if null_target is not None:
            nulls = [r for r in batch if _null_like(r.get("concentration"))]
            if nulls:
                groups.append((null_target, nulls))
        return groups or [(targets[0], list(batch))]

    def _prepare_for_upsert(self, df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """Apply natural-key hygiene to a DataFrame before insert."""
        config = NATURAL_KEY_CONFIG.get(table_name)
        if not config:
            return df

        df = df.copy()

        for col, sentinel in (config.get("sentinels") or {}).items():
            if col in df.columns:
                df[col] = df[col].where(df[col].notna(), sentinel)

        declared = list(config.get("columns") or [])
        fallback = list(config.get("in_batch_key") or [])

        if declared and all(c in df.columns for c in declared):
            dedup_cols = declared
        elif fallback and all(c in df.columns for c in fallback):
            dedup_cols = fallback
            logger.info(
                "  %s: collapsing in-batch duplicates on %s because the conflict "
                "target %s is incomplete; NULL-keyed rows are matched through a "
                "sentinel copy.", table_name, fallback, declared)
        else:
            if any(c in df.columns for c in declared):
                logger.warning(
                    "  %s: conflict target %s is missing from this batch and no "
                    "in-batch fallback applies; duplicates are NOT collapsed.",
                    table_name, declared)
            return df

        return self._collapse(df, table_name, dedup_cols)

    def _collapse(self, df: pd.DataFrame, table_name: str,
                  dedup_cols: List[str]) -> pd.DataFrame:
        """Merge or drop in-batch rows that share one natural key."""
        policy = MEASURE_AGGREGATES.get(table_name)
        before = len(df)

        if policy:
            merged = self._aggregate(df, dedup_cols, policy)
            collapsed = before - len(merged)
            if collapsed:
                logger.info(
                    "  Merged %d row(s) within the batch for %s by %s, summing "
                    "the measurement columns (%s)", collapsed, table_name,
                    dedup_cols, ", ".join(sorted(policy)))
                self._note_drop(table_name, "merged_by_natural_key", collapsed)
            return merged

        try:
            df = df.sort_values(by=dedup_cols, kind="mergesort")
        except (TypeError, ValueError) as exc:
            logger.debug("  %s: cannot sort on %s (%s); keeping source order",
                         table_name, dedup_cols, exc)

        comparables = df[dedup_cols].astype(object)
        comparable = comparables.where(comparables.notna(), NULL_SENTINEL)
        df = df.loc[~comparable.duplicated(keep="last")].reset_index(drop=True)
        collapsed = before - len(df)
        if collapsed:
            logger.info(
                "  Kept one of %d duplicate row(s) within the batch for %s by %s "
                "(NULLs compare equal); the rest are discarded, not merged",
                collapsed, table_name, dedup_cols)
            self._note_drop(table_name, "duplicate_natural_key", collapsed)
        return df

    @staticmethod
    def _aggregate(df: pd.DataFrame, keys: Sequence[str],
                   policy: Dict[str, Any]) -> pd.DataFrame:
        """Collapse duplicate keys, combining the columns the policy names."""
        aggregations: Dict[str, Any] = {}
        for column, combine in policy.items():
            if column in df.columns and column not in keys:
                aggregations[column] = combine
        for column in df.columns:
            if column not in keys and column not in aggregations:
                aggregations[column] = "first"

        try:
            frame = df.sort_values(by=list(keys), kind="mergesort")
        except (TypeError, ValueError):
            frame = df
        return frame.groupby(list(keys), dropna=False, sort=False,
                             as_index=False).agg(aggregations)

    @staticmethod
    def _unique_on_target(rows: List[Dict[str, Any]], columns: Sequence[str]
                          ) -> Tuple[List[Dict[str, Any]], int]:
        """Keep one row per conflict-target tuple, counting the rest."""
        if not columns:
            return rows, 0
        seen: set = set()
        kept: List[Dict[str, Any]] = []
        for row in rows:
            key = tuple(NULL_SENTINEL if _null_like(row.get(column))
                        else row.get(column) for column in columns)
            if key in seen:
                continue
            seen.add(key)
            kept.append(row)
        return kept, len(rows) - len(kept)

    def _copy_upsert_postgresql(self, df: pd.DataFrame, table_name: str,
                                model: Any) -> int:
        """Bulk-load via ``COPY`` into a temp table, then merge."""
        pk_cols = self._get_primary_key_columns(table_name)
        targets = self._get_conflict_targets(table_name, pk_cols)
        conflict_targets = list(targets[0].columns)

        staging_table = f"{table_name}_staging"
        model_columns = [c.name for c in model.__table__.columns]

        df = df[[col for col in df.columns if col in model_columns]].copy()
        for col in model_columns:
            if col not in df.columns:
                df[col] = None

        for pk in pk_cols:
            if pk in df.columns and df[pk].isna().all():
                df = df.drop(columns=[pk])

        for column in df.columns:
            if df[column].dtype == object:
                hits = df[column].map(
                    lambda v: isinstance(v, str) and _COPY_NULL_MARKER in v
                )
                if bool(hits.any()):
                    raise ValueError(
                        f"{table_name}.{column}: {int(hits.sum())} value(s) "
                        f"contain the literal {_COPY_NULL_MARKER!r}, which COPY "
                        f"would read as NULL. Use upsert with use_copy=False for "
                        f"this batch, or clean the source."
                    )

        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False, header=False, na_rep=_COPY_NULL_MARKER,
                  quoting=_csv.QUOTE_MINIMAL)
        csv_buffer.seek(0)
        logger.debug(
            "COPY buffer for %s: %.1f MB, %d rows",
            table_name, len(csv_buffer.getvalue()) / 1e6, len(df),
        )

        update_cols = [
            col for col in df.columns
            if col not in pk_cols
            and col not in conflict_targets
            and df[col].notna().any()
        ]

        with SessionLocal() as session:
            connection = session.connection()
            raw_connection = connection.connection
            cursor = raw_connection.cursor()
            try:
                cursor.execute(f"DROP TABLE IF EXISTS {staging_table}")
                cursor.execute(
                    f"CREATE TEMP TABLE {staging_table} "
                    f"(LIKE {table_name} INCLUDING DEFAULTS) ON COMMIT DROP"
                )
                cursor.copy_expert(
                    f"COPY {staging_table} ({', '.join(df.columns)}) "
                    f"FROM STDIN WITH (FORMAT CSV, NULL '{_COPY_NULL_MARKER}')",
                    csv_buffer,
                )

                column_list = ", ".join(df.columns)
                if update_cols:
                    update_set = ", ".join(
                        f"{col} = EXCLUDED.{col}" for col in update_cols
                    )
                    upsert_sql = (
                        f"INSERT INTO {table_name} ({column_list}) "
                        f"SELECT {column_list} FROM {staging_table} "
                        f"ON CONFLICT ({', '.join(conflict_targets)}) "
                        f"DO UPDATE SET {update_set}"
                    )
                else:
                    upsert_sql = (
                        f"INSERT INTO {table_name} ({column_list}) "
                        f"SELECT {column_list} FROM {staging_table} "
                        f"ON CONFLICT ({', '.join(conflict_targets)}) DO NOTHING"
                    )
                cursor.execute(upsert_sql)
                raw_connection.commit()
            except Exception:
                raw_connection.rollback()
                raise
            finally:
                try:
                    cursor.execute(f"DROP TABLE IF EXISTS {staging_table}")
                    raw_connection.commit()
                except Exception:  # noqa: BLE001 - the temp table is already gone
                    pass
                cursor.close()

        logger.info(
            "Upserted %d rows into %s via COPY and staging", len(df), table_name
        )
        return len(df)

    def _select_columns(self, df: pd.DataFrame, pk_cols: List[str]
                        ) -> List[str]:
        """Columns to send, dropping an autoincrement PK that is entirely NULL."""
        columns = list(df.columns)
        for pk in pk_cols:
            if pk in columns and df[pk].isna().all():
                columns.remove(pk)
        return columns

    def _payload(self, df: pd.DataFrame, columns: List[str]) -> List[Dict[str, Any]]:
        """Row dicts with NaN mapped to None, so NULL means NULL at the cursor."""
        return [
            {col: (None if _null_like(record.get(col)) else record.get(col))
             for col in columns}
            for record in df.to_dict(orient="records")
        ]

    def _effective_batch(self, columns: Sequence[str]) -> int:
        """Rows per statement, capped so ``rows x columns`` stays inside the cap."""
        width = max(1, len(columns))
        return max(1, min(int(self.batch_size), _SQLITE_BIND_LIMIT // width))

    def _conflict_statement(self, dialect_insert: Any, model: Any,
                            columns: Sequence[str], pk_cols: Sequence[str],
                            target: _ConflictTarget,
                            rows: Sequence[Dict[str, Any]]) -> Any:
        """The ON CONFLICT statement for one target and one batch of rows."""
        statement = dialect_insert(model)
        if not target.columns:
            return statement

        active = [
            col for col in columns
            if col not in pk_cols
            and col not in target.columns
            and any(row.get(col) is not None for row in rows)
        ]
        if active:
            return statement.on_conflict_do_update(
                index_elements=list(target.columns),
                index_where=target.index_where,
                set_={col: statement.excluded[col] for col in active},
            )
        return statement.on_conflict_do_nothing(
            index_elements=list(target.columns),
            index_where=target.index_where,
        )

    def _insert_rows(self, statement: Any, rows: Sequence[Dict[str, Any]]) -> int:
        """Execute one ON CONFLICT statement, returning the rows written."""
        with SessionLocal() as session:
            try:
                result = session.execute(statement, list(rows))
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                raise
        rowcount = getattr(result, "rowcount", None)
        return len(rows) if rowcount is None or rowcount < 0 else int(rowcount)

    def _bisect_insert(self, attempt: Any, rows: List[Dict[str, Any]],
                       table_name: str) -> Tuple[int, int]:
        """Insert rows, halving the batch whenever a constraint rejects it."""
        try:
            return int(attempt(rows)), 0
        except IntegrityError as exc:
            reason = _constraint_reason(exc)
            if len(rows) <= 1:
                logger.warning(
                    "  %s: one row rejected (%s): %s", table_name, reason,
                    str(getattr(exc, "orig", exc))[:300])
                self._note_drop(table_name, f"rejected:{reason}", 1)
                return 0, 1
            middle = len(rows) // 2
            written_left, rejected_left = self._bisect_insert(
                attempt, rows[:middle], table_name)
            written_right, rejected_right = self._bisect_insert(
                attempt, rows[middle:], table_name)
            return written_left + written_right, rejected_left + rejected_right

    def _upsert(self, dialect_insert: Any, df: pd.DataFrame, table_name: str,
                model: Any) -> int:
        """Upsert a frame one conflict target at a time, recovering row by row."""
        pk_cols = self._get_pk_list(table_name)
        columns = self._select_columns(df, pk_cols)
        records = self._payload(df, columns)

        total_written = 0
        for target, batch in self._split_by_conflict_target(
                table_name, pk_cols, records):
            batch, collapsed = self._unique_on_target(batch, target.columns)
            if collapsed:
                logger.warning(
                    "  %s: %d row(s) shared the conflict target %s and were "
                    "collapsed in the batch; the natural-key pass cannot see this "
                    "when the key itself contains a nullable column.",
                    table_name, collapsed, list(target.columns))
                self._note_drop(table_name, "duplicate_target_key", collapsed)

            chunk = self._effective_batch(columns)
            for start in range(0, len(batch), chunk):
                rows = batch[start:start + chunk]
                if not rows:
                    continue

                def attempt(subset: List[Dict[str, Any]],
                            _target: _ConflictTarget = target) -> int:
                    statement = self._conflict_statement(
                        dialect_insert, model, columns, pk_cols, _target, subset)
                    return self._insert_rows(statement, subset)

                written, rejected = self._bisect_insert(attempt, rows, table_name)
                total_written += written
                if rejected:
                    logger.error(
                        "  %s: %d row(s) were rejected by a constraint, recorded "
                        "in the audit ledger and NOT written.", table_name, rejected)

        return total_written

    def _upsert_postgresql(self, df: pd.DataFrame, table_name: str,
                           model: Any) -> int:
        """Batched ``ON CONFLICT DO UPDATE`` for PostgreSQL."""
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
        return self._upsert(dialect_insert, df, table_name, model)

    def _upsert_sqlite(self, df: pd.DataFrame, table_name: str,
                       model: Any) -> int:
        """Batched ``ON CONFLICT DO UPDATE`` for SQLite."""
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
        return self._upsert(dialect_insert, df, table_name, model)

    def _simple_insert(self, df: pd.DataFrame, table_name: str) -> int:
        """Plain append. Bypasses every sentinel and dedup rule above."""
        written = 0
        for start in range(0, len(df), self.batch_size):
            chunk = df.iloc[start:start + self.batch_size]
            chunk.to_sql(
                table_name, engine, if_exists="append", index=False, method="multi"
            )
            written += len(chunk)
        return written

    def load_table(self, table_name: str, df: pd.DataFrame) -> int:
        """Load a DataFrame into ``table_name``. Returns rows actually written."""
        if df.empty:
            logger.info("No data to load for table %s", table_name)
            return 0
        if table_name not in self._table_models:
            raise ValueError(
                f"Unknown table: {table_name}. "
                f"Available: {sorted(self._table_models)}"
            )

        model = self._table_models[table_name]
        input_rows = len(df)

        inspector = inspect(engine)
        model_columns = [c.name for c in model.__table__.columns]
        model_col_map = {c.name: c for c in model.__table__.columns}

        pk_cols = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns", [])
            or []
        )
        autoincrement_pks = {
            name for name in pk_cols
            if name in model_col_map
            and model_col_map[name].autoincrement is True
        }

        df = df[[col for col in df.columns if col in model_columns]].copy()
        for col in model_columns:
            if col not in df.columns:
                df[col] = None

        for col_meta in inspector.get_columns(table_name):
            if col_meta.get("nullable", True):
                continue
            col = col_meta["name"]
            if col in autoincrement_pks or col not in df.columns:
                continue

            model_col = model_col_map.get(col)
            py_default = getattr(model_col, "default", None)
            default_arg = getattr(py_default, "arg", None)

            if default_arg is not None and not callable(default_arg):
                nan_mask = df[col].isna()
                n_filled = int(nan_mask.sum())
                if n_filled:
                    df[col] = df[col].where(~nan_mask, default_arg)
                    logger.info(
                        "  %s.%s: filled %d NaN value(s) with the model default %r",
                        table_name, col, n_filled, default_arg,
                    )

        for col_meta in inspector.get_columns(table_name):
            if col_meta.get("nullable", True):
                continue
            col = col_meta["name"]
            if col in autoincrement_pks or col not in df.columns:
                continue

            remaining = int(df[col].isna().sum())
            if remaining == 0:
                continue

            if col_meta.get("default") is not None:
                logger.error(
                    "  %s.%s: %d row(s) still NaN after default fill, but the "
                    "column has only a server default. Add a Python-side "
                    "`default=` to the model, or fix the mapping to supply the "
                    "column. The INSERT will fail on NOT NULL.",
                    table_name, col, remaining,
                )
                continue

            before = len(df)
            df = df[df[col].notna()].copy()
            logger.warning(
                "  %s.%s: dropped %d row(s) with NaN (column is NOT NULL and has "
                "no default)", table_name, col, before - len(df),
            )
            self._note_drop(table_name, f"not_null:{col}", before - len(df))

        for col in df.select_dtypes(include=["datetime64[ns]"]).columns:
            df[col] = df[col].dt.date

        df = self._prepare_for_upsert(df, table_name)
        if df.empty:
            logger.info(
                "No rows to load for %s after natural-key preparation "
                "(input had %d rows)", table_name, input_rows,
            )
            return 0

        try:
            if self.upsert:
                if self._db_type == "postgresql":
                    partial = table_name in _CONFLICT_WHERE
                    if (self.use_copy and not partial
                            and len(df) > self.batch_size * 2):
                        written = self._copy_upsert_postgresql(df, table_name, model)
                    else:
                        written = self._upsert_postgresql(df, table_name, model)
                elif self._db_type == "sqlite":
                    written = self._upsert_sqlite(df, table_name, model)
                else:
                    written = self._simple_insert(df, table_name)
            else:
                written = self._simple_insert(df, table_name)
        except SQLAlchemyError as exc:
            logger.error("Failed to load table %s: %s", table_name, exc)
            raise

        logger.info(
            "Loaded %d rows into %s (input had %d)", written, table_name, input_rows
        )
        return written

    def load_all(self, table_dfs: Dict[str, pd.DataFrame]) -> Dict[str, int]:
        """Load multiple tables. Returns ``{table: rows written}``."""
        return {
            table_name: self.load_table(table_name, df)
            for table_name, df in table_dfs.items()
        }

    def load_resolutions(self, resolutions: "List[Any]") -> int:
        """Persist a run's resolutions: append the ledger, project the current state."""
        if not resolutions:
            logger.info("No resolutions to load")
            return 0

        if self._db_type not in ("postgresql", "sqlite"):
            raise ValueError(
                f"load_resolutions needs ON CONFLICT support; {self._db_type!r} "
                f"does not have it. Supported: postgresql, sqlite."
            )

        from etl.transformers.concept_resolver import DECISIONS

        resolved_at = datetime.datetime.utcnow()

        ledger_rows: Dict[tuple, Dict[str, Any]] = {}
        projection_rows: Dict[tuple, Dict[str, Any]] = {}
        incomplete = bad_decision = missing_run = withheld = inconsistent = 0

        for resolution in resolutions:
            source_table = str(getattr(resolution, "source_table", "") or "").strip()
            source_column = str(getattr(resolution, "source_column", "") or "").strip()
            run_id = str(getattr(resolution, "run_id", "") or "").strip()
            decision = str(getattr(resolution, "decision", "") or "").strip()

            if not source_table or not source_column:
                incomplete += 1
                continue
            if not run_id:
                missing_run += 1
                continue
            if decision not in DECISIONS:
                bad_decision += 1
                continue

            raw_value = getattr(resolution, "source_value", None)
            source_value = "" if raw_value is None else str(raw_value)

            match_score = getattr(resolution, "match_score", None)
            candidates = getattr(resolution, "candidates", None)
            concept_code = getattr(resolution, "concept_code", None)
            vocabulary_id = getattr(resolution, "vocabulary_id", None)
            vocabulary_version = getattr(resolution, "vocabulary_version", None)
            resolver_version = getattr(resolution, "resolver_version", None)

            ledger_rows[(source_table, source_column, source_value, run_id)] = {
                "source_table": source_table,
                "source_column": source_column,
                "source_value": source_value,
                "vocabulary_id": vocabulary_id,
                "vocabulary_version": vocabulary_version,
                "concept_code": concept_code,
                "match_method": getattr(resolution, "match_method", None),
                "match_score": None if match_score is None else float(match_score),
                "decision": decision,
                "candidates": candidates,
                "resolver_version": resolver_version,
                "run_id": run_id,
                "resolved_at": resolved_at,
            }

            if decision != _APPLIED_DECISION:
                withheld += 1
                continue
            if concept_code is None:
                inconsistent += 1
                continue

            projection_rows[(source_table, source_column, source_value)] = {
                "source_table": source_table,
                "source_column": source_column,
                "source_value": source_value,
                "vocabulary_id": vocabulary_id,
                "concept_code": concept_code,
                "match_method": getattr(resolution, "match_method", None),
                "match_score": None if match_score is None else float(match_score),
                "decision": decision,
                "vocabulary_version": vocabulary_version,
                "resolver_version": resolver_version,
                "last_run_id": run_id,
                "mapped_at": resolved_at,
            }

        if incomplete or bad_decision or missing_run:
            logger.warning(
                "load_resolutions: skipped %d resolution(s) with no "
                "source_table/source_column, %d with an unrecognised decision, "
                "and %d with no run_id (run_id is NOT NULL and part of the "
                "ledger's unique key).",
                incomplete, bad_decision, missing_run,
            )
        if inconsistent:
            logger.error(
                "load_resolutions: %d resolution(s) are %r but carry no "
                "concept_code. The contract says that combination does not "
                "exist, so they are kept in the ledger only. Find the writer "
                "that produced them.",
                inconsistent, _APPLIED_DECISION,
            )

        if not ledger_rows:
            logger.warning("No usable resolutions to load")
            return 0

        insert_ledger = text(
            """
            INSERT INTO fact_resolution_log
                (source_table, source_column, source_value,
                 vocabulary_id, vocabulary_version, concept_code,
                 match_method, match_score, decision, candidates,
                 resolver_version, run_id, resolved_at)
            VALUES
                (:source_table, :source_column, :source_value,
                 :vocabulary_id, :vocabulary_version, :concept_code,
                 :match_method, :match_score, :decision, :candidates,
                 :resolver_version, :run_id, :resolved_at)
            ON CONFLICT (source_table, source_column, source_value, run_id)
            DO NOTHING
            """
        )

        upsert_projection = text(
            """
            INSERT INTO fact_concept_map
                (source_table, source_column, source_value,
                 vocabulary_id, concept_code, match_method, match_score,
                 decision, vocabulary_version, resolver_version,
                 last_run_id, mapped_at)
            VALUES
                (:source_table, :source_column, :source_value,
                 :vocabulary_id, :concept_code, :match_method, :match_score,
                 :decision, :vocabulary_version, :resolver_version,
                 :last_run_id, :mapped_at)
            ON CONFLICT (source_table, source_column, source_value)
            DO UPDATE SET
                vocabulary_id      = EXCLUDED.vocabulary_id,
                concept_code       = EXCLUDED.concept_code,
                match_method       = EXCLUDED.match_method,
                match_score        = EXCLUDED.match_score,
                decision           = EXCLUDED.decision,
                vocabulary_version = EXCLUDED.vocabulary_version,
                resolver_version   = EXCLUDED.resolver_version,
                last_run_id        = EXCLUDED.last_run_id
            WHERE NOT (
                fact_concept_map.is_approved = 1
                AND fact_concept_map.concept_code IS NOT NULL
                AND EXCLUDED.concept_code IS NOT NULL
                AND fact_concept_map.concept_code <> EXCLUDED.concept_code
            )
            """
        )

        ledger_batch = [ledger_rows[key] for key in sorted(ledger_rows)]
        projection_batch = [projection_rows[key] for key in sorted(projection_rows)]

        submitted = written = 0
        try:
            for start in range(0, len(ledger_batch), self.batch_size):
                batch = ledger_batch[start:start + self.batch_size]
                with SessionLocal() as session:
                    result = session.execute(insert_ledger, batch)
                    session.commit()
                submitted += len(batch)
                rowcount = getattr(result, "rowcount", None)
                written += len(batch) if rowcount is None or rowcount < 0 else rowcount

            logger.info(
                "Appended %d ledger row(s) (%d submitted) across %d run id(s); "
                "%d withheld decision(s) recorded in the ledger only",
                written, submitted,
                len({row["run_id"] for row in ledger_batch}), withheld,
            )
        except SQLAlchemyError as exc:
            logger.error("Failed to append the resolution ledger: %s", exc)
            raise

        if not projection_batch:
            logger.info(
                "No resolution applied a concept; fact_concept_map is unchanged "
                "(every decision is in fact_resolution_log)"
            )
            return submitted

        refused = self._count_protected(projection_batch)
        if refused:
            logger.warning(
                "%d row(s) in fact_concept_map carry a human approval "
                "(is_approved = TRUE) and a different concept_code than this run "
                "produced. Their concept was NOT overwritten: a re-run must not "
                "silently re-decide a row a person approved. Review the ledger "
                "for the new evidence and clear the approval deliberately if the "
                "new decision is correct.",
                refused,
            )

        projected = 0
        try:
            for start in range(0, len(projection_batch), self.batch_size):
                batch = projection_batch[start:start + self.batch_size]
                with SessionLocal() as session:
                    session.execute(upsert_projection, batch)
                    session.commit()
                projected += len(batch)
        except SQLAlchemyError as exc:
            logger.error(
                "Appended the resolution ledger but failed to update "
                "fact_concept_map: %s. The ledger is authoritative; re-running "
                "this method is safe, because the ledger write is idempotent.",
                exc,
            )
            raise

        logger.info(
            "Projected %d current-state row(s) into fact_concept_map", projected
        )
        return submitted

    def _count_protected(self, projection_batch: List[Dict[str, Any]]) -> int:
        """How many projections the review guard will refuse to apply."""
        protected = 0
        keys = [
            (row["source_table"], row["source_column"], row["source_value"])
            for row in projection_batch
        ]
        with SessionLocal() as session:
            for start in range(0, len(keys), self.batch_size):
                chunk = keys[start:start + self.batch_size]
                placeholders = ", ".join(
                    f"(:t{i}, :c{i}, :v{i})" for i in range(len(chunk))
                )
                params: Dict[str, Any] = {}
                for i, (table, column, value) in enumerate(chunk):
                    params[f"t{i}"] = table
                    params[f"c{i}"] = column
                    params[f"v{i}"] = value
                try:
                    protected += int(
                        session.execute(
                            text(
                                "SELECT COUNT(*) FROM fact_concept_map "
                                f"WHERE (source_table, source_column, source_value) "
                                f"IN ({placeholders}) "
                                "AND is_approved = 1 AND concept_code IS NOT NULL"
                            ),
                            params,
                        ).scalar()
                        or 0
                    )
                except Exception as exc:  # noqa: BLE001 - a diagnostic only
                    logger.debug("could not count protected rows: %s", exc)
                    return 0
        return protected


__all__ = ["DBLoader", "MEASURE_AGGREGATES", "NATURAL_KEY_CONFIG", "NULL_SENTINEL"]