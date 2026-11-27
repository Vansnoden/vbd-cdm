# -*- coding: utf-8 -*-
"""Row accounting: every row read is either written or dropped for a stated reason."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text

from database.session import SessionLocal, require_identifier
from etl.loaders.db_loader import DBLoader

REASON_DUPLICATE = "duplicate_natural_key"
REASON_NOT_NULL = "not_null"
REASON_NULL_LOCATION = "null_location_id"

DIGEST_SIZE_LIMIT = 256 * 1024 * 1024


def file_digest(path: Any) -> Dict[str, Any]:
    """SHA-256 and size of an input file, or size only when it is very large."""
    candidate = Path(str(path))
    if not candidate.is_file():
        return {"path": str(candidate), "exists": False}
    size = candidate.stat().st_size
    entry: Dict[str, Any] = {"path": str(candidate), "bytes": int(size)}
    if size <= DIGEST_SIZE_LIMIT:
        digest = hashlib.sha256()
        with open(candidate, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        entry["sha256"] = digest.hexdigest()
    return entry


@dataclass
class TableAudit:
    """Accounting for one target table loaded from one source."""

    table: str
    rows_staged: int = 0
    rows_written: int = 0
    rows_before: int = 0
    rows_after: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def rows_dropped(self) -> int:
        """Rows this table discarded before they reached the database."""
        return sum(self.reasons.values())

    @property
    def rows_inserted(self) -> int:
        """Rows added to the table, from the before and after counts."""
        return max(0, self.rows_after - self.rows_before)

    @property
    def rows_updated(self) -> int:
        """Rows that existed already and were overwritten."""
        return max(0, self.rows_written - self.rows_inserted)

    @property
    def rows_unexplained(self) -> int:
        """Rows staged minus written minus dropped. Must be zero."""
        return self.rows_staged - self.rows_written - self.rows_dropped

    def drop(self, reason: str, count: int = 1) -> None:
        """Record ``count`` rows dropped for ``reason``."""
        if count <= 0:
            return
        self.reasons[str(reason)] = self.reasons.get(str(reason), 0) + int(count)

    def as_dict(self) -> Dict[str, Any]:
        """Plain dict, with the derived counts spelled out."""
        return {
            "table": self.table,
            "rows_staged": self.rows_staged,
            "rows_written": self.rows_written,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "rows_dropped": self.rows_dropped,
            "rows_unexplained": self.rows_unexplained,
            "drop_reasons": dict(sorted(self.reasons.items())),
        }


class AuditLedger:
    """One run's row accounting, per source and per target table."""

    def __init__(self, run_id: str = "", config_path: Any = None,
                 registry_digest: str = "") -> None:
        self.run_id = str(run_id or "")
        self.config_path = None if config_path is None else str(config_path)
        self.registry_digest = str(registry_digest or "")
        self.started_at = None
        self._tables: Dict[tuple, TableAudit] = {}
        self._files: List[Dict[str, Any]] = []

    def table(self, source_key: str, table_name: str) -> TableAudit:
        """The audit record for one source and one target table."""
        key = (str(source_key), str(table_name))
        audit = self._tables.get(key)
        if audit is None:
            audit = TableAudit(table=str(table_name))
            self._tables[key] = audit
        return audit

    def note_drop(self, source_key: str, table_name: str, reason: str,
                  count: int = 1) -> None:
        """Record rows discarded for a stated reason."""
        self.table(source_key, table_name).drop(reason, count)

    def hook(self, source_key: str) -> Callable[[str, str, int], None]:
        """A drop hook bound to ``source_key``, for ``DBLoader._drop_hook``."""
        def _record(table_name: str, reason: str, count: int = 1) -> None:
            self.note_drop(source_key, table_name, reason, count)
        return _record

    def file_read(self, source_key: str, path: Any, rows: int,
                 **extra: Any) -> None:
        """Record one input file, its digest, and how many rows were parsed."""
        entry = file_digest(path)
        entry.update({"source_key": str(source_key), "rows_parsed": int(rows)})
        entry.update(extra)
        self._files.append(entry)

    def reconciliation(self) -> List[Dict[str, Any]]:
        """One row per source and table, for the completeness table."""
        out: List[Dict[str, Any]] = []
        for (source_key, _table), audit in sorted(self._tables.items()):
            entry = audit.as_dict()
            entry["source_key"] = source_key
            out.append(entry)
        return out

    def imbalanced(self) -> List[Dict[str, Any]]:
        """Every table whose staged rows are not written plus dropped."""
        return [row for row in self.reconciliation()
                if row["rows_unexplained"] != 0]

    def as_dict(self) -> Dict[str, Any]:
        """The whole ledger, JSON-serialisable and deterministic."""
        return {
            "run_id": self.run_id,
            "config_path": self.config_path,
            "registry_digest": self.registry_digest,
            "files": sorted(self._files, key=lambda e: e["path"]),
            "tables": self.reconciliation(),
            "imbalanced": self.imbalanced(),
        }

    def write(self, path: Any) -> Path:
        """Write the ledger as canonical JSON and return the path."""
        target = Path(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"
        target.write_text(payload, encoding="utf-8")
        return target


class AuditedLoader(DBLoader):
    """A ``DBLoader`` that records every table it touches into an ``AuditLedger``."""

    def __init__(self, ledger: AuditLedger, source_key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ledger = ledger
        self.source_key = str(source_key)
        self._drop_hook = ledger.hook(self.source_key)

    def load_table(self, table_name: str, df) -> int:
        """Load one table, recording staged, written and row-count deltas."""
        audit = self.ledger.table(self.source_key, table_name)
        audit.rows_staged += int(len(df))
        before = self.count_rows(table_name)
        written = super().load_table(table_name, df)
        audit.rows_before += before
        audit.rows_after += self.count_rows(table_name)
        audit.rows_written += int(written)
        return written

    def drop(self, table_name: str, reason: str, count: int) -> None:
        """Record rows dropped before they reached ``load_table``."""
        self.ledger.note_drop(self.source_key, table_name, reason, count)

    @staticmethod
    def count_rows(table_name: str) -> int:
        """The current row count of one table, or 0 when it is unreadable."""
        try:
            name = require_identifier(table_name, what="table name")
        except ValueError:
            return 0
        try:
            with SessionLocal() as session:
                return int(session.execute(
                    text(f"SELECT COUNT(*) FROM {name}")).scalar() or 0)
        except Exception:
            return 0


__all__ = [
    "REASON_DUPLICATE", "REASON_NOT_NULL", "REASON_NULL_LOCATION",
    "AuditLedger", "AuditedLoader", "TableAudit", "file_digest",
]