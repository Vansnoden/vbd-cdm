# -*- coding: utf-8 -*-
"""In-run dimension key index: one raw value, one CDM code, one concept."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text

from database.session import require_identifier as _require_identifier

from etl.transformers.concept_resolver import (
    AUTO_ACCEPTED,
    DECISIONS,
    UNRESOLVED,
)

logger = logging.getLogger(__name__)


CODE_POLICY_IDENTITY = "identity"
CODE_POLICY_SLUG = "slug"
CODE_POLICY_CONCEPT = "concept"

CODE_POLICIES = frozenset({
    CODE_POLICY_IDENTITY,
    CODE_POLICY_SLUG,
    CODE_POLICY_CONCEPT,
})

ORIGIN_ASSIGNED = "assigned"

ORIGIN_SEEDED = "seeded"

_IMMUTABLE_FIELDS = frozenset({"code", "raw_key"})

_WHITESPACE = re.compile(r"\s+")
_SLUG_PUNCT = re.compile(r"[^a-z0-9]+")


def _as_text(value: Any) -> str:
    """``str(value)`` with None/NaN/NA mapped to ''. Never raises."""
    if value is None:
        return ""
    if type(value).__name__ in ("NAType", "NaTType"):
        return ""
    try:
        if value != value:  
            return ""
    except Exception:  
        return ""
    return str(value)


def _coerce_int(value: Any) -> Optional[int]:
    """``int`` from a value that may be text or a float, or None."""
    text_value = _as_text(value).strip()
    if not text_value:
        return None
    try:
        return int(text_value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(text_value))
    except (TypeError, ValueError):
        return None


def _default_key(value: str) -> str:
    """The default in-run key: trimmed, whitespace-collapsed, case-folded."""
    return _WHITESPACE.sub(" ", value.strip()).casefold()


def _slugify(value: str) -> str:
    """A lower-case underscore code built from the value's alphanumerics."""
    folded = _WHITESPACE.sub(" ", value.strip()).casefold()
    return _SLUG_PUNCT.sub("_", folded).strip("_")


def _read_field(source: Any, name: str) -> Any:
    """Read ``name`` off a mapping or an object, or None."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _concept_fields(concept: Any) -> tuple[dict[str, Any], Optional[str]]:
    """Translate a resolution into ``DimensionEntry`` fields."""
    if concept is None:
        return {}, None

    concept_code = _as_text(_read_field(concept, "concept_code")).strip()
    decision = _as_text(_read_field(concept, "decision")).strip()

    if not concept_code:
        return {}, None

    if decision and decision != AUTO_ACCEPTED:
        return {}, f"decision is {decision!r}"

    fields: dict[str, Any] = {
        "concept_code": concept_code,
        "decision": AUTO_ACCEPTED,
    }

    concept_name = _as_text(_read_field(concept, "concept_name")).strip()
    if concept_name:
        fields["concept_name"] = concept_name

    concept_id = _coerce_int(_read_field(concept, "concept_id"))
    if concept_id is not None:
        fields["concept_id"] = concept_id

    standard_code = _as_text(_read_field(concept, "standard_code")).strip()
    fields["standard_code"] = standard_code or concept_code

    return fields, None


@dataclass(frozen=True)
class DimensionEntry:
    """One dimension row as the transform loop needs it."""

    code: str
    pk: Optional[int] = None
    concept_code: Optional[str] = None
    concept_name: Optional[str] = None
    concept_id: Optional[int] = None
    standard_code: Optional[str] = None
    decision: Optional[str] = None
    raw: Optional[str] = None
    raw_key: Optional[str] = None
    origin: str = ORIGIN_ASSIGNED

    def __post_init__(self) -> None:
        """Enforce the two structural invariants at construction time."""
        if not _as_text(self.code).strip():
            raise ValueError("DimensionEntry.code must be a non-empty string")

        if self.decision is not None:
            if self.decision not in DECISIONS:
                raise ValueError(
                    f"DimensionEntry(code={self.code!r}) carries decision "
                    f"{self.decision!r}, which is not one of {sorted(DECISIONS)}."
                )
            if self.decision != AUTO_ACCEPTED:
                raise ValueError(
                    f"DimensionEntry(code={self.code!r}) carries decision "
                    f"{self.decision!r}; only {AUTO_ACCEPTED!r} may be recorded. "
                    f"A needs_review or ambiguous resolution is evidence for "
                    f"fact_resolution_log, not an instruction to stamp a "
                    f"dimension row."
                )
            if not self.concept_code:
                raise ValueError(
                    f"DimensionEntry(code={self.code!r}) carries decision "
                    f"{AUTO_ACCEPTED!r} but no concept_code. A verdict without a "
                    f"concept is not a decision, it is a bug."
                )

    @property
    def has_concept(self) -> bool:
        """Whether a vocabulary concept is attached."""
        return bool(self.concept_code)

    @property
    def is_seeded(self) -> bool:
        """Whether this entry came from the database rather than a source row."""
        return self.origin == ORIGIN_SEEDED

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict view, for logging or a test assertion."""
        return {
            "code": self.code,
            "pk": self.pk,
            "concept_code": self.concept_code,
            "concept_name": self.concept_name,
            "concept_id": self.concept_id,
            "standard_code": self.standard_code,
            "decision": self.decision,
            "raw": self.raw,
            "raw_key": self.raw_key,
            "origin": self.origin,
        }


