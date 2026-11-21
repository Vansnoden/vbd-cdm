# -*- coding: utf-8 -*-
"""
The vocabulary registry: which packs exist, at which release, ranked how.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import yaml
from sqlalchemy import text

from utilities.vocabulary import normalizers
from utilities.vocabulary.field_catalog import (
    DOMAIN_TO_DIM_TABLE,
    FieldNotInCatalog,
    resolve_field,
)

logger = logging.getLogger(__name__)


REGISTRY_VERSION = "2026.11.1"

MANIFEST_FILENAME = "manifest.yaml"

APPLIES_WHEN_KEYS = frozenset({"countries", "admin_level", "sample_regex"})

REQUIRED_KEYS = (
    "pack_id",
    "version",
    "domain",
    "precedence",
    "specificity",
    "normalizer_chain",
    "fuzzy_threshold",
    "fuzzy_margin",
    "field_scope",
    "released_with",
)

OPTIONAL_DEFAULTS: Mapping[str, Any] = {
    "case_sensitive": False,
    "applies_when": {},
    "canonical_rule": None,
    "layers": (),
    "admin_level_map": {},
    "grid_level": None,
    "resistance_thresholds": None,
}

LAYER_KEYS = frozenset({"level", "layer", "gid_column", "name_columns"})

PARAMETER_IDENTITY_KEYS = frozenset(
    {"pack_id", "version", "released_with", "description"}
)


# =============================================================================
# Errors
# =============================================================================

class RegistryError(Exception):
    """Base class for registry problems."""


class ManifestError(RegistryError):
    """A manifest is malformed or invalid."""


class DuplicatePack(RegistryError):
    """Two manifests claim the same ``pack_id``."""


class PackRankCollision(RegistryError):
    """Two packs in one domain cannot be ordered by ``rank_key``."""


class UnknownPack(RegistryError):
    """A ``pack_id`` that is not loaded."""


# =============================================================================
# Manifest parsing helpers
# =============================================================================

def _require_str(data: Mapping[str, Any], key: str, source: Path) -> str:
    try:
        value = data[key]
    except KeyError:
        raise ManifestError(f"{source}: missing required key {key!r}") from None
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(
            f"{source}: {key!r} must be a non-empty string, got {value!r}"
        )
    return value.strip()


def _optional_str(data: Mapping[str, Any], key: str, source: Path) -> str | None:
    value = data.get(key, OPTIONAL_DEFAULTS.get(key))
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError(f"{source}: {key!r} must be a string, got {value!r}")
    return value


def _require_int(data: Mapping[str, Any], key: str, source: Path) -> int:
    try:
        value = data[key]
    except KeyError:
        raise ManifestError(f"{source}: missing required key {key!r}") from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{source}: {key!r} must be an integer, got {value!r}")
    return int(value)


def _require_float(data: Mapping[str, Any], key: str, source: Path) -> float:
    try:
        value = data[key]
    except KeyError:
        raise ManifestError(f"{source}: missing required key {key!r}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{source}: {key!r} must be a number, got {value!r}")
    return float(value)


def _optional_bool(data: Mapping[str, Any], key: str, source: Path) -> bool:
    value = data.get(key, OPTIONAL_DEFAULTS.get(key))
    if not isinstance(value, bool):
        raise ManifestError(f"{source}: {key!r} must be true or false, got {value!r}")
    return value


def _str_tuple(
    data: Mapping[str, Any], key: str, source: Path, *, required: bool,
) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        if required:
            raise ManifestError(f"{source}: missing required key {key!r}")
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ManifestError(
            f"{source}: {key!r} must be a list of strings, got {value!r}"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ManifestError(
                f"{source}: {key!r} entries must be non-empty strings, got {item!r}"
            )
        out.append(item.strip())
    if required and not out:
        raise ManifestError(f"{source}: {key!r} must not be empty")
    return tuple(out)


@functools.lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    """Compile (and cache) a manifest-declared regular expression."""
    return re.compile(pattern)


# =============================================================================
# Pack-shape detection
# =============================================================================

def is_parameter_manifest(data: Mapping[str, Any]) -> bool:
    """Whether parsed manifest YAML declares values rather than concepts.

    Structural and cheap: a concept pack *must* declare ``domain`` and
    ``field_scope``, so a manifest carrying neither is not one. Requiring
    **both** to be absent — rather than either — means a half-written concept
    manifest is still reported as a malformed concept pack with a useful
    message, instead of being silently reclassified as a parameter pack and
    skipped.
    """
    return "domain" not in data and "field_scope" not in data


# =============================================================================
# Coercion of the batch-4 manifest blocks
# =============================================================================

def _coerce_layers(value: Any, source: Path) -> tuple[Mapping[str, Any], ...]:
    """Validate and normalise ``layers``: plain dicts, sorted by level.

    Sorted here rather than at every reader: the order of a YAML list is an
    authoring accident, and two manifests declaring the same layers in a
    different order must produce the same hierarchy.
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ManifestError(
            f"{source}: `layers` must be a list of mappings, got {value!r}"
        )

    out: list[dict[str, Any]] = []
    seen_levels: set[int] = set()

    for position, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ManifestError(
                f"{source}: layers[{position}] must be a mapping, got {entry!r}"
            )
        unknown = sorted(set(entry) - LAYER_KEYS)
        if unknown:
            raise ManifestError(
                f"{source}: layers[{position}] has unknown key(s): "
                f"{', '.join(unknown)}. Allowed: {', '.join(sorted(LAYER_KEYS))}"
            )

        raw_level = entry.get("level")
        if isinstance(raw_level, bool) or not isinstance(raw_level, int):
            raise ManifestError(
                f"{source}: layers[{position}].level must be an integer, "
                f"got {raw_level!r}"
            )
        if raw_level in seen_levels:
            raise ManifestError(
                f"{source}: layers declares level {raw_level} more than once; "
                f"a level identifies one layer"
            )
        seen_levels.add(raw_level)

        out.append({
            "level": int(raw_level),
            "layer": _require_str(entry, "layer", source),
            "gid_column": _require_str(entry, "gid_column", source),
            "name_columns": _str_tuple(entry, "name_columns", source, required=True),
        })

    out.sort(key=lambda spec: spec["level"])
    return tuple(out)


