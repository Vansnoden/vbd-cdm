# -*- coding: utf-8 -*-
"""Per-pack calibration: does the ladder still put each value in the rung the
fixture says it belongs in?

Each fixture is a **rung lattice**: for one pack it declares
``(raw_value, expected_code, expected_method)``. The test builds the smallest
vocabulary that can produce those rungs — each code's ``exact`` raw value becomes
the concept's name, each ``alias`` raw value becomes a synonym — writes it to
``dim_concept`` and asserts the resolver walks back down to the declared rung. The
real resolver, pack, normaliser chain and thresholds all run.

Adding a pack: drop ``tests/fixtures/calibration/<pack_id>.csv`` with the
three-column header. No code change.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import pandas as pd
import pytest

from database import models
from etl.transformers.concept_resolver import (
    AUTO_ACCEPTED, METHOD_ALIAS, METHOD_EXACT, METHOD_NORMALIZED, UNRESOLVED,
    ConceptResolver,
)

logger = logging.getLogger(__name__)

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
CALIBRATION_DIR = TESTS_DIR / "fixtures" / "calibration"
REGISTRY_PATH = PROJECT_ROOT / "vocabularies" / "registry"

REQUIRED_COLUMNS = ("raw_value", "expected_code", "expected_method")

#: ``fuzzy`` and ``ambiguous`` are absent on purpose: both depend on the
#: neighbourhood of a name in a real vocabulary, so a three-row fixture cannot
#: declare them without hard-coding a score. ``unresolved`` needs only the absence
#: of a match, which is a property of the fixture itself.
DECLARABLE_METHODS = frozenset(
    {METHOD_EXACT, METHOD_NORMALIZED, METHOD_ALIAS, UNRESOLVED})

_EMPTY = ""


def _fixture_paths() -> List[Path]:
    if not CALIBRATION_DIR.is_dir():
        return []
    return sorted(CALIBRATION_DIR.glob("*.csv"), key=lambda p: p.stem)


def _load_fixture(path: Path) -> pd.DataFrame:
    """``keep_default_na=False`` plus explicit ``na_values`` so an empty
    ``expected_code`` becomes NaN while a genuine name like ``"NA"`` stays a
    value."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False,
                        na_values=[_EMPTY], skipinitialspace=True)
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise pytest.UsageError(
            f"{path}: calibration fixture is missing column(s) "
            f"{', '.join(missing)}; expected {list(REQUIRED_COLUMNS)}")
    if frame.empty:
        raise pytest.UsageError(f"{path}: calibration fixture has no rows")
    return frame


def _concepts_from_fixture(frame: pd.DataFrame, pack) -> List[Dict]:
    """Derive the smallest vocabulary that can produce the declared rungs."""
    by_code: Dict[str, List[pd.Series]] = defaultdict(list)
    for _, row in frame.iterrows():
        code = row["expected_code"]
        if pd.isna(code) or not str(code).strip():
            continue
        by_code[str(code).strip()].append(row)

    concepts: List[Dict] = []
    for code in sorted(by_code):
        rows = by_code[code]
        exact_names = [
            str(row["raw_value"]).strip() for row in rows
            if str(row["expected_method"]).strip().lower() == METHOD_EXACT]
        if not exact_names:
            raise pytest.UsageError(
                f"pack {pack.pack_id!r}: expected_code {code!r} has no `exact` "
                f"row, so the fixture does not say what the concept is called. "
                f"Add a row with expected_method '{METHOD_EXACT}' whose raw_value "
                f"is the concept name.")

        synonyms = sorted({
            str(row["raw_value"]).strip() for row in rows
            if str(row["expected_method"]).strip().lower() == METHOD_ALIAS
            and not pd.isna(row["raw_value"])})

        concepts.append({"concept_code": code,
                         "concept_name": exact_names[0],
                         "synonyms": synonyms})

    if not concepts:
        raise pytest.UsageError(
            f"pack {pack.pack_id!r}: no row declares an expected_code.")
    return concepts


