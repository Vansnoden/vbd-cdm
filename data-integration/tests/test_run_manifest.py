import inspect

import pytest
from sqlalchemy import text

import main as entrypoint
from database import models  # noqa: F401
from tests.helpers import row_count


class _Digest:
    """A registry stand-in whose digests are fixed strings."""

    def digest(self):
        """A stable 64-character digest."""
        return "a" * 64

    def digest_with_kind(self):
        """A stable 64-character digest including the artefact kind."""
        return "b" * 64

    def packs(self):
        """No packs; the manifest records the digest, not the contents."""
        return []


class _Context:
    """A RunContext stand-in with a known run id."""

    def __init__(self, countries=("KEN",), run_id="run-manifest-test"):
        self.registry = _Digest()
        self.run_id = run_id
        self.countries = list(countries)
        self.registry_digest = "a" * 64


def test_manifest_signature_is_reachable():
    """The two helpers this module wires must be importable from run_manifest."""
    from utilities.run_manifest import record_row_counts, record_run

    assert callable(record_run)
    assert callable(record_row_counts)
    assert inspect.signature(record_run).parameters
    assert inspect.signature(record_row_counts).parameters


def test_one_manifest_row_with_provenance(session, tmp_path):
    """17: after a fixture run one row carries git_sha, config_sha256, versions."""
    config = tmp_path / "cfg.yaml"
    config.write_text(
        "core:\n  countries: [KEN]\nprocess:\n  steps: []\n", encoding="utf-8")
    context = _Context()

    entrypoint.record_run_manifest(context, config)

    row = session.execute(text(
        "SELECT git_sha, config_sha256, registry_digest, package_versions, "
        "       countries, config_path "
        "FROM run_manifest WHERE run_id = :run_id"),
        {"run_id": context.run_id}).first()

    assert row is not None, "record_run wrote no run_manifest row"
    git_sha, config_sha256, registry_digest, package_versions, countries, path = row
    assert git_sha, "git_sha is null"
    assert config_sha256, "config_sha256 is null"
    assert registry_digest == "a" * 64
    assert package_versions, "package_versions is null"
    assert "KEN" in (countries or "")
    assert str(config) in (path or "")


def test_row_counts_are_recorded(session, tmp_path):
    """17: record_row_counts fills run_manifest.row_counts for a known run."""
    config = tmp_path / "cfg.yaml"
    config.write_text("core:\n  countries: [KEN]\n", encoding="utf-8")
    context = _Context(run_id="run-counts-test")

    entrypoint.record_run_manifest(context, config)
    before = session.execute(text(
        "SELECT row_counts FROM run_manifest WHERE run_id = :r"),
        {"r": context.run_id}).scalar()

    entrypoint.record_run_counts(context)

    after = session.execute(text(
        "SELECT row_counts FROM run_manifest WHERE run_id = :r"),
        {"r": context.run_id}).scalar()

    assert after is not None
    assert after != before
    assert len(after) > 2


def test_burden_expansion_receives_a_run_id(monkeypatch, session):
    """17: run_burden_expansion passes its run id through to the expansion."""
    captured = {}

    class _Consolidator:
        def expand_burden_to_daily(self, **kwargs):
            """Record the kwargs the expansion was called with."""
            captured.update(kwargs)
            return {"inserted": 0, "already_present": 0, "skipped": 0,
                    "no_population": 0, "errors": 0, "case_rows": 0}

        def close(self):
            """Nothing to release."""
            return {}

    monkeypatch.setattr(entrypoint, "DataConsolidator", lambda: _Consolidator())

    entrypoint.run_burden_expansion(run_id="run-expansion-test")

    assert captured.get("run_id") == "run-expansion-test"