def _coerce_admin_level_map(value: Any, source: Path) -> Mapping[int, str]:
    """Validate and normalise ``admin_level_map`` to ``{int: str}``, ascending.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"{source}: `admin_level_map` must be a mapping, got {value!r}"
        )

    out: dict[int, str] = {}
    for raw_key, raw_value in value.items():
        try:
            level = int(raw_key)
        except (TypeError, ValueError):
            raise ManifestError(
                f"{source}: admin_level_map key {raw_key!r} is not an integer "
                f"boundary level"
            ) from None
        if level in out:
            raise ManifestError(
                f"{source}: admin_level_map declares level {level} twice"
            )
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ManifestError(
                f"{source}: admin_level_map[{raw_key!r}] must be a non-empty "
                f"string, got {raw_value!r}"
            )
        out[level] = raw_value.strip()

    return dict(sorted(out.items()))


def _coerce_thresholds(value: Any, source: Path) -> Mapping[str, float] | None:
    """Validate a ``resistance_thresholds`` block: ``{str: number}``.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"{source}: `resistance_thresholds` must be a mapping, got {value!r}"
        )
    if not value:
        raise ManifestError(
            f"{source}: `resistance_thresholds` must not be empty; omit the key "
            f"instead"
        )

    out: dict[str, float] = {}
    for key, raw in value.items():
        name = str(key).strip()
        if not name:
            raise ManifestError(
                f"{source}: resistance_thresholds has an empty key"
            )
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ManifestError(
                f"{source}: resistance_thresholds[{name!r}] must be a number, "
                f"got {raw!r}"
            )
        number = float(raw)
        if not (0.0 <= number <= 100.0):
            raise ManifestError(
                f"{source}: resistance_thresholds[{name!r}] must be a "
                f"percentage in [0, 100], got {number}"
            )
        out[name] = number

    if "susceptible" in out and "suspected" in out:
        if out["suspected"] > out["susceptible"]:
            raise ManifestError(
                f"{source}: resistance_thresholds.suspected "
                f"({out['suspected']}) exceeds susceptible "
                f"({out['susceptible']}); the bands would be inverted"
            )

    return dict(sorted(out.items()))


# =============================================================================
# ConceptPack
# =============================================================================

