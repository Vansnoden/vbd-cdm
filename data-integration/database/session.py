# -*- coding: utf-8 -*-
"""
Engine and session construction, and the single database-URL resolver.

Why this file matters more than it looks
----------------------------------------
``alembic/env.py`` used to build its own URL — ``sqlite:///./<name>``, resolved
against the *current working directory* — while this module resolved the same
name against the project root and ignored ``DB_USER``/``DB_HOST`` entirely.
Two resolvers meant ``alembic upgrade head`` could migrate a different file from
the one the pipeline wrote, and (worse) could target PostgreSQL while the
pipeline still targeted SQLite. :func:`resolve_database_url` is now the only
place that decides, and ``alembic/env.py`` imports it.
"""

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

project_root = Path(__file__).parent.parent


SAFE_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_identifier(name: Any, *, what: str = "identifier") -> str:
    """Validate a SQL identifier before it is interpolated into a statement.

    Parameters
    ----------
    name : Any
        The candidate identifier. Coerced with ``str`` and stripped, so a
        SQLAlchemy ``Column`` name and a plain string behave the same way.
    what : str, optional
        What the value is, for the error message: ``"table name"``,
        ``"column name"``.

    Returns
    -------
    str
        The stripped identifier, unchanged otherwise.

    Raises
    ------
    ValueError
        If the value is empty, or if it contains anything outside
        ``[A-Za-z0-9_]`` or starts with a digit. A quoted identifier or a
        schema-qualified name is rejected too: no call site in this codebase
        needs one, and accepting it would re-open the interpolation hole the
        check exists to close.

    Notes
    -----
    This validates. It does not quote. A caller that wants the string to survive
    reserved-word status should still quote at the call site — but every
    identifier that passes here is a bare name, so no caller currently needs to.
    """
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
    """``sqlite:///`` URL for the configured database file.

    A relative ``SQL_LITE_DB_NAME`` is resolved against the project root, not
    against the process's working directory. That single decision is what makes
    the pipeline behave identically whether it is started from the repo root, a
    script directory, or a systemd unit with ``WorkingDirectory=/``.
    """
    name = os.getenv("SQL_LITE_DB_NAME", "vbd_cdm.db")
    path = Path(name)
    if not path.is_absolute():
        path = project_root / path
    return f"sqlite:///{path}"


def _postgres_url() -> str | None:
    """``postgresql://`` URL from the ``DB_*`` variables, or None.

    Credentials are percent-encoded. An unencoded password containing ``@``,
    ``:``, ``/``, ``?`` or ``#`` does not merely fail — it parses as a different
    host or database, which is the kind of bug that shows up as "connection
    refused" and gets debugged in the wrong place.
    """
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
    """The one database URL this installation uses.

    Resolution order, and each step is stated rather than implied:

    1. ``DATABASE_URL`` — an explicit override always wins. This is what the
       test harness sets to bind a throw-away SQLite file before importing
       anything, and what a deployment uses to point at a managed instance.
    2. the ``DB_*`` tuple, when all five are present, as PostgreSQL. An
       installation that has configured a real database should not silently be
       back on SQLite because it forgot to also set ``DATABASE_URL``.
    3. the SQLite file.

    Returns
    -------
    str
        A SQLAlchemy URL.

    Notes
    -----
    This is a function, not a constant, so :data:`DATABASE_URL` and
    ``alembic/env.py`` can call it at their own import time and a test can call
    it after setting the environment. It has no side effects.
    """
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    postgres = _postgres_url()
    if postgres:
        return postgres
    return _sqlite_url()


DATABASE_URL = resolve_database_url()

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

#: ``check_same_thread`` is required because FastAPI-style request handlers and
#: the pytest session share one engine across threads. ``timeout`` gives a
#: writer a moment rather than an immediate "database is locked", which the ETL
#: hits whenever two phases overlap on a chunked source.
connect_args = (
    {"check_same_thread": False, "timeout": 30.0} if _IS_SQLITE else {}
)

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,
    # A pooled connection can be dead (server restart, idle timeout, laptop
    # sleep) without SQLAlchemy knowing. Without this, the first statement after
    # a pause raises OperationalError and the run looks like a code failure.
    pool_pre_ping=True,
)


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _enable_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
        """Turn on the two SQLite settings the schema depends on.

        ``foreign_keys`` is **off** by default in SQLite. Without it every
        ``ForeignKeyConstraint`` in the CDM is documentation: a fact row can
        reference a ``location_id`` that does not exist and SQLite will not
        complain. Every "FK integrity" check in this codebase is an application
        level re-implementation of a constraint the engine was not enforcing.
        Enabling it makes the engine agree with the schema.

        ``busy_timeout`` covers the case where a second connection (the
        consolidator's, a script, a viewer) holds a write lock.

        Deliberately absent: ``journal_mode=WAL``. It is a persistent,
        file-level setting that a test harness would then leave behind, and it
        cannot be set inside a transaction. Set it once, by hand, if a
        deployment wants it.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)
Base = declarative_base()


def get_db():
    """FastAPI-style session dependency: one session per request, always closed."""
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