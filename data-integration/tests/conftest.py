# -*- coding: utf-8 -*-
"""
Session-wide pytest fixtures for the vbd-cdm test suite.

Why this file was unusable before batch 6D
------------------------------------------
It was correct but unimportable: ``from database import models`` executes
``database/__init__.py``, which imports ``schemas``, and ``schemas`` raised
``NameError`` at class-creation time (finding §3.4 / §5.12). Batch 6A replaced
``database/schemas.py``; nothing else was blocking. So this file is batch 0's
conftest, re-emitted with the import gate restored — not a redesign.

What happens at import time, in this order (the order matters)
--------------------------------------------------------------
1. ``sys.path`` gains the repository root and ``scripts/``, so tests can import
   the application packages and the standalone golden-export script.
2. ``DATABASE_URL`` is pointed at a throw-away SQLite file inside a temporary
   directory, *before* any application module is imported. Every module that
   binds an engine or a session factory at import time (``database.session``,
   ``etl.loaders.db_loader``, ``utilities.consolidation``,
   ``utilities.database_summary``) therefore binds to that file. A test run can
   never touch a developer's working ``vbd_cdm.db``, and it can never touch a
   PostgreSQL instance configured in ``.env``: ``load_dotenv`` does not
   overwrite a variable that is already set.
3. The application packages are imported, which registers every table on
   ``Base.metadata``.

The temporary directory is removed in ``pytest_sessionfinish``.

The pipeline fixtures below also provision the *input* side of the harness: two
synthetic sources, one mapping CSV each, and a ``base_config.yaml``. They live
in a temporary directory rather than in ``tests/fixtures/`` on purpose — the
only thing under version control is the golden output, so changing fixture data
shows up as a golden diff rather than as an invisible edit.

Module-level assumptions
------------------------
* ``database.session`` reads ``DATABASE_URL`` from the environment at import and
  exposes ``Base`` and a module-level ``engine``.
* ``database/__init__.py`` imports ``models``, ``schemas``, ``session``,
  ``geoutils`` and ``constants``; all five must import cleanly (6A's repair).
* ``main.main()`` reads ``sys.argv`` and calls ``sys.exit`` on a fatal error.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

import pandas as pd
import pytest
import yaml
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


# ---------------------------------------------------------------------------
# Repository layout
# ---------------------------------------------------------------------------

TESTS_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = TESTS_DIR.parent
GOLDEN_DIR: Path = TESTS_DIR / "fixtures" / "golden"
SCRIPTS_DIR: Path = PROJECT_ROOT / "scripts"

for _search_path in (str(SCRIPTS_DIR), str(PROJECT_ROOT)):
    if _search_path not in sys.path:
        sys.path.insert(0, _search_path)


# ---------------------------------------------------------------------------
# Bind the application to a throw-away database before it is imported
# ---------------------------------------------------------------------------

_SESSION_TMP_ROOT: Path = Path(tempfile.mkdtemp(prefix="vbd-cdm-tests-"))
_SESSION_DB_PATH: Path = _SESSION_TMP_ROOT / "vbd_cdm_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_SESSION_DB_PATH.as_posix()}"

from database import models  # noqa: E402,F401  registers tables on Base.metadata
from database.session import Base, engine as project_engine  # noqa: E402


def pytest_sessionfinish(session: "pytest.Session", exitstatus: int) -> None:
    """Dispose the fixture engine and delete the temporary database file."""
    project_engine.dispose()
    shutil.rmtree(_SESSION_TMP_ROOT, ignore_errors=True)


# ---------------------------------------------------------------------------
# Schema fixtures
# ---------------------------------------------------------------------------

def _reset_schema(engine: Engine) -> None:
    """Drop and recreate every CDM table on ``engine``."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture(scope="session")
def database_url() -> str:
    """The ``DATABASE_URL`` the whole test session is bound to."""
    return os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """
    The application engine, bound to a temp-file SQLite database.

    The schema is created on setup and **dropped on teardown**, so the temp file
    is left empty and the session cannot leak tables into a later run. A test
    that needs an empty CDM additionally requests ``clean_database``.
    """
    Base.metadata.create_all(bind=project_engine)
    try:
        yield project_engine
    finally:
        Base.metadata.drop_all(bind=project_engine)


@pytest.fixture(scope="session")
def session_factory(db_engine: Engine) -> "sessionmaker[Session]":
    """
    Session factory bound to the fixture database.

    Constructed exactly as ``database.session.SessionLocal`` is, so it is
    interchangeable with it; the fixtures use this one explicitly so a test
    cannot accidentally reach a differently-bound engine.
    """
    return sessionmaker(autocommit=False, autoflush=False, bind=db_engine)