@dataclass(frozen=True)
class ConceptPack:
    """One vocabulary release, as declared by its manifest.
    """

    pack_id: str
    version: str
    domain: str
    precedence: int
    specificity: int
    normalizer_chain: tuple[str, ...]
    fuzzy_threshold: float
    fuzzy_margin: float
    case_sensitive: bool
    field_scope: tuple[str, ...]
    applies_when: Mapping[str, Any] = field(default_factory=dict, compare=False)
    canonical_rule: str | None = None
    released_with: str = ""

    layers: tuple[Mapping[str, Any], ...] = field(default=(), compare=False)
    admin_level_map: Mapping[int, str] = field(default_factory=dict, compare=False)
    grid_level: str | None = None
    resistance_thresholds: Mapping[str, float] | None = field(
        default=None, compare=False
    )

    # -- identity --------------------------------------------------------

    @property
    def vocabulary_id(self) -> str:
        """Version-qualified id used as ``dim_vocabulary.vocabulary_id``.

        Qualifying the id is what lets two releases coexist in ``dim_concept``
        and ``fact_concept_map`` with no schema change.
        """
        return f"{self.pack_id}@{self.version}"

    def rank_key(self) -> tuple[int, int, str]:
        """``(precedence, -specificity, pack_id)``, per the contract.

        Higher ``precedence`` wins; among equal precedence the more specific
        pack wins (hence the negation); ``pack_id`` makes the order total, so
        the winner is never decided by dict or set ordering.
        """
        return (self.precedence, -self.specificity, self.pack_id)

    # -- behaviour -------------------------------------------------------

    def normalizer(self):
        """The composed normaliser for this pack's chain."""
        return _composed(self.normalizer_chain)

    def applies(
        self, sample: Any = None, context: Mapping[str, Any] | None = None
    ) -> bool:
        """Whether this pack's ``applies_when`` predicates hold.

        An empty ``applies_when`` means "always". Predicates are evaluated in
        sorted key order so a pack with two failing predicates reports the same
        diagnostic every time.
        """
        rules = dict(self.applies_when or {})
        if not rules:
            return True

        ctx = dict(context or {})
        for key in sorted(rules):
            expected = rules[key]

            if key == "countries":
                actual = str(
                    ctx.get("country_code") or ctx.get("country") or ""
                ).upper()
                allowed = {str(value).upper() for value in expected}
                if actual not in allowed:
                    return False

            elif key == "admin_level":
                if str(ctx.get("admin_level") or "") != str(expected):
                    return False

            elif key == "sample_regex":
                if sample is None:
                    return False
                if _compiled(str(expected)).search(str(sample)) is None:
                    return False

            else:  # pragma: no cover - rejected at load time
                raise RegistryError(
                    f"pack {self.pack_id!r}: unsupported applies_when key {key!r}"
                )

        return True

    # -- boundary-layout accessors ---------------------------------------

    def admin_level_for(self, level: Any) -> str | None:
        """The CDM ``admin_level`` an integer boundary level maps to.

        ``None`` when the pack declares no mapping for it — a legitimate answer:
        a caller then falls back to whatever the row already carries rather than
        inventing a label.
        """
        try:
            key = int(level)
        except (TypeError, ValueError):
            return None
        return self.admin_level_map.get(key)

    def layer_at(self, level: int) -> Mapping[str, Any] | None:
        """The layer spec declared for ``level``, or None."""
        for spec in self.layers:
            if int(spec["level"]) == int(level):
                return spec
        return None

    def deepest_level(self) -> int | None:
        """The highest integer level declared, or None."""
        if not self.layers:
            return None
        return max(int(spec["level"]) for spec in self.layers)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_manifest(cls, data: Mapping[str, Any], source: Path) -> "ConceptPack":
        """Build a pack from parsed YAML, validating every field."""
        if not isinstance(data, Mapping):
            raise ManifestError(f"{source}: manifest must be a mapping")

        unknown = sorted(set(data) - set(REQUIRED_KEYS) - set(OPTIONAL_DEFAULTS))
        if unknown:
            raise ManifestError(
                f"{source}: unknown manifest key(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(REQUIRED_KEYS + tuple(OPTIONAL_DEFAULTS))}"
            )

        pack_id = _require_str(data, "pack_id", source)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.\-]*", pack_id):
            raise ManifestError(
                f"{source}: pack_id {pack_id!r} must be lower-case "
                f"alphanumerics with '_', '.', or '-'"
            )

        version = _require_str(data, "version", source)
        domain = _require_str(data, "domain", source)
        precedence = _require_int(data, "precedence", source)
        specificity = _require_int(data, "specificity", source)
        chain = _str_tuple(data, "normalizer_chain", source, required=True)
        threshold = _require_float(data, "fuzzy_threshold", source)
        margin = _require_float(data, "fuzzy_margin", source)
        case_sensitive = _optional_bool(data, "case_sensitive", source)
        scope = _str_tuple(data, "field_scope", source, required=True)
        released_with = _require_str(data, "released_with", source)

        # -- normaliser chain must be resolvable now, not at match time --
        for name in chain:
            if name not in normalizers.NORMALIZERS:
                raise ManifestError(
                    f"{source}: unknown normalizer {name!r} in normalizer_chain; "
                    f"registered ids: {', '.join(normalizers.available())}"
                )

        # -- thresholds ---------------------------------------------------
        if not (0.0 < threshold <= 1.0):
            raise ManifestError(
                f"{source}: fuzzy_threshold must be in (0, 1], got {threshold}"
            )
        if not (0.0 <= margin < 1.0):
            raise ManifestError(
                f"{source}: fuzzy_margin must be in [0, 1), got {margin}"
            )

        # -- field_scope must be real catalog fields, in this pack's domain
        for target_field in scope:
            try:
                binding = resolve_field(target_field)
            except FieldNotInCatalog as exc:
                raise ManifestError(
                    f"{source}: field_scope entry {target_field!r} is not in "
                    f"FIELD_CATALOG. {exc}"
                ) from None
            if binding.domain != domain:
                raise ManifestError(
                    f"{source}: field {target_field!r} belongs to domain "
                    f"{binding.domain!r} in FIELD_CATALOG, but this pack "
                    f"declares domain {domain!r}"
                )

        if domain not in DOMAIN_TO_DIM_TABLE:
            raise ManifestError(
                f"{source}: domain {domain!r} has no dimension table in "
                f"DOMAIN_TO_DIM_TABLE"
            )

        # -- applies_when must be understood now --------------------------
        applies_when = data.get("applies_when", OPTIONAL_DEFAULTS["applies_when"]) or {}
        if not isinstance(applies_when, Mapping):
            raise ManifestError(
                f"{source}: applies_when must be a mapping, got {applies_when!r}"
            )
        unknown_predicates = sorted(set(applies_when) - APPLIES_WHEN_KEYS)
        if unknown_predicates:
            raise ManifestError(
                f"{source}: unsupported applies_when key(s): "
                f"{', '.join(unknown_predicates)}. "
                f"Supported: {', '.join(sorted(APPLIES_WHEN_KEYS))}"
            )
        if "sample_regex" in applies_when:
            try:
                _compiled(str(applies_when["sample_regex"]))
            except re.error as exc:
                raise ManifestError(
                    f"{source}: sample_regex does not compile: {exc}"
                ) from None
        if "countries" in applies_when and not isinstance(
            applies_when["countries"], (list, tuple)
        ):
            raise ManifestError(
                f"{source}: applies_when.countries must be a list"
            )

        canonical_rule = _optional_str(data, "canonical_rule", source)

        # -- boundary layout (optional, batch 4) --------------------------
        layers = _coerce_layers(data.get("layers"), source)
        admin_level_map = _coerce_admin_level_map(
            data.get("admin_level_map"), source
        )
        grid_level = data.get("grid_level", OPTIONAL_DEFAULTS["grid_level"])
        if grid_level is not None:
            if not isinstance(grid_level, str) or not grid_level.strip():
                raise ManifestError(
                    f"{source}: grid_level must be a non-empty string or "
                    f"omitted, got {grid_level!r}"
                )
            grid_level = grid_level.strip()

        if layers and not admin_level_map:
            raise ManifestError(
                f"{source}: `layers` is declared but `admin_level_map` is "
                f"empty; every layer level needs a dim_location.admin_level"
            )
        if admin_level_map and not layers:
            raise ManifestError(
                f"{source}: `admin_level_map` is declared but `layers` is "
                f"empty; the mapping describes layers that do not exist"
            )
        unmapped = sorted(
            {int(spec["level"]) for spec in layers} - set(admin_level_map)
        )
        if unmapped:
            raise ManifestError(
                f"{source}: layer level(s) {unmapped} have no entry in "
                f"admin_level_map; they would load and then be unlabelled"
            )

        # -- classification policy (optional, batch 4) --------------------
        resistance_thresholds = _coerce_thresholds(
            data.get("resistance_thresholds"), source
        )

        return cls(
            pack_id=pack_id,
            version=version,
            domain=domain,
            precedence=precedence,
            specificity=specificity,
            normalizer_chain=chain,
            fuzzy_threshold=threshold,
            fuzzy_margin=margin,
            case_sensitive=case_sensitive,
            field_scope=scope,
            applies_when=dict(applies_when),
            canonical_rule=canonical_rule,
            released_with=released_with,
            layers=layers,
            admin_level_map=admin_level_map,
            grid_level=grid_level,
            resistance_thresholds=resistance_thresholds,
        )


