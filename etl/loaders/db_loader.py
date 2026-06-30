"""
DBLoader - Loads DataFrames into the CDM database with upsert support and high-performance COPY for PostgreSQL.
"""

import logging
from typing import Dict, Optional, Any
import pandas as pd
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from io import StringIO

from database.session import SessionLocal, engine, DATABASE_URL
from database import models

logger = logging.getLogger(__name__)


class DBLoader:
    """
    Loads DataFrames (from MappingTransformer) into the appropriate database tables.
    Supports upsert (INSERT ON CONFLICT) for PostgreSQL and SQLite.
    For PostgreSQL, optionally uses COPY for ultra‑fast bulk loading when use_copy=True.
    """

    def __init__(self, batch_size: int = 1000, upsert: bool = True, use_copy: bool = False):
        """
        Args:
            batch_size: Number of rows to insert per batch (used for non‑COPY methods).
            upsert: If True, use ON CONFLICT (PostgreSQL) or INSERT OR REPLACE (SQLite).
                    If False, simple append (may fail on duplicates).
            use_copy: (PostgreSQL only) If True, use COPY with a staging table for bulk upsert.
                      Ignored for other databases.
        """
        self.batch_size = batch_size
        self.upsert = upsert
        self.use_copy = use_copy
        self._table_models = self._build_model_map()
        self._db_type = self._detect_db_type()

    def _build_model_map(self) -> Dict[str, Any]:
        """Map table names (as strings) to SQLAlchemy model classes."""
        model_map = {}
        for name, obj in models.__dict__.items():
            if hasattr(obj, '__tablename__'):
                model_map[obj.__tablename__] = obj
        return model_map

    def _detect_db_type(self) -> str:
        """Return 'postgresql', 'sqlite', or 'mysql' based on DATABASE_URL."""
        if DATABASE_URL.startswith('postgresql'):
            return 'postgresql'
        elif DATABASE_URL.startswith('sqlite'):
            return 'sqlite'
        elif DATABASE_URL.startswith('mysql'):
            return 'mysql'
        else:
            raise ValueError(f"Unsupported database type: {DATABASE_URL}")

    def _get_primary_key_columns(self, table_name: str) -> list:
        """Return list of primary key column names for a given table."""
        inspector = inspect(engine)
        pk = inspector.get_pk_constraint(table_name)
        return pk.get('constrained_columns', [])

    def _get_column_types(self, table_name: str) -> Dict[str, str]:
        """Return a dict mapping column name to SQL type (e.g., 'INTEGER', 'TEXT')."""
        inspector = inspect(engine)
        columns = inspector.get_columns(table_name)
        return {col['name']: str(col['type']) for col in columns}

    # -------------------------------------------------------------------------
    # PostgreSQL COPY with staging table upsert (high performance for millions of rows)
    # -------------------------------------------------------------------------
    def _copy_upsert_postgresql(self, df: pd.DataFrame, table_name: str, model: Any):
        """
        Use PostgreSQL COPY to bulk load data into a temporary staging table,
        then merge into the target table with ON CONFLICT DO UPDATE.
        """
        pk_cols = self._get_primary_key_columns(table_name)
        if not pk_cols:
            logger.warning(f"Table {table_name} has no primary key; COPY upsert not possible. Falling back to simple insert.")
            self._simple_insert(df, table_name)
            return

        staging_table = f"{table_name}_staging"
        # Ensure all columns exist in the target table (model columns)
        model_columns = [c.name for c in model.__table__.columns]
        df = df[[col for col in df.columns if col in model_columns]]
        for col in model_columns:
            if col not in df.columns:
                df[col] = None

        # Convert DataFrame to CSV string in memory for COPY
        # Use StringIO, replace NaN with \N for PostgreSQL NULL
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False, header=False, na_rep='\\N')
        csv_buffer.seek(0)

        with SessionLocal() as session:
            # Create temporary staging table (like the target)
            # We drop any existing staging table from previous runs
            session.execute(text(f"DROP TABLE IF EXISTS {staging_table}"))
            session.execute(text(f"""
                CREATE TEMP TABLE {staging_table} (LIKE {table_name} INCLUDING DEFAULTS)
            """))
            session.commit()

            # Use COPY to load data
            raw_conn = session.connection().connection
            cursor = raw_conn.cursor()
            try:
                cursor.copy_expert(
                    f"COPY {staging_table} ({', '.join(df.columns)}) FROM STDIN WITH (FORMAT CSV, NULL '\\N')",
                    csv_buffer
                )
                raw_conn.commit()
                logger.info(f"COPY loaded {len(df)} rows into staging table {staging_table}")
            except Exception as e:
                raw_conn.rollback()
                logger.error(f"COPY failed: {e}")
                raise
            finally:
                cursor.close()

            # Perform upsert from staging to target
            # Build update set for non‑PK columns
            update_cols = [col for col in df.columns if col not in pk_cols]
            if update_cols:
                update_set = ", ".join([f"{col} = EXCLUDED.{col}" for col in update_cols])
            else:
                update_set = ""

            conflict_target = ", ".join(pk_cols)
            upsert_sql = f"""
                INSERT INTO {table_name} ({', '.join(df.columns)})
                SELECT {', '.join(df.columns)} FROM {staging_table}
                ON CONFLICT ({conflict_target}) DO UPDATE SET {update_set}
            """
            session.execute(text(upsert_sql))
            session.execute(text(f"DROP TABLE {staging_table}"))
            session.commit()
            logger.info(f"Upserted {len(df)} rows into {table_name} via COPY and staging")

    # -------------------------------------------------------------------------
    # Standard batched upsert (for smaller datasets or non‑PostgreSQL)
    # -------------------------------------------------------------------------
    def _upsert_postgresql(self, df: pd.DataFrame, table_name: str, model: Any):
        """Standard batched upsert using SQLAlchemy's insert with ON CONFLICT."""
        from sqlalchemy.dialects.postgresql import insert

        pk_cols = self._get_primary_key_columns(table_name)
        if not pk_cols:
            logger.warning(f"Table {table_name} has no primary key; falling back to simple insert.")
            self._simple_insert(df, table_name)
            return

        records = df.to_dict(orient='records')
        with SessionLocal() as session:
            for i in range(0, len(records), self.batch_size):
                batch = records[i:i+self.batch_size]
                stmt = insert(model).values(batch)
                update_dict = {col: getattr(stmt.excluded, col) for col in df.columns if col not in pk_cols}
                upsert_stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_dict)
                session.execute(upsert_stmt)
                session.commit()
                logger.debug(f"Upserted batch {i//self.batch_size + 1} into {table_name}")

    def _upsert_sqlite(self, df: pd.DataFrame, table_name: str, model: Any):
        """Upsert for SQLite using INSERT OR REPLACE (requires primary key)."""
        pk_cols = self._get_primary_key_columns(table_name)
        if not pk_cols:
            logger.warning(f"Table {table_name} has no primary key; using simple insert (may duplicate).")
            self._simple_insert(df, table_name)
            return

        columns = ', '.join([f'"{col}"' for col in df.columns])
        placeholders = ', '.join([f':{col}' for col in df.columns])
        sql = f"INSERT OR REPLACE INTO {table_name} ({columns}) VALUES ({placeholders})"

        with SessionLocal() as session:
            for i in range(0, len(df), self.batch_size):
                batch = df.iloc[i:i+self.batch_size]
                values = batch.to_dict('records')
                session.execute(text(sql), values)
                session.commit()
                logger.debug(f"Upserted batch {i//self.batch_size + 1} into {table_name}")

    def _simple_insert(self, df: pd.DataFrame, table_name: str):
        """Simple append (no conflict handling) using batched to_sql."""
        for i in range(0, len(df), self.batch_size):
            chunk = df.iloc[i:i+self.batch_size]
            chunk.to_sql(table_name, engine, if_exists='append', index=False, method='multi')
            logger.debug(f"Inserted batch {i//self.batch_size + 1} into {table_name}")

    # -------------------------------------------------------------------------
    # Public methods
    # -------------------------------------------------------------------------
    def load_table(self, table_name: str, df: pd.DataFrame) -> int:
        """
        Load a single DataFrame into the database.

        Args:
            table_name: Target table name (must match SQLAlchemy model __tablename__).
            df: DataFrame with columns matching the table schema.

        Returns:
            Number of rows inserted/upserted.

        Raises:
            ValueError: If table_name not found in models.
            SQLAlchemyError: On database errors.
        """
        if df.empty:
            logger.info(f"No data to load for table {table_name}")
            return 0

        if table_name not in self._table_models:
            raise ValueError(f"Unknown table: {table_name}. Available: {list(self._table_models.keys())}")

        model = self._table_models[table_name]
        original_rows = len(df)

        # Ensure column names match the model (drop extra columns, fill missing with None)
        model_columns = [c.name for c in model.__table__.columns]
        df = df[[col for col in df.columns if col in model_columns]]
        for col in model_columns:
            if col not in df.columns:
                df[col] = None  # add missing columns as NULL

        # Convert pandas datetime to Python date objects to avoid COPY issues
        for col in df.select_dtypes(include=['datetime64[ns]']).columns:
            df[col] = df[col].dt.date

        try:
            if self.upsert:
                if self._db_type == 'postgresql':
                    if self.use_copy and len(df) > self.batch_size * 2:
                        # Use COPY for large DataFrames (> 2 batches)
                        self._copy_upsert_postgresql(df, table_name, model)
                    else:
                        self._upsert_postgresql(df, table_name, model)
                elif self._db_type == 'sqlite':
                    self._upsert_sqlite(df, table_name, model)
                else:
                    self._simple_insert(df, table_name)
            else:
                self._simple_insert(df, table_name)

            logger.info(f"Loaded {original_rows} rows into {table_name}")
            return original_rows

        except SQLAlchemyError as e:
            logger.error(f"Failed to load table {table_name}: {e}")
            raise

    def load_all(self, table_dfs: Dict[str, pd.DataFrame]) -> Dict[str, int]:
        """
        Load multiple tables (the output of MappingTransformer.transform).

        Args:
            table_dfs: Dictionary mapping table name -> DataFrame.

        Returns:
            Dictionary mapping table name -> number of rows loaded.
        """
        results = {}
        for table_name, df in table_dfs.items():
            results[table_name] = self.load_table(table_name, df)
        return results