def _seed_vocabulary(session, pack, concepts: List[Dict]) -> None:
    """Write the pack's vocabulary row and the fixture's concepts.

    The vocabulary id and domain are the *pack's*, which is what the resolver
    queries on. A concept written under any other id is invisible to it, and that
    invisibility is the silent failure this test exists to prevent.
    """
    vocabulary_id = pack.vocabulary_id
    if session.get(models.DimVocabulary, vocabulary_id) is None:
        session.add(models.DimVocabulary(
            vocabulary_id=vocabulary_id,
            vocabulary_name=f"{pack.pack_id} calibration fixture ({pack.domain})",
            version=pack.version,
            description="Synthesised by tests/test_calibration.py from "
                        f"tests/fixtures/calibration/{pack.pack_id}.csv",
            pack_id=pack.pack_id,
            released_with=pack.released_with or None))

    for concept in concepts:
        session.add(models.DimConcept(
            vocabulary_id=vocabulary_id,
            concept_code=concept["concept_code"],
            concept_name=concept["concept_name"],
            concept_class=pack.domain,
            domain=pack.domain,
            parent_code=None,
            extra=json.dumps({"synonyms": concept["synonyms"]},
                             sort_keys=True, separators=(",", ":"))))
    session.commit()


def _registry_or_skip(pack_id: str):
    """A checkout without built packs is legitimate; a malformed registry raises."""
    if not REGISTRY_PATH.is_dir():
        pytest.skip(f"no vocabulary registry at {REGISTRY_PATH}; calibration "
                    f"needs the shipped pack manifests")
    from utilities.vocabulary.registry import UnknownPack, VocabularyRegistry

    registry = VocabularyRegistry.load_from(REGISTRY_PATH)
    try:
        pack = registry.get(pack_id)
    except UnknownPack:
        pytest.skip(f"registry holds no pack named {pack_id!r}")
    if not pack.field_scope:
        pytest.skip(f"pack {pack_id!r} claims no target field")
    return registry, pack


_FIXTURES = _fixture_paths()
_FIXTURE_IDS = [p.stem for p in _FIXTURES] or ["<no-calibration-fixtures>"]


def test_calibration_fixtures_are_well_formed():
    """Every fixture parses, and none silently declares nothing.

    Separate from the parametrised test on purpose: a fixture with a typo in its
    header would otherwise surface only as a skip or an empty parameter list, and
    a suite that silently checks nothing is worse than one that fails.
    """
    if not _FIXTURES:
        pytest.skip(f"no calibration fixtures under {CALIBRATION_DIR}")

    for path in _FIXTURES:
        frame = _load_fixture(path)
        declared = {str(v).strip().lower() for v in frame["expected_method"].dropna()}
        unknown = sorted(declared - DECLARABLE_METHODS)
        assert not unknown, (
            f"{path.name}: declares method(s) this suite cannot check: {unknown}. "
            f"Declarable: {sorted(DECLARABLE_METHODS)}.")

        for required in (METHOD_EXACT, UNRESOLVED):
            assert required in declared, (
                f"{path.name}: no row declares expected_method {required!r}. Every "
                f"fixture must pin at least one positive and one negative example, "
                f"or it cannot fail.")

        for _, row in frame.iterrows():
            method = str(row["expected_method"]).strip().lower()
            if method in (UNRESOLVED, METHOD_EXACT):
                continue
            assert not pd.isna(row["expected_code"]), (
                f"{path.name}: {row['raw_value']!r} declares expected_method "
                f"{method!r} but no expected_code.")

        assert re.fullmatch(r"[a-z0-9][a-z0-9_.\-]*", path.stem), (
            f"{path.name}: the file stem must be a pack id (lower-case "
            f"alphanumerics, '_', '.', '-'); it is used as registry.get(<stem>).")