class DimensionKeyIndex:
    """In-run map raw value -> CDM code, and code -> concept + PK."""

    _SEED_ALIASES: tuple = (
        ("pk", "pk_field"),
        ("concept_code", "concept_code_field"),
        ("concept_id", "concept_id_field"),
        ("standard_code", "standard_code_field"),
        ("concept_name", "concept_name_field"),
        ("alias_text", "alias_field"),
    )

    def __init__(self, *, code_policy: str = CODE_POLICY_IDENTITY,
                 key: Optional[Callable[[str], str]] = None,
                 label: Optional[str] = None) -> None:
        policy = _as_text(code_policy).strip().lower() or CODE_POLICY_IDENTITY
        if policy not in CODE_POLICIES:
            raise ValueError(
                f"unknown code_policy {code_policy!r}; expected one of "
                f"{', '.join(sorted(CODE_POLICIES))}"
            )
        if key is not None and not callable(key):
            raise TypeError(
                f"key must be callable (str -> str), got {type(key).__name__}"
            )

        self.code_policy = policy
        self.label = _as_text(label).strip() or "dimension"
        self._key_fn: Callable[[str], str] = key or _default_key
        self._by_code: dict[str, DimensionEntry] = {}
        self._by_key: dict[str, str] = {}
        self._seed_spec: Optional[dict] = None
        self._warned: set[tuple[str, str]] = set()
        self._stats: Counter = Counter()

    @classmethod
    def from_session(cls, session, table: str, *, code_field: str,
                     pk_field: Optional[str] = None,
                     concept_code_field: Optional[str] = None,
                     concept_id_field: Optional[str] = None,
                     standard_code_field: Optional[str] = None,
                     concept_name_field: Optional[str] = None,
                     alias_field: Optional[str] = None,
                     **kwargs: Any) -> "DimensionKeyIndex":
        """Seed an index from the dimension table it describes."""
        identifier_args = {
            "table": _require_identifier(table),
            "code_field": _require_identifier(code_field, what="column name"),
        }
        prepared: dict[str, Optional[str]] = {}
        for argument, value in (
            ("pk_field", pk_field),
            ("concept_code_field", concept_code_field),
            ("concept_id_field", concept_id_field),
            ("standard_code_field", standard_code_field),
            ("concept_name_field", concept_name_field),
            ("alias_field", alias_field),
        ):
            prepared[argument] = (
                None if value is None
                else _require_identifier(value, what="column name"))

        kwargs.setdefault("label", identifier_args["table"])
        index = cls(**kwargs)
        index._seed_spec = dict(identifier_args, **prepared)
        index.refresh(session)
        logger.info(
            "seeded %s from %s.%s: %d code(s), %d binding(s), %d with concept",
            index.label, identifier_args["table"], identifier_args["code_field"],
            len(index._by_code), len(index._by_key), index._stats["with_concept"],
        )
        return index

    def _seed_query(self) -> Any:
        """The SELECT that reads this index's dimension table."""
        spec = self._seed_spec
        if spec is None:
            raise RuntimeError(
                f"{self.label}: no seed spec; build the index with from_session().")
        parts = [f"{spec['code_field']} AS code"]
        for alias, argument in self._SEED_ALIASES:
            column = spec.get(argument)
            parts.append(f"{column} AS {alias}" if column else f"NULL AS {alias}")
        return text(f"SELECT {', '.join(parts)} FROM {spec['table']} "
                    f"ORDER BY {spec['code_field']}")

    def refresh(self, session) -> int:
        """Re-read the dimension table, merging new codes and PKs into this index."""
        rows = session.execute(self._seed_query()).mappings().all()
        for row in rows:
            self.register(
                row["code"],
                raw=row["alias_text"],
                pk=row["pk"],
                concept_code=row["concept_code"],
                concept_name=row["concept_name"],
                concept_id=row["concept_id"],
                standard_code=row["standard_code"],
            )
        return len(rows)

    def assign(self, raw: Any, concept: Any = None, *,
               code: Optional[str] = None,
               pk: Any = None) -> Optional[str]:
        """Record one raw value against a dimension code, returning that code."""
        raw_text = _as_text(raw)
        if not raw_text.strip():
            logger.debug(
                "%s: blank raw value; returning None (contract Amendment §5.9)",
                self.label,
            )
            return None

        key = self._make_key(raw_text)
        if not key:
            logger.debug(
                "%s: %r normalised to an empty key; returning None",
                self.label, raw_text,
            )
            return None

        source = raw_text.strip()
        fields, blocked = _concept_fields(concept)

        bound_code = self._by_key.get(key)
        if bound_code is not None:
            held = self._by_code.get(bound_code)
            if held is None:
                self._by_key.pop(key, None)
            else:
                if blocked:
                    self._note_blocked(held.code, source, blocked)
                return self._finish(held, fields, pk, source)

        cdm_code = _as_text(code).strip() or self._derive_code(source, fields)

        collision = self._by_code.get(cdm_code)
        if collision is not None:
            self._by_key[key] = collision.code
            self._note_collision(collision.code, collision, key, source)
            if blocked:
                self._note_blocked(collision.code, source, blocked)
            self._stats["aliased"] += 1
            return self._finish(collision, fields, pk, source)

        entry = DimensionEntry(
            code=cdm_code,
            raw=source,
            raw_key=key,
            origin=ORIGIN_ASSIGNED,
        )
        self._store(entry)
        self._by_key[key] = entry.code
        self._stats["assigned"] += 1

        if blocked:
            self._note_blocked(entry.code, source, blocked)
        logger.debug(
            "%s: %r -> %r (%s)", self.label, source, entry.code,
            "with concept" if fields else "no concept",
        )
        return self._finish(entry, fields, pk, source)

    def resolve(self, raw: Any, concept: Any = None) -> Optional[DimensionEntry]:
        """``assign`` then ``lookup``, returning the entry."""
        return self.lookup(self.assign(raw, concept))

    def lookup(self, code: Any) -> Optional[DimensionEntry]:
        """The entry for ``code``, or None."""
        return self._by_code.get(_as_text(code).strip())

    def lookup_raw(self, raw: Any) -> Optional[DimensionEntry]:
        """The entry whose in-run key matches ``raw``, or None."""
        code = self.code_for(raw)
        return None if code is None else self._by_code.get(code)

    def code_for(self, raw: Any) -> Optional[str]:
        """The code already assigned to ``raw``, without creating one."""
        value = _as_text(raw).strip()
        if not value:
            return None
        return self._by_key.get(self._make_key(value))

    def codes(self) -> tuple[str, ...]:
        """Every known code, sorted. Deterministic."""
        return tuple(sorted(self._by_code))

    def entries(self) -> tuple[DimensionEntry, ...]:
        """Every entry, ordered by code. Deterministic."""
        return tuple(self._by_code[code] for code in sorted(self._by_code))

    def __len__(self) -> int:
        return len(self._by_code)

    def __contains__(self, code: object) -> bool:
        return _as_text(code).strip() in self._by_code

    def register(self, code: Any, *, raw: Any = None, pk: Any = None,
                 concept_code: Any = None, concept_name: Any = None,
                 concept_id: Any = None, standard_code: Any = None,
                 origin: str = ORIGIN_SEEDED) -> DimensionEntry:
        """Record a known mapping without inventing anything."""
        code_text = _as_text(code).strip()
        if not code_text:
            raise ValueError(
                f"register() needs a non-empty code in {self.label}; got {code!r}"
            )

        raw_text = _as_text(raw).strip()
        concept_code_text = _as_text(concept_code).strip()
        standard_text = _as_text(standard_code).strip()

        entry = DimensionEntry(
            code=code_text,
            pk=_coerce_int(pk),
            concept_code=concept_code_text or None,
            concept_name=_as_text(concept_name).strip() or None,
            concept_id=_coerce_int(concept_id),
            standard_code=standard_text or concept_code_text or None,
            decision=AUTO_ACCEPTED if concept_code_text else None,
            raw=raw_text or None,
            raw_key=self._make_key(raw_text) if raw_text else None,
            origin=origin,
        )

        current = self._by_code.get(code_text)
        if current is not None:
            entry = self._merge(current, entry)
        else:
            self._stats["registered"] += 1

        self._store(entry)

        if entry.raw_key and entry.raw:
            bound = self._by_key.get(entry.raw_key)
            if bound is None:
                self._by_key[entry.raw_key] = code_text
                self._stats["bound"] += 1
            elif bound != code_text:
                self._warn_once(
                    code_text,
                    f"bind:{entry.raw_key}",
                    "%s: %r is already bound to %r; leaving it there and not "
                    "rebinding it to %r. Two dimension rows sharing a name is a "
                    "vocabulary defect, not something an index may settle.",
                    self.label, entry.raw, bound, code_text,
                )

        return entry

    def update(self, code: Any, **fields: Any) -> DimensionEntry:
        """Enrich an existing entry. Fills gaps; never clears a value."""
        code_text = _as_text(code).strip()
        current = self._by_code.get(code_text)
        if current is None:
            raise KeyError(
                f"update({code_text!r}) in {self.label}: no such code. "
                f"Assign or register it first."
            )

        unknown = sorted(set(fields) - set(current.as_dict()))
        if unknown:
            raise ValueError(
                f"update({code_text!r}) got unknown field(s) {unknown}; "
                f"DimensionEntry has {sorted(current.as_dict())}"
            )
        forbidden = sorted(set(fields) & _IMMUTABLE_FIELDS)
        if forbidden:
            raise ValueError(
                f"update({code_text!r}) cannot change {forbidden}: the code is "
                f"the index key and raw_key is derived from the key function."
            )

        updates = {
            name: value for name, value in fields.items() if value is not None
        }
        if not updates:
            return current

        entry = replace(current, **updates)
        self._store(entry)

        if entry.concept_code and not current.concept_code:
            self._stats["with_concept"] += 1
        return entry

    def stats(self) -> dict[str, Any]:
        """Counts describing this index. Deterministic (sorted keys)."""
        summary: dict[str, Any] = {
            name: self._stats[name] for name in sorted(self._stats)
        }
        summary["codes"] = len(self._by_code)
        summary["keys"] = len(self._by_key)
        summary["with_pk"] = sum(
            1 for entry in self._by_code.values() if entry.pk is not None
        )
        return summary

    def snapshot(self) -> dict[str, Any]:
        """A deterministic description, for a run manifest or a log line."""
        return {
            "label": self.label,
            "code_policy": self.code_policy,
            "codes": len(self._by_code),
            "keys": len(self._by_key),
            "with_concept": sum(
                1 for entry in self._by_code.values() if entry.has_concept
            ),
            "with_pk": sum(
                1 for entry in self._by_code.values() if entry.pk is not None
            ),
            "seeded": sum(
                1 for entry in self._by_code.values() if entry.is_seeded
            ),
            "collisions": self._stats["aliased"],
        }

    def _finish(self, entry: DimensionEntry, fields: Mapping[str, Any],
                pk: Any, source: str) -> str:
        """Attach the concept and the PK to ``entry``, and return its code."""
        if fields and fields.get("concept_code"):
            entry = self._attach(entry.code, fields, source=source)

        if pk is not None:
            coerced = _coerce_int(pk)
            if coerced is None:
                pass
            elif entry.pk is None:
                entry = replace(entry, pk=coerced)
                self._store(entry)
            elif entry.pk != coerced:
                self._warn_once(
                    entry.code,
                    f"pk:{coerced}",
                    "%s: %r resolved to primary key %r but code %r already "
                    "carries %r. Keeping the first: a natural key that maps to "
                    "two rows means the dimension table has a duplicate.",
                    self.label, source, coerced, entry.code, entry.pk,
                )

        return entry.code

    def _make_key(self, value: str) -> str:
        """Apply the injected key function, surviving one that raises."""
        try:
            key = self._key_fn(value)
        except Exception as exc:  # noqa: BLE001 - a key function must not kill a run
            marker = ("key", type(exc).__name__)
            if marker not in self._warned:
                self._warned.add(marker)
                logger.error(
                    "%s: the key function %r raised %s: %s. Falling back to the "
                    "default key (trim, collapse whitespace, casefold) for this "
                    "index; codes may not match the pack's canonicalisation.",
                    self.label,
                    getattr(self._key_fn, "__qualname__", self._key_fn),
                    type(exc).__name__, exc,
                )
            key = _default_key(value)

        text_key = _as_text(key).strip()
        if not text_key:
            raise ValueError(
                f"the key function for {self.label} produced an empty key for "
                f"{value!r}; every value needs a key or the index cannot tell "
                f"two raws apart."
            )
        return text_key

    def _derive_code(self, value: str, fields: Mapping[str, Any]) -> str:
        """Apply ``code_policy`` to a value that has no code yet."""
        if self.code_policy == CODE_POLICY_CONCEPT:
            concept_code = _as_text(fields.get("concept_code")).strip()
            if concept_code:
                return concept_code
            logger.debug(
                "%s: %r has no accepted concept; CODE_POLICY_CONCEPT falls back "
                "to identity",
                self.label, value,
            )
            return value

        if self.code_policy == CODE_POLICY_SLUG:
            slug = _slugify(value)
            if not slug:
                raise ValueError(
                    f"{value!r} slugifies to nothing in {self.label}; it has no "
                    f"alphanumeric character to build a code from."
                )
            return slug

        return value

    def _merge(self, current: DimensionEntry,
               incoming: DimensionEntry) -> DimensionEntry:
        """Fold an incoming entry into an existing one, first value winning."""
        merged: dict[str, Any] = {}
        conflicts: list[str] = []

        for name, value in current.as_dict().items():
            incoming_value = incoming.as_dict().get(name)
            if value is None and incoming_value is not None:
                merged[name] = incoming_value
            else:
                merged[name] = value
                if (
                    value is not None
                    and incoming_value is not None
                    and name not in ("raw", "raw_key", "origin")
                    and value != incoming_value
                ):
                    conflicts.append(name)

        if current.is_seeded or incoming.is_seeded:
            merged["origin"] = ORIGIN_SEEDED
        elif current.origin != incoming.origin:
            merged["origin"] = current.origin

        if conflicts:
            self._warn_once(
                current.code,
                "conflict:" + ",".join(sorted(conflicts)),
                "%s: value %r resolves to code %r with conflicting %s "
                "(%r kept, %r ignored). The first decision for a code wins; "
                "check the resolver's output for this value.",
                self.label, incoming.raw or current.raw, current.code,
                ", ".join(sorted(conflicts)), current.code, incoming.code,
            )

        return replace(current, **merged)

    def _attach(self, code: str, fields: Mapping[str, Any], *,
                source: str) -> DimensionEntry:
        """Attach a concept to an existing entry, respecting the invariant."""
        current = self._by_code.get(code)
        if current is None:  # pragma: no cover - callers pass a known code
            return self.register(code, raw=source, **dict(fields))

        incoming_code = _as_text(fields.get("concept_code")).strip()
        if (
            current.concept_code
            and incoming_code
            and current.concept_code != incoming_code
        ):
            self._warn_once(
                code,
                f"concept:{incoming_code}",
                "%s: %r resolved to concept %r but code %r already carries %r. "
                "Keeping the first. Two accepted concepts for one code means one "
                "of them is wrong, and an index is the wrong place to pick.",
                self.label, source, incoming_code, code, current.concept_code,
            )
            return current

        updates = {
            name: value
            for name, value in fields.items()
            if name in current.as_dict() and value is not None
        }
        if not updates:
            return current

        entry = replace(current, **updates)
        self._store(entry)

        if not current.has_concept and entry.has_concept:
            self._stats["with_concept"] += 1
        return entry

    def _store(self, entry: DimensionEntry) -> None:
        """Write an entry into the code map."""
        self._by_code[entry.code] = entry

    def _bind(self, raw: str, code: str) -> str:
        """Bind a raw spelling to a code without creating anything."""
        value = _as_text(raw).strip()
        if not value:
            return code
        key = self._make_key(value)
        existing = self._by_key.get(key)
        if existing is not None and existing != code:
            logger.debug(
                "%s: %r already bound to %r; not rebinding to %r",
                self.label, value, existing, code,
            )
            return existing
        self._by_key[key] = code
        return code

    def _note_blocked(self, code: str, value: str, reason: str) -> None:
        """Record that a concept was withheld, once per code and reason."""
        self._warn_once(
            code,
            f"withheld:{reason}",
            "%s: %r matched a concept but it was not attached because %s. "
            "The match is in fact_resolution_log for review; code %r keeps no "
            "concept.",
            self.label, value, reason, code,
        )

    def _note_collision(self, code: str, existing: DimensionEntry,
                        key: str, value: str) -> None:
        """Record that two distinct raws derived one code."""
        self._warn_once(
            code,
            f"collision:{key}",
            "%s: %r and %r both derive code %r under policy %s. Merging them "
            "into one entry. If they are different things, this policy is wrong "
            "for this column; if they are the same, this merge is the intended "
            "outcome.",
            self.label, existing.raw, value, code, self.code_policy,
        )

    def _warn_once(self, code: str, reason: str, message: str,
                   *args: Any) -> None:
        """Emit a warning the first time (code, reason) is seen."""
        marker = (code, reason)
        if marker in self._warned:
            return
        self._warned.add(marker)
        logger.warning(message, *args)


