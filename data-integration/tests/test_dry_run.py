import pytest
from sqlalchemy import text

import main as entrypoint
from database import models  # noqa: F401
from tests.helpers import (
    PackRegistry, add_burden, add_disease, add_location, add_source, row_count,
)


class _NoPacks(PackRegistry):
    """A registry with no packs: the built-in expansion defaults apply."""

    def __init__(self):
        super().__init__()


def _case_counts(session):
    """Rows per CDM table, so a dry run can be compared against a real one."""
    return {
        table: row_count(session, table)
        for table in ("dim_time", "fact_disease_case", "fact_environment",
                      "fact_socioeconomic_condition", "run_manifest")
    }


def test_setup_environment_dry_run_seeds_nothing(session):
    """20: a dry run does not seed dim_time."""
    before = row_count(session, "dim_time")

    entrypoint.setup_environment(dry_run=True)

    assert row_count(session, "dim_time") == before


def test_expand_dry_run_writes_nothing(session):
    """20: the expansion computes its projection and returns without writing."""
    from utilities.burden_expansion import BurdenExpander

    source = add_source(session)
    location = add_location(session)
    disease = add_disease(session)
    add_burden(session, location_id=location, year=2021, disease_code=disease,
               source_code=source, cases_mean=240.0)
    before = _case_counts(session)

    stats = BurdenExpander(session, registry=_NoPacks()).expand(dry_run=True)

    assert stats["dry_run"] is True
    assert stats["case_rows"] == 365
    assert stats["groups"] == 1
    assert stats["inserted"] == 0
    assert _case_counts(session) == before


def test_expand_dry_run_is_idempotent_against_a_real_run(session):
    """20: the projection a dry run reports equals the rows a real run inserts."""
    from utilities.burden_expansion import BurdenExpander

    source = add_source(session)
    location = add_location(session)
    disease = add_disease(session)
    add_burden(session, location_id=location, year=2020, disease_code=disease,
               source_code=source, cases_mean=150.0)

    projected = BurdenExpander(session, registry=_NoPacks()).expand(dry_run=True)
    written = BurdenExpander(session, registry=_NoPacks()).expand()

    assert projected["case_rows"] == written["inserted"] == 366
    assert row_count(session, "fact_disease_case") == 366


def test_expand_dry_run_does_not_create_the_method_row(session):
    """20: even the ESTIMATE vocabulary row is left alone."""
    from utilities.burden_expansion import BurdenExpander

    source = add_source(session)
    location = add_location(session)
    disease = add_disease(session)
    add_burden(session, location_id=location, year=2021, disease_code=disease,
               source_code=source, cases_mean=10.0)

    BurdenExpander(session, registry=_NoPacks()).expand(dry_run=True)

    assert row_count(session, "dim_identification_method") == 0


def test_consolidation_dry_run_writes_nothing(session):
    """20: consolidate_all(dry_run=True) leaves every table unchanged."""
    from utilities.consolidation import DataConsolidator

    add_source(session)
    add_location(session, code="loc-dry", country_code="kenya")
    before = _case_counts(session)

    consolidator = DataConsolidator(session=session)
    result = consolidator.consolidate_all(dry_run=True,
                                          registry=_NoPacks())

    assert result["dry_run"] is True
    assert result["burden"]["inserted"] == 0
    assert result["resistance"].get("status") == "dry_run"
    assert _case_counts(session) == before


def test_cli_dry_run_writes_nothing(session, monkeypatch, tmp_path):
    """20: main --dry-run completes and adds no row to any CDM table."""
    config = tmp_path / "base_config.yaml"
    config.write_text(
        "core:\n  countries: [KEN]\n"
        "process:\n  steps:\n    - consolidate\n    - generate_summary\n",
        encoding="utf-8")

    before = _case_counts(session)
    monkeypatch.setattr(entrypoint, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(entrypoint, "load_run_registry", lambda path=None: None)
    monkeypatch.setattr(entrypoint, "generate_summary", lambda output_dir="": None)
    monkeypatch.setattr(entrypoint.sys, "argv",
                        ["main.py", "--config", str(config), "--dry-run"])

    entrypoint.main()

    assert _case_counts(session) == before