def test_calibration_parametrisation_is_not_empty():
    """Guards against the parametrised test below being vacuous.

    ``parametrize`` over an empty list generates **zero** test cases, so the
    skip inside the test body never runs and the module passes while checking
    nothing. That is the failure this test exists to make visible.
    """
    if not CALIBRATION_DIR.is_dir():
        pytest.skip(f"no calibration fixtures under {CALIBRATION_DIR}")
    assert _FIXTURES, (
        f"{CALIBRATION_DIR} exists but holds no *.csv fixture; the parametrised "
        f"calibration test would collect zero cases and pass silently.")


@pytest.mark.parametrize("fixture_path", _FIXTURES, ids=_FIXTURE_IDS)
def test_calibration(fixture_path: Path, db_session, clean_database) -> None:
    """Every row in one fixture resolves to the rung the fixture declares.

    One assertion per row, so a failure names the pack, the raw value, the rung it
    must reach and the rung it did reach.
    """
    pack_id = fixture_path.stem
    registry, pack = _registry_or_skip(pack_id)

    frame = _load_fixture(fixture_path)
    _seed_vocabulary(db_session, pack, _concepts_from_fixture(frame, pack))

    resolver = ConceptResolver(registry, db_session)
    target_field = pack.field_scope[0]
    source_table, _, source_column = target_field.partition(".")
    context = {"source_table": source_table or "dim_unknown",
               "source_column": source_column or "unknown",
               "target_field": target_field}

    checked = 0
    for _, row in frame.iterrows():
        raw_value = row["raw_value"]
        if pd.isna(raw_value) or not str(raw_value).strip():
            continue
        raw_text = str(raw_value).strip()
        expected_method = str(row["expected_method"]).strip().lower()

        assert expected_method in DECLARABLE_METHODS, (
            f"{fixture_path.name}: {raw_text!r} declares expected_method "
            f"{expected_method!r}, which this fixture format cannot express. "
            f"Declarable: {sorted(DECLARABLE_METHODS)}.")

        resolution = resolver.resolve(raw_text, pack, context)
        checked += 1

        if expected_method == UNRESOLVED:
            assert resolution.match_method == UNRESOLVED, (
                f"{fixture_path.name}: {raw_text!r} was expected to be unresolved "
                f"against pack {pack.pack_id!r}, but the ladder reached "
                f"{resolution.match_method!r} (concept {resolution.concept_code!r}, "
                f"score {resolution.match_score:.3f}).")
            assert resolution.decision == UNRESOLVED
            assert resolution.concept_code is None
            continue

        expected_code = row["expected_code"]
        assert not pd.isna(expected_code), (
            f"{fixture_path.name}: {raw_text!r} declares expected_method "
            f"{expected_method!r} but no expected_code.")
        expected_code = str(expected_code).strip()

        assert resolution.match_method == expected_method, (
            f"{fixture_path.name}: {raw_text!r} was expected to match by "
            f"{expected_method!r} against pack {pack.pack_id!r}, but the ladder "
            f"reached {resolution.match_method!r} (concept "
            f"{resolution.concept_code!r}, score "
            f"{resolution.match_score:.3f}). The rung order, a rung score, the "
            f"pack's normalizer_chain or its thresholds have changed.")
        assert resolution.concept_code == expected_code, (
            f"{fixture_path.name}: {raw_text!r} matched by "
            f"{resolution.match_method!r} but resolved to "
            f"{resolution.concept_code!r}, not {expected_code!r}. Check the "
            f"synonyms the fixture declares for that code.")
        assert resolution.decision == AUTO_ACCEPTED, (
            f"{fixture_path.name}: {raw_text!r} matched by "
            f"{resolution.match_method!r} but carries decision "
            f"{resolution.decision!r}. Every string rung must be auto_accepted; "
            f"only the fuzzy rung's margin can withhold one.")
        assert resolution.vocabulary_id == pack.vocabulary_id, (
            f"{fixture_path.name}: {raw_text!r} was attributed to "
            f"{resolution.vocabulary_id!r}, not {pack.vocabulary_id!r}. A decision "
            f"must name the release that produced it.")

    assert checked > 0, (
        f"{fixture_path.name}: every row was blank; a calibration fixture must "
        f"declare at least one raw value")