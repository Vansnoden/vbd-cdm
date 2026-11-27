import logging

import pytest
from sqlalchemy import text

from database.models import DimVector
from etl.loaders.db_loader import DBLoader
from etl.transformers.concept_resolver import (
    AUTO_ACCEPTED, METHOD_ALIAS, METHOD_EXACT, Resolution,
)
from utilities.burden_expansion import (
    BURDEN_KERNEL_KEY, BurdenExpander, FLAT_BURDEN_KERNEL,
    ensure_dim_time, generate_daily_case_counts, load_burden_kernel,
)
from utilities.consolidation import DataConsolidator
from tests.helpers import (
    FakePack, PackRegistry, add_burden, add_concept, add_day, add_disease,
    add_location, add_source, add_vocabulary, parameter_pack, row_count,
)

VOCAB = "test-vectors@1.0"


class _NoPacks(PackRegistry):
    def __init__(self):
        super().__init__()


def test_invariant_1_ladder_order_is_first_match_wins(session):
    add_concept(session, VOCAB, "VEC-EXACT", "gambiae")
    add_concept(session, VOCAB, "VEC-NORM", "GAMBIAE", synonyms=[])
    add_concept(session, VOCAB, "VEC-ALIAS", "Anopheles gambiae",
                synonyms=["gambiae"])

    from etl.transformers.concept_resolver import ConceptResolver

    result = ConceptResolver(registry=None, session=session).resolve(
        "gambiae", FakePack(vocabulary_id=VOCAB),
        {"source_table": "dim_vector", "source_column": "species_name"})

    assert result.match_method == METHOD_EXACT
    assert result.concept_code == "VEC-EXACT"


def test_invariant_2_approved_concept_is_never_overwritten(session):
    add_vocabulary(session, VOCAB)
    key = {"source_table": "dim_vector", "source_column": "species_name",
           "source_value": "An. gambiae"}

    def _resolution(concept_code):
        return Resolution(
            source_table=key["source_table"], source_column=key["source_column"],
            source_value=key["source_value"], vocabulary_id=VOCAB,
            vocabulary_version="1.0", concept_code=concept_code,
            match_method=METHOD_EXACT, match_score=1.0,
            decision=AUTO_ACCEPTED, candidates=None, resolver_version="1.1.0",
            run_id="run-1")

    loader = DBLoader(batch_size=100)
    loader.load_resolutions([_resolution("VEC-A")])
    session.execute(text(
        "UPDATE fact_concept_map SET is_approved = 1, reviewed_by = 'reviewer' "
        "WHERE source_value = 'An. gambiae'"))
    session.commit()

    loader.load_resolutions([_resolution("VEC-B")])

    stored = session.execute(text(
        "SELECT concept_code FROM fact_concept_map "
        "WHERE source_value = 'An. gambiae'")).scalar()
    assert stored == "VEC-A"


def _seed_vector_pair(session, *, counts):
    source = add_source(session)
    location = add_location(session)
    date_id = add_day(session, "2020-03-01")
    session.execute(text(
        "INSERT INTO dim_vector (vector_code, species_name) VALUES "
        "('V1', 'Anopheles gambiae'), ('V2', 'Anopheles gambiae')"))
    session.commit()
    for index, (code, count) in enumerate(zip(("V1", "V2"), counts)):
        session.execute(text(
            "INSERT INTO fact_vector_observation (location_id, date_id, "
            "vector_code, source_code, life_stage, sex, collection_method, count) "
            "VALUES (:l, :d, :v, :s, :ls, :sx, :cm, :c)"),
            {"l": location, "d": date_id, "v": code, "s": source,
             "ls": f"stage-{index}", "sx": None, "cm": None, "c": count})
    session.commit()
    return source, location, date_id