def seed_from_rows(index: DimensionKeyIndex, rows: Iterable[Mapping[str, Any]],
                   *, code_field: str = "code",
                   raw_field: Optional[str] = None,
                   pk_field: Optional[str] = None,
                   concept_code_field: Optional[str] = None,
                   concept_id_field: Optional[str] = None,
                   standard_code_field: Optional[str] = None,
                   concept_name_field: Optional[str] = None) -> int:
    """Register every row with a non-empty code. Returns rows registered."""
    registered = 0
    for row in rows:
        code = row.get(code_field)
        if not _as_text(code).strip():
            continue
        index.register(
            code,
            raw=row.get(raw_field) if raw_field else None,
            pk=row.get(pk_field) if pk_field else None,
            concept_code=row.get(concept_code_field) if concept_code_field else None,
            concept_name=row.get(concept_name_field) if concept_name_field else None,
            concept_id=row.get(concept_id_field) if concept_id_field else None,
            standard_code=row.get(standard_code_field) if standard_code_field else None,
        )
        registered += 1
    return registered


__all__ = [
    "AUTO_ACCEPTED",
    "CODE_POLICIES",
    "CODE_POLICY_CONCEPT",
    "CODE_POLICY_IDENTITY",
    "CODE_POLICY_SLUG",
    "DimensionEntry",
    "DimensionKeyIndex",
    "ORIGIN_ASSIGNED",
    "ORIGIN_SEEDED",
    "seed_from_rows",
]