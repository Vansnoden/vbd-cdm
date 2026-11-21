# -*- coding: utf-8 -*-
"""Run provenance: one row per pipeline invocation, in the database it wrote.

Why this module exists
---------------------
Every number a modelling paper reports has to be attributable to a stated
procedure. The ETL already records *which vocabulary release* produced each
resolution (Registry.digest, dim_vocabulary.checksum), but nothing recorded
*which expansion policy* produced the modelled case rows, nor which dependency
versions produced the database at all. A reader could not regenerate the figure
and a reviewer could not tell two runs apart.

This module writes one ``run_manifest`` row per run, upserted so that a run can
record identity at start and add parameters later, and a JSON ``parameters``
blob that later phases extend without a schema change. It is deliberately
append-by-run-id, not append-only: a run legitimately learns about itself as it
proceeds, and the last writer for a given run id is that run.

Design notes
------------
* Nothing here raises into the caller. Provenance recording is important, but a
  run that already wrote 1.2 million fact rows must not be lost because a JSON
  serialisation failed. Failures are logged at WARNING and returned as False.
* ``git_sha`` and package versions are best-effort: a source tarball has no
  ``.git``, and an unusual environment may not expose ``importlib.metadata``.
  Both degrade to ``None`` rather than to a wrong value.
* Times are UTC. The pipeline logs local time in some places; the manifest does
  not, because a manifest is compared across machines.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: Bumped by hand when a change alters what a stored database *means* — a new
#: expansion policy, a changed classification rule, a schema change. A reader
#: comparing two manifests uses this to decide whether the difference matters.
TOOL_VERSION = "1.1.0"

#: Packages whose version can change a stored number. Listed explicitly rather
#: than enumerating the environment, so the manifest stays small, diffable, and
#: free of machine-specific noise.
_TRACKED_PACKAGES: tuple[str, ...] = (
    "sqlalchemy", "pandas", "numpy", "geopandas", "shapely", "pyogrio",
    "rasterio", "rioxarray", "xarray", "cfgrib", "pyreadstat", "rapidfuzz",
    "reverse_geocode", "pydantic", "python-dotenv",
)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def new_run_id() -> str:
    """``YYYYmmddTHHMMSS-<6 hex>``, UTC.

    The random suffix is not decoration: two runs started in the same second —
    a Makefile with parallel targets, or a test suite — must not share a run id,
    because ``fact_resolution_log`` is keyed on it.
    """
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{os.urandom(3).hex()}"


def utcnow() -> datetime.datetime:
    """Naive UTC, because every DateTime column in the CDM is timezone-naive."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def git_sha(project_root: Optional[Path] = None) -> Optional[str]:
    """The current commit, with ``-dirty`` when the tree has uncommitted work.

    The suffix matters for a paper: a run from a dirty tree is not reproducible
    from the commit it names, and silently printing the bare sha would imply
    that it is.
    """
    root = str(project_root or Path.cwd())
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
        if not sha:
            return None
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception as exc:  # noqa: BLE001 - provenance must never break a run
        logger.debug("git sha unavailable: %s", exc)
        return None


def package_versions(names: Iterable[str] = _TRACKED_PACKAGES) -> Dict[str, str]:
    """Installed versions of the packages that can change a stored number."""
    versions: Dict[str, str] = {}
    try:
        from importlib import metadata
    except Exception:  # pragma: no cover - Python < 3.8
        return versions
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except Exception:  # noqa: BLE001 - not installed is a fact, not an error
            continue
    versions["python"] = platform.python_version()
    return versions