@functools.lru_cache(maxsize=128)
def _composed(chain: tuple[str, ...]):
    """Cache one composed normaliser per chain tuple."""
    return normalizers.compose(chain)


# =============================================================================
# ParameterPack
# =============================================================================

@dataclass(frozen=True)
class ParameterPack:
    """A manifest that declares values rather than concepts.
    """

    pack_id: str
    version: str
    parameters: Mapping[str, Any] = field(default_factory=dict, compare=False)
    released_with: str = ""
    description: str = ""

    @property
    def vocabulary_id(self) -> str:
        """Version-qualified id, for symmetry with ``ConceptPack``."""
        return f"{self.pack_id}@{self.version}"

    def get(self, key: str, default: Any = None) -> Any:
        """One parameter, or ``default``."""
        return (self.parameters or {}).get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in (self.parameters or {})

    @property
    def keys(self) -> tuple[str, ...]:
        """Declared parameter names, sorted. Deterministic."""
        return tuple(sorted(self.parameters or {}))

    @classmethod
    def from_manifest(
        cls, data: Mapping[str, Any], source: Path
    ) -> "ParameterPack":
        """Build a parameter pack from parsed YAML.

        Far less validation than a ``ConceptPack`` gets, because there is far
        less to validate: no domain, no field scope, no thresholds, no
        normaliser chain. What *is* checked is what the registry needs in order
        to store and rank the pack — an id and a version — plus the requirement
        that there be at least one parameter. An "empty" parameter pack is a
        manifest someone forgot to fill in, and it would silently answer every
        question with ``None``.
        """
        if not isinstance(data, Mapping):
            raise ManifestError(f"{source}: manifest must be a mapping")

        pack_id = _require_str(data, "pack_id", source)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.\-]*", pack_id):
            raise ManifestError(
                f"{source}: pack_id {pack_id!r} must be lower-case "
                f"alphanumerics with '_', '.', or '-'"
            )
        version = _require_str(data, "version", source)

        parameters = {
            key: value
            for key, value in data.items()
            if key not in PARAMETER_IDENTITY_KEYS
        }
        if not parameters:
            raise ManifestError(
                f"{source}: parameter pack {pack_id!r} declares no parameters. "
                f"Give it at least one top-level key besides "
                f"{', '.join(sorted(PARAMETER_IDENTITY_KEYS))}, or delete it."
            )

        description = data.get("description") or ""
        if not isinstance(description, str):
            raise ManifestError(
                f"{source}: description must be a string, got {description!r}"
            )

        if "resistance_thresholds" in parameters:
            _coerce_thresholds(parameters["resistance_thresholds"], source)

        return cls(
            pack_id=pack_id,
            version=version,
            parameters=parameters,
            released_with=str(data.get("released_with") or ""),
            description=description.strip(),
        )


