import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from etl.extractors.file_extractor import FileExtractor

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "tests" / "fixtures" / "sources"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"

FAMILIES = {
    "vector_occurrence": "vector_occurrence.csv",
    "bioassay": "bioassay.csv",
    "burden_raster_row": "burden_raster_row.csv",
    "climate": "climate.csv",
    "socioeconomic": "socioeconomic.csv",
}


def _snapshot_module():
    path = ROOT / "scripts" / "snapshot_golden.py"
    spec = importlib.util.spec_from_file_location("vdcdm_snapshot_golden", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("family,filename", sorted(FAMILIES.items()))
def test_source_fixture_loads(family, filename):
    frame = FileExtractor(extract_mode="single").extract(
        str(SOURCES / filename), sep=",")

    assert len(frame) >= 3
    assert frame.columns[0]


def _seed(session):
    session.execute(text(
        "INSERT INTO dim_source (source_code, source_name) VALUES "
        "('SRC-A', 'Source A'), ('SRC-B', 'Source B')"))
    session.execute(text(
        "INSERT INTO dim_location (name, location_code, country_code, "
        "country_name, admin_level, origin) VALUES "
        "('Kisumu', 'loc-1', 'KE', 'Kenya', 'site', 'source'), "
        "('Siaya', 'loc-2', 'KE', 'Kenya', 'site', 'source')"))
    session.execute(text(
        "INSERT INTO dim_vector (vector_code, species_name) VALUES "
        "('VEC-1', 'Anopheles gambiae')"))
    session.execute(text(
        "INSERT INTO dim_time (date_id, date, year, month, week, day_of_year, "
        "season, is_holiday) VALUES "
        "('2020-01-01', '2020-01-01', 2020, 1, 1, 1, NULL, 0)"))
    session.execute(text(
        "INSERT INTO fact_vector_observation (location_id, date_id, vector_code, "
        "source_code, life_stage, sex, collection_method, count) VALUES "
        "(1, '2020-01-01', 'VEC-1', 'SRC-A', 'adult', NULL, NULL, 12)"))
    session.commit()


def test_snapshot_is_deterministic(session, engine, tmp_path):
    _seed(session)
    module = _snapshot_module()

    first = tmp_path / "first"
    second = tmp_path / "second"
    counts_a = module.snapshot_database(engine, first)
    counts_b = module.snapshot_database(engine, second)

    assert counts_a == counts_b
    names = sorted(path.name for path in first.glob("*.csv"))
    assert names
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_snapshot_matches_committed_baseline(session, engine, tmp_path):
    _seed(session)
    module = _snapshot_module()
    produced = tmp_path / "produced"
    module.snapshot_database(engine, produced)

    baseline = sorted(path.name for path in GOLDEN.glob("*.csv"))
    if not baseline:
        pytest.skip("no committed baseline; run `make golden` to create one")

    for name in baseline:
        generated = produced / name
        assert generated.is_file(), f"{name} missing from the snapshot"
        assert generated.read_bytes() == (GOLDEN / name).read_bytes(), (
            f"{name} differs from tests/fixtures/golden/{name}; "
            f"regenerate with `make golden` if the change is intended"
        )