@pytest.fixture()
def db_session(session_factory: "sessionmaker[Session]") -> Iterator[Session]:
    """A short-lived session against the fixture database."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def clean_database(db_engine: Engine) -> Iterator[None]:
    """Give the test an empty CDM, and leave the schema empty afterwards."""
    _reset_schema(db_engine)
    try:
        yield
    finally:
        _reset_schema(db_engine)


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    """Absolute path of the golden-fixture directory, created if absent."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    return GOLDEN_DIR


# ---------------------------------------------------------------------------
# Pipeline fixture data
# ---------------------------------------------------------------------------

MAPPING_COLUMNS: Tuple[str, ...] = (
    "source_column",
    "target_table",
    "target_column",
    "transform",
    "transform_params",
    "required",
    "static_value",
    "default_value",
    "source_data_type",
)

VECTORATLAS_SOURCE_COLUMNS: Tuple[str, ...] = (
    "site_code", "site_name", "admin_level", "country_name", "country_code",
    "latitude", "longitude", "species", "genus", "event_date",
    "collection_method", "count",
)

IRMAPPER_SOURCE_COLUMNS: Tuple[str, ...] = (
    "site_code", "site_name", "admin_level", "country_name", "country_code",
    "latitude", "longitude", "species", "genus", "test_date", "insecticide",
    "test_type", "concentration", "n_tested", "n_dead",
)

#: Occurrence-style source. Site centroids are deliberately tens of kilometres
#: apart so that location clustering in ``consolidate_locations`` never merges
#: them; the baseline should record ingestion, not a DBSCAN outcome.
VECTORATLAS_SOURCE_ROWS: Tuple[Dict[str, object], ...] = (
    {
        "site_code": "KE-SIAYA-01", "site_name": "Siaya Site One",
        "admin_level": "site", "country_name": "Kenya", "country_code": "KE",
        "latitude": 0.05, "longitude": 34.28,
        "species": "Anopheles gambiae", "genus": "Anopheles",
        "event_date": "2021-03-04", "collection_method": "CDC light trap",
        "count": 12,
    },
    {
        "site_code": "KE-SIAYA-01", "site_name": "Siaya Site One",
        "admin_level": "site", "country_name": "Kenya", "country_code": "KE",
        "latitude": 0.05, "longitude": 34.28,
        "species": "Anopheles gambiae", "genus": "Anopheles",
        "event_date": "2021-04-11", "collection_method": "CDC light trap",
        "count": 7,
    },
    {
        "site_code": "KE-BUSIA-01", "site_name": "Busia Site One",
        "admin_level": "site", "country_name": "Kenya", "country_code": "KE",
        "latitude": 0.34, "longitude": 34.11,
        "species": "Anopheles arabiensis", "genus": "Anopheles",
        "event_date": "2021-05-09", "collection_method": "pyrethrum spray catch",
        "count": 4,
    },
)

#: Bioassay-style source. Repeats one site and one species so the dimension
#: upsert path is exercised, and adds a third site.
IRMAPPER_SOURCE_ROWS: Tuple[Dict[str, object], ...] = (
    {
        "site_code": "KE-SIAYA-01", "site_name": "Siaya Site One",
        "admin_level": "site", "country_name": "Kenya", "country_code": "KE",
        "latitude": 0.05, "longitude": 34.28,
        "species": "Anopheles gambiae", "genus": "Anopheles",
        "test_date": "2021-06-02", "insecticide": "Deltamethrin",
        "test_type": "WHO tube", "concentration": 0.05,
        "n_tested": 100, "n_dead": 91,
    },
    {
        "site_code": "KE-KISUMU-01", "site_name": "Kisumu Site One",
        "admin_level": "site", "country_name": "Kenya", "country_code": "KE",
        "latitude": -0.09, "longitude": 34.77,
        "species": "Anopheles arabiensis", "genus": "Anopheles",
        "test_date": "2021-06-03", "insecticide": "Pirimiphos-methyl",
        "test_type": "CDC bottle", "concentration": 1.0,
        "n_tested": 100, "n_dead": 45,
    },
)

#: Tables the ETL must have populated once a given source has run. A silent
#: ingestion failure in ``run_ingestion_for_source`` (it catches everything and
#: returns False) would otherwise produce empty goldens and a green test.
SOURCE_EXPECTATIONS: Dict[str, Tuple[str, ...]] = {
    "vectoratlas": ("dim_source", "dim_location", "dim_vector", "fact_vector_observation"),
    "irmapper_ir_1": ("dim_insecticide", "dim_resistance_test_type", "fact_insecticide_resistance"),
}


def _m(**overrides: str) -> Dict[str, str]:
    """
    One row of a mapping CSV, with every column present.

    ``required`` is always an explicit ``"True"`` or ``"False"``: a blank cell
    would read back as NaN, and ``bool(nan)`` is True, which would silently turn
    every optional column into a required one.
    """
    row: Dict[str, str] = {column: "" for column in MAPPING_COLUMNS}
    row["transform"] = "none"
    row["required"] = "False"
    row.update(overrides)
    return row


