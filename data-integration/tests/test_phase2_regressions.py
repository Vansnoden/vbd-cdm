import logging

import pytest
from sqlalchemy import text

from database import models  # noqa: F401
from etl.loaders.db_loader import DBLoader
from tests.helpers import (
    PackRegistry, add_burden, add_day, add_disease, add_location, add_source,
    row_count,
)

VOCAB = "test-vectors@1.0"


class _NoPacks(PackRegistry):
    def __init__(self):
        super().__init__()


def test_burden_species_survive(session):
    """3.1: two rows differing only by pathogen_species both survive."""
    source = add_source(session)
    location = add_location(session)
    disease = add_disease(session)

    frame = models.FactDiseaseBurden.__table__
    rows = [
        {"location_id": location, "year": 2020, "disease_code": disease,
         "pathogen_species": "Plasmodium falciparum", "source_code": source,
         "cases_mean": 1200.0},
        {"location_id": location, "year": 2020, "disease_code": disease,
         "pathogen_species": None, "source_code": source, "cases_mean": 640.0},
    ]
    import pandas as pd

    loader = DBLoader(batch_size=100)
    loader.load_table("fact_disease_burden", pd.DataFrame(rows))

    stored = session.execute(text(
        "SELECT pathogen_species, cases_mean FROM fact_disease_burden "
        "ORDER BY cases_mean")).fetchall()
    assert len(stored) == 2
    assert [r[1] for r in stored] == [640.0, 1200.0]


def test_null_concentration_is_its_own_key(session):
    """3.2: two NULL-concentration assays with one key collide; a value does not."""
    source = add_source(session)
    location = add_location(session)
    date_id = add_day(session, "2020-05-01")
    session.execute(text(
        "INSERT INTO dim_vector (vector_code, species_name) VALUES "
        "('V1', 'Anopheles gambiae')"))
    session.execute(text(
        "INSERT INTO dim_insecticide (insecticide_code, insecticide_name) VALUES "
        "('I1', 'Deltamethrin')"))
    session.execute(text(
        "INSERT INTO dim_resistance_test_type (test_type_code, test_type_name) "
        "VALUES ('T1', 'WHO tube')"))
    session.commit()

    base = {"location_id": location, "date_id": date_id, "vector_code": "V1",
            "insecticide_code": "I1", "test_type_code": "T1",
            "source_code": source}
    session.execute(text(
        "INSERT INTO fact_insecticide_resistance (location_id, date_id, "
        "vector_code, insecticide_code, test_type_code, source_code, "
        "concentration, n_insect_tested) VALUES "
        "(:location_id, :date_id, :vector_code, :insecticide_code, "
        ":test_type_code, :source_code, NULL, 100)"), base)
    session.commit()

    with pytest.raises(Exception):
        session.execute(text(
            "INSERT INTO fact_insecticide_resistance (location_id, date_id, "
            "vector_code, insecticide_code, test_type_code, source_code, "
            "concentration, n_insect_tested) VALUES "
            "(:location_id, :date_id, :vector_code, :insecticide_code, "
            ":test_type_code, :source_code, NULL, 50)"), base)
        session.commit()
    session.rollback()

    session.execute(text(
        "INSERT INTO fact_insecticide_resistance (location_id, date_id, "
        "vector_code, insecticide_code, test_type_code, source_code, "
        "concentration, n_insect_tested) VALUES "
        "(:location_id, :date_id, :vector_code, :insecticide_code, "
        ":test_type_code, :source_code, 0.05, 100)"), base)
    session.commit()

    assert row_count(session, "fact_insecticide_resistance") == 2


def test_second_source_does_not_overwrite(session):
    """3.3: a second climate product and socio series keep their own rows."""
    import pandas as pd

    location = add_location(session)
    date_id = add_day(session, "2020-06-01")
    for code in ("SRC-A", "SRC-B"):
        add_source(session, code)

    loader = DBLoader(batch_size=100)
    environment = pd.DataFrame([
        {"location_id": location, "date_id": date_id,
         "variable": "temperature_mean", "value": 24.8, "source_code": "SRC-A"},
        {"location_id": location, "date_id": date_id,
         "variable": "temperature_mean", "value": 25.4, "source_code": "SRC-B"},
    ])
    assert loader.load_table("fact_environment", environment) == 2
    assert row_count(session, "fact_environment") == 2

    session.execute(text(
        "INSERT INTO dim_socioeconomic_indicator (indicator_code, indicator_name) "
        "VALUES ('SP.POP.TOTL', 'Population, total')"))
    session.commit()
    socioeconomic = pd.DataFrame([
        {"location_id": location, "year": 2020, "indicator_code": "SP.POP.TOTL",
         "indicator_value": 1.0, "source_code": "SRC-A"},
        {"location_id": location, "year": 2020, "indicator_code": "SP.POP.TOTL",
         "indicator_value": 2.0, "source_code": "SRC-B"},
    ])
    assert loader.load_table("fact_socioeconomic_condition", socioeconomic) == 2
    assert row_count(session, "fact_socioeconomic_condition") == 2