# =============================================================================
# MergePolicy
# =============================================================================

@dataclass(frozen=True)
class MergePolicy:
    """How several packs covering one field combine.

    Returned by :meth:`VocabularyRegistry.merge_policies`. This is the declared
    answer to "what happens when two vocabularies govern one field", so the
    resolver does not have to invent one.
    """

    target_field: str
    domain: str
    primary: str                    # winning pack_id
    fallback: tuple[str, ...]       # the rest, in rank order
    fuzzy_threshold: float          # the winner's threshold
    fuzzy_margin: float             # the most conservative margin in the field
    case_sensitive: bool            # the winner's setting
    requires_agreement: bool        # True when more than one pack covers it


# =============================================================================
# VocabularyRegistry
# =============================================================================

class VocabularyRegistry:
    """The set of active packs, plus every query the resolver needs."""

    def __init__(self) -> None:
        self._packs: dict[str, ConceptPack] = {}
        self._parameter_packs: dict[str, ParameterPack] = {}
        self._manifests: dict[str, Path] = {}
        self._checksums: dict[str, str] = {}

    # -- construction ----------------------------------------------------

    @classmethod
    def load_from(
        cls, path: str | Path | Iterable[str | Path]
    ) -> "VocabularyRegistry":
        """Load every manifest under ``path`` (or the given explicit paths).
        """
        registry = cls()
        manifests = list(_iter_manifests(path))
        if not manifests:
            raise ManifestError(f"no {MANIFEST_FILENAME} found under {path!r}")

        for manifest in manifests:
            registry._load_manifest(manifest)

        registry._validate_ranking()
        return registry

    def _load_manifest(self, path: Path) -> None:
        """Parse one manifest and store it as whichever kind it turns out to be.

        Routing happens here rather than in ``load_from`` so both kinds are
        discovered by the same recursive walk, share the same duplicate and
        checksum bookkeeping, and appear in the same sorted order in every
        diagnostic. A manifest with ``domain`` or ``field_scope`` is *tried* as
        a concept pack, so its error names the concept-pack key that is wrong.
        """
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ManifestError(f"cannot read manifest {path}: {exc}") from exc

        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ManifestError(
                f"{path}: manifest is not valid UTF-8: {exc}"
            ) from exc
        except yaml.YAMLError as exc:
            raise ManifestError(f"{path}: invalid YAML: {exc}") from exc

        if not isinstance(data, Mapping):
            raise ManifestError(f"{path}: manifest must be a YAML mapping")

        digest = hashlib.sha256(raw).hexdigest()

        if is_parameter_manifest(data):
            parameter_pack = ParameterPack.from_manifest(data, path)
            if (
                parameter_pack.pack_id in self._packs
                or parameter_pack.pack_id in self._parameter_packs
            ):
                raise DuplicatePack(
                    f"duplicate pack_id {parameter_pack.pack_id!r}: "
                    f"{path} and {self._manifests.get(parameter_pack.pack_id)}"
                )
            self._parameter_packs[parameter_pack.pack_id] = parameter_pack
            self._manifests[parameter_pack.pack_id] = path
            self._checksums[parameter_pack.pack_id] = digest
            return

        pack = ConceptPack.from_manifest(data, path)
        if pack.pack_id in self._packs or pack.pack_id in self._parameter_packs:
            raise DuplicatePack(
                f"duplicate pack_id {pack.pack_id!r}: "
                f"{path} and {self._manifests[pack.pack_id]}"
            )
        self._packs[pack.pack_id] = pack
        self._manifests[pack.pack_id] = path
        self._checksums[pack.pack_id] = digest

    def _validate_ranking(self) -> None:
        """Reject unordered packs; warn about string-tiebreaker decisions."""
        by_domain: dict[str, dict[tuple[int, int, str], str]] = {}
        by_domain_prefix: dict[str, dict[tuple[int, int], list[str]]] = {}

        for pack in self._ordered():
            rank = pack.rank_key()
            seen = by_domain.setdefault(pack.domain, {})
            if rank in seen:
                raise PackRankCollision(
                    f"packs {seen[rank]!r} and {pack.pack_id!r} share domain "
                    f"{pack.domain!r} and an identical rank_key {rank}. "
                    f"There is no deterministic order between them; change one "
                    f"pack's precedence or specificity."
                )
            seen[rank] = pack.pack_id

            prefix = (rank[0], rank[1])
            by_domain_prefix.setdefault(pack.domain, {}).setdefault(
                prefix, []
            ).append(pack.pack_id)

        for domain in sorted(by_domain_prefix):
            for prefix in sorted(by_domain_prefix[domain], reverse=True):
                members = sorted(by_domain_prefix[domain][prefix])
                if len(members) < 2:
                    continue
                winner = max(
                    (self._packs[pack_id] for pack_id in members),
                    key=ConceptPack.rank_key,
                )
                _log_warning(
                    f"domain {domain!r}: packs {members} share precedence/"
                    f"specificity {prefix}; ordering falls back to pack_id, "
                    f"which selects {winner.pack_id!r}. Set precedence or "
                    f"specificity explicitly if that is not the intent."
                )

    # -- queries ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._packs)

    def __contains__(self, pack_id: object) -> bool:
        return pack_id in self._packs

    def __iter__(self) -> Iterator[ConceptPack]:
        return self._ordered()

    def _ordered(self) -> Iterator[ConceptPack]:
        """Concept packs in sorted ``pack_id`` order. The only iteration order."""
        for pack_id in sorted(self._packs):
            yield self._packs[pack_id]

    def get(self, pack_id: str) -> ConceptPack:
        """Return a concept pack by id, raising :class:`UnknownPack`."""
        try:
            return self._packs[pack_id]
        except KeyError:
            raise UnknownPack(
                f"unknown pack_id {pack_id!r}; loaded packs: "
                f"{', '.join(sorted(self._packs)) or '(none)'}"
            ) from None

    def packs(self) -> tuple[ConceptPack, ...]:
        """All active concept packs, sorted by ``pack_id``."""
        return tuple(self._ordered())

    def active_version(self, pack_id: str) -> str | None:
        """The release string of ``pack_id``, or None if it is not loaded."""
        pack = self._packs.get(pack_id)
        return None if pack is None else pack.version

    # -- parameter packs -------------------------------------------------

    def parameter_packs(self) -> tuple[ParameterPack, ...]:
        """Every parameter pack, sorted by ``pack_id``. Deterministic."""
        return tuple(
            self._parameter_packs[pack_id]
            for pack_id in sorted(self._parameter_packs)
        )

    def parameter_pack(self, pack_id: str) -> ParameterPack:
        """One parameter pack by id, raising :class:`UnknownPack`."""
        try:
            return self._parameter_packs[pack_id]
        except KeyError:
            raise UnknownPack(
                f"unknown parameter pack_id {pack_id!r}; loaded parameter "
                f"packs: {', '.join(sorted(self._parameter_packs)) or '(none)'}"
            ) from None

    def parameters(self, pack_id: str) -> Mapping[str, Any]:
        """The parameter payload of one pack, by id."""
        return dict(self.parameter_pack(pack_id).parameters or {})

    def find_parameter_pack(self, key: str) -> ParameterPack | None:
        """The unique parameter pack declaring ``key``, or None.

        This lookup keeps policy out of code: a caller states the parameter it
        needs (``"resistance_thresholds"``) rather than the pack that carries
        it, so shipping a second classification scheme is a new manifest and no
        code change.

        Ambiguity is an error rather than a silent pick. Two packs declaring one
        parameter means the tool cannot say which policy a decision was made
        under, which is the provenance failure the registry exists to prevent.
        """
        matches = [
            pack
            for pack in self.parameter_packs()
            if key in (pack.parameters or {})
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise RegistryError(
                f"parameter {key!r} is declared by more than one pack "
                f"({', '.join(pack.pack_id for pack in matches)}). A value that "
                f"decides a stored classification must have exactly one source; "
                f"rename one of the keys or remove one of the packs."
            )
        return matches[0]

    def require_parameter_pack(self, key: str) -> ParameterPack:
        """Like :meth:`find_parameter_pack`, but raises when absent.

        For callers where a missing parameter is a packaging defect rather than
        a legitimate "not configured" state: a shipped manifest that a shipped
        function depends on.
        """
        pack = self.find_parameter_pack(key)
        if pack is None:
            raise RegistryError(
                f"no parameter pack declares {key!r}. Shipped packs: "
                f"{', '.join(sorted(self._parameter_packs)) or '(none)'}. "
                f"Expected a manifest under vocabularies/registry/*/manifest.yaml "
                f"carrying a top-level {key!r} block."
            )
        return pack

    # -- provenance ------------------------------------------------------

    def digest(self) -> str:
        """SHA-256 over the sorted ``(pack_id, version)`` pairs.

        The payload is exactly two components per pack, per the contract
        Amendment: a digest recorded by an earlier tool release must stay
        comparable, and adding a third component would make every recorded value
        mean something different.

        Both kinds are included. A run that classified a bioassay under a
        different set of cut-offs must not produce the same digest as one that
        did not, or the digest stops being evidence of which knowledge produced
        the numbers. An installation with no parameter packs digests exactly as
        it did before they existed, because the pair format is unchanged.

        Layout-independent: the same packs in a different directory tree produce
        the same digest.
        """
        pairs = sorted(
            [(pack.pack_id, pack.version) for pack in self._ordered()]
            + [
                (pack.pack_id, pack.version)
                for pack in self.parameter_packs()
            ]
        )
        payload = "\n".join(f"{pack_id}\t{version}" for pack_id, version in pairs)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def digest_with_kind(self) -> str:
        """SHA-256 over ``(pack_id, version, kind)`` triples.

        The discriminator :meth:`digest` deliberately omits. Use it when the
        *kind* is what changed — a manifest moving from concept pack to
        parameter pack keeps its id and version but means something different,
        and only this digest records that.

        Never record this where a pre-existing digest was recorded
        (``RunContext.registry_digest``): the two are different namespaces and
        comparing them is meaningless.
        """
        triples = sorted(
            [(pack.pack_id, pack.version, "concept") for pack in self._ordered()]
            + [
                (pack.pack_id, pack.version, "parameter")
                for pack in self.parameter_packs()
            ]
        )
        payload = "\n".join(
            f"{pack_id}\t{version}\t{kind}" for pack_id, version, kind in triples
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def manifest_checksums(self) -> dict[str, str]:
        """``pack_id -> SHA-256 of its manifest bytes``, sorted by id."""
        return {pack_id: self._checksums[pack_id] for pack_id in sorted(self._checksums)}

    # -- selection -------------------------------------------------------

    def select_pack(
        self,
        field: str,
        sample: Any = None,
        context: Mapping[str, Any] | None = None,
    ) -> ConceptPack | None:
        """Return the pack that governs ``field``, or None if none does.
        """
        resolve_field(field)  # validates the name; raises FieldNotInCatalog

        candidates = [pack for pack in self._ordered() if field in pack.field_scope]
        if not candidates:
            return None

        eligible = [pack for pack in candidates if pack.applies(sample, context)]
        pool = eligible or candidates
        return max(pool, key=ConceptPack.rank_key)

    def field_bindings(self) -> dict[str, tuple[ConceptPack, ...]]:
        """``field -> packs in rank order (winner first)``; fields sorted."""
        bindings: dict[str, tuple[ConceptPack, ...]] = {}
        for field_name in sorted(self._catalog_fields()):
            candidates = [
                pack for pack in self._ordered() if field_name in pack.field_scope
            ]
            if not candidates:
                continue
            candidates.sort(key=ConceptPack.rank_key, reverse=True)
            bindings[field_name] = tuple(candidates)
        return bindings

    def merge_policies(self) -> dict[str, MergePolicy]:
        """Per-field policy for combining several packs (or one)."""
        policies: dict[str, MergePolicy] = {}
        for field_name, packs in self.field_bindings().items():
            primary = packs[0]
            policies[field_name] = MergePolicy(
                target_field=field_name,
                domain=primary.domain,
                primary=primary.pack_id,
                fallback=tuple(pack.pack_id for pack in packs[1:]),
                fuzzy_threshold=primary.fuzzy_threshold,
                fuzzy_margin=max(pack.fuzzy_margin for pack in packs),
                case_sensitive=primary.case_sensitive,
                requires_agreement=len(packs) > 1,
            )
        return policies

    def _catalog_fields(self) -> tuple[str, ...]:
        """Every field claimed by at least one concept pack, sorted."""
        fields: set[str] = set()
        for pack in self._ordered():
            fields.update(pack.field_scope)
        return tuple(sorted(fields))

    # -- persistence -----------------------------------------------------

    def load_into(self, session) -> dict[str, str]:
        """Upsert one ``dim_vocabulary`` row per concept pack.
        """
        from utilities.vocabulary.loader import (
            CHECKSUM_ARTEFACT_MANIFEST,
            checksum_artefact_json,
            dim_vocabulary_supports_extra,
        )

        supports_extra = dim_vocabulary_supports_extra(session)
        artefact = checksum_artefact_json(CHECKSUM_ARTEFACT_MANIFEST)

        columns = [
            "vocabulary_id", "vocabulary_name", "source_url", "version",
            "description", "pack_id", "checksum", "released_with",
        ]
        values = [
            ":vocabulary_id", ":vocabulary_name", ":source_url", ":version",
            ":description", ":pack_id", ":checksum", ":released_with",
        ]
        assignments = [
            "vocabulary_name = EXCLUDED.vocabulary_name",
            "version         = EXCLUDED.version",
            "description     = EXCLUDED.description",
            "pack_id         = EXCLUDED.pack_id",
            "checksum        = EXCLUDED.checksum",
            "released_with   = EXCLUDED.released_with",
        ]
        if supports_extra:
            columns.append("extra")
            values.append(":extra")
            assignments.append("extra           = EXCLUDED.extra")

        statement = text(
            f"INSERT INTO dim_vocabulary ({', '.join(columns)}) "
            f"VALUES ({', '.join(values)}) "
            f"ON CONFLICT (vocabulary_id) DO UPDATE SET "
            + ", ".join(assignments)
        )

        written: dict[str, str] = {}
        for pack in self._ordered():
            params: dict[str, Any] = {
                "vocabulary_id": pack.vocabulary_id,
                "vocabulary_name": f"{pack.pack_id} {pack.domain} (v{pack.version})",
                "source_url": None,
                "version": pack.version,
                "description": "fields: " + ", ".join(pack.field_scope),
                "pack_id": pack.pack_id,
                "checksum": self._checksums[pack.pack_id],
                "released_with": pack.released_with or None,
            }
            if supports_extra:
                params["extra"] = artefact
            session.execute(statement, params)
            written[pack.pack_id] = pack.vocabulary_id

        session.commit()

        logger.info(
            "recorded %d pack(s) in dim_vocabulary (digest %s, "
            "checksum_artefact=%r recorded in extra: %s)",
            len(written), self.digest()[:12], CHECKSUM_ARTEFACT_MANIFEST,
            supports_extra,
        )
        return written

    # -- diagnostics -----------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """A deterministic snapshot for logging and run manifests."""
        return {
            "registry_version": REGISTRY_VERSION,
            "digest": self.digest(),
            "digest_with_kind": self.digest_with_kind(),
            "packs": [
                {
                    "pack_id": pack.pack_id,
                    "version": pack.version,
                    "vocabulary_id": pack.vocabulary_id,
                    "domain": pack.domain,
                    "rank_key": list(pack.rank_key()),
                    "released_with": pack.released_with,
                    "checksum": self._checksums[pack.pack_id],
                    "field_scope": list(pack.field_scope),
                    "normalizer_chain": list(pack.normalizer_chain),
                    "layer_levels": [int(spec["level"]) for spec in pack.layers],
                    "grid_level": pack.grid_level,
                    "resistance_thresholds": (
                        None
                        if pack.resistance_thresholds is None
                        else dict(pack.resistance_thresholds)
                    ),
                }
                for pack in self._ordered()
            ],
            "parameter_packs": [
                {
                    "pack_id": pack.pack_id,
                    "version": pack.version,
                    "vocabulary_id": pack.vocabulary_id,
                    "released_with": pack.released_with,
                    "checksum": self._checksums[pack.pack_id],
                    "parameters": list(pack.keys),
                }
                for pack in self.parameter_packs()
            ],
        }


# =============================================================================
# Path handling
# =============================================================================

def _iter_manifests(path: str | Path | Iterable[str | Path]) -> Iterator[Path]:
    """Yield manifest paths in a deterministic order.

    Recursive discovery is sorted by the POSIX form of the path, so load order
    never depends on the filesystem's directory order.
    """
    seen: set[Path] = set()

    def _emit(candidate: Path) -> Iterator[Path]:
        if candidate in seen:
            return
        seen.add(candidate)
        yield candidate

    def _one(item: Path) -> Iterator[Path]:
        if item.is_dir():
            found = sorted(
                (candidate for candidate in item.rglob(MANIFEST_FILENAME) if candidate.is_file()),
                key=lambda candidate: candidate.as_posix(),
            )
            for manifest in found:
                yield from _emit(manifest)
        elif item.is_file():
            yield from _emit(item)
        else:
            raise ManifestError(f"registry path does not exist: {item}")

    if isinstance(path, (str, Path)):
        yield from _one(Path(path))
        return

    for item in path:
        yield from _one(Path(item))


def _log_warning(message: str) -> None:
    """Emit a warning through the project logger.

    Wrapped because a registry warning must never be able to break loading: an
    unconfigured handler is not a reason to refuse to start.
    """
    try:
        logger.warning(message)
    except Exception:  # pragma: no cover - logging must never break loading
        pass


__all__ = [
    "APPLIES_WHEN_KEYS",
    "ConceptPack",
    "DuplicatePack",
    "LAYER_KEYS",
    "MANIFEST_FILENAME",
    "ManifestError",
    "MergePolicy",
    "OPTIONAL_DEFAULTS",
    "PARAMETER_IDENTITY_KEYS",
    "PackRankCollision",
    "ParameterPack",
    "REGISTRY_VERSION",
    "REQUIRED_KEYS",
    "RegistryError",
    "UnknownPack",
    "VocabularyRegistry",
    "is_parameter_manifest",
]