_LOCATION_MAPPING: Tuple[Dict[str, str], ...] = (
    _m(source_column="site_code", target_table="dim_location",
       target_column="location_code", required="True"),
    _m(source_column="site_name", target_table="dim_location",
       target_column="name", required="True"),
    _m(source_column="admin_level", target_table="dim_location",
       target_column="admin_level"),
    _m(source_column="latitude", target_table="dim_location",
       target_column="centroid_lat", transform="cast",
       transform_params='{"type": "float"}'),
    _m(source_column="longitude", target_table="dim_location",
       target_column="centroid_lon", transform="cast",
       transform_params='{"type": "float"}'),
    _m(source_column="country_name", target_table="dim_location",
       target_column="country_name"),
    _m(source_column="country_code", target_table="dim_location",
       target_column="country_code"),
)

_VECTOR_MAPPING: Tuple[Dict[str, str], ...] = (
    _m(source_column="species", target_table="dim_vector",
       target_column="vector_code", required="True"),
    _m(source_column="species", target_table="dim_vector",
       target_column="species_name", required="True"),
    _m(source_column="genus", target_table="dim_vector", target_column="genus"),
    _m(target_table="dim_vector", target_column="family",
       static_value="Culicidae"),
)

VECTORATLAS_MAPPING: Tuple[Dict[str, str], ...] = (
    _m(target_table="dim_source", target_column="source_code",
       static_value="VECTORATLAS", required="True"),
    _m(target_table="dim_source", target_column="source_name",
       static_value="Vector Atlas Occurrence", required="True"),
    _m(target_table="dim_source", target_column="source_description",
       static_value="Synthetic source used by the golden regression fixture"),
    *_LOCATION_MAPPING,
    *_VECTOR_MAPPING,
    _m(source_column="site_code", target_table="fact_vector_observation",
       target_column="location_id", transform="map",
       transform_params='{"ref": "dim_location.location_code"}', required="True"),
    _m(source_column="event_date", target_table="fact_vector_observation",
       target_column="date_id", transform="date_parse",
       transform_params='{"format": "%Y-%m-%d"}', required="True"),
    _m(source_column="species", target_table="fact_vector_observation",
       target_column="vector_code", required="True"),
    _m(target_table="fact_vector_observation", target_column="source_code",
       static_value="VECTORATLAS", required="True"),
    _m(source_column="collection_method", target_table="fact_vector_observation",
       target_column="collection_method"),
    _m(source_column="count", target_table="fact_vector_observation",
       target_column="count", transform="cast", transform_params='{"type": "int"}'),
)

IRMAPPER_MAPPING: Tuple[Dict[str, str], ...] = (
    _m(target_table="dim_source", target_column="source_code",
       static_value="IRMAPPER_IR_1", required="True"),
    _m(target_table="dim_source", target_column="source_name",
       static_value="IRMapper 1: WHO susceptibility, complex/subgroup",
       required="True"),
    *_LOCATION_MAPPING,
    *_VECTOR_MAPPING,
    _m(source_column="insecticide", target_table="dim_insecticide",
       target_column="insecticide_code", required="True"),
    _m(source_column="insecticide", target_table="dim_insecticide",
       target_column="insecticide_name", required="True"),
    _m(source_column="test_type", target_table="dim_resistance_test_type",
       target_column="test_type_code", required="True"),
    _m(source_column="test_type", target_table="dim_resistance_test_type",
       target_column="test_type_name", required="True"),
    _m(source_column="site_code", target_table="fact_insecticide_resistance",
       target_column="location_id", transform="map",
       transform_params='{"ref": "dim_location.location_code"}', required="True"),
    _m(source_column="test_date", target_table="fact_insecticide_resistance",
       target_column="date_id", transform="date_parse",
       transform_params='{"format": "%Y-%m-%d"}', required="True"),
    _m(source_column="species", target_table="fact_insecticide_resistance",
       target_column="vector_code", required="True"),
    _m(source_column="insecticide", target_table="fact_insecticide_resistance",
       target_column="insecticide_code", required="True"),
    _m(source_column="test_type", target_table="fact_insecticide_resistance",
       target_column="test_type_code", required="True"),
    _m(target_table="fact_insecticide_resistance", target_column="source_code",
       static_value="IRMAPPER_IR_1", required="True"),
    _m(source_column="concentration", target_table="fact_insecticide_resistance",
       target_column="concentration", transform="cast",
       transform_params='{"type": "float"}'),
    _m(source_column="n_tested", target_table="fact_insecticide_resistance",
       target_column="n_insect_tested", transform="cast",
       transform_params='{"type": "int"}'),
    _m(source_column="n_dead", target_table="fact_insecticide_resistance",
       target_column="n_insect_dead", transform="cast",
       transform_params='{"type": "int"}'),
)


