# -*- coding: utf-8 -*-
"""Ladder, margin, determinism and cache tests for ``ConceptResolver``.

No database: the resolver reads its vocabulary once per pack with a single
``SELECT ... ORDER BY concept_code``, so a four-line stand-in session that returns
a fixed row list and counts its queries is enough. The counter is what makes the
cache test meaningful — a memo hit must not query at all.

The pack is a frozen dataclass rather than a ``ConceptPack``, because
``concept_resolver`` is duck-typed on purpose and a real manifest would make every
test depend on the shipped YAML.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from etl.transformers.concept_resolver import (
    ALIAS_SCORE, AUTO_ACCEPTED, EXACT_SCORE, METHOD_ALIAS, METHOD_AMBIGUOUS,
    METHOD_EXACT, METHOD_FUZZY, METHOD_NORMALIZED, METHOD_UNRESOLVED,
    NEEDS_REVIEW, NORMALIZED_SCORE, UNRESOLVED, UNRESOLVED_SCORE,
    ConceptResolver, Resolution, _Candidate,
)
from utilities.vocabulary.normalizers import compose

DISEASE_CONTEXT: Dict[str, Any] = {
    "source_table": "dim_disease", "source_column": "disease_name",
    "run_id": "run-1"}
VECTOR_CONTEXT: Dict[str, Any] = {
    "source_table": "dim_vector", "source_column": "species_name",
    "run_id": "run-1"}


class _Result:
    def __init__(self, rows: Sequence[Tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._rows)

    def scalar(self) -> Any:
        return self._rows[0][0] if self._rows else None


class _Session:
    """Fixed rows plus a query counter. The resolver issues exactly one query per
    pack, so "no new query" is exactly "the memo table served it"."""
    def __init__(self, rows: Sequence[Tuple[Any, ...]]) -> None:
        self.rows = list(rows)
        self.calls = 0

    def execute(self, statement: Any, params: Optional[dict] = None) -> _Result:
        self.calls += 1
        return _Result(self.rows)


@dataclass(frozen=True)
class _Pack:
    pack_id: str = "test-pack"
    version: str = "1.0"
    domain: Optional[str] = None
    normalizer_chain: Tuple[str, ...] = ()
    fuzzy_threshold: float = 0.80
    fuzzy_margin: float = 0.05
    case_sensitive: bool = False
    field_scope: Tuple[str, ...] = ()

    @property
    def vocabulary_id(self) -> str:
        return f"{self.pack_id}@{self.version}"

    def normalizer(self):
        return compose(self.normalizer_chain)


def _concept(code: str, name: str, synonyms: Optional[Sequence[str]] = None,
             rank: Optional[int] = None) -> Tuple[str, str, Optional[str]]:
    payload: Dict[str, Any] = {}
    if synonyms:
        payload["synonyms"] = list(synonyms)
    if rank is not None:
        payload["rank"] = rank
    extra = (json.dumps(payload, sort_keys=True, separators=(",", ":"))
             if payload else None)
    return (code, name, extra)


def _resolver(rows, pack=None):
    session = _Session(rows)
    return ConceptResolver(None, session), session


# -- L1 exact ---------------------------------------------------------------

def test_ladder_exact() -> None:
    resolver, _ = _resolver([_concept("IDOMAL:0000657",
                                     "Plasmodium falciparum malaria")])
    pack = _Pack(pack_id="idomal", domain="disease")

    resolution = resolver.resolve("Plasmodium falciparum malaria", pack,
                                  DISEASE_CONTEXT)
    assert resolution.match_method == METHOD_EXACT
    assert resolution.match_score == EXACT_SCORE
    assert resolution.concept_code == "IDOMAL:0000657"
    assert resolution.decision == AUTO_ACCEPTED
    assert resolution.vocabulary_id == "idomal@1.0"
    assert resolution.vocabulary_version == "1.0"

    again = resolver.resolve("PLASMODIUM FALCIPARUM MALARIA", pack, DISEASE_CONTEXT)
    assert again.match_method == METHOD_EXACT
    assert again.concept_code == "IDOMAL:0000657"


# -- L2 normalized ----------------------------------------------------------

def test_ladder_normalized() -> None:
    resolver, _ = _resolver([_concept("NCBI:7159", "Anopheles gambiae")])
    pack = _Pack(pack_id="ncbi", domain="vector",
                 normalizer_chain=("species_canon",))

    resolution = resolver.resolve("  GAMBIAE COMPLEX  ", pack, VECTOR_CONTEXT)
    assert resolution.match_method == METHOD_NORMALIZED
    assert resolution.match_score == NORMALIZED_SCORE
    assert resolution.concept_code == "NCBI:7159"
    assert resolution.decision == AUTO_ACCEPTED


# -- L3 alias ---------------------------------------------------------------

def test_ladder_alias() -> None:
    resolver, _ = _resolver([_concept(
        "IDOMAL:0000657", "Plasmodium falciparum malaria",
        synonyms=["malaria", "falciparum malaria"])])
    pack = _Pack(pack_id="idomal", domain="disease",
                 normalizer_chain=("generic",))

    exact_synonym = resolver.resolve("malaria", pack, DISEASE_CONTEXT)
    assert exact_synonym.match_method == METHOD_ALIAS
    assert exact_synonym.match_score == ALIAS_SCORE
    assert exact_synonym.concept_code == "IDOMAL:0000657"

    respelled = resolver.resolve("  Malaria ", pack, DISEASE_CONTEXT)
    assert respelled.match_method == METHOD_ALIAS
    assert respelled.concept_code == "IDOMAL:0000657"


# -- L4 fuzzy ---------------------------------------------------------------

def test_ladder_fuzzy() -> None:
    resolver, _ = _resolver([_concept("NCBI:7159", "Anopheles gambiae")])
    # One concept and a zero margin: nothing to be confused with, so the rung is
    # exercised on the threshold alone.
    pack = _Pack(pack_id="ncbi", domain="vector", fuzzy_threshold=0.80,
                 fuzzy_margin=0.0)

    resolution = resolver.resolve("Anopheles gambie", pack, VECTOR_CONTEXT)
    assert resolution.match_method == METHOD_FUZZY
    assert 0.80 <= resolution.match_score < 1.00
    assert resolution.concept_code == "NCBI:7159"
    assert resolution.decision == AUTO_ACCEPTED


# -- L5 ambiguous -----------------------------------------------------------

def test_ladder_ambiguous() -> None:
    """A fuzzy hit whose runner-up is too close is recorded, not applied.

    ``concept_code`` is asserted ``None``: a withheld decision carries no applied
    concept, so the machine's leaning lives in ``candidates`` only. Four consumers
    depend on this (the projection table, the transport schema, the reconciliation
    writer, the golden test).
    """
    resolver, _ = _resolver([
        _concept("V1", "anopheles gambiae"),
        _concept("V2", "gambiae complex"),
    ])
    # A 0.99 margin guarantees the runner-up is "too close" whatever the exact
    # arithmetic is, without hard-coding a rapidfuzz score.
    pack = _Pack(pack_id="ncbi", domain="vector", fuzzy_threshold=0.50,
                 fuzzy_margin=0.99)

    resolution = resolver.resolve("gambiae", pack, VECTOR_CONTEXT)
    assert resolution.match_method == METHOD_AMBIGUOUS
    assert resolution.decision == NEEDS_REVIEW
    assert resolution.concept_code is None
    assert resolution.candidates, "an ambiguous decision must carry its evidence"

    evidence = json.loads(resolution.candidates)
    assert len(evidence) >= 2
    assert {entry["code"] for entry in evidence[:2]} == {"V1", "V2"}
    assert resolution.match_score >= 0.50


# -- L6 unresolved ----------------------------------------------------------

def test_ladder_unresolved() -> None:
    resolver, _ = _resolver([_concept("IDOMAL:0000657",
                                      "Plasmodium falciparum malaria")])
    pack = _Pack(pack_id="idomal", domain="disease")

    resolution = resolver.resolve("Febrile illness of unknown origin", pack,
                                  DISEASE_CONTEXT)
    assert resolution.match_method == METHOD_UNRESOLVED
    assert resolution.decision == UNRESOLVED
    assert resolution.match_score == UNRESOLVED_SCORE
    assert resolution.concept_code is None

    for blank in (None, "", "   "):
        blanked = resolver.resolve(blank, pack, DISEASE_CONTEXT)
        assert blanked.decision == UNRESOLVED
        assert blanked.concept_code is None


def test_missing_context_is_an_error() -> None:
    """``source_table``/``source_column`` are a precondition, not a hint: both are
    NOT NULL in the ledger, so a decision without them cannot be recorded."""
    resolver, _ = _resolver([_concept("A", "alpha")])
    for context in ({}, {"source_table": "dim_disease"},
                    {"source_column": "disease_name"},
                    {"source_table": "  ", "source_column": "  "}):
        with pytest.raises(ValueError):
            resolver.resolve("alpha", _Pack(), context)


# -- margin rule ------------------------------------------------------------

def test_margin_rule_near_tie_is_ambiguous() -> None:
    """``best - runner_up < margin`` -> needs_review, through the real path."""
    resolver = ConceptResolver(None, _Session([]))
    pack = _Pack(fuzzy_margin=0.05)

    best = _Candidate(vocabulary_id="p@1", code="A", name="alpha",
                      method=METHOD_FUZZY, score=0.96, concept_rank=0)
    runner = _Candidate(vocabulary_id="p@1", code="B", name="beta",
                        method=METHOD_FUZZY, score=0.95, concept_rank=0)

    accepted, decision, method = resolver._fuzzy_outcome(best, runner, pack)
    assert accepted is False
    assert decision == NEEDS_REVIEW
    assert method == METHOD_AMBIGUOUS

    clear = _Candidate(vocabulary_id="p@1", code="C", name="gamma",
                       method=METHOD_FUZZY, score=0.98, concept_rank=0)
    accepted, decision, method = resolver._fuzzy_outcome(clear, runner, pack)
    assert accepted is True
    assert decision == AUTO_ACCEPTED
    assert method == METHOD_FUZZY

    # A single-concept vocabulary has nothing to be confused with.
    assert resolver._apply_margin(0.86, None, pack) is True
    # Zero margin accepts a tie; a non-zero one does not.
    assert resolver._apply_margin(0.96, 0.96, _Pack(fuzzy_margin=0.0)) is True
    assert resolver._apply_margin(0.96, 0.96, pack) is False


# -- determinism ------------------------------------------------------------

def test_determinism_same_input_twice_identical_resolution() -> None:
    """Two independent resolvers over the same rows agree exactly."""
    rows = [
        _concept("A1", "Same Name"),
        _concept("A2", "Same Name"),
        _concept("B1", "anopheles gambiae"),
        _concept("B2", "gambiae complex"),
    ]
    pack = _Pack(pack_id="ncbi", domain="vector", fuzzy_threshold=0.50,
                 fuzzy_margin=0.99)

    first = ConceptResolver(None, _Session(rows)).resolve("gambiae", pack,
                                                          VECTOR_CONTEXT)
    second = ConceptResolver(None, _Session(rows)).resolve("gambiae", pack,
                                                           VECTOR_CONTEXT)
    assert isinstance(first, Resolution)
    assert first == second

    # Two rows sharing one name are settled by concept code, never by row order.
    exact_a = ConceptResolver(None, _Session(rows)).resolve("Same Name", pack,
                                                            VECTOR_CONTEXT)
    exact_b = ConceptResolver(None, _Session(rows)).resolve("Same Name", pack,
                                                            VECTOR_CONTEXT)
    assert exact_a.concept_code == "A1"
    assert exact_a == exact_b


def test_determinism_row_order_cannot_change_the_decision() -> None:
    """Reversing the stored row order changes nothing observable."""
    rows = [
        _concept("B1", "anopheles gambiae"),
        _concept("B2", "gambiae complex"),
        _concept("B3", "gambiae arabiensis"),
    ]
    pack = _Pack(pack_id="ncbi", domain="vector", fuzzy_threshold=0.50,
                 fuzzy_margin=0.99)

    forward = ConceptResolver(None, _Session(rows)).resolve("gambiae", pack,
                                                            VECTOR_CONTEXT)
    reversed_ = ConceptResolver(None, _Session(list(reversed(rows)))).resolve(
        "gambiae", pack, VECTOR_CONTEXT)

    assert forward == reversed_
    assert json.loads(forward.candidates) == json.loads(reversed_.candidates)


# -- cache ------------------------------------------------------------------

def test_cache_serves_repeats_without_querying_the_database() -> None:
    """The memo table, asserted through a query counter."""
    resolver, session = _resolver([_concept("IDOMAL:0000657", "malaria")])
    pack = _Pack(pack_id="idomal", domain="disease")

    first = resolver.resolve("malaria", pack, DISEASE_CONTEXT)
    after_first = session.calls
    assert after_first == 1, "the vocabulary is read exactly once per pack"

    second = resolver.resolve("malaria", pack, DISEASE_CONTEXT)
    assert session.calls == after_first, "a memo hit must not query"
    assert second is first
    assert resolver.stats()["cache_hit"] == 1
    assert resolver.stats()["resolved"] == 1

    # The key is (field, value): one value in another column is another decision.
    other_field = dict(DISEASE_CONTEXT, source_table="dim_insecticide",
                       source_column="insecticide_name")
    third = resolver.resolve("malaria", pack, other_field)
    assert third is not first
    assert session.calls == after_first, "the vocabulary is still in memory"
    assert resolver.stats()["resolved"] == 2
    assert resolver.stats()["cache_hit"] == 1

    resolver.resolve("Plasmodium vivax malaria", pack, DISEASE_CONTEXT)
    assert session.calls == after_first
    assert resolver.stats()["resolved"] == 3