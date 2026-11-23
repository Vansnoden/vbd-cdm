# -*- coding: utf-8 -*-
"""Engine and session construction, and the single database-URL resolver."""

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


load_dotenv()


SAFE_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


project_root = Path(__file__).parent.parent


def require_identifier(name: Any, *, what: str = "identifier") -> str:
    """Validate a SQL identifier before it is interpolated into a statement."""
    text_value = str(name).strip() if name is not None else ""
    if not text_value:
        raise ValueError(
            f"refusing to interpolate an empty SQL {what}; a blank name cannot "
            f"identify anything and would produce a statement that runs against "
            f"the wrong table or column"
        )
    if not SAFE_SQL_IDENTIFIER.match(text_value):
        raise ValueError(
            f"{text_value!r} is not a safe SQL {what}; only bare names matching "
            f"{SAFE_SQL_IDENTIFIER.pattern} are accepted"
        )
    return text_value


def _sqlite_url() -> str:
    name = os.getenv("SQL_LITE_DB_NAME", "vbd_cdm.db")
    path = Path(name)
    if not path.is_absolute():
        path = project_root / path
    return f"sqlite:///{path}"


def _postgres_url() -> str | None:
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT")
    name = os.getenv("DB_NAME")
    if not all([user, password, host, port, name]):
        return None
    return (
        f"postgresql://{quote_plus(str(user))}:{quote_plus(str(password))}"
        f"@{host}:{port}/{name}"
    )


def resolve_database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    postgres = _postgres_url()
    if postgres:
        return postgres
    return _sqlite_url()


DATABASE_URL = resolve_database_url()
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

connect_args = (
    {"check_same_thread": False, "timeout": 30.0} if _IS_SQLITE else {}
)

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,
    pool_pre_ping=True,
)


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _enable_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            # WAL + NORMAL: a rebuildable ETL database, not a system of record.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = [
    "Base",
    "DATABASE_URL",
    "SessionLocal",
    "connect_args",
    "engine",
    "get_db",
    "project_root",
    "require_identifier",
    "resolve_database_url",
]