@dataclass(frozen=True)
class FixtureWorkspace:
    """Everything a pipeline test needs to run the real orchestrator."""

    root: Path
    config_path: Path
    source_names: Tuple[str, ...]
    expectations: Mapping[str, Tuple[str, ...]]


def _write_rows(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    columns: Sequence[str],
) -> Path:
    """Write ``rows`` to ``path`` as CSV with a fixed column order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([dict(row) for row in rows], columns=list(columns))
    frame.to_csv(path, index=False, lineterminator="\n")
    return path


def _source_config(source_name: str, data_path: Path, mapping_path: Path) -> Dict[str, object]:
    """A ``data_sources`` entry that mirrors the shape of the real config.

    ``matching:`` — not ``columns:`` — because this fixture pins the legacy CSV
    path (invariant 5). The inline path is covered by ``SourceSpec``'s own
    tests, not by the golden baseline.
    """
    return {
        "source_name": source_name,
        "type": "file",
        "extension": "csv",
        "path": str(data_path),
        "extension_options": {
            "sep": ",", "quotechar": '"', "encoding": "utf-8",
            "on_bad_lines": "warn", "engine": "python",
        },
        "matching": str(mapping_path),
        "extract_mode": "single",
    }


@pytest.fixture(scope="session")
def pipeline_workspace(tmp_path_factory: "pytest.TempPathFactory") -> FixtureWorkspace:
    """
    Provision the fixture sources, mapping CSVs and ``base_config.yaml``.

    The file stems deliberately contain no four-digit number: ``main.py``
    scrapes a year out of the source filename and adds it as a column, which
    would otherwise add an unmapped column to every extraction.
    """
    root = Path(tmp_path_factory.mktemp("pipeline"))
    data_dir = root / "data"
    mapping_dir = root / "mappings"

    vectoratlas_data = _write_rows(
        data_dir / "vectoratlas_occurrence.csv",
        VECTORATLAS_SOURCE_ROWS, VECTORATLAS_SOURCE_COLUMNS,
    )
    vectoratlas_mapping = _write_rows(
        mapping_dir / "vectoratlas.mapping.csv",
        VECTORATLAS_MAPPING, MAPPING_COLUMNS,
    )
    irmapper_data = _write_rows(
        data_dir / "irmapper_susceptibility.csv",
        IRMAPPER_SOURCE_ROWS, IRMAPPER_SOURCE_COLUMNS,
    )
    irmapper_mapping = _write_rows(
        mapping_dir / "irmapper_ir_1.mapping.csv",
        IRMAPPER_MAPPING, MAPPING_COLUMNS,
    )

    config = {
        "core": {
            "countries": ["KEN"],
            "study_area": {
                "admin_level": "admin2",
                "names": ["Siaya", "Busia", "Kisumu"],
            },
            "grid_scope": "study_area",
        },
        "data_sources": {
            "vectoratlas": _source_config(
                "Vector Atlas Occurrence", vectoratlas_data, vectoratlas_mapping
            ),
            "irmapper_ir_1": _source_config(
                "IRMapper 1: WHO susceptibility, complex/subgroup",
                irmapper_data, irmapper_mapping,
            ),
        },
        # `generate_summary` is deliberately absent: it writes CSV reports into
        # the repository's `outputs/` directory, cannot be redirected, and never
        # touches the CDM. It is not part of what the golden files pin.
        "process": {"steps": ["ingest_data", "consolidate"]},
    }

    config_path = root / "base_config.yaml"
    with config_path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(config, handle, sort_keys=False, default_flow_style=False)

    return FixtureWorkspace(
        root=root,
        config_path=config_path,
        source_names=("vectoratlas", "irmapper_ir_1"),
        expectations=SOURCE_EXPECTATIONS,
    )


@pytest.fixture()
def run_pipeline(monkeypatch: "pytest.MonkeyPatch") -> Callable[..., None]:
    """
    Return a callable that invokes ``main.main()`` exactly as the CLI would.

    The orchestrator is imported lazily: importing ``main`` creates the
    ``logs/`` directory and installs a file handler, which should not happen
    during test collection.
    """
    from main import main as pipeline_main

    def _run(config_path: Path, source: Optional[str] = None) -> None:
        argv = ["main.py", "--config", str(config_path), "--skip-summary"]
        if source is not None:
            argv.extend(["--source", source])
        monkeypatch.setattr(sys, "argv", argv)
        try:
            pipeline_main()
        except SystemExit as exc:
            if exc.code not in (0, None):
                pytest.fail(
                    f"the pipeline exited with status {exc.code!r} while "
                    f"processing source {source!r}; see logs/etl.log for the "
                    f"traceback",
                    pytrace=False,
                )

    return _run