def test_invariant_3_cascade_never_loses_facts(session):
    source, location, date_id = _seed_vector_pair(session, counts=(5, 7))
    session.execute(text(
        "UPDATE fact_vector_observation SET life_stage = 'adult'"))
    session.commit()

    consolidator = DataConsolidator(session=session)
    report = consolidator._consolidate_dimension(
        DimVector, "species_name", "vector_code")

    assert report["deleted"] == 1
    assert row_count(session, "dim_vector") == 1
    assert row_count(session, "fact_vector_observation") == 1
    total = session.execute(text(
        "SELECT SUM(count) FROM fact_vector_observation")).scalar()
    assert int(total) == 12
    orphans = session.execute(text(
        "SELECT COUNT(*) FROM fact_vector_observation f WHERE NOT EXISTS "
        "(SELECT 1 FROM dim_vector d WHERE d.vector_code = f.vector_code)"
    )).scalar()
    assert int(orphans) == 0


def test_invariant_3_duplicate_kept_when_repointing_fails(session, monkeypatch):
    _seed_vector_pair(session, counts=(1, 2))
    consolidator = DataConsolidator(session=session)

    def _boom(*args, **kwargs):
        raise RuntimeError("cannot re-point")

    monkeypatch.setattr(consolidator, "_update_fact_references", _boom)
    report = consolidator._consolidate_dimension(
        DimVector, "species_name", "vector_code")

    assert report["deleted"] == 0
    assert report["skipped_referenced"] == 1
    assert row_count(session, "dim_vector") == 2


def test_invariant_3_dry_run_deletes_nothing(session):
    _seed_vector_pair(session, counts=(1, 2))
    before = row_count(session, "dim_vector")

    DataConsolidator(session=session)._consolidate_dimension(
        DimVector, "species_name", "vector_code", dry_run=True)

    assert row_count(session, "dim_vector") == before


@pytest.mark.parametrize("annual,year", [(0.0, 2020), (1.0, 2024),
                                        (365.0, 2023), (9999.6, 2024)])
def test_invariant_4_daily_counts_sum_to_rounded_annual(annual, year):
    frame = generate_daily_case_counts(
        annual_cases=annual, year=year, location_id=1, seed=42,
        kernel=FLAT_BURDEN_KERNEL)
    assert int(frame["case_count"].sum()) == round(annual)


@pytest.mark.parametrize("year", [2019, 2024])
def test_invariant_5_ensure_dim_time_is_idempotent_per_year(session, year):
    first = ensure_dim_time(session, year)
    second = ensure_dim_time(session, year)

    assert first == (366 if year == 2024 else 365)
    assert second == 0
    assert row_count(session, "dim_time") == first


def test_invariant_6_records_sharing_a_key_are_summed(session):
    source = add_source(session)
    location = add_location(session)
    disease = add_disease(session)
    add_burden(session, location_id=location, year=2020, disease_code=disease,
               source_code=source, cases_mean=100.0,
               pathogen_species="Plasmodium falciparum")
    add_burden(session, location_id=location, year=2020, disease_code=disease,
               source_code=source, cases_mean=50.0,
               pathogen_species="Plasmodium vivax")

    stats = BurdenExpander(session, registry=_NoPacks()).expand()

    assert stats["records"] == 2
    assert stats["groups"] == 1
    assert stats["merged"] == 1
    assert stats["inserted"] == 365
    assert row_count(session, "fact_disease_case") == 365
    total = session.execute(text(
        "SELECT SUM(case_count) FROM fact_disease_case")).scalar()
    assert int(total) == 150


def test_invariant_7_only_the_top_level_block_is_read(session, caplog):
    caplog.set_level(logging.INFO, logger="utilities.burden_expansion")
    registry = PackRegistry([
        parameter_pack({"seasonality": {"1": 1.4, "7": 0.6}})])

    kernel = load_burden_kernel(registry)

    assert kernel is FLAT_BURDEN_KERNEL
    assert kernel.is_flat
    assert "Everything outside it is ignored" in caplog.text


def test_invariant_7_unknown_key_in_the_block_fails_loudly(session):
    registry = PackRegistry([
        parameter_pack({BURDEN_KERNEL_KEY: {"seasonality": {"1": 1.4},
                                           "national_alocation": True}})])

    with pytest.raises(ValueError, match="national_alocation"):
        load_burden_kernel(registry)