def test_wide_batch_stays_under_bind_limit(session):
    """3.8: a 10,000-row wide batch does not exceed SQLite's 32,766 binds."""
    import pandas as pd

    source = add_source(session)
    location = add_location(session)
    rows = []
    for index in range(200):
        rows.append({
            "location_id": location,
            "date_id": f"2020-{1 + index % 12:02d}-{1 + index % 28:02d}",
            "vector_code": "V1", "source_code": source,
            "life_stage": f"stage-{index}", "sex": None,
            "collection_method": None, "count": index,
        })
    session.execute(text(
        "INSERT INTO dim_vector (vector_code, species_name) VALUES "
        "('V1', 'Anopheles gambiae')"))
    for month in range(1, 13):
        for day in range(1, 29):
            add_day(session, f"2020-{month:02d}-{day:02d}")

    loader = DBLoader(batch_size=10_000)
    width = len(models.FactVectorObservation.__table__.columns)
    assert loader._effective_batch(["x"] * width) <= 30000 // width
    assert loader.load_table("fact_vector_observation",
                             pd.DataFrame(rows)) == 200


def test_no_bounding_box_fallback():
    """3.4: the two heuristic fallbacks are gone."""
    import database.geoutils as geoutils

    assert not hasattr(geoutils, "_get_country_from_bounding_box")
    assert not hasattr(geoutils, "_get_country_from_nearest")


def test_no_boundary_file_returns_nothing(tmp_path, caplog):
    """3.4: an absent boundary file yields (None, None) and logs once."""
    import database.geoutils as geoutils

    caplog.set_level(logging.WARNING, logger="database.geoutils")
    geoutils.set_boundary_path(tmp_path / "absent.gpkg")
    geoutils.get_country_from_coords.cache_clear()

    assert geoutils.get_country_from_coords(-1.29, 36.82) == (None, None)
    geoutils.set_boundary_path(None)


def test_dim_time_season_is_null_from_every_writer(session):
    """3.5: dim_time rows written by any entry point carry season = NULL."""
    from main import init_dim_time
    from utilities.burden_expansion import ensure_dim_time

    assert ensure_dim_time(session, 2021) == 365
    init_dim_time("2022-01-01", "2022-12-31")

    seasons = [row[0] for row in session.execute(
        text("SELECT DISTINCT season FROM dim_time"))]
    assert seasons == [None]
    assert row_count(session, "dim_time") == 365 + 365


def test_merged_subtracts_failures(session, monkeypatch):
    """3.6: a year that cannot be prepared is not counted as a merge."""
    from utilities.burden_expansion import BurdenExpander
    import utilities.burden_expansion as module

    source = add_source(session)
    location = add_location(session)
    disease = add_disease(session)
    for year in (2020, 2021):
        add_burden(session, location_id=location, year=year,
                   disease_code=disease, source_code=source, cases_mean=100.0)

    def _broken(session_arg, year):
        raise RuntimeError("cannot prepare")

    monkeypatch.setattr(module, "ensure_dim_time", _broken)

    stats = BurdenExpander(session, registry=_NoPacks()).expand()

    assert stats["records"] == 2
    assert stats["errors"] == 2
    assert stats["groups"] == 0
    assert stats["merged"] == 0


def test_main_exits_nonzero_when_a_source_fails(monkeypatch, tmp_path):
    """3.7: a failed source makes main() exit non-zero."""
    import main as entrypoint

    config = tmp_path / "cfg.yaml"
    config.write_text(
        "core:\n  countries: [KEN]\n"
        "data_sources:\n  broken:\n    source_name: broken\n"
        "    extension: csv\n    path: missing.csv\n"
        "    matching: missing.mapping.csv\n"
        "process:\n  steps:\n    - ingest_data\n",
        encoding="utf-8")

    monkeypatch.setattr(entrypoint, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(entrypoint, "setup_environment", lambda: None)
    monkeypatch.setattr(entrypoint, "load_run_registry", lambda path=None: None)
    monkeypatch.setattr(entrypoint, "RunContext",
                        lambda countries=None: _StubContext(countries))

    with pytest.raises(SystemExit) as exit_info:
        monkeypatch.setattr(entrypoint.sys, "argv",
                            ["main.py", "--config", str(config)])
        entrypoint.main()

    assert exit_info.value.code == 1


class _StubContext:
    """A RunContext stand-in with no registry and a fixed run id."""

    def __init__(self, countries=None):
        self.registry = None
        self.run_id = "run-stub"
        self.countries = list(countries or [])
        self.registry_digest = ""

    def describe(self):
        """A JSON-serialisable summary."""
        return {"run_id": self.run_id}