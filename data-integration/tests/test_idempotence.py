import json

from sqlalchemy import text

from etl.loaders.db_loader import DBLoader
from etl.transformers.concept_resolver import AUTO_ACCEPTED, Resolution
from utilities.burden_expansion import BurdenExpander
from utilities.consolidation import DataConsolidator
from utilities.run_manifest import record_run
from tests.helpers import (
    PackRegistry, add_burden, add_disease, add_location, add_source,
    add_vocabulary, row_count,
)

VOCAB = "test-vectors@1.0"


class _NoPacks(PackRegistry):
    def __init__(self):
        super().__init__()


def _resolution(value, code):
    return Resolution(
        source_table="dim_vector", source_column="species_name",
        source_value=value, vocabulary_id=VOCAB, vocabulary_version="1.0",
        concept_code=code, match_method="exact", match_score=1.0,
        decision=AUTO_ACCEPTED, candidates=None, resolver_version="1.1.0",
        run_id="run-1")


def test_expansion_is_idempotent(session):
    source = add_source(session)
    location = add_location(session)
    disease = add_disease(session)
    add_burden(session, location_id=location, year=2021,
               disease_code=disease, source_code=source, cases_mean=240.0)

    first = BurdenExpander(session, registry=_NoPacks()).expand()
    counts = {name: row_count(session, name) for name in
              ("fact_disease_case", "dim_time", "fact_disease_burden")}
    total = session.execute(text(
        "SELECT SUM(case_count) FROM fact_disease_case")).scalar()

    second = BurdenExpander(session, registry=_NoPacks()).expand()

    assert first["inserted"] == 365
    assert second["inserted"] == 0
    assert second["already_present"] == 1
    assert {name: row_count(session, name) for name in counts} == counts
    assert session.execute(text(
        "SELECT SUM(case_count) FROM fact_disease_case")).scalar() == total


def test_country_standardisation_is_idempotent(session):
    add_location(session, name="A", code="a", country_code="kenya")
    add_location(session, name="B", code="b", country_code="KEN")
    consolidator = DataConsolidator(session=session)

    first = consolidator.standardize_country_codes()
    second = consolidator.standardize_country_codes()

    assert first["rewritten"] == 2
    assert second["rewritten"] == 0
    assert first["distinct_codes_after"] == second["distinct_codes_after"] == 1
    codes = [row[0] for row in session.execute(
        text("SELECT DISTINCT country_code FROM dim_location"))]
    assert codes == ["KE"]


def test_foreign_key_alignment_is_idempotent(session):
    add_source(session)
    add_location(session, code="loc-x")
    session.execute(text(
        "INSERT INTO dim_vector (vector_code, species_name) VALUES "
        "('V1', 'Anopheles gambiae')"))
    session.execute(text(
        "INSERT INTO fact_vector_observation (location_id, date_id, vector_code, "
        "source_code) VALUES (1, '2020-01-01', 'ORPHAN', 'SRC-1')"))
    session.commit()
    consolidator = DataConsolidator(session=session)

    first = consolidator.align_all_foreign_keys()
    counts_after_first = {
        name: row_count(session, name)
        for name in ("dim_vector", "fact_vector_observation", "dim_location")}
    second = consolidator.align_all_foreign_keys()

    assert first["summary"]["orphans_found"] > 0
    assert second["summary"]["orphans_found"] == 0
    assert second["summary"]["total_fixed"] == 0
    assert {name: row_count(session, name) for name in counts_after_first} \
        == counts_after_first


def test_concept_map_decisions_are_stable(session):
    add_vocabulary(session, VOCAB)
    loader = DBLoader(batch_size=100)
    resolutions = [_resolution("An. gambiae", "VEC-A"),
                   _resolution("An. arabiensis", "VEC-B")]

    loader.load_resolutions(resolutions)
    first = session.execute(text(
        "SELECT source_value, decision, concept_code FROM fact_concept_map "
        "ORDER BY source_value")).fetchall()
    first_ledger = row_count(session, "fact_resolution_log")

    loader.load_resolutions(resolutions)
    second = session.execute(text(
        "SELECT source_value, decision, concept_code FROM fact_concept_map "
        "ORDER BY source_value")).fetchall()

    assert first == second
    assert row_count(session, "fact_concept_map") == len(first)
    assert row_count(session, "fact_resolution_log") == first_ledger


def test_no_duplicate_groups_appear(session):
    from database.models import DimVector

    session.execute(text(
        "INSERT INTO dim_vector (vector_code, species_name) VALUES "
        "('V1', 'Anopheles gambiae'), ('V2', 'Anopheles gambiae')"))
    session.commit()
    consolidator = DataConsolidator(session=session)

    consolidator._consolidate_dimension(DimVector, "species_name", "vector_code")
    consolidator._consolidate_dimension(DimVector, "species_name", "vector_code")

    assert row_count(session, "dim_vector") == 1


def test_run_manifest_digest_is_stable(session):
    class _Digest:
        def digest(self):
            return "a" * 64

        def digest_with_kind(self):
            return "b" * 64

    record_run(session, run_id="run-1", registry=_Digest(), countries=["KE"],
               project_root=None)
    session.commit()
    first = session.execute(text(
        "SELECT registry_digest FROM run_manifest WHERE run_id = 'run-1'")).scalar()

    record_run(session, run_id="run-1", registry=_Digest(), countries=["KE"],
               project_root=None)
    session.commit()
    second = session.execute(text(
        "SELECT registry_digest FROM run_manifest WHERE run_id = 'run-1'")).scalar()

    assert first == second == "a" * 64
    assert row_count(session, "run_manifest") == 1
    payload = session.execute(text(
        "SELECT package_versions FROM run_manifest WHERE run_id = 'run-1'")).scalar()
    assert isinstance(json.loads(payload), dict)