def sha256_file(path: Any) -> Optional[str]:
    """SHA-256 of a file's bytes, or None when it cannot be read."""
    if path is None:
        return None
    candidate = Path(str(path))
    if not candidate.is_file():
        return None
    try:
        digest = hashlib.sha256()
        with open(candidate, "rb") as handle:
            for block in iter(lambda: handle.read(256 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError as exc:
        logger.debug("cannot hash %s: %s", candidate, exc)
        return None


def _json(payload: Any) -> Optional[str]:
    """Canonical JSON text, or None. Never raises."""
    if payload is None:
        return None
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not serialise manifest payload: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _upsert(session, **fields: Any) -> bool:
    """Insert-or-update one manifest row, merging the JSON columns.

    ``parameters``, ``package_versions`` and ``row_counts`` are merged rather
    than replaced: the run-identity call writes ``{countries}``, the expansion
    call adds ``{expansion: {...}}``, and the final call adds row counts. Each
    caller knows only its own keys, and none should erase the others.
    """
    run_id = fields.get("run_id")
    if not run_id:
        logger.warning("refusing to record a manifest row without a run_id")
        return False

    try:
        if fields.get("merge_parameters") and fields.get("parameters"):
            row = session.execute(
                text("SELECT parameters FROM run_manifest WHERE run_id = :r"),
                {"r": run_id}).first()
            merged: Dict[str, Any] = {}
            if row is not None and row[0]:
                try:
                    merged = json.loads(row[0]) or {}
                except (TypeError, ValueError):
                    merged = {}
            merged.update(fields["parameters"])
            fields["parameters"] = merged

        payload = {
            "run_id": run_id,
            "started_at": fields.get("started_at") or utcnow(),
            "finished_at": fields.get("finished_at"),
            "tool_version": fields.get("tool_version") or TOOL_VERSION,
            "git_sha": fields.get("git_sha"),
            "registry_digest": fields.get("registry_digest"),
            "registry_digest_with_kind": fields.get("registry_digest_with_kind"),
            "countries": _json(fields.get("countries")),
            "config_path": (None if fields.get("config_path") is None
                            else str(fields["config_path"])),
            "config_sha256": fields.get("config_sha256"),
            "parameters": _json(fields.get("parameters")),
            "package_versions": _json(fields.get("package_versions")),
            "row_counts": _json(fields.get("row_counts")),
            "notes": fields.get("notes"),
        }

        session.execute(text("""
            INSERT INTO run_manifest
                (run_id, started_at, finished_at, tool_version, git_sha,
                 registry_digest, registry_digest_with_kind, countries,
                 config_path, config_sha256, parameters, package_versions,
                 row_counts, notes)
            VALUES
                (:run_id, :started_at, :finished_at, :tool_version, :git_sha,
                 :registry_digest, :registry_digest_with_kind, :countries,
                 :config_path, :config_sha256, :parameters, :package_versions,
                 :row_counts, :notes)
            ON CONFLICT (run_id) DO UPDATE SET
                finished_at              = COALESCE(EXCLUDED.finished_at,
                                                    run_manifest.finished_at),
                tool_version             = EXCLUDED.tool_version,
                git_sha                  = COALESCE(EXCLUDED.git_sha,
                                                    run_manifest.git_sha),
                registry_digest          = COALESCE(EXCLUDED.registry_digest,
                                                    run_manifest.registry_digest),
                registry_digest_with_kind = COALESCE(
                    EXCLUDED.registry_digest_with_kind,
                    run_manifest.registry_digest_with_kind),
                countries                = COALESCE(EXCLUDED.countries,
                                                    run_manifest.countries),
                config_path              = COALESCE(EXCLUDED.config_path,
                                                    run_manifest.config_path),
                config_sha256            = COALESCE(EXCLUDED.config_sha256,
                                                    run_manifest.config_sha256),
                parameters               = COALESCE(EXCLUDED.parameters,
                                                    run_manifest.parameters),
                package_versions         = COALESCE(EXCLUDED.package_versions,
                                                    run_manifest.package_versions),
                row_counts               = COALESCE(EXCLUDED.row_counts,
                                                    run_manifest.row_counts),
                notes                    = COALESCE(EXCLUDED.notes,
                                                    run_manifest.notes)
        """), payload)
        return True
    except Exception as exc:  # noqa: BLE001 - provenance must never break a run
        logger.warning("could not record the run manifest (%s: %s); the run "
                       "continues and this database will not carry a manifest "
                       "row for %s.", type(exc).__name__, exc, run_id)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def record_run(session, *, run_id: str, registry: Any = None,
               countries: Optional[Iterable[str]] = None,
               config_path: Any = None, project_root: Any = None,
               notes: Optional[str] = None) -> bool:
    """Record a run's identity. Call once, immediately after configuration load.

    Parameters
    ----------
    session : sqlalchemy.orm.Session
    run_id : str
        From :func:`new_run_id`.
    registry : VocabularyRegistry, optional
        Its two digests are recorded. Both, because they are different
        namespaces: ``digest()`` is comparable across releases and
        ``digest_with_kind()`` is only comparable within one.
    countries : iterable of str, optional
    config_path : path-like, optional
        Hashed, so a manifest names the exact configuration that produced the
        database even after the file is edited.
    """
    digests: Dict[str, Optional[str]] = {"digest": None, "with_kind": None}
    if registry is not None:
        try:
            digests["digest"] = registry.digest()
            digests["with_kind"] = registry.digest_with_kind()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read registry digests: %s", exc)

    return _upsert(
        session,
        run_id=run_id,
        registry_digest=digests["digest"],
        registry_digest_with_kind=digests["with_kind"],
        countries=sorted({str(c).strip().upper()
                          for c in (countries or []) if str(c).strip()}),
        config_path=config_path,
        config_sha256=sha256_file(config_path),
        package_versions=package_versions(),
        git_sha=git_sha(Path(str(project_root)) if project_root else None),
        notes=notes,
    )


def record_parameters(session, *, run_id: str,
                      parameters: Mapping[str, Any],
                      namespace: str = "parameters") -> bool:
    """Merge one phase's parameters into the run's manifest.

    ``namespace`` groups the keys, so two phases that both record a ``seed``
    do not collide: ``{"expansion": {...}}`` and ``{"analysis": {...}}``.
    """
    if not run_id:
        logger.warning("no run id: expansion parameters for this phase cannot "
                       "be recorded, and the modelled rows it writes will not "
                       "be attributable to a stated policy.")
        return False
    return _upsert(session, run_id=run_id,
                   parameters={str(namespace): dict(parameters)},
                   merge_parameters=True)


def record_row_counts(session, *, run_id: str,
                      counts: Mapping[str, int]) -> bool:
    """Record table row counts at the end of a run.

    This is what lets a reader confirm that their database and the one described
    in a paper are the same object, without transferring 200 MB to find out.
    """
    if not run_id:
        return False
    return _upsert(session, run_id=run_id, row_counts=dict(counts),
                   finished_at=utcnow())


def count_tables(session, tables: Optional[Iterable[str]] = None) -> Dict[str, int]:
    """Row counts for the named tables, or for every dim_/fact_ table."""
    counts: Dict[str, int] = {}
    if tables is None:
        try:
            from sqlalchemy import inspect
            names: List[str] = [
                name for name in inspect(session.get_bind()).get_table_names()
                if name.startswith(("dim_", "fact_"))]
        except Exception as exc:  # noqa: BLE001
            logger.debug("cannot enumerate tables: %s", exc)
            return counts
    else:
        names = list(tables)
    for name in sorted(names):
        try:
            counts[name] = int(session.execute(
                text(f"SELECT COUNT(*) FROM {name}")).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cannot count %s: %s", name, exc)
    return counts


__all__ = [
    "TOOL_VERSION",
    "count_tables",
    "git_sha",
    "new_run_id",
    "package_versions",
    "record_parameters",
    "record_row_counts",
    "record_run",
    "sha256_file",
    "utcnow",
]