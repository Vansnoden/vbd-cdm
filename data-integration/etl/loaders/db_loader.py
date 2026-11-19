# -*- coding: utf-8 -*-
"""
DBLoader - Loads DataFrames into the CDM with upsert support and, for
PostgreSQL, COPY through a staging table.
"""

from __future__ import annotations

import csv as _csv
import datetime
import logging
from io import StringIO
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from database import models
from database.session import DATABASE_URL, SessionLocal, engine

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Natural-key configuration
#
# Each entry maps a table name to:
#   'columns'   — the conflict target. MUST match a real unique constraint.
#   'sentinels' — values substituted for NULLs in nullable natural-key columns
#                 before insert. PostgreSQL and SQLite treat NULL as distinct in
#                 a unique index, so without a sentinel the constraint does not
#                 bite for those rows.
#   'in_batch_key' — optional extra key used ONLY to collapse duplicates within
#                 one batch, for tables whose real conflict target can be NULL.
#                 It does not change the ON CONFLICT clause.
#
# `dim_location` and `fact_disease_burden.pathogen_species` are the two places
# where a nullable column sits inside a unique constraint. Neither can take a
# sentinel without inventing data, so both get an in-batch key instead and the
# limitation is logged. The real fix is a schema change (a NOT NULL code, or
# PostgreSQL 15's NULLS NOT DISTINCT), and is out of this file's scope.
# -----------------------------------------------------------------------------
NATURAL_KEY_CONFIG: Dict[str, Dict[str, Any]] = {
    # ---------- Dimensions ----------
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

    # ---------- Facts ----------
    "fact_environment": {
        "columns": ["location_id", "date_id", "variable"],
        "sentinels": {},
    },
    "fact_disease_burden": {
        "columns": ["location_id", "year", "disease_code", "pathogen_species"],
        "sentinels": {},
        "in_batch_key": ["location_id", "year", "disease_code"],
    },
    "fact_socioeconomic_condition": {
        "columns": ["location_id", "year", "indicator_code"],
        "sentinels": {},
    },
    "fact_health_facility": {
        "columns": ["facility_code"],
        "sentinels": {},
    },
    "fact_vector_observation": {
        "columns": ["location_id", "date_id", "vector_code", "source_code",
                    "life_stage", "sex", "collection_method"],
        "sentinels": {"life_stage": "", "sex": "", "collection_method": ""},
    },
    "fact_insecticide_resistance": {
        "columns": ["location_id", "date_id", "vector_code", "insecticide_code",
                    "test_type_code", "source_code", "concentration"],
        "sentinels": {"concentration": -1.0},
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

#: The only decision that authorises applying a concept to a dimension row.
_APPLIED_DECISION = "auto_accepted"

#: The COPY NULL marker. PostgreSQL's ``COPY ... NULL '\N'`` reads an *unquoted*
#: ``\N`` as NULL, which means a genuine two-character ``\N`` in a cell becomes
#: NULL. Pandas only quotes when it must, so the load path checks for the literal
#: before writing and refuses rather than silently nulling a value.
_COPY_NULL_MARKER = "\\N"


class DBLoader:
    """Loads DataFrames into the CDM tables.

    Parameters
    ----------
    batch_size : int
        Rows per statement for the non-COPY paths.
    upsert : bool
        When True (the default), use ``ON CONFLICT``. When False, plain append —
        which will fail on a duplicate and is only useful for a first load into
        an empty table.
    use_copy : bool
        (PostgreSQL only) Use ``COPY`` into a staging table when the batch is
        larger than ``2 * batch_size``. Ignored on other backends.
    """

    def __init__(self, batch_size: int = 1000, upsert: bool = True,
                 use_copy: bool = False) -> None:
        self.batch_size = batch_size
        self.upsert = upsert
        self.use_copy = use_copy
        self._table_models = self._build_model_map()
        self._db_type = self._detect_db_type()

    # -------------------------------------------------------------------------
    # Construction helpers
    # -------------------------------------------------------------------------

    def _build_model_map(self) -> Dict[str, Any]:
        """Map table names to SQLAlchemy model classes, by derivation."""
        model_map = {}
        for _name, obj in vars(models).items():
            if hasattr(obj, "__tablename__"):
                model_map[obj.__tablename__] = obj
        return model_map

    def _detect_db_type(self) -> str:
        if DATABASE_URL.startswith("postgresql"):
            return "postgresql"
        if DATABASE_URL.startswith("sqlite"):
            return "sqlite"
        if DATABASE_URL.startswith("mysql"):
            return "mysql"
        raise ValueError(f"Unsupported database type: {DATABASE_URL}")

    def _get_primary_key_columns(self, table_name: str) -> List[str]:
        inspector = inspect(engine)
        return inspector.get_pk_constraint(table_name).get("constrained_columns", []) or []

    def _get_column_types(self, table_name: str) -> Dict[str, str]:
        inspector = inspect(engine)
        return {
            col["name"]: str(col["type"])
            for col in inspector.get_columns(table_name)
        }

    def _get_conflict_targets(self, table_name: str, pk_cols: List[str]) -> List[str]:
        """The natural-key columns that form the ``ON CONFLICT`` target.

        Falls back to the primary key only when the table has no declared
        natural key, which is what ``fact_concept_map`` and ``fact_resolution_log``
        rely on — both are keyed by a surrogate and written by dedicated SQL
        rather than through this path.
        """
        config = NATURAL_KEY_CONFIG.get(table_name)
        if config and config["columns"]:
            return list(config["columns"])
        return pk_cols

    # -------------------------------------------------------------------------
    # Natural-key hygiene
    # -------------------------------------------------------------------------

    def _prepare_for_upsert(self, df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """Apply natural-key hygiene to a DataFrame before insert.

        1. Substitute sentinels for NULLs in nullable natural-key columns, so the
           unique constraint catches duplicates reliably.
        2. Collapse duplicate rows within the batch. PostgreSQL rejects a
           multi-row ``ON CONFLICT`` that touches the same target row twice.

        Step 2 uses the declared conflict target when every one of its columns is
        present and non-null; otherwise it falls back to ``in_batch_key``. A row
        that has a NULL in the conflict target cannot participate in the
        constraint at all — SQL treats each NULL as distinct — so without the
        fallback, re-ingesting a source that omits ``location_code`` multiplies
        ``dim_location`` on every run and leaves the consolidation phase to merge
        the copies back.
        """
        config = NATURAL_KEY_CONFIG.get(table_name)
        if not config:
            return df

        df = df.copy()

        for col, sentinel in (config.get("sentinels") or {}).items():
            if col in df.columns:
                df[col] = df[col].where(df[col].notna(), sentinel)

        def _usable(columns: List[str]) -> bool:
            return bool(columns) and all(
                col in df.columns and not df[col].isna().any() for col in columns
            )

        declared = list(config.get("columns") or [])
        fallback = list(config.get("in_batch_key") or [])

        natural_cols: List[str] = []
        if _usable(declared):
            natural_cols = declared
        elif _usable(fallback):
            natural_cols = fallback
            logger.info(
                "  %s: collapsing in-batch duplicates on %s because the conflict "
                "target %s contains NULLs, which the unique index cannot see. "
                "Provide those columns, or expect duplicates across runs.",
                table_name, fallback, declared,
            )
        elif any(col in df.columns for col in declared):
            logger.warning(
                "  %s: conflict target %s contains NULL values and no in-batch "
                "fallback key applies; in-batch duplicates are NOT collapsed.",
                table_name, declared,
            )

        if natural_cols:
            before = len(df)
            df = df.sort_values(by=natural_cols, kind="mergesort")
            df = df.drop_duplicates(subset=natural_cols, keep="last")
            # Restore the caller's row order as far as it can be restored; the
            # sort above only exists so drop_duplicates keeps a deterministic
            # survivor when two rows disagree.
            df = df.reset_index(drop=True)
            dropped = before - len(df)
            if dropped:
                logger.info(
                    "  Collapsed %d duplicate row(s) within the batch for %s "
                    "by %s", dropped, table_name, natural_cols,
                )

        return df

    # -------------------------------------------------------------------------
    # PostgreSQL COPY through a staging table
    # -------------------------------------------------------------------------

    def _copy_upsert_postgresql(self, df: pd.DataFrame, table_name: str,
                                model: Any) -> int:
        """Bulk-load via ``COPY`` into a temp table, then merge.

        Three defects from the previous version are fixed here:

        * it called ``session.commit()`` between ``CREATE TEMP TABLE`` and the
          ``COPY``. A commit returns the connection to the pool, and a ``TEMP``
          table is connection-scoped — so the staging table could be gone by the
          time the copy ran. Everything now happens on one ``session.connection()``
          with no commit in between.
        * it built the whole file in a ``StringIO``. That is kept (a staging
          table with no rows has to be filled somehow) but the size is logged, so
          a multi-million-row batch is visible rather than a memory spike.
        * a literal ``\\N`` in a cell silently became NULL. Checked and refused
          before the copy.
        """
        pk_cols = self._get_primary_key_columns(table_name)
        conflict_targets = self._get_conflict_targets(table_name, pk_cols)

        staging_table = f"{table_name}_staging"
        model_columns = [c.name for c in model.__table__.columns]

        df = df[[col for col in df.columns if col in model_columns]].copy()
        for col in model_columns:
            if col not in df.columns:
                df[col] = None

        # Autoincrement PKs that are entirely NULL are left to the database.
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

    # -------------------------------------------------------------------------
    # Batched upserts
    # -------------------------------------------------------------------------

    def _upsert_postgresql(self, df: pd.DataFrame, table_name: str,
                           model: Any) -> int:
        """Batched ``ON CONFLICT DO UPDATE`` for PostgreSQL."""
        from sqlalchemy.dialects.postgresql import insert

        pk_cols = self._get_primary_key_columns(table_name)
        conflict_targets = self._get_conflict_targets(table_name, pk_cols)

        records = df.to_dict(orient="records")
        for record in records:
            for pk in pk_cols:
                if record.get(pk) is None:
                    record.pop(pk, None)

        update_dict = {
            col: None  # placeholder; replaced per batch below
            for col in df.columns
            if col not in pk_cols
            and col not in conflict_targets
            and df[col].notna().any()
        }

        total_written = 0
        with SessionLocal() as session:
            for start in range(0, len(records), self.batch_size):
                batch = records[start:start + self.batch_size]
                if not batch:
                    continue

                stmt = insert(model).values(batch)
                active_cols = [
                    col for col in update_dict if col in batch[0]
                ]

                if active_cols:
                    upsert_stmt = stmt.on_conflict_do_update(
                        index_elements=conflict_targets,
                        set_={col: stmt.excluded[col] for col in active_cols},
                    )
                else:
                    upsert_stmt = stmt.on_conflict_do_nothing(
                        index_elements=conflict_targets
                    )

                result = session.execute(upsert_stmt)
                rowcount = getattr(result, "rowcount", None)
                total_written += (
                    len(batch) if rowcount is None or rowcount < 0 else rowcount
                )
                session.commit()

        return total_written

    def _upsert_sqlite(self, df: pd.DataFrame, table_name: str,
                       model: Any) -> int:
        """``ON CONFLICT DO UPDATE`` for SQLite.

        Uses ``DO UPDATE`` rather than ``INSERT OR REPLACE`` so an
        autoincremented primary key is never deleted and regenerated. Only
        columns with at least one non-null value in this batch participate in the
        ``SET`` clause, so a source that does not populate a column cannot clobber
        a value another source wrote at the same natural key.
        """
        from sqlalchemy.dialects.sqlite import insert

        pk_cols = self._get_primary_key_columns(table_name)
        conflict_targets = self._get_conflict_targets(table_name, pk_cols)

        update_cols = [
            col for col in df.columns
            if col not in pk_cols
            and col not in conflict_targets
            and df[col].notna().any()
        ]

        records = df.to_dict(orient="records")
        for record in records:
            for pk in pk_cols:
                if record.get(pk) is None:
                    record.pop(pk, None)

        total_written = 0
        with SessionLocal() as session:
            for start in range(0, len(records), self.batch_size):
                batch = records[start:start + self.batch_size]
                if not batch:
                    continue

                stmt = insert(model).values(batch)
                if update_cols and conflict_targets:
                    upsert_stmt = stmt.on_conflict_do_update(
                        index_elements=conflict_targets,
                        set_={
                            col: stmt.excluded[col]
                            for col in update_cols
                            if col in batch[0]
                        },
                    )
                elif conflict_targets:
                    upsert_stmt = stmt.on_conflict_do_nothing(
                        index_elements=conflict_targets
                    )
                else:
                    upsert_stmt = stmt

                result = session.execute(upsert_stmt)
                rowcount = getattr(result, "rowcount", None)
                rc = len(batch) if rowcount is None or rowcount < 0 else rowcount
                total_written += rc
                session.commit()

        return total_written

    def _simple_insert(self, df: pd.DataFrame, table_name: str) -> int:
        """Plain append. Bypasses every sentinel and dedup rule above.

        Only reached when ``upsert=False``. ``to_sql(method='multi')`` is used
        because it emits one multi-row ``INSERT``; it will fail on a duplicate,
        which is the point of the setting.
        """
        written = 0
        for start in range(0, len(df), self.batch_size):
            chunk = df.iloc[start:start + self.batch_size]
            chunk.to_sql(
                table_name, engine, if_exists="append", index=False, method="multi"
            )
            written += len(chunk)
        return written

    # -------------------------------------------------------------------------
    # Public: table load
    # -------------------------------------------------------------------------

    def load_table(self, table_name: str, df: pd.DataFrame) -> int:
        """Load a DataFrame into ``table_name``. Returns rows actually written.

        Two passes over the NOT NULL columns of the target table:

        *Stage 1* fills NaN from the model's Python-side default when one is a
        plain scalar. This rescues ``fact_disease_case.case_count``, which is NOT
        NULL with ``server_default='1'`` but which the mapping leaves as None when
        the source omits the column.

        *Stage 2* drops rows that are still NaN in a NOT NULL column with **no**
        default, and refuses to drop rows whose column has only a server default
        (an explicit NULL in the INSERT column list cannot be rescued by one, so
        dropping would hide a configuration bug).

        Autoincrement primary keys are exempt throughout: their absence means "the
        database will assign one", not "this row is malformed".
        """
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
                    if self.use_copy and len(df) > self.batch_size * 2:
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

    # -------------------------------------------------------------------------
    # Public: resolution ledger
    # -------------------------------------------------------------------------

    def load_resolutions(self, resolutions: "List[Any]") -> int:
        """Persist a run's resolutions: append the ledger, project the current state.

        ``fact_resolution_log``
            Append-only, keyed ``(source_table, source_column, source_value,
            run_id)``. ``ON CONFLICT DO NOTHING`` — a decision that can be
            rewritten is not a record of what was decided. **Every** resolution is
            written here, including ``unresolved`` and ``needs_review``.

        ``fact_concept_map``
            The current state: one row per source triple, upserted. Only
            resolutions whose ``decision`` is ``auto_accepted`` appear here. A
            withheld decision has no applied concept, so a row claiming one would
            be a lie about the database's state; the evidence lives in the ledger.

        A human review is protected. The projection's ``DO UPDATE`` carries a
        ``WHERE`` that refuses to change ``concept_code`` on a row whose
        ``is_approved = TRUE`` — otherwise a re-run would silently re-decide a
        row a reviewer approved, while leaving ``reviewed_by`` and ``reviewed_at``
        in place and attributing the new decision to that reviewer. The number of
        refusals is counted and logged.

        Columns never written to ``fact_concept_map``: ``is_approved``,
        ``reviewed_by``, ``reviewed_at``, ``mapped_at``. Those are the human's
        verdict and the row's creation time, and a re-run must not clear them.

        Returns
        -------
        int
            Distinct ledger rows submitted. Duplicates within the call collapse;
            ``DO NOTHING`` may reduce the number inserted; unusable rows are not
            counted. A summary line states all three.
        """
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
                # Contract: auto_accepted implies a concept. A writer that
                # produced the opposite is excluded from the projection and
                # named, rather than being allowed to smuggle a conceptless
                # "applied" row into the table the pipeline reads.
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

        # The `WHERE` on DO UPDATE is the review guarantee. SQLite has no
        # `IS NOT DISTINCT FROM`, so the comparison is spelled out.
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


__all__ = ["DBLoader", "NATURAL_KEY_CONFIG"]