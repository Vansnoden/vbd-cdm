import logging

import pytest

from etl.transformers.concept_resolver import (
    ALIAS_SCORE,
    AUTO_ACCEPTED,
    EXACT_SCORE,
    METHOD_ALIAS,
    METHOD_AMBIGUOUS,
    METHOD_EXACT,
    METHOD_FUZZY,
    METHOD_NORMALIZED,
    NEEDS_REVIEW,
    NORMALIZED_SCORE,
    UNRESOLVED,
    UNRESOLVED_SCORE,
    ConceptResolver,
)
from tests.helpers import FakePack, add_concept

VOCAB = "test-vectors@1.0"
CONTEXT = {
    "source_table": "fact_vector_observation",
    "source_column": "vector_code",
    "run_id": "run-1",
}


def _pack(**overrides):
    kwargs = {"vocabulary_id": VOCAB}
    kwargs.update(overrides)
    return FakePack(**kwargs)


def _resolve(session, value, pack=None, context=None):
    resolver = ConceptResolver(registry=None, session=session)
    return resolver.resolve(value, pack or _pack(), context or dict(CONTEXT))


def test_exact_rung(session):
    add_concept(session, VOCAB, "VEC-1", "Anopheles gambiae")

    result = _resolve(session, "Anopheles gambiae")

    assert result.decision == AUTO_ACCEPTED
    assert result.match_method == METHOD_EXACT
    assert result.concept_code == "VEC-1"
    assert result.match_score == pytest.approx(EXACT_SCORE)


def test_normalised_rung(session):
    add_concept(session, VOCAB, "LOC-1", "Kisumu County")

    result = _resolve(session, "  kisumu   county ")

    assert result.decision == AUTO_ACCEPTED
    assert result.match_method == METHOD_NORMALIZED
    assert result.concept_code == "LOC-1"
    assert result.match_score == pytest.approx(NORMALIZED_SCORE)


def test_alias_rung(session):
    add_concept(session, VOCAB, "DIS-1", "Rift Valley fever",
                synonyms=["RVF"])

    result = _resolve(session, "rvf")

    assert result.decision == AUTO_ACCEPTED
    assert result.match_method == METHOD_ALIAS
    assert result.concept_code == "DIS-1"
    assert result.match_score == pytest.approx(ALIAS_SCORE)


def test_fuzzy_accepted(session):
    add_concept(session, VOCAB, "DIS-1", "Plasmodium falciparum")
    add_concept(session, VOCAB, "DIS-2", "Plasmodium vivax")

    result = _resolve(session, "Plasmodium falciparu",
                      pack=_pack(fuzzy_threshold=0.75, fuzzy_margin=0.05))

    assert result.decision == AUTO_ACCEPTED
    assert result.match_method == METHOD_FUZZY
    assert result.concept_code == "DIS-1"
    assert result.candidates is not None


def test_fuzzy_ambiguous_needs_review(session):
    add_concept(session, VOCAB, "VEC-1", "Anopheles gambiae")
    add_concept(session, VOCAB, "VEC-2", "Anopheles arabiensis")

    result = _resolve(session, "Anopheles",
                      pack=_pack(fuzzy_threshold=0.80, fuzzy_margin=0.05))

    assert result.decision == NEEDS_REVIEW
    assert result.match_method == METHOD_AMBIGUOUS
    assert result.concept_code is None
    assert result.candidates is not None


def test_unresolved(session):
    add_concept(session, VOCAB, "VEC-1", "Anopheles gambiae")

    result = _resolve(session, "zzzz qqqq",
                      pack=_pack(fuzzy_threshold=0.95))

    assert result.decision == UNRESOLVED
    assert result.match_method == "unresolved"
    assert result.concept_code is None
    assert result.match_score == pytest.approx(UNRESOLVED_SCORE)


def test_exact_wins_over_alias(session):
    add_concept(session, VOCAB, "VEC-EXACT", "gambiae")
    add_concept(session, VOCAB, "VEC-ALIAS", "Anopheles gambiae",
                synonyms=["gambiae"])

    result = _resolve(session, "gambiae")

    assert result.match_method == METHOD_EXACT
    assert result.concept_code == "VEC-EXACT"


def test_cache_hit(session):
    add_concept(session, VOCAB, "VEC-1", "Anopheles gambiae")
    resolver = ConceptResolver(registry=None, session=session)

    first = resolver.resolve("Anopheles gambiae", _pack(), dict(CONTEXT))
    second = resolver.resolve("Anopheles gambiae", _pack(), dict(CONTEXT))

    assert first == second
    assert resolver.stats()["cache_hit"] == 1


def test_empty_vocabulary(session, caplog):
    caplog.set_level(logging.WARNING, logger="etl.transformers.concept_resolver")

    result = _resolve(session, "Anopheles gambiae")

    assert result.decision == UNRESOLVED
    assert result.match_method == "unresolved"
    assert "no concepts to match against" in caplog.text


def test_missing_context_raises(session):
    add_concept(session, VOCAB, "VEC-1", "Anopheles gambiae")
    resolver = ConceptResolver(registry=None, session=session)

    with pytest.raises(ValueError, match="source_table"):
        resolver.resolve("Anopheles gambiae", _pack(), {"source_column": "x"})