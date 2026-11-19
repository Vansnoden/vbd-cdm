# -*- coding: utf-8 -*-
"""Golden-file regression test for the end-to-end CDM pipeline.

Runs the real orchestrator once per fixture source with consolidation enabled,
then compares every ``dim_*``/``fact_*`` table against a committed CSV snapshot.

Regenerate the baseline with::

    UPDATE_GOLDEN=1 pytest tests/test_backward_compat.py
"""

from __future__ import annotations

import difflib
import io
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import pandas as pd
import pytest
from sqlalchemy.engine import Engine

import snapshot_golden

if TYPE_CHECKING:
    from conftest import FixtureWorkspace

logger = logging.getLogger(__name__)

UPDATE_ENV_VAR = "UPDATE_GOLDEN"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MAX_DIFF_LINES = 400

#: Snapshotted once, unprefixed, after every source. It is a decision log keyed by
#: raw value, so its content is global rather than per source.
CONCEPT_MAP_TABLE = "fact_concept_map"

_ADDED_COLUMNS: dict = {
    "dim_vocabulary": frozenset({"pack_id", "checksum", "released_with", "extra"}),
    "dim_concept": frozenset({"concept_norm"}),
    "fact_concept_map": frozenset({
        "decision", "vocabulary_version", "resolver_version", "last_run_id",
        "reviewed_by", "reviewed_at"}),
}

_ALLOWED_VALUE_CHANGES: dict = {
    "fact_concept_map": {
        "match_method": frozenset({("none", "unresolved")}),
    },
}

_APPLICABLE_METHODS = frozenset({"exact", "normalized", "alias"})


def _update_requested() -> bool:
    return os.environ.get(UPDATE_ENV_VAR, "").strip().lower() in _TRUTHY


def _canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Round-trip a frame through the golden writer, then read it back.

    Both sides of every comparison take the same path, so an int column read as
    ``int64`` on one side and ``object`` on the other cannot produce a spurious
    diff. Values stay strings; a blank cell becomes NaN.
    """
    text = snapshot_golden.render_csv(frame)
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False,
                       na_values=[""])


def _normalise_vocabulary_id(value):
    """``'gadm@4.1'`` and ``'GADM'`` name the same product; the version suffix is
    not part of "which product"."""
    if pd.isna(value) or value == "":
        return ""
    return str(value).split("@", 1)[0].strip().upper()


def _strip_added_columns(table: str, frame: pd.DataFrame) -> pd.DataFrame:
    added = _ADDED_COLUMNS.get(table)
    if not added:
        return frame
    present = [c for c in added if c in frame.columns]
    return frame.drop(columns=present) if present else frame


def _compare_tables(table: str, expected: pd.DataFrame,
                    actual: pd.DataFrame) -> list:
    """Cell-level differences the allowances above do not cover.

    Returns ``(row_position, column, expected, actual)`` tuples.
    """
    expected = _strip_added_columns(table, expected.copy())
    actual = _strip_added_columns(table, actual.copy())

    if "vocabulary_id" in expected.columns and "vocabulary_id" in actual.columns:
        expected["vocabulary_id"] = expected["vocabulary_id"].map(_normalise_vocabulary_id)
        actual["vocabulary_id"] = actual["vocabulary_id"].map(_normalise_vocabulary_id)

    missing = [c for c in expected.columns if c not in actual.columns]
    if missing:
        return [(-1, c, "<column present>", "<column missing>") for c in missing]
    extra = [c for c in actual.columns if c not in expected.columns]
    if extra:
        return [(-1, c, "<column absent>", "<column present>") for c in extra]

    ordering = [c for c in snapshot_golden.SORT_KEYS.get(table, ())
                if c in expected.columns]
    if ordering:
        expected = expected.sort_values(ordering, kind="mergesort").reset_index(drop=True)
        actual = actual.sort_values(ordering, kind="mergesort").reset_index(drop=True)

    if len(expected) != len(actual):
        return [(-1, "<row count>", str(len(expected)), str(len(actual)))]

    allowed = _ALLOWED_VALUE_CHANGES.get(table, {})
    differences = []
    for position in range(len(expected)):
        for column in expected.columns:
            before, after = expected.at[position, column], actual.at[position, column]
            before_text = "" if pd.isna(before) else str(before)
            after_text = "" if pd.isna(after) else str(after)
            if before_text == after_text:
                continue
            transitions = allowed.get(column)
            if transitions and (before_text, after_text) in transitions:
                continue
            differences.append((position, column, before_text, after_text))
    return differences


def _format_differences(table: str, differences: list, limit: int = 25) -> str:
    lines = [f"{table}: {len(differences)} unexplained difference(s)"]
    for position, column, before, after in differences[:limit]:
        lines.append(f"  row {position} column {column!r}: "
                     f"golden={before!r} actual={after!r}")
    if len(differences) > limit:
        lines.append(f"  <{len(differences) - limit} more>")
    return "\n".join(lines)


def _assert_populated(engine: Engine, tables) -> None:
    """Fail when the pipeline reported success but produced no rows.

    ``run_ingestion_for_source`` swallows every exception and returns False, and
    ``main`` does not propagate that. Without this, a broken fixture snapshots
    empty tables and passes.
    """
    empty = [t for t in tables if snapshot_golden.read_table(engine, t).empty]
    if empty:
        pytest.fail(
            "the pipeline completed but produced no rows for: " + ", ".join(empty)
            + ". See logs/etl.log for the underlying error.", pytrace=False)


def test_refactor_preserves_golden_semantics(
    pipeline_workspace: "FixtureWorkspace",
    run_pipeline: Callable[..., None],
    clean_database: None,
    golden_dir: Path,
    db_engine: Engine,
) -> None:
    """The pipeline reproduces the baseline's *content*, not its bytes.

    Three assertions: every table matches once refactor-added columns are set
    aside; ``fact_concept_map`` differs only by those columns plus the transitions
    enumerated in ``_ALLOWED_VALUE_CHANGES``; and every ``auto_accepted`` row was
    matched by a string rung, never by ``fuzzy`` or ``ambiguous``.

    The test never regenerates. A batch that changes behaviour deliberately states
    its diff here, so a later accidental diff cannot hide behind a regeneration.
    """
    if _update_requested():
        pytest.skip(f"{UPDATE_ENV_VAR} is set; this test compares, it does not "
                    f"regenerate. Unset it to run.")
    if not any(Path(golden_dir).glob("*.csv")):
        pytest.skip(f"no golden fixtures in {golden_dir}; generate with "
                    f"UPDATE_GOLDEN=1 pytest tests/test_backward_compat.py")

    for source in pipeline_workspace.source_names:
        run_pipeline(pipeline_workspace.config_path, source=source)
        _assert_populated(db_engine, pipeline_workspace.expectations.get(source, ()))

        for table in snapshot_golden.list_cdm_tables(db_engine):
            if table == CONCEPT_MAP_TABLE:
                continue
            golden_path = Path(golden_dir) / f"{source}_{table}.csv"
            if not golden_path.is_file():
                logger.info("no golden baseline for %s (%s)", table, golden_path.name)
                continue

            expected = _canonical_frame(pd.read_csv(
                golden_path, dtype=str, keep_default_na=False, na_values=[""]))
            actual = _canonical_frame(snapshot_golden.snapshot_frame(db_engine, table))

            differences = _compare_tables(table, expected, actual)
            if differences:
                pytest.fail(
                    _format_differences(table, differences)
                    + "\n\nAdded columns are set aside automatically; a value "
                      "change must be added to _ALLOWED_VALUE_CHANGES with a "
                      "reason, or it is a regression.", pytrace=False)

    golden_path = Path(golden_dir) / f"{CONCEPT_MAP_TABLE}.csv"
    if not golden_path.is_file():
        pytest.skip(f"no {golden_path.name} baseline; generate with "
                    f"UPDATE_GOLDEN=1 pytest tests/test_backward_compat.py")

    expected_map = _canonical_frame(pd.read_csv(
        golden_path, dtype=str, keep_default_na=False, na_values=[""]))
    actual_map = _canonical_frame(
        snapshot_golden.snapshot_frame(db_engine, CONCEPT_MAP_TABLE))

    differences = _compare_tables(CONCEPT_MAP_TABLE, expected_map, actual_map)
    if differences:
        pytest.fail(
            _format_differences(CONCEPT_MAP_TABLE, differences)
            + "\n\nfact_concept_map may differ only by the columns batch 1 added "
              "and the transitions in _ALLOWED_VALUE_CHANGES.", pytrace=False)

    raw_map = snapshot_golden.snapshot_frame(db_engine, CONCEPT_MAP_TABLE)
    for column in ("decision", "match_method"):
        if column not in raw_map.columns:
            pytest.fail(f"fact_concept_map has no `{column}` column; the "
                        f"migrations did not run against this database.",
                        pytrace=False)

    accepted = raw_map[raw_map["decision"] == "auto_accepted"]
    if accepted.empty:
        logger.info("no auto_accepted rows in this fixture (no vocabulary is "
                    "loaded); the method rule is covered by tests/test_calibration.py")
    else:
        offending = accepted[
            ~accepted["match_method"].astype(str).isin(_APPLICABLE_METHODS)]
        if not offending.empty:
            detail = offending[["source_table", "source_column", "source_value",
                                "match_method", "decision"]].to_string(index=False)
            pytest.fail(
                f"auto_accepted rows whose match_method is not one of "
                f"{sorted(_APPLICABLE_METHODS)}:\n\n{detail}\n\nA fuzzy match is a "
                f"margin question; an ambiguous match is withheld. Neither may "
                f"authorise applying a concept.", pytrace=False)

    withheld = raw_map[raw_map["decision"].astype(str).isin(
        {"needs_review", "unresolved"})]
    if not withheld.empty and "concept_code" in withheld.columns:
        applied = withheld[withheld["concept_code"].notna()
                           & (withheld["concept_code"].astype(str) != "")]
        if not applied.empty:
            detail = applied[["source_table", "source_column", "source_value",
                              "match_method", "decision",
                              "concept_code"]].to_string(index=False)
            pytest.fail(
                "rows that are not auto_accepted but do carry a concept_code:"
                f"\n\n{detail}\n\nA withheld decision may record the candidate it "
                "leaned towards in `candidates`, but a concept_code on an "
                "unresolved row means something applied it.", pytrace=False)

    logger.info("baseline check passed: %d auto_accepted, %d withheld",
                len(accepted), len(withheld))


def test_pipeline_output_matches_golden(
    pipeline_workspace: "FixtureWorkspace",
    run_pipeline: Callable[..., None],
    clean_database: None,
    golden_dir: Path,
    db_engine: Engine,
) -> None:
    """Byte-for-byte snapshot of every CDM table, per source.

    The snapshot is cumulative: the file for the second source contains the rows
    the first source contributed too, because that is the state the database is
    actually in when that source's consolidation runs.
    """
    update = _update_requested()
    if update:
        logger.info("%s is set: regenerating golden fixtures under %s",
                    UPDATE_ENV_VAR, golden_dir)

    def _compare(frame, path: Path) -> None:
        if update:
            snapshot_golden.write_csv(frame, path)
            logger.info("regenerated golden fixture %s", path.name)
            return
        if not path.is_file():
            pytest.fail(f"golden fixture {path.name} is missing from {path.parent}."
                        f"\nCreate it with:\n    UPDATE_GOLDEN=1 pytest "
                        f"{Path(__file__).name}", pytrace=False)

        expected = path.read_text(encoding="utf-8")
        actual = snapshot_golden.render_csv(frame)
        if expected == actual:
            return

        diff = list(difflib.unified_diff(
            expected.splitlines(), actual.splitlines(),
            fromfile=f"golden/{path.name}", tofile=f"actual/{path.name}",
            lineterm="", n=2))
        if len(diff) > _MAX_DIFF_LINES:
            diff = diff[:_MAX_DIFF_LINES] + ["<diff truncated>"]

        pytest.fail(
            f"the CDM output for {path.name} changed.\nA diff is only acceptable "
            f"when it is explainably better: say which invariant it serves and why "
            f"the new values are correct, then regenerate with "
            f"UPDATE_GOLDEN=1.\n\n" + "\n".join(diff), pytrace=False)

    for source in pipeline_workspace.source_names:
        run_pipeline(pipeline_workspace.config_path, source=source)
        _assert_populated(db_engine, pipeline_workspace.expectations.get(source, ()))

        for table in snapshot_golden.list_cdm_tables(db_engine):
            if table == CONCEPT_MAP_TABLE:
                continue
            _compare(snapshot_golden.snapshot_frame(db_engine, table),
                     Path(golden_dir) / f"{source}_{table}.csv")

    _compare(snapshot_golden.snapshot_frame(db_engine, CONCEPT_MAP_TABLE),
             Path(golden_dir) / f"{CONCEPT_MAP_TABLE}.csv")