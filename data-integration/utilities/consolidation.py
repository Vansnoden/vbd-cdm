# -*- coding: utf-8 -*-
"""Post-ingestion consolidation.
"""

from __future__ import annotations
import hashlib
import calendar
import difflib
import logging
import re
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set,
                    Tuple)

import numpy as np
import pandas as pd
from sqlalchemy import Integer, bindparam, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.constants import COUNTRY_LOOKUP
from database.geoutils import (ISO_COUNTRY_CODES, ISO_TO_COUNTRY_NAME,
                               get_country_from_coords, get_country_name,
                               get_iso_country_code, haversine_distance)
from database.models import (DimDisease, DimIdentificationMethod, DimInsecticide,
                             DimLocation, DimResistanceTestType,
                             DimSocioeconomicIndicator, DimSource, DimVector,
                             FactBionomics, FactDiseaseBurden, FactDiseaseCase,
                             FactEnvironment, FactHealthFacility,
                             FactInsecticideResistance, FactIntervention,
                             FactSocioeconomicCondition, FactVectorObservation)
from database.session import Base, SessionLocal, require_identifier
from etl.transformers.concept_resolver import (AUTO_ACCEPTED, METHOD_ALIAS,
                                               METHOD_AMBIGUOUS, METHOD_EXACT,
                                               METHOD_FUZZY, METHOD_NORMALIZED,
                                               NEEDS_REVIEW, RESISTANCE_BANDS,
                                               UNRESOLVED, ConceptResolver,
                                               Resolution)
from utilities.vocabulary import normalizers as _normalizers
from utilities.vocabulary.alignment import (align_dimension_values,
                                            align_fact_values,
                                            alignment_report,
                                            load_shipped_vocabularies)
from utilities.vocabulary.field_catalog import (can_stamp, dimension_fields,
                                                iter_bindings, resolve_field)
from utilities.vocabulary.registry import VocabularyRegistry
from utilities.run_manifest import record_parameters as _record_manifest_parameters

logger = logging.getLogger(__name__)
MAX_IN_VARS = 900

_GRID_NAME_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?\s*$")
_NUMERIC_TOKEN_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")
_NAME_SEPARATOR_RE = re.compile(r"[/\\|]")
_GID_LIKE_RE = re.compile(r"^(?:GID_\d+_[A-Z]{3}|[A-Z]{3}\.\d+(?:\.\d+)*_\d+)$")
_GID_COLUMN_RE = re.compile(r"^GID_(\d+)$", re.IGNORECASE)
_NAME_COLUMN_RE = re.compile(r"^NAME_(\d+)$", re.IGNORECASE)

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "vocabularies" / "registry"

_REGISTRY_CACHE: dict = {}
_WARNED: set = set()
_LEGACY_ALIGNMENT_WARNED: set = set()
_FACT_REFERENCE_CACHE: Dict[str, Tuple[Tuple[str, str], ...]] = {}


def _warn_once(marker: str, message: str, *args: Any) -> None:
    if marker in _WARNED:
        return
    _WARNED.add(marker)
    logger.warning(message, *args)


def _warn_legacy_alignment() -> None:
    """LEGACY: no score, no concept-map row, no reviewable trace."""
    if _LEGACY_ALIGNMENT_WARNED:
        return
    _LEGACY_ALIGNMENT_WARNED.add("align_fact_foreign_keys")
    logger.warning("_align_fact_foreign_keys is LEGACY (review C12): substring "
                   "match, no score, no fact_concept_map row.")


def _load_registry(registry: Any = None) -> Optional[VocabularyRegistry]:
    if registry is not None:
        return registry
    key = str(DEFAULT_REGISTRY_PATH)
    cached = _REGISTRY_CACHE.get(key)
    if cached is not None:
        return cached
    if not DEFAULT_REGISTRY_PATH.is_dir():
        _warn_once("registry:missing",
                   "no vocabulary registry at %s; standardisation skipped.",
                   DEFAULT_REGISTRY_PATH)
        return None
    loaded = VocabularyRegistry.load_from(DEFAULT_REGISTRY_PATH)
    _REGISTRY_CACHE[key] = loaded
    return loaded


def _model_for_table(table_name: str):
    from database import models as _models
    for attribute in sorted(vars(_models)):
        candidate = getattr(_models, attribute)
        if getattr(candidate, "__tablename__", None) == table_name:
            return candidate
    return None


def _dimension_models() -> Tuple[Any, ...]:
    """Every dim_* model with a primary key, sorted by table name."""
    from database import models as _models
    found: Dict[str, Any] = {}
    for attribute in sorted(vars(_models)):
        candidate = getattr(_models, attribute)
        name = getattr(candidate, "__tablename__", None)
        if not name or not str(name).startswith("dim_"):
            continue
        table = getattr(candidate, "__table__", None)
        if table is None or not list(table.primary_key.columns):
            continue
        found[str(name)] = candidate
    return tuple(found[n] for n in sorted(found))


def _dimension_fact_map(model_class) -> Tuple[Tuple[str, str], ...]:
    """((fact_table, fk_column), ...) read from Base.metadata.

    A hand-written map omitted dim_resistance_test_type, so B5 deleted merged rows
    that fact_insecticide_resistance still referenced.
    """
    table_name = getattr(model_class, "__tablename__", None)
    if not table_name:
        return ()
    cached = _FACT_REFERENCE_CACHE.get(str(table_name))
    if cached is not None:
        return cached
    table = getattr(model_class, "__table__", None)
    if table is None:
        return ()
    keys = {c.name for c in table.primary_key.columns}
    pairs: List[Tuple[str, str]] = []
    for candidate in Base.metadata.tables.values():
        if not candidate.name.startswith("fact_"):
            continue
        for column in candidate.columns:
            for foreign_key in column.foreign_keys:
                try:
                    target = foreign_key.column
                except Exception:  # noqa: BLE001
                    continue
                if target is None or target.table.name != str(table_name):
                    continue
                if target.name in keys:
                    pairs.append((candidate.name, column.name))
    ordered = tuple(sorted(set(pairs)))
    _FACT_REFERENCE_CACHE[str(table_name)] = ordered
    return ordered


def _default_vocab_key(value: Any) -> str:
    """Trim/lower/deaccent. Weak on purpose: never folds synonyms."""
    if value is None:
        return ""
    text_value = str(value).strip()
    if not text_value:
        return ""
    try:
        decomposed = _normalizers.NORMALIZERS["generic"](text_value)
    except Exception:  # noqa: BLE001
        decomposed = text_value.lower()
    return re.sub(r"\s+", " ", decomposed).strip()


def _pack_key_fn(pack: Any):
    chain = tuple(getattr(pack, "normalizer_chain", ()) or ())
    label = getattr(pack, "pack_id", "?")
    try:
        composed = _normalizers.compose(chain)
    except Exception as exc:  # noqa: BLE001
        _warn_once(f"compose:{label}",
                   "pack %r: normalizer_chain %s not composable (%s: %s); using "
                   "the generic key.", label, list(chain), type(exc).__name__, exc)
        return _default_vocab_key

    def _guarded(value: Any) -> str:
        try:
            return composed(value)
        except Exception as exc:  # noqa: BLE001
            _warn_once(f"normalise:{label}:{type(exc).__name__}",
                       "pack %r: normaliser raised %s: %s; using the generic key.",
                       label, type(exc).__name__, exc)
            return _default_vocab_key(value)

    return _guarded


# -- names and similarity --------------------------------------------------

def _looks_like_coordinate(value: Any) -> bool:
    text_value = str(value or "").strip()
    return bool(text_value) and bool(_NUMERIC_TOKEN_RE.match(text_value))


def _name_tokens(value: Any) -> List[str]:
    text_value = str(value or "").strip()
    if not text_value:
        return []
    return [t.strip() for t in _NAME_SEPARATOR_RE.split(text_value) if t.strip()]


def _is_human_label(value: Any) -> bool:
    """Has a letter, is not a number, is not a boundary GID."""
    text_value = str(value or "").strip()
    if not text_value or not any(ch.isalpha() for ch in text_value):
        return False
    if _NUMERIC_TOKEN_RE.match(text_value) or _GID_LIKE_RE.match(text_value.upper()):
        return False
    return True


def _title_name(token: Any) -> str:
    """Recase only uniformly-cased tokens (str.title() breaks "Côte d'Ivoire")."""
    text_value = re.sub(r"\s+", " ", str(token or "").strip())
    if not text_value:
        return ""
    return text_value.title() if (text_value.islower() or text_value.isupper()) \
        else text_value


def _looks_generated_name(name: Any, location_code: Any = None) -> bool:
    text_name = str(name or "").strip()
    if not text_name or _GRID_NAME_RE.match(text_name):
        return True
    text_code = str(location_code or "").strip()
    if text_code and text_name == text_code:
        return True
    tokens = _name_tokens(text_name)
    return len(tokens) >= 2 and any(_looks_like_coordinate(t) for t in tokens)


def _similarity_key(value: Any) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    try:
        text_value = _normalizers.NORMALIZERS["generic"](text_value)
    except Exception:  # noqa: BLE001
        text_value = text_value.lower()
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", text_value)).strip()


def _name_similarity(left: Any, right: Any) -> float:
    """max(equality, Jaccard, containment, difflib ratio). Evaluation only."""
    a, b = _similarity_key(left), _similarity_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta = {t for t in a.split() if t}
    tb = {t for t in b.split() if t}
    jaccard = containment = 0.0
    if ta and tb:
        shared = ta & tb
        jaccard = len(shared) / len(ta | tb)
        if ta <= tb or tb <= ta:
            smaller = min(len(ta), len(tb))
            containment = len(shared) / smaller if smaller else 0.0
    return max(difflib.SequenceMatcher(None, a, b).ratio(), jaccard, containment)


def _identity_tokens(value: Any, country_tokens: Sequence[Any] = ()) -> List[str]:
    tokens = _name_tokens(value)
    if not tokens:
        return []
    drop = {str(t).strip().casefold() for t in country_tokens if str(t).strip()}
    words = [t for t in tokens if not _looks_like_coordinate(t)]
    usable = [w for w in words if w.casefold() not in drop] or words
    return [_title_name(w) for w in usable if _is_human_label(w)]


def _best_identity_token(location_code: Any, name: Any,
                         country_tokens: Sequence[Any] = ()) -> Optional[str]:
    for candidate in (location_code, name):
        tokens = _identity_tokens(candidate, country_tokens)
        if tokens:
            return tokens[-1]
    return None


def _approx_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    try:
        mean_lat = np.radians((float(lat1) + float(lat2)) / 2.0)
        dlat = (float(lat1) - float(lat2)) * 111.32
        dlon = (float(lon1) - float(lon2)) * 111.32 * np.cos(mean_lat)
        return float(np.hypot(dlat, dlon))
    except (TypeError, ValueError):
        return float("inf")


# -- country standardisation (ISO 3166-1 alpha-2 + canonical label) --------

_COUNTRY_BY_ISO2: Dict[str, str] = {}
_COUNTRY_ALIAS_TO_ISO2: Dict[str, str] = {}
_COUNTRY_TABLES_READY = False
_COUNTRY_NAME_PREFIXES = ("the ", "republic of ", "state of ", "kingdom of ")


def _country_label(value: Any) -> str:
    text_value = re.sub(r"\s+", " ", str(value or "").strip())
    if not text_value or not any(ch.isalpha() for ch in text_value):
        return ""
    if _NUMERIC_TOKEN_RE.match(text_value) or _GID_LIKE_RE.match(text_value.upper()):
        return ""
    if len(text_value) <= 3 and text_value.isalpha():
        return ""
    return _title_name(text_value)


def _confirmed_iso2(value: Any) -> Optional[str]:
    """The value as ISO2, or None. Round-tripped so a bad pair cannot pass."""
    candidate = str(value or "").strip().upper()
    if not (len(candidate) == 2 and candidate.isalpha()):
        return None
    try:
        resolved = str(get_iso_country_code(candidate) or "").strip().upper()
    except Exception:  # noqa: BLE001
        return candidate
    return None if (resolved and resolved != candidate) else candidate


def _build_country_tables() -> None:
    """Populate iso2->name and alias->iso2 from the geoutils tables, once.

    Read by inspection: the shipped tables have held iso2->name, iso3->iso2 and
    name->iso2 at different times, so each pair is taken whichever way it fits.
    """
    global _COUNTRY_TABLES_READY
    if _COUNTRY_TABLES_READY:
        return
    _COUNTRY_TABLES_READY = True

    pairs: List[Tuple[Any, Any]] = []
    for mapping in (ISO_COUNTRY_CODES, ISO_TO_COUNTRY_NAME):
        try:
            pairs.extend(list(dict(mapping or {}).items()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read an ISO country table (%s: %s)",
                           type(exc).__name__, exc)

    for key, value in pairs:
        key_text = str(key).strip()
        value_text = "" if value is None else str(value).strip()
        for code_side, label_side in ((key_text, value_text),
                                      (value_text, key_text)):
            iso2 = _confirmed_iso2(code_side)
            if iso2 is None:
                continue
            label = _country_label(label_side)
            if label:
                _COUNTRY_BY_ISO2.setdefault(iso2, label)

    for iso2, label in _COUNTRY_BY_ISO2.items():
        _COUNTRY_ALIAS_TO_ISO2.setdefault(iso2, iso2)
        alias = _similarity_key(label)
        if alias:
            _COUNTRY_ALIAS_TO_ISO2.setdefault(alias, iso2)

    for key, value in pairs:
        key_text = str(key).strip()
        value_text = "" if value is None else str(value).strip()
        iso2 = _confirmed_iso2(value_text) or _confirmed_iso2(key_text)
        if iso2 is None:
            continue
        label = _country_label(key_text if iso2 != key_text.upper() else value_text)
        if label:
            alias = _similarity_key(label)
            if alias:
                _COUNTRY_ALIAS_TO_ISO2.setdefault(alias, iso2)
        other = key_text.upper()
        if len(other) == 3 and other.isalpha():
            _COUNTRY_ALIAS_TO_ISO2.setdefault(other, iso2)


def _iso3_prefix_iso2(code: str) -> Optional[str]:
    """ISO2 for a 3-letter code that prefixes its country name (KEN -> KE).

    Not a loose match: "NER" is not a prefix of "NIGER", so an unrelated code
    stays unresolved rather than landing on a neighbour.
    """
    target = str(code or "").strip().upper()
    if not (len(target) == 3 and target.isalpha()):
        return None
    matches = [iso2 for iso2, label in _COUNTRY_BY_ISO2.items()
               if _similarity_key(label).replace(" ", "").upper().startswith(target)]
    return matches[0] if len(matches) == 1 else None


def _country_iso2(raw: Any) -> Optional[str]:
    """ISO2 for a code or name. Never a bare substring match on a name."""
    if raw is None:
        return None
    text_value = re.sub(r"\s+", " ", str(raw).strip())
    if not text_value:
        return None
    _build_country_tables()

    upper = text_value.upper()
    if upper in _COUNTRY_ALIAS_TO_ISO2:
        return _COUNTRY_ALIAS_TO_ISO2[upper]
    key = _similarity_key(text_value)
    if not key:
        return None
    if key in _COUNTRY_ALIAS_TO_ISO2:
        return _COUNTRY_ALIAS_TO_ISO2[key]
    stripped = key
    for prefix in _COUNTRY_NAME_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
    if stripped in _COUNTRY_ALIAS_TO_ISO2:
        return _COUNTRY_ALIAS_TO_ISO2[stripped]

    prefixed = _iso3_prefix_iso2(upper)
    if prefixed:
        return prefixed
    if len(upper) == 3 and upper.isalpha():
        try:
            candidate = str(get_iso_country_code(upper) or "").strip().upper()
        except Exception:  # noqa: BLE001
            candidate = ""
        if candidate in _COUNTRY_BY_ISO2:
            return candidate
    return None


def _country_name_for(iso2: Any) -> Optional[str]:
    if iso2 is None:
        return None
    _build_country_tables()
    return _COUNTRY_BY_ISO2.get(str(iso2).strip().upper())


def _canonical_country(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    iso2 = _country_iso2(raw)
    if iso2 is None:
        return None, None
    return iso2, _country_name_for(iso2) or iso2


# -- boundary geometry -----------------------------------------------------

@dataclass(frozen=True)
class _BoundaryUnit:
    level: int
    gid: str
    name: str
    geometry: Any


class _BoundaryIndex:
    """Point-in-polygon over administrative layers; STRtree when available."""

    def __init__(self, units: Sequence[_BoundaryUnit], label: str = "") -> None:
        self.units = list(units)
        self.label = label
        self.levels = sorted({unit.level for unit in self.units})
        self._tree = None
        self._geometry_positions: Dict[int, int] = {}
        self._bounds: List[Tuple[Tuple[float, float, float, float], int]] = []
        self._build_index()

    def _build_index(self) -> None:
        if not self.units:
            return
        try:
            from shapely.strtree import STRtree
            geometries = [unit.geometry for unit in self.units]
            self._tree = STRtree(geometries)
            self._geometry_positions = {id(g): i for i, g in enumerate(geometries)}
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("no STRtree for %s (%s: %s); scanning bounding boxes",
                         self.label, type(exc).__name__, exc)
            self._tree = None
        for position, unit in enumerate(self.units):
            try:
                self._bounds.append((unit.geometry.bounds, position))
            except Exception:  # noqa: BLE001
                continue

    def _candidate_positions(self, point) -> List[int]:
        if self._tree is not None:
            try:
                matching = self._tree.query(point)
            except Exception:  # noqa: BLE001
                return list(range(len(self.units)))
            positions: List[int] = []
            for item in matching:
                if isinstance(item, (int, np.integer)):
                    positions.append(int(item))
                    continue
                position = self._geometry_positions.get(id(item))
                if position is not None:
                    positions.append(position)
            return positions
        x, y = point.x, point.y
        return [p for bounds, p in self._bounds
                if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]]

    def containing(self, lat: Any, lon: Any) -> List[_BoundaryUnit]:
        """Units holding the point, deepest first. touches() counts."""
        if not self.units:
            return []
        try:
            from shapely.geometry import Point
            point = Point(float(lon), float(lat))
        except Exception:  # noqa: BLE001
            return []
        if not np.isfinite(point.x) or not np.isfinite(point.y):
            return []
        found: List[_BoundaryUnit] = []
        for position in self._candidate_positions(point):
            if position >= len(self.units):
                continue
            unit = self.units[position]
            try:
                if unit.geometry.contains(point) or unit.geometry.touches(point):
                    found.append(unit)
            except Exception:  # noqa: BLE001
                continue
        found.sort(key=lambda unit: -unit.level)
        return found

    def nearest(self, lat: Any, lon: Any) -> Tuple[Optional[_BoundaryUnit], float]:
        if not self.units:
            return None, float("inf")
        try:
            from shapely.geometry import Point
            point = Point(float(lon), float(lat))
        except Exception:  # noqa: BLE001
            return None, float("inf")
        best: Optional[_BoundaryUnit] = None
        best_degrees = float("inf")
        for unit in self.units:
            try:
                distance = unit.geometry.distance(point)
            except Exception:  # noqa: BLE001
                continue
            if distance < best_degrees:
                best, best_degrees = unit, distance
        if best is None:
            return None, float("inf")
        return best, float(best_degrees) * 111.32


def _list_boundary_layers(path: Path) -> List[Optional[str]]:
    names: List[str] = []
    try:
        import fiona
        names = [str(n) for n in fiona.listlayers(str(path))]
    except Exception:  # noqa: BLE001
        try:
            import geopandas as gpd
            names = [str(n) for n in gpd.list_layers(str(path)).iloc[:, 0].tolist()]
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not list layers in %s: %s: %s",
                         path, type(exc).__name__, exc)
            return [None]

    def _level(name: str) -> int:
        match = re.search(r"_(\d+)$", name)
        return int(match.group(1)) if match else 99

    return sorted(names, key=_level) or [None]


def _load_boundary_index(path: Any, *, layer: Optional[str] = None,
                         precision: Optional[float] = 1e-5) -> _BoundaryIndex:
    """Read every GID_n/NAME_n layer of a boundary file into an index."""
    candidate = Path(str(path))
    if not candidate.exists():
        raise FileNotFoundError(f"boundary file not found: {candidate}")
    try:
        import geopandas as gpd
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("coordinate naming needs geopandas and shapely; "
                           "`pip install geopandas shapely`") from exc

    units: List[_BoundaryUnit] = []
    for layer_name in ([layer] if layer else _list_boundary_layers(candidate)):
        try:
            frame = gpd.read_file(candidate, layer=layer_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("  boundary: could not read layer %r from %s: %s: %s",
                           layer_name, candidate, type(exc).__name__, exc)
            continue
        if frame is None or frame.empty:
            continue
        gid_column = next((str(c) for c in frame.columns
                           if _GID_COLUMN_RE.match(str(c))), None)
        if gid_column is None:
            logger.debug("  boundary: layer %r has no GID_n column; skipped",
                         layer_name)
            continue
        level = int(_GID_COLUMN_RE.match(gid_column).group(1))
        name_column = None
        for column in frame.columns:
            match = _NAME_COLUMN_RE.match(str(column))
            if match and int(match.group(1)) == level:
                name_column = str(column)
                break
        geometry_column = "geometry"
        geometry_attribute = getattr(frame, "geometry", None)
        if geometry_attribute is not None:
            geometry_column = geometry_attribute.name
        added = 0
        for record in frame.itertuples(index=False):
            values = record._asdict()
            geometry = values.get(geometry_column)
            if geometry is None:
                continue
            try:
                if geometry.is_empty:
                    continue
                if precision is not None:
                    geometry = geometry.simplify(precision, preserve_topology=True)
            except Exception:  # noqa: BLE001
                continue
            gid = str(values.get(gid_column) or "").strip()
            if not gid:
                continue
            label = _title_name(values.get(name_column)) if name_column else ""
            units.append(_BoundaryUnit(level=level, gid=gid, name=label,
                                       geometry=geometry))
            added += 1
        logger.debug("  boundary: layer %r -> %d unit(s) at level %d",
                     layer_name, added, level)

    if not units:
        raise RuntimeError(
            f"{candidate} holds no layer with a GID_n/NAME_n column pair.")
    return _BoundaryIndex(units, label=str(candidate))


def _choose_boundary_unit(units: Sequence[_BoundaryUnit], identity: Optional[str],
                          threshold: float) -> Tuple[Optional[_BoundaryUnit],
                                                     float, str]:
    """Deepest human-named containing unit, unless the source token outvotes it."""
    human = [u for u in units if _is_human_label(u.name)]
    if not human:
        return None, 0.0, "no_human_unit"
    human.sort(key=lambda u: -u.level)
    deepest = human[0]
    if not identity:
        return deepest, 0.0, "deepest_unit"
    scored = [(u, _name_similarity(identity, u.name)) for u in human]
    best_unit, best_score = max(scored, key=lambda p: (p[1], p[0].level))
    deepest_score = _name_similarity(identity, deepest.name)
    if best_score >= 0.995:
        return best_unit, best_score, "identity_confirmed"
    if best_score >= threshold > deepest_score:
        return best_unit, best_score, "identity_preferred"
    return deepest, deepest_score, "deepest_unit"


# -- canonical rules -------------------------------------------------------

def _rule_tokens(pack: Any) -> tuple:
    raw = getattr(pack, "canonical_rule", None)
    if not raw:
        return ()
    return tuple(t for t in re.split(r"[,\s]+", str(raw).strip()) if t)


def _rule_require_spatial_or_human(pack: Any, resolution: "Resolution") -> bool:
    return False


def _rule_never_collapse_hybrids(pack: Any, resolution: "Resolution") -> bool:
    return resolution.match_method != METHOD_FUZZY


_CANONICAL_MATCH_RULES = {
    "require_spatial_or_human": _rule_require_spatial_or_human,
    "never_collapse_hybrids": _rule_never_collapse_hybrids}


def _bonus_genus_prefix(name: str) -> int:
    return 10_000 if str(name or "").strip().lower().startswith("anopheles") else 0


_CANONICAL_FORM_BONUSES = {"prefer_genus_prefix": _bonus_genus_prefix}


def _may_apply_concept(pack: Any, resolution: "Resolution") -> bool:
    for token in _rule_tokens(pack):
        predicate = _CANONICAL_MATCH_RULES.get(token)
        if predicate is not None:
            if not predicate(pack, resolution):
                return False
            continue
        if token in _CANONICAL_FORM_BONUSES:
            continue
        _warn_once(f"rule:{token}",
                   "pack %r declares canonical_rule %r, unimplemented; nothing "
                   "governed by it is applied automatically.",
                   getattr(pack, "pack_id", "?"), token)
        return False
    return True


def _effective_decision(pack: Any, resolution: "Resolution") -> str:
    decision = resolution.decision
    if decision == AUTO_ACCEPTED and not _may_apply_concept(pack, resolution):
        return NEEDS_REVIEW
    return decision


# -- resistance thresholds -------------------------------------------------

RESISTANCE_THRESHOLDS_KEY = "resistance_thresholds"
_SUSCEPTIBLE_KEY = "susceptible"
_SUSPECTED_KEY = "suspected"


def _resistance_thresholds(registry=None) -> Tuple[float, float]:
    """(susceptible, suspected) percentages from the parameter pack; never defaulted."""
    from utilities.vocabulary.registry import RegistryError
    loaded = _load_registry(registry)
    if loaded is None:
        raise RuntimeError("cannot classify resistance: no registry loadable. "
                           "Cut-offs live in who-resistance/manifest.yaml.")
    try:
        pack = loaded.require_parameter_pack(RESISTANCE_THRESHOLDS_KEY)
    except RegistryError as exc:
        raise RuntimeError(f"cannot classify resistance: {exc}") from exc
    block = pack.parameters.get(RESISTANCE_THRESHOLDS_KEY)
    if not isinstance(block, dict):
        raise RuntimeError(f"parameter pack {pack.pack_id!r} declares "
                           f"{RESISTANCE_THRESHOLDS_KEY!r} as "
                           f"{type(block).__name__}; expected a mapping.")
    missing = [k for k in (_SUSCEPTIBLE_KEY, _SUSPECTED_KEY) if k not in block]
    if missing:
        raise RuntimeError(f"parameter pack {pack.pack_id!r} is missing {missing} "
                           f"from its {RESISTANCE_THRESHOLDS_KEY!r} block.")
    try:
        susceptible = float(block[_SUSCEPTIBLE_KEY])
        suspected = float(block[_SUSPECTED_KEY])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"parameter pack {pack.pack_id!r} has a non-numeric "
                           f"{RESISTANCE_THRESHOLDS_KEY!r} value: {block!r}") from exc
    if not (0.0 <= suspected <= susceptible <= 100.0):
        raise RuntimeError(f"parameter pack {pack.pack_id!r} declares an "
                           f"impossible band: {susceptible!r}/{suspected!r}.")
    return susceptible, suspected


BURDEN_KERNEL_KEY = "burden_expansion"
_BURDEN_KERNEL_DEFAULTS: Dict[str, Any] = {
    "intra_month_amplitude": 0.0,
    "base_variation": 0.0,
    "spike_probability": 0.0,
    "spike_mean_excess": 0.5,
}

_HOLIDAY_KEY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


@dataclass(frozen=True, eq=False)
class _BurdenKernel:
    """Weights that distribute an annual total across the days of a year.
    """

    monthly: Mapping[int, float]
    weekly: Mapping[int, float]
    holidays: Mapping[str, float]
    intra_amplitude: float
    base_variation: float
    spike_probability: float
    spike_mean_excess: float
    origin: str
    pack_id: Optional[str] = None
    version: Optional[str] = None
    source: Optional[str] = None

    @property
    def is_flat(self) -> bool:
        """True when allocation is uniform and the seed cannot matter."""
        return (not self.monthly and not self.weekly and not self.holidays
                and self.intra_amplitude <= 0 and self.base_variation <= 0
                and self.spike_probability <= 0)

    def describe(self) -> Dict[str, Any]:
        """A JSON-serialisable summary for the run manifest."""
        return {
            "origin": self.origin,
            "pack_id": self.pack_id,
            "version": self.version,
            "source": self.source,
            "flat": self.is_flat,
            "intra_month_amplitude": self.intra_amplitude,
            "base_variation": self.base_variation,
            "spike_probability": self.spike_probability,
            "monthly": dict(sorted(self.monthly.items())),
            "weekly": dict(sorted(self.weekly.items())),
            "holidays": dict(sorted(self.holidays.items())),
        }


FLAT_BURDEN_KERNEL = _BurdenKernel(
    monthly={}, weekly={}, holidays={}, intra_amplitude=0.0,
    base_variation=0.0, spike_probability=0.0, spike_mean_excess=0.5,
    origin="flat")


def _kernel_float(value: Any, default: float) -> float:
    """A non-negative float, or ``default`` when unusable. Never raises."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _kernel_month_map(value: Any) -> Dict[int, float]:
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, (list, tuple)):
        items = enumerate(value, start=1)
    else:
        return {}
    out: Dict[int, float] = {}
    for raw_key, raw_value in items:
        try:
            month = int(raw_key)
        except (TypeError, ValueError):
            continue
        if not 1 <= month <= 12:
            continue
        weight = _kernel_float(raw_value, -1.0)
        if weight > 0:
            out[month] = weight
    return out


def _kernel_weekday_map(value: Any) -> Dict[int, float]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[int, float] = {}
    for raw_key, raw_value in value.items():
        try:
            day = int(raw_key)
        except (TypeError, ValueError):
            continue
        if not 1 <= day <= 7:
            continue
        weight = _kernel_float(raw_value, -1.0)
        if weight > 0:
            out[day] = weight
    return out


def _kernel_holiday_map(value: Any) -> Dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not _HOLIDAY_KEY_RE.match(key):
            continue
        weight = _kernel_float(raw_value, -1.0)
        if weight > 0:
            out[key] = weight
    return out


def load_burden_kernel(registry: Any = None) -> _BurdenKernel:
    """The allocation policy for this run.
    """
    loaded = _load_registry(registry)
    if loaded is None:
        logger.info("  burden kernel: no registry, allocating uniformly "
                    "(origin=flat).")
        return FLAT_BURDEN_KERNEL

    try:
        pack = loaded.find_parameter_pack(BURDEN_KERNEL_KEY)
    except Exception as exc:  # noqa: BLE001 - RegistryError on an ambiguous key
        logger.warning("  burden kernel: cannot resolve %r (%s: %s); allocating "
                       "uniformly.", BURDEN_KERNEL_KEY, type(exc).__name__, exc)
        return FLAT_BURDEN_KERNEL

    if pack is None:
        logger.info("  burden kernel: no %r parameter pack, allocating uniformly "
                    "(origin=flat). Ship "
                    "vocabularies/registry/burden-expansion/manifest.yaml to "
                    "declare a seasonal policy.", BURDEN_KERNEL_KEY)
        return FLAT_BURDEN_KERNEL

    block = pack.parameters.get(BURDEN_KERNEL_KEY)
    if block is None:
        block = {key: pack.parameters.get(key)
                 for key in ("seasonality", "weekly_pattern", "holidays",
                             "holiday_factors", "intra_month_amplitude",
                             "base_variation", "spike_probability",
                             "spike_mean_excess")}
    if not isinstance(block, Mapping):
        logger.warning("  burden kernel: pack %r declares %r as %s, not a "
                       "mapping; allocating uniformly.", pack.pack_id,
                       BURDEN_KERNEL_KEY, type(block).__name__)
        return FLAT_BURDEN_KERNEL

    monthly = _kernel_month_map(block.get("seasonality"))
    if not monthly:
        logger.warning("  burden kernel: pack %r declares no usable "
                       "`seasonality` block; allocating uniformly.",
                       pack.pack_id)
        return FLAT_BURDEN_KERNEL

    kernel = _BurdenKernel(
        monthly=monthly,
        weekly=_kernel_weekday_map(block.get("weekly_pattern")),
        holidays=_kernel_holiday_map(
            block.get("holidays") or block.get("holiday_factors")),
        intra_amplitude=_kernel_float(
            block.get("intra_month_amplitude"),
            _BURDEN_KERNEL_DEFAULTS["intra_month_amplitude"]),
        base_variation=_kernel_float(
            block.get("base_variation"),
            _BURDEN_KERNEL_DEFAULTS["base_variation"]),
        spike_probability=_kernel_float(
            block.get("spike_probability"),
            _BURDEN_KERNEL_DEFAULTS["spike_probability"]),
        spike_mean_excess=_kernel_float(
            block.get("spike_mean_excess"),
            _BURDEN_KERNEL_DEFAULTS["spike_mean_excess"]),
        origin=f"pack:{pack.vocabulary_id}",
        pack_id=pack.pack_id,
        version=pack.version,
        source=(str(pack.parameters.get("source") or "").strip() or None),
    )
    logger.info("  burden kernel: %s (%d month(s), %d weekday(s), %d holiday(s), "
                "variation %.3f%s)", kernel.origin, len(kernel.monthly),
                len(kernel.weekly), len(kernel.holidays), kernel.base_variation,
                "" if kernel.source else "; NO SOURCE CITED - a seasonal claim "
                "from this series would be unsupported")
    return kernel


def _stable_location_key(location_code: Any, name: Any = None) -> int:
    """A 32-bit key derived from a location's own identifier, not its surrogate.

    The generator seeds on this rather than on ``location_id`` because the
    surrogate is assigned by insertion order. Rebuilding the CDM from the same
    sources can give a cell a different ``location_id``, and a series seeded on
    the surrogate would then differ cell-by-cell between two databases that are
    conceptually identical - which makes a golden snapshot meaningless.

    ``location_code`` first, because it is the row's own identity; ``name`` as a
    fallback, because a survey site may carry a name and no code. Both absent
    yields 0, which gives that row the same series in every database.
    """
    for candidate in (location_code, name):
        text_value = str(candidate or "").strip()
        if text_value:
            digest = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
            return int(digest[:8], 16)
    return 0


def _burden_generate_daily_case_counts(
    annual_cases: float, year: int, location_id: int,
    base_variation: Optional[float] = None, seed: Optional[int] = None,
    *,
    kernel: Optional[_BurdenKernel] = None,
    stable_key: Optional[int] = None,
) -> pd.DataFrame:
    """Daily series whose sum equals ``round(annual_cases)``, exactly.

    Weights come from ``kernel``; the multiplicative deviation and any spike come
    from a generator seeded on ``(seed, stable_key, year)``. The guarantee is
    arithmetic rather than statistical: weights are normalised, truncated to
    integers, and the residual distributed by largest remainder, so the daily
    column sums to the rounded annual total whatever the weights are.
    """
    active = kernel or FLAT_BURDEN_KERNEL
    variation = (active.base_variation if base_variation is None
                 else float(base_variation))

    key = int(location_id if stable_key is None else stable_key)
    rng = np.random.default_rng(
        np.random.SeedSequence([0 if seed is None else int(seed), key, int(year)]))

    date_range = pd.date_range(datetime(year, 1, 1), datetime(year, 12, 31))
    weights: List[float] = []
    for date in date_range:
        month = date.month
        days_in_month = calendar.monthrange(year, month)[1]

        weight = float(active.monthly.get(month, 1.0))
        weight *= float(active.weekly.get(date.isocalendar()[2], 1.0))

        if active.intra_amplitude > 0:
            phase = (date.day / max(days_in_month, 1)) * 2 * np.pi
            weight *= 1.0 + active.intra_amplitude * np.sin(phase + month * 0.5)

        weight *= float(active.holidays.get(date.strftime("%m-%d"), 1.0))

        if variation > 0:
            weight *= float(rng.lognormal(0.0, variation * 0.7))

        if active.spike_probability > 0 and rng.random() < active.spike_probability:
            weight *= 1.0 + float(rng.exponential(active.spike_mean_excess))

        weights.append(weight)

    total = sum(weights)
    shares = ([weight / total for weight in weights] if total > 0
              else [1.0 / len(date_range)] * len(date_range))

    target = int(round(max(0.0, float(annual_cases))))
    raw = [annual_cases * share for share in shares]
    counts = [int(value) for value in raw]

    remainder = target - sum(counts)
    if remainder > 0:
        order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - counts[i]), i))
        for i in order[:remainder]:
            counts[i] += 1
    elif remainder < 0:
        order = sorted(range(len(raw)), key=lambda i: (raw[i] - counts[i], i))
        for i in order[:abs(remainder)]:
            if counts[i] > 0:
                counts[i] -= 1

    return pd.DataFrame({
        "date_id": [date.strftime("%Y-%m-%d") for date in date_range],
        "year": year,
        "month": [date.month for date in date_range],
        "case_count": counts,
        "location_id": location_id,
    })

# -- fact aggregation spec -------------------------------------------------

_FACT_AGGREGATION_SPEC: Dict[str, Dict[str, Any]] = {
    "fact_vector_observation": {
        "natural_key": ["location_id", "date_id", "vector_code", "source_code",
                        "life_stage", "sex", "collection_method"],
        "sums": ["count"],
        "means": ["density_per_trap", "infection_rate"],
        "first": ["tested_for_pathogen", "infection_status"]},
    "fact_insecticide_resistance": {
        "natural_key": ["location_id", "date_id", "vector_code", "insecticide_code",
                        "test_type_code", "source_code", "concentration"],
        "sums": ["n_insect_tested", "n_insect_susceptible", "n_insect_resistant",
                 "n_insect_dead", "vgsc_n_insect_tested",
                 "vgsc995l_vgsc995l_n", "vgsc995l_vgsc995f_n",
                 "vgsc995f_vgsc995f_n", "vgsc995l_vgsc995s_n",
                 "vgsc995s_vgsc995s_n", "vgsc995f_vgsc995s_n"],
        "means": [], "first": ["exposure_time", "vgsc_generation"],
        "derived": ["mortality", "resistance_status"]},
    "fact_bionomics": {
        "natural_key": ["location_id", "date_id", "vector_code", "source_code"],
        "sums": [], "means": ["biting_rate"],
        "first": ["feeding_preference", "biting_location", "resting_behavior",
                  "larval_site_type"]},
    "fact_environment": {
        "natural_key": ["location_id", "date_id", "variable"],
        "sums": [], "means": ["value"], "first": ["source_code"]},
    "fact_disease_case": {
        "natural_key": ["location_id", "date_id_onset", "disease_code",
                        "identification_method_code", "source_code",
                        "case_status", "age_group", "sex"],
        "sums": ["case_count"], "means": [],
        "first": ["date_id_reported", "host_type", "outcome", "age_years"]},
    "fact_disease_burden": {
        "natural_key": ["location_id", "year", "disease_code", "pathogen_species"],
        "sums": ["cases_mean"],
        "means": ["incidence_rate", "prevalence_rate", "mortality_rate"],
        "first": ["source_code"]},
    "fact_intervention": {
        "natural_key": ["location_id", "date_start", "target_disease_code",
                        "source_code", "intervention_type"],
        "sums": [], "means": ["coverage"], "first": ["date_end"]},
    "fact_socioeconomic_condition": {
        "natural_key": ["location_id", "year", "indicator_code"],
        "sums": [], "means": ["indicator_value"], "first": ["source_code"]}}


# -- module helpers --------------------------------------------------------

def enrich_location_dataframe(dim_df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing country_code/country_name from centroids, in canonical form."""
    if dim_df is None or dim_df.empty:
        return dim_df
    if "centroid_lat" not in dim_df.columns or "centroid_lon" not in dim_df.columns:
        return dim_df
    frame = dim_df.copy()
    for column in ("country_code", "country_name"):
        if column not in frame.columns:
            frame[column] = None
    needs = frame["country_code"].isna() | (
        frame["country_code"].astype(str).str.strip() == "")
    if not needs.any():
        return frame
    filled = 0
    for index in frame.index[needs]:
        lat, lon = frame.at[index, "centroid_lat"], frame.at[index, "centroid_lon"]
        if pd.isna(lat) or pd.isna(lon):
            continue
        try:
            name, code = get_country_from_coords(float(lat), float(lon))
        except Exception as exc:  
            logger.debug("country lookup failed for (%s, %s): %s", lat, lon, exc)
            continue
        iso2, canonical = _canonical_country(code) if code else (None, None)
        if iso2 is None:
            iso2, canonical = _canonical_country(name)
        code = iso2 or code
        if code:
            frame.at[index, "country_code"] = code
            frame.at[index, "country_name"] = canonical or name
            filled += 1
    if filled:
        logger.info("  filled country_code for %d location row(s)", filled)
    return frame


def _chunked_in(session, base_sql: str, static_params: dict, values,
                expanding_name: str = "vals", chunk_size: int = MAX_IN_VARS):
    values = list(values)
    if not values:
        return []
    stmt = text(base_sql).bindparams(bindparam(expanding_name, expanding=True))
    out = []
    for i in range(0, len(values), chunk_size):
        params = dict(static_params)
        params[expanding_name] = values[i:i + chunk_size]
        out.extend(session.execute(stmt, params).fetchall())
    return out


def _or_where(pairs: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    clauses, params = [], {}
    for name, value in pairs.items():
        if value is None:
            clauses.append(f"{name} IS NULL")
        else:
            params[name] = value
            clauses.append(f"{name} = :{name}")
    return (" AND ".join(clauses) or "1=1"), params


def _nearest_index(glat, glon, alat, alon, chunk: int = 2000):
    n = len(glat)
    out = np.zeros(n, dtype=int)
    if n == 0 or len(alat) == 0:
        return out
    glat = np.asarray(glat, dtype=float)
    glon = np.asarray(glon, dtype=float)
    alat = np.asarray(alat, dtype=float)
    alon = np.asarray(alon, dtype=float)
    scale = float(np.cos(np.radians(np.mean(np.concatenate([glat, alat])))))
    glon_s, alon_s = glon * scale, alon * scale
    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        d2 = ((glat[start:end, None] - alat[None, :]) ** 2
              + (glon_s[start:end, None] - alon_s[None, :]) ** 2)
        out[start:end] = np.argmin(d2, axis=1)
    return out


_BURDEN_LOG_SAMPLE = 10

_BURDEN_MEASURE_CASES_MEAN = "cases_mean"
_BURDEN_MEASURE_INCIDENCE_RATE = "incidence_rate"
_BURDEN_MEASURE_PREFERENCES = (_BURDEN_MEASURE_CASES_MEAN,
                               _BURDEN_MEASURE_INCIDENCE_RATE)

_RATE_SCALES: Dict[str, float] = {"per_100k": 100_000.0, "per_person": 1.0}


def _positive(value: Any) -> Optional[float]:
    """``float(value)`` when it is a usable positive number, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True)
class _PopulationHit:
    """Where a record's denominator came from, so the log can name it."""

    value: float
    tier: str
    year: Optional[int]

    @property
    def is_country_level(self) -> bool:
        """True when the value describes a whole country, not this location."""
        return self.tier.startswith("country")


class _PopulationIndex:

    def __init__(self, session, indicator: str = "SP.POP.TOTL") -> None:
        self.session = session
        self.indicator = str(indicator).strip() or "SP.POP.TOTL"
        self._series: Dict[int, Dict[int, float]] = {}
        self._country_series: Dict[str, Dict[int, float]] = {}
        self.country_of: Dict[int, str] = {}
        self.single_country: Optional[str] = None
        self.location_rows = 0
        self.series_locations = 0
        self.country_series_level: Dict[str, Dict[str, Any]] = {}

    _LEVEL_RANK: Mapping[str, int] = {
        "country": 0, "nation": 0,
        "admin1": 1, "region": 1, "province": 1, "county": 1, "state": 1,
        "admin2": 2, "district": 2,
        "admin3": 3, "ward": 3, "division": 3,
        "admin4": 4, "admin5": 5, "admin6": 6,
        "site": 7, "occurrence": 8, "grid": 9, "cell": 9, "pixel": 9,
    }

    @classmethod
    def _rank(cls, label: Any) -> int:
        return cls._LEVEL_RANK.get(str(label or "").strip().lower(), 5)

    def load(self) -> "_PopulationIndex":
        """Read the population rows and the location->country map. Idempotent.

        Builds two indexes from one location read:
        """
        levels: Dict[int, str] = {}
        for location_id, country_code, admin_level in self.session.execute(text(
            "SELECT location_id, country_code, admin_level FROM dim_location "
            "ORDER BY location_id")).fetchall():
            if location_id is None:
                continue
            key = int(location_id)
            self.country_of[key] = str(country_code or "").strip().upper()
            levels[key] = str(admin_level or "").strip()

        rows = self.session.execute(text(
            "SELECT location_id, year, indicator_value "
            "FROM fact_socioeconomic_condition "
            "WHERE indicator_code = :i "
            "  AND indicator_value IS NOT NULL AND indicator_value > 0 "
            "ORDER BY location_id, year"), {"i": self.indicator}).fetchall()

        for location_id, year, value in rows:
            if location_id is None or year is None:
                continue
            try:
                number = float(value)
                when = int(year)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            self._series.setdefault(int(location_id), {})[when] = number
            self.location_rows += 1

        self.series_locations = len(self._series)

        by_country: Dict[str, Dict[int, List[int]]] = {}
        for location_id in sorted(self._series):
            country = self.country_of.get(location_id) or ""
            if not country:
                continue
            rank = self._rank(levels.get(location_id))
            by_country.setdefault(country, {}).setdefault(rank, []).append(
                location_id)

        for country in sorted(by_country):
            per_rank = by_country[country]
            chosen_rank = min(per_rank)
            members = per_rank[chosen_rank]
            bucket: Dict[int, float] = {}
            for location_id in members:
                for year, value in self._series[location_id].items():
                    bucket[year] = bucket.get(year, 0.0) + value
            if not bucket:
                continue
            self._country_series[country] = bucket
            label = levels.get(members[0]) or "(unlabelled)"
            self.country_series_level[country] = {
                "admin_level": label,
                "locations": len(members),
            }
            if chosen_rank > 0:
                logger.warning(
                    "  population index: no country-level series for %s; the "
                    "country series is the SUM of %d location(s) at %r. Rate "
                    "records will use that total as their denominator, which is "
                    "correct only if those units tile the country.",
                    country, len(members), label)

        countries = [code for code in self._country_series if code]
        self.single_country = countries[0] if len(countries) == 1 else None

        logger.info(
            "  population index: %d row(s) under %r across %d location(s); "
            "country series for %s%s",
            self.location_rows, self.indicator, self.series_locations,
            ", ".join(f"{code}({info['admin_level']}x{info['locations']})"
                      for code, info in sorted(self.country_series_level.items()))
            or "(none)",
            " (single country, used for locations with no country_code)"
            if self.single_country else "")
        return self

    # -- inspection ------------------------------------------------------

    def countries(self) -> Tuple[str, ...]:
        """The country codes that have a series, sorted. Deterministic."""
        return tuple(sorted(code for code in self._country_series if code))

    def country_for(self, location_id: Any) -> Optional[str]:
        """The country a location belongs to, for allocation bookkeeping."""
        try:
            key = int(location_id)
        except (TypeError, ValueError):
            return None
        return self.country_of.get(key) or self.single_country

    # -- lookup ----------------------------------------------------------

    @staticmethod
    def _nearest_year(series: Mapping[int, float],
                      year: Optional[int]) -> Optional[int]:
        """The year closest to ``year``; ties go to the earlier year.

        Earlier on a tie, not later: an epidemiology figure should not be
        silently rounded up to a projection when the actual year is equidistant.
        """
        if not series:
            return None
        if year is None:
            return max(series)
        return min(series, key=lambda candidate: (abs(candidate - year), candidate))

    def resolve(self, location_id: Any, year: Any, *,
                allow_country: bool = False) -> Optional[_PopulationHit]:
        """The denominator for one (location, year), or None.

        """
        try:
            key = int(location_id)
        except (TypeError, ValueError):
            return None
        try:
            when = None if year is None else int(year)
        except (TypeError, ValueError):
            when = None

        series = self._series.get(key)
        if series:
            if when is not None and when in series:
                return _PopulationHit(series[when], "location_year", when)
            nearest = self._nearest_year(series, when)
            if nearest is not None:
                return _PopulationHit(series[nearest],
                                      "location_nearest_year", nearest)

        if not allow_country:
            return None

        country = self.country_for(key)
        country_series = self._country_series.get(country or "")
        if not country_series:
            return None
        if when is not None and when in country_series:
            return _PopulationHit(country_series[when], "country_year", when)
        nearest = self._nearest_year(country_series, when)
        if nearest is None:
            return None
        return _PopulationHit(country_series[nearest],
                              "country_nearest_year", nearest)


# -- DataConsolidator ------------------------------------------------------

class DataConsolidator:
    """Consolidation phases over one CDM session."""

    def __init__(self, session: Optional[Session] = None):
        self.session = session or SessionLocal()
        self._country_lookup = COUNTRY_LOOKUP
        self._closed = False
        self._close_report: Dict[str, Any] = {}
        self._pending_resolutions: List[Any] = []
        self._boundary_index: Optional[_BoundaryIndex] = None
        self._boundary_path: Optional[str] = None
        self._boundary_precision: Optional[float] = None

    def _scalar_or_zero(self, sql: str) -> int:
        try:
            return int(self.session.execute(text(sql)).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("diagnostic count failed (%s): %s", sql, exc)
            return 0

    def _scalar_or_zero_raw(self, sql: str, params: Dict[str, Any]) -> int:
        try:
            return int(self.session.execute(text(sql), params).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("diagnostic count failed (%s): %s", sql, exc)
            return 0

    def _distinct_value_count(self, field: str) -> int:
        try:
            binding = resolve_field(field)
        except Exception:  # noqa: BLE001
            return 0
        column = field.split(".", 1)[1]
        return self._scalar_or_zero(
            f"SELECT COUNT(DISTINCT {column}) FROM {binding.dimension_table} "
            f"WHERE {column} IS NOT NULL AND TRIM(CAST({column} AS TEXT)) <> ''")

    # -- A6a ---------------------------------------------------------------

    def _country_from_point(self, lat: Any, lon: Any) -> Optional[str]:
        try:
            flat, flon = float(lat), float(lon)
        except (TypeError, ValueError):
            return None
        if not (np.isfinite(flat) and np.isfinite(flon)):
            return None
        if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
            return None
        try:
            _name, code = get_country_from_coords(flat, flon)
        except Exception as exc:  # noqa: BLE001
            logger.debug("country lookup failed for (%.5f, %.5f): %s",
                         flat, flon, exc)
            return None
        return _country_iso2(code) if code else None

    def standardize_country_codes(self, *, dry_run: bool = False,
                                  fill_from_geometry: bool = True,
                                  audit_geometry: bool = False,
                                  sample_limit: int = 10) -> Dict[str, Any]:
        """Canonicalise country_code to ISO2 and country_name to its label.

        Ladder: country_code -> country_name -> centroid. Geometry is fill and
        audit only, never correction: a row that states a country keeps it and the
        disagreement is counted. Idempotent; a canonical row is not written.
        """
        logger.info("=" * 60)
        logger.info("A6a: STANDARDISE COUNTRY CODES%s",
                    " (dry run)" if dry_run else "")
        logger.info("=" * 60)

        _build_country_tables()
        if not _COUNTRY_BY_ISO2:
            logger.error("  the ISO country tables yielded no code at all; country "
                         "standardisation cannot run.")
            return {"status": "error", "reason": "no usable ISO table"}

        try:
            rows = self.session.execute(text(
                "SELECT location_id, country_code, country_name, "
                "       centroid_lat, centroid_lon FROM dim_location "
                "ORDER BY location_id")).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.error("  could not read dim_location: %s: %s",
                         type(exc).__name__, exc)
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

        report: Dict[str, Any] = {
            "status": "dry_run" if dry_run else "ok", "examined": len(rows),
            "rewritten": 0, "already_canonical": 0, "from_code": 0,
            "from_name": 0, "from_geometry": 0, "unresolved": 0,
            "geometry_checked": 0, "geometry_conflicts": 0,
            "code_changes": {}, "name_changes": {}, "conflict_samples": [],
            "samples": [], "distinct_codes_before": 0, "distinct_codes_after": 0,
            "iso_names": len(_COUNTRY_BY_ISO2), "dry_run": bool(dry_run)}

        def _bump(bucket: Dict[str, int], key: str, limit: int = 40) -> None:
            if key in bucket:
                bucket[key] += 1
            elif len(bucket) < limit:
                bucket[key] = 1
            else:
                bucket["(other)"] = bucket.get("(other)", 0) + 1

        updates: List[Dict[str, Any]] = []
        seen_before: Set[str] = set()
        seen_after: Set[str] = set()

        for location_id, raw_code, raw_name, lat, lon in rows:
            code_text = "" if raw_code is None else str(raw_code).strip()
            name_text = "" if raw_name is None else str(raw_name).strip()
            if code_text:
                seen_before.add(code_text.upper())

            iso2: Optional[str] = None
            canonical: Optional[str] = None
            source = ""
            if code_text:
                iso2, canonical = _canonical_country(code_text)
                source = "code" if iso2 else ""
            if iso2 is None and name_text:
                iso2, canonical = _canonical_country(name_text)
                source = "name" if iso2 else ""

            geometry_iso2: Optional[str] = None
            if (iso2 is None and fill_from_geometry) or audit_geometry:
                geometry_iso2 = self._country_from_point(lat, lon)
                report["geometry_checked"] += 1

            if iso2 is None and geometry_iso2:
                iso2, canonical = geometry_iso2, _country_name_for(geometry_iso2)
                source = "geometry"
            elif iso2 and geometry_iso2 and geometry_iso2 != iso2:
                report["geometry_conflicts"] += 1
                if len(report["conflict_samples"]) < sample_limit:
                    report["conflict_samples"].append({
                        "location_id": int(location_id),
                        "country_code": code_text, "country_name": name_text,
                        "centroid_says": geometry_iso2, "kept": iso2,
                        "kept_from": source})

            if iso2 is None:
                report["unresolved"] += 1
                if len(report["samples"]) < sample_limit:
                    report["samples"].append({
                        "location_id": int(location_id), "country_code": code_text,
                        "country_name": name_text,
                        "reason": "no resolvable country"})
                continue

            report[f"from_{source}"] = int(report.get(f"from_{source}", 0)) + 1
            canonical = canonical or _country_name_for(iso2) or iso2
            seen_after.add(iso2)

            code_changed = code_text.upper() != iso2
            name_changed = name_text != canonical
            if not code_changed and not name_changed:
                report["already_canonical"] += 1
                continue
            if code_changed:
                _bump(report["code_changes"], f"{code_text or '(blank)'} -> {iso2}")
            if name_changed:
                _bump(report["name_changes"],
                      f"{name_text or '(blank)'} -> {canonical}")
            if len(report["samples"]) < sample_limit:
                report["samples"].append({
                    "location_id": int(location_id),
                    "from": [code_text, name_text], "to": [iso2, canonical],
                    "source": source})
            updates.append({"location_id": int(location_id),
                            "country_code": iso2, "country_name": canonical})

        if updates and not dry_run:
            statement = text(
                "UPDATE dim_location SET country_code = :country_code, "
                "country_name = :country_name WHERE location_id = :location_id")
            try:
                for start in range(0, len(updates), MAX_IN_VARS):
                    self.session.execute(statement,
                                         updates[start:start + MAX_IN_VARS])
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                self.session.rollback()
                logger.error("  country: the commit failed: %s", exc)
                return {"status": "error",
                        "reason": f"{type(exc).__name__}: {exc}"}

        report["rewritten"] = len(updates)
        report["distinct_codes_before"] = len(seen_before)
        report["distinct_codes_after"] = len(seen_after)

        logger.info("  country: %d row(s) examined, %d rewritten (%d from the code, "
                    "%d from the name, %d from the centroid), %d already canonical, "
                    "%d unresolved%s", report["examined"], report["rewritten"],
                    report["from_code"], report["from_name"],
                    report["from_geometry"], report["already_canonical"],
                    report["unresolved"], " (dry run)" if dry_run else "")
        logger.info("  country: %d distinct code(s) -> %d ISO 3166-1 alpha-2 "
                    "code(s); %d known name(s)",
                    report["distinct_codes_before"], report["distinct_codes_after"],
                    report["iso_names"])
        if report["geometry_checked"]:
            if report["geometry_conflicts"]:
                logger.warning("  country: %d row(s) declare a country their "
                               "centroid does not sit in; the declared value was "
                               "KEPT. audit_geometry=True checks settled rows too.",
                               report["geometry_conflicts"])
            else:
                logger.info("  country: %d centroid(s) checked, no conflict with "
                            "the declared country", report["geometry_checked"])
        if report["unresolved"]:
            logger.warning("  country: %d row(s) have no resolvable country code or "
                           "name and no usable centroid; left untouched.",
                           report["unresolved"])
        for sample in report["samples"]:
            if "to" in sample:
                logger.info("    [%r, %r] -> [%r, %r] (%s)", sample["from"][0],
                            sample["from"][1], sample["to"][0], sample["to"][1],
                            sample["source"])
            else:
                logger.info("    location %s kept [%r, %r]: %s",
                            sample["location_id"], sample["country_code"],
                            sample["country_name"], sample["reason"])
        for sample in report["conflict_samples"]:
            logger.info("    location %s declared %r/%r, centroid says %s, kept %s "
                        "(from the %s)", sample["location_id"],
                        sample["country_code"], sample["country_name"],
                        sample["centroid_says"], sample["kept"],
                        sample["kept_from"])
        return report

    def fix_location_country_codes(self) -> int:
        """Deprecated: standardize_country_codes also fixes the spelling."""
        warnings.warn("fix_location_country_codes is deprecated; use "
                      "standardize_country_codes.", DeprecationWarning, stacklevel=2)
        return int(self.standardize_country_codes().get("rewritten", 0) or 0)

    # -- A6 ----------------------------------------------------------------

    def consolidate_locations(self, *, gadm_gpkg: Optional[str] = None,
                              country_codes: Iterable[str] = (),
                              dry_run: bool = False,
                              audit_country_geometry: bool = False
                              ) -> Dict[str, Any]:
        """A6a country -> hierarchy -> detach -> identity merge -> grid parents."""
        logger.info("=" * 60)
        logger.info("A6: CONSOLIDATE LOCATIONS%s", " (dry run)" if dry_run else "")
        logger.info("=" * 60)

        codes = [str(c).strip().upper() for c in (country_codes or []) if str(c).strip()]
        before = self._scalar_or_zero("SELECT COUNT(*) FROM dim_location")
        country_report = ({"status": "dry_run", "rewritten": 0} if dry_run else
                          self.standardize_country_codes(
                              audit_geometry=audit_country_geometry))
        filled = int((country_report or {}).get("rewritten", 0) or 0)

        parents_resolved: Dict[str, Any] = {}
        if gadm_gpkg and not dry_run:
            try:
                from utilities.admin_alignment import resolve_parent_locations
                parents_resolved = resolve_parent_locations(
                    self.session, gpkg_path=gadm_gpkg,
                    registry=getattr(self, "registry", None),
                    country_codes=codes or None, dry_run=False) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("  boundary hierarchy skipped: %s: %s",
                               type(exc).__name__, exc)
                parents_resolved = {"status": "skipped", "reason": str(exc)}
        elif gadm_gpkg is None:
            logger.info("  no boundary file configured; skipping the hierarchy pass")

        registry = _load_registry(getattr(self, "registry", None))
        repaired = self._repair_dangling_parents(dry_run=dry_run)
        # A name is not identity: only an equal standard_code is.
        merge_report = self._merge_locations_by_identity(dry_run=dry_run)
        rebuilt = self._rebuild_admin_hierarchy_from_standard_code(dry_run=dry_run)
        grid_parents = self._assign_grid_parents(dry_run=dry_run)
        name_duplicates = self._location_name_duplicates(limit=50)

        grid_level = None
        if registry is not None:
            try:
                grid_level = getattr(registry.select_pack("dim_location.name"),
                                     "grid_level", None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("no location pack for the grid count: %s", exc)
        grid_count = 0
        if grid_level:
            grid_count = self._scalar_or_zero_raw(
                "SELECT COUNT(*) FROM dim_location WHERE admin_level = :lvl",
                {"lvl": grid_level})

        after = self._scalar_or_zero("SELECT COUNT(*) FROM dim_location")
        merged_groups = (merge_report or {}).get("merged") or []
        merged_count = sum(len(e.get("duplicate_codes") or []) for e in merged_groups)

        summary = {
            "original_count": before, "final_count": after,
            "country_standard": country_report, "country_codes_filled": filled,
            "parents_resolved": parents_resolved, "repaired_parents": repaired,
            "hierarchy_rebuilt": rebuilt, "grid_count": grid_count,
            "grid_parents": grid_parents,
            "non_grid_count": max(0, after - grid_count),
            "cluster_results": {"merged_count": merged_count,
                                "group_count": len(merged_groups),
                                "groups": merged_groups},
            "name_duplicate_groups": name_duplicates,
            "name_assignment": {"status": "deferred",
                                "reason": "coordinate-based naming runs at A9b"},
            "final_country_fixes": filled, "merge_report": merge_report,
            "dry_run": bool(dry_run)}

        logger.info("  locations: %d -> %d row(s) (%d merged across %d group(s) by "
                    "standard_code), %d country row(s) standardised, %d parent(s) "
                    "repaired, %d grid cell(s) parented, %d same-name group(s) kept "
                    "distinct%s", before, after, merged_count, len(merged_groups),
                    filled, repaired.get("total", 0),
                    int((grid_parents or {}).get("assigned", 0)),
                    len(name_duplicates), " (dry run)" if dry_run else "")
        return summary

    # -- A9b ---------------------------------------------------------------

    def _boundary_index_for(self, path: Any, precision: Optional[float] = 1e-5,
                            ) -> Optional[_BoundaryIndex]:
        """Precision is part of the cache key, so a coarser index is never reused."""
        candidate = None if path is None else str(path)
        if (self._boundary_index is not None and candidate is not None
                and candidate == self._boundary_path
                and precision == self._boundary_precision):
            return self._boundary_index
        index = (_load_boundary_index(candidate, precision=precision)
                 if candidate else None)
        if index is not None:
            self._boundary_index = index
            self._boundary_path = candidate
            self._boundary_precision = precision
        return index

    def _concept_labels_by_code(self, vocabulary_id: Optional[str]) -> Dict[str, str]:
        if not vocabulary_id:
            return {}
        try:
            rows = self.session.execute(text(
                "SELECT concept_code, concept_name FROM dim_concept "
                "WHERE vocabulary_id = :v AND concept_name IS NOT NULL "
                "AND TRIM(concept_name) <> ''"), {"v": vocabulary_id}).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read concept labels for %r: %s",
                         vocabulary_id, exc)
            return {}
        return {str(code).strip(): _title_name(name)
                for code, name in rows if str(code or "").strip()}

    def resolve_location_names(self, *, gadm_gpkg: Optional[str] = None,
                               dry_run: bool = False,
                               include_grid: bool = False,
                               overwrite_human: bool = False,
                               similarity_threshold: float = 0.80,
                               max_distance_km: float = 25.0,
                               token_fallback: bool = False,
                               precision: Optional[float] = 1e-5,
                               sample_limit: int = 10) -> Dict[str, Any]:
        """Name every location from its coordinates.

        Point-in-polygon at every level; the deepest human-named containing unit
        supplies the label (dim_concept first, else the layer's NAME_n). The source
        token is evidence only: it can confirm or outvote depth, and its similarity
        is recorded. Ladder: boundary -> nearest_boundary -> ancestor ->
        nearest_anchor -> token (opt-in). Grid cells are skipped by default; a
        pixel has no name and 28k identical labels would break the "<lon>/<lat>"
        fallback in _assign_grid_parents. location_code is never touched.
        standard_code is NOT stamped: _merge_locations_by_identity would collapse
        distinct sites inside one ward.
        """
        registry = _load_registry(getattr(self, "registry", None))
        pack = None
        if registry is not None:
            try:
                pack = registry.select_pack("dim_location.name")
            except Exception as exc:  # noqa: BLE001
                logger.debug("no location pack for the naming pass: %s", exc)

        grid_level = getattr(pack, "grid_level", None)
        admin_levels = tuple((getattr(pack, "admin_level_map", {}) or {}).values())
        vocabulary_id = getattr(pack, "vocabulary_id", None)

        try:
            rows = self.session.execute(text(
                "SELECT location_id, name, location_code, admin_level, "
                "       country_code, country_name, centroid_lat, centroid_lon, "
                "       parent_location_id, concept_id "
                "FROM dim_location ORDER BY location_id")).mappings().all()
        except Exception as exc:  # noqa: BLE001
            logger.error("  location naming: could not read dim_location: %s", exc)
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

        report: Dict[str, Any] = {
            "status": "ok", "examined": len(rows), "renamed": 0,
            "grid_skipped": 0, "already_named": 0, "from_boundary": 0,
            "from_nearest_boundary": 0, "from_ancestor": 0,
            "from_nearest_anchor": 0, "from_token": 0,
            "unresolved_no_coordinates": 0, "unresolved_no_match": 0,
            "boundary_file": None, "boundary_units": 0, "decisions": {},
            "similarity": {"evaluated": 0, "mean": 0.0, "min": 0.0,
                           "below_threshold": 0, "samples": []},
            "human_name_conflicts": 0, "human_name_conflict_samples": [],
            "samples": [], "dry_run": bool(dry_run)}

        if not rows:
            report["status"] = "no_data"
            logger.info("  location names: dim_location is empty; nothing to name")
            return report

        def _count(decision: str) -> None:
            """from_<prefix> plus a raw histogram. The bare prefix is not a key."""
            prefix = decision.split(":", 1)[0]
            bucket = f"from_{prefix}"
            report[bucket] = int(report.get(bucket, 0)) + 1
            report["decisions"][decision] = report["decisions"].get(decision, 0) + 1

        boundary_path = gadm_gpkg or self._boundary_path
        index: Optional[_BoundaryIndex] = None
        if boundary_path:
            try:
                index = self._boundary_index_for(boundary_path, precision=precision)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  location naming: no boundary geometry (%s: %s). "
                               "Falling back to the parent chain and to named "
                               "administrative rows.", type(exc).__name__, exc)
                index = None
        if index is not None:
            report["boundary_file"] = index.label
            report["boundary_units"] = len(index.units)
            logger.info("  location naming: %d polygon(s) at level(s) %s from %s",
                        len(index.units),
                        ", ".join(str(level) for level in index.levels), index.label)
        else:
            logger.warning("  location naming: no boundary file. Configure "
                           "core.admin_boundaries.path to name from coordinates.")

        concept_labels = self._concept_labels_by_code(vocabulary_id)
        token_concepts: Dict[str, str] = {}
        if token_fallback and concept_labels:
            for code, label in concept_labels.items():
                key = _similarity_key(label)
                if key:
                    token_concepts.setdefault(key, code)

        name_by_id = {int(r["location_id"]): str(r["name"] or "") for r in rows}
        parent_of = {int(r["location_id"]): (None if r["parent_location_id"] is None
                                            else int(r["parent_location_id"]))
                     for r in rows}

        def _depth(location_id: int, seen: Optional[Set[int]] = None) -> int:
            seen = seen or set()
            if location_id in seen:
                return 0
            seen.add(location_id)
            parent = parent_of.get(location_id)
            return 0 if parent is None else 1 + _depth(parent, seen)

        ordered = sorted(rows, key=lambda r: (_depth(int(r["location_id"])),
                                              int(r["location_id"])))

        updates: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        anchors: Dict[str, List[Tuple[int, str, float, float]]] = {}
        similarities: List[float] = []

        for row in ordered:
            location_id = int(row["location_id"])
            text_name = str(row["name"] or "").strip()
            text_code = str(row["location_code"] or "").strip()
            label = str(row["admin_level"] or "").strip()
            country = str(row["country_code"] or "").strip().upper()
            is_admin = bool(label) and (not admin_levels or label in admin_levels)

            is_grid = bool(grid_level) and label == grid_level
            if not is_grid:
                is_grid = bool(_GRID_NAME_RE.match(text_name)
                               or _GRID_NAME_RE.match(text_code))
            if is_grid and not include_grid:
                report["grid_skipped"] += 1
                continue

            country_tokens = [row["country_name"], country,
                              ISO_TO_COUNTRY_NAME.get(country, ""),
                              _country_name_for(country)]
            identity = _best_identity_token(text_code, text_name, country_tokens)
            is_anchor = bool(row["concept_id"] is not None or is_admin)

            try:
                flat, flon = float(row["centroid_lat"]), float(row["centroid_lon"])
            except (TypeError, ValueError):
                flat = flon = None
            has_coords = (flat is not None and np.isfinite(flat)
                          and np.isfinite(flon))
            human_existing = bool(text_name) and not _looks_generated_name(
                text_name, text_code)

            if human_existing and not overwrite_human:
                report["already_named"] += 1
                if is_anchor and has_coords:
                    anchors.setdefault(country, []).append(
                        (location_id, text_name, flat, flon))
                if index is not None and has_coords:
                    try:
                        units = index.containing(flat, flon)
                    except (TypeError, ValueError):
                        units = []
                    unit, score, _choice = _choose_boundary_unit(
                        units, identity, similarity_threshold)
                    if unit is not None and score < similarity_threshold:
                        report["human_name_conflicts"] += 1
                        if len(report["human_name_conflict_samples"]) < sample_limit:
                            report["human_name_conflict_samples"].append(
                                {"location_id": location_id, "name": text_name,
                                 "boundary": (concept_labels.get(unit.gid)
                                              or unit.name),
                                 "similarity": round(score, 3)})
                continue

            if not has_coords:
                deferred.append({"row": row, "reason": "no_coordinates",
                                 "identity": identity, "country": country})
                continue

            chosen: Optional[str] = None
            decision = ""
            if index is not None:
                units = index.containing(flat, flon)
                if units:
                    unit, score, choice = _choose_boundary_unit(
                        units, identity, similarity_threshold)
                    if unit is not None:
                        chosen = concept_labels.get(unit.gid) or unit.name
                        if identity:
                            similarities.append(score)
                            if (score < similarity_threshold
                                    and len(report["similarity"]["samples"])
                                    < sample_limit):
                                report["similarity"]["samples"].append(
                                    {"location_id": location_id,
                                     "identity": identity, "boundary": chosen,
                                     "similarity": round(score, 3),
                                     "decision": choice})
                        decision = f"boundary:{choice}"
                else:
                    unit, distance_km = index.nearest(flat, flon)
                    if unit is not None and distance_km <= max_distance_km:
                        chosen = concept_labels.get(unit.gid) or unit.name
                        decision = f"nearest_boundary:{distance_km:.1f}km"

            if chosen is None:
                ancestor = parent_of.get(location_id)
                hops = 0
                while ancestor is not None and hops < 32:
                    candidate = name_by_id.get(ancestor, "")
                    if candidate and not _looks_generated_name(candidate):
                        chosen, decision = candidate, "ancestor"
                        break
                    ancestor = parent_of.get(ancestor)
                    hops += 1

            if chosen is None or not _is_human_label(chosen):
                deferred.append({"row": row,
                                 "reason": ("no_polygon" if chosen is None
                                            else "candidate_not_human"),
                                 "identity": identity, "country": country,
                                 "lat": flat, "lon": flon})
                continue

            name_by_id[location_id] = chosen
            updates.append({"location_id": location_id, "name": chosen})
            _count(decision)
            if is_anchor:
                anchors.setdefault(country, []).append(
                    (location_id, chosen, flat, flon))
            if len(report["samples"]) < sample_limit:
                report["samples"].append({
                    "location_id": location_id, "from": text_name, "to": chosen,
                    "decision": decision,
                    "similarity": (round(_name_similarity(identity, chosen), 3)
                                   if identity else None)})

        pending: Dict[str, List[Dict[str, Any]]] = {}
        for entry in deferred:
            if entry.get("reason") in ("no_polygon", "candidate_not_human"):
                pending.setdefault(entry["country"], []).append(entry)

        for country in sorted(pending):
            group = pending[country]
            pool = anchors.get(country) or []
            if not pool:
                continue
            nearest = _nearest_index(
                [e["lat"] for e in group], [e["lon"] for e in group],
                [item[2] for item in pool], [item[3] for item in pool])
            for offset, entry in enumerate(group):
                anchor = pool[int(nearest[offset])]
                distance_km = _approx_km(entry["lat"], entry["lon"],
                                         anchor[2], anchor[3])
                if distance_km > max_distance_km:
                    continue
                chosen = anchor[1]
                if not _is_human_label(chosen):
                    continue
                location_id = int(entry["row"]["location_id"])
                name_by_id[location_id] = chosen
                updates.append({"location_id": location_id, "name": chosen})
                _count(f"nearest_anchor:{distance_km:.1f}km")
                entry["resolved"] = True
                if len(report["samples"]) < sample_limit:
                    report["samples"].append({
                        "location_id": location_id,
                        "from": str(entry["row"]["name"] or ""), "to": chosen,
                        "decision": f"nearest_anchor:{distance_km:.1f}km",
                        "similarity": (round(_name_similarity(
                            entry["identity"], chosen), 3)
                            if entry["identity"] else None)})

        resolved_ids = {entry["location_id"] for entry in updates}
        for entry in deferred:
            location_id = int(entry["row"]["location_id"])
            if location_id in resolved_ids or entry.get("resolved"):
                continue
            if entry["reason"] == "no_coordinates":
                identity = entry["identity"]
                if token_fallback and identity:
                    concept_code = token_concepts.get(_similarity_key(identity))
                    if concept_code:
                        chosen = concept_labels.get(concept_code)
                        if chosen and _is_human_label(chosen):
                            name_by_id[location_id] = chosen
                            updates.append({"location_id": location_id,
                                            "name": chosen})
                            _count("token:no_coordinates")
                            continue
                report["unresolved_no_coordinates"] += 1
            else:
                report["unresolved_no_match"] += 1
            if len(report["samples"]) < sample_limit:
                report["samples"].append({
                    "location_id": location_id,
                    "from": str(entry["row"]["name"] or ""), "to": None,
                    "decision": entry["reason"]})

        if similarities:
            report["similarity"]["evaluated"] = len(similarities)
            report["similarity"]["mean"] = round(float(np.mean(similarities)), 3)
            report["similarity"]["min"] = round(float(np.min(similarities)), 3)
            report["similarity"]["below_threshold"] = sum(
                1 for score in similarities if score < similarity_threshold)

        if updates and not dry_run:
            statement = text("UPDATE dim_location SET name = :name "
                             "WHERE location_id = :location_id")
            try:
                for start in range(0, len(updates), MAX_IN_VARS):
                    self.session.execute(statement,
                                         updates[start:start + MAX_IN_VARS])
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                self.session.rollback()
                logger.error("  location naming: the commit failed: %s", exc)
                return {"status": "error",
                        "reason": f"{type(exc).__name__}: {exc}"}

        report["renamed"] = len(updates)
        report["status"] = "dry_run" if dry_run else "ok"

        logger.info(
            "  location names: %d of %d row(s) renamed (%d containing polygon, %d "
            "nearest polygon, %d named ancestor, %d nearby named unit, %d "
            "vocabulary token), %d already named, %d grid cell(s) kept%s",
            len(updates), report["examined"], report["from_boundary"],
            report["from_nearest_boundary"], report["from_ancestor"],
            report["from_nearest_anchor"], report["from_token"],
            report["already_named"], report["grid_skipped"],
            " (dry run)" if dry_run else "")
        if report["similarity"]["evaluated"]:
            logger.info("  name/identity similarity: %d compared, mean %.3f, "
                        "min %.3f, %d below %.2f",
                        report["similarity"]["evaluated"],
                        report["similarity"]["mean"], report["similarity"]["min"],
                        report["similarity"]["below_threshold"], similarity_threshold)
        if report["unresolved_no_coordinates"]:
            logger.warning("  %d row(s) have no coordinates and kept their name.",
                           report["unresolved_no_coordinates"])
        if report["unresolved_no_match"]:
            logger.warning("  %d row(s) fall inside no polygon and near no named "
                           "unit within %.0f km; they kept their name.",
                           report["unresolved_no_match"], max_distance_km)
        if report["human_name_conflicts"]:
            logger.info("  %d existing name(s) disagree with their containing "
                        "polygon; left alone (overwrite_human=False)",
                        report["human_name_conflicts"])
        for sample in report["samples"]:
            if sample.get("to"):
                logger.info("    %r -> %r (%s, similarity %s)", sample["from"],
                            sample["to"], sample["decision"],
                            sample.get("similarity"))
            else:
                logger.info("    location %s kept %r: %s", sample["location_id"],
                            sample["from"], sample["decision"])
        for sample in report["human_name_conflict_samples"]:
            logger.info("    kept %r, polygon says %r (similarity %.3f)",
                        sample["name"], sample["boundary"], sample["similarity"])
        return report

    def _assign_location_names(self, **kwargs: Any) -> Dict[str, Any]:
        """Deprecated alias for resolve_location_names."""
        warnings.warn("_assign_location_names is deprecated; use "
                      "resolve_location_names.", DeprecationWarning, stacklevel=2)
        return self.resolve_location_names(**kwargs)

    def _merge_locations_by_identity(self, dry_run: bool = False) -> Dict[str, Any]:
        """Merge rows sharing a non-empty standard_code. Never merges by name."""
        keys = self.session.execute(text(
            "SELECT standard_code FROM dim_location "
            "WHERE standard_code IS NOT NULL AND TRIM(standard_code) <> '' "
            "GROUP BY standard_code HAVING COUNT(*) > 1 "
            "ORDER BY standard_code")).fetchall()
        merged: List[Dict[str, Any]] = []
        deleted_total = skipped_total = 0

        for (standard_code,) in keys:
            members = self.session.query(DimLocation).filter(
                DimLocation.standard_code == standard_code
            ).order_by(DimLocation.location_id).all()
            if len(members) < 2:
                continue
            canonical = sorted(members, key=lambda r: (
                0 if getattr(r, "concept_id", None) is not None else 1,
                0 if getattr(r, "parent_location_id", None) is not None else 1,
                int(getattr(r, "location_id", 0))))[0]
            canonical_id = int(canonical.location_id)
            duplicate_keys: List[Any] = []
            for duplicate in members:
                if duplicate is canonical:
                    continue
                duplicate_key = int(duplicate.location_id)
                if dry_run:
                    duplicate_keys.append(duplicate_key)
                    continue
                if canonical.concept_id is None and duplicate.concept_id is not None:
                    canonical.concept_id = duplicate.concept_id
                try:
                    self.session.execute(text(
                        "UPDATE dim_location SET parent_location_id = :new_id "
                        "WHERE parent_location_id = :old_id"),
                        {"new_id": canonical_id, "old_id": duplicate_key})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("  dim_location: could not re-parent children "
                                   "of %s: %s", duplicate_key, exc)
                try:
                    self._update_fact_references(DimLocation, duplicate_key,
                                                 canonical_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("  dim_location: could not re-point the facts of "
                                   "%s onto %s: %s; keeping the duplicate.",
                                   duplicate_key, canonical_id, exc)
                    skipped_total += 1
                    continue
                remaining = self._count_references(DimLocation, duplicate_key)
                if remaining:
                    logger.warning("  dim_location: %s still has %d referencing "
                                   "row(s); not deleted.", duplicate_key, remaining)
                    skipped_total += 1
                    continue
                self.session.delete(duplicate)
                duplicate_keys.append(duplicate_key)
                deleted_total += 1

            if duplicate_keys:
                merged.append({
                    "group_key": f"standard:{standard_code}",
                    "canonical_code": canonical_id,
                    "canonical_name": canonical.name,
                    "merged_count": len(members),
                    "duplicate_codes": duplicate_keys,
                    "member_names": [m.name for m in members]})

        if deleted_total and not dry_run:
            self.session.commit()
        if merged:
            logger.info("  dim_location: %d row(s) merged across %d standard_code "
                        "group(s)%s%s", deleted_total, len(merged),
                        (f", {skipped_total} kept because facts still referenced "
                         f"them" if skipped_total else ""),
                        " (dry run)" if dry_run else "")
        else:
            logger.info("  dim_location: no duplicate standard_code group")
        return {"status": "dry_run" if dry_run else "ok", "table": "dim_location",
                "merged": merged, "deleted": deleted_total,
                "skipped_referenced": skipped_total}

    def _location_name_duplicates(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Same-name groups, read-only. A long list is expected after naming."""
        keys = self.session.execute(text(
            "SELECT name, country_code, admin_level, COUNT(*) AS n "
            "FROM dim_location WHERE name IS NOT NULL "
            "GROUP BY name, country_code, admin_level HAVING COUNT(*) > 1 "
            "ORDER BY n DESC, name LIMIT :lim"),
            {"lim": max(0, int(limit))}).fetchall()
        if not keys:
            return []
        names = sorted({str(row[0]) for row in keys})
        rows = _chunked_in(
            self.session,
            "SELECT location_id, name, country_code, admin_level "
            "FROM dim_location WHERE name IN :vals ORDER BY location_id",
            {}, names)
        by_key: Dict[Tuple[str, Any, Any], List[int]] = {}
        for location_id, name, country, level in rows:
            by_key.setdefault((str(name), country, level), []).append(int(location_id))
        groups: List[Dict[str, Any]] = []
        for name, country, level, count in keys:
            groups.append({"name": name, "country_code": country,
                           "admin_level": level, "count": int(count),
                           "location_ids": by_key.get((str(name), country, level),
                                                      [])})
        logger.info("  dim_location: %d same-name group(s) reported, not merged",
                    len(groups))
        return groups

    def _repair_dangling_parents(self, dry_run: bool = False) -> Dict[str, Any]:
        """NULL unresolvable and self-referential parents."""
        dangling = self.session.execute(text(
            "SELECT COUNT(*) FROM dim_location l WHERE l.parent_location_id IS NOT NULL "
            "AND (l.parent_location_id = l.location_id OR NOT EXISTS "
            "(SELECT 1 FROM dim_location p WHERE p.location_id = l.parent_location_id))"
        )).scalar() or 0
        self_parent = self._scalar_or_zero(
            "SELECT COUNT(*) FROM dim_location WHERE parent_location_id = location_id")
        missing = self._scalar_or_zero(
            "SELECT COUNT(*) FROM dim_location l WHERE l.parent_location_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM dim_location p "
            "WHERE p.location_id = l.parent_location_id)")
        if dangling and not dry_run:
            self.session.execute(text(
                "UPDATE dim_location SET parent_location_id = NULL "
                "WHERE parent_location_id = location_id OR (parent_location_id IS NOT NULL "
                "AND parent_location_id NOT IN (SELECT location_id FROM dim_location))"))
            self.session.commit()
        if dangling:
            logger.warning("  %d location row(s) had a dangling or self-referential "
                           "parent and were detached (%d self, %d missing target)%s",
                           dangling, self_parent, missing,
                           " (dry run)" if dry_run else "")
        return {"total": int(dangling), "self_parent": int(self_parent),
                "missing_parent": int(missing),
                "repaired": 0 if dry_run else int(dangling)}

    def _rebuild_admin_hierarchy_from_standard_code(
            self, dry_run: bool = False) -> Dict[str, Any]:
        """Place unparented rows using the GID's own encoding of its parent."""
        rows = self.session.execute(text(
            "SELECT location_id, standard_code FROM dim_location "
            "WHERE parent_location_id IS NULL AND standard_code IS NOT NULL "
            "ORDER BY location_id")).fetchall()
        if not rows:
            return {"examined": 0, "placed": 0}
        from utilities.admin_alignment import _parent_gid

        id_by_gid: Dict[str, int] = {}
        for loc_id, standard_code, location_code in self.session.execute(text(
            "SELECT location_id, standard_code, location_code FROM dim_location "
            "ORDER BY location_id")).fetchall():
            for candidate in (standard_code, location_code):
                gid = str(candidate or "").strip()
                if gid:
                    id_by_gid.setdefault(gid, int(loc_id))

        updates = []
        for location_id, standard_code in rows:
            parent_gid = _parent_gid(str(standard_code or ""))
            if not parent_gid:
                continue
            parent_id = id_by_gid.get(parent_gid)
            if parent_id is None or parent_id == int(location_id):
                continue
            updates.append({"location_id": int(location_id),
                            "parent_location_id": parent_id})
        if updates and not dry_run:
            statement = text(
                "UPDATE dim_location SET parent_location_id = :parent_location_id "
                "WHERE location_id = :location_id")
            for start in range(0, len(updates), MAX_IN_VARS):
                self.session.execute(statement, updates[start:start + MAX_IN_VARS])
            self.session.commit()
        if updates:
            logger.info("  hierarchy: placed %d/%d location(s) from standard_code%s",
                        len(updates), len(rows), " (dry run)" if dry_run else "")
        return {"examined": len(rows), "placed": len(updates)}

    def _assign_grid_parents(self, *, force: bool = False,
                             dry_run: bool = False) -> Dict[str, Any]:
        """Give every grid cell a parent filtered by its own country_code."""
        registry = _load_registry(getattr(self, "registry", None))
        pack = None
        if registry is not None:
            try:
                pack = registry.select_pack("dim_location.name")
            except Exception as exc:  # noqa: BLE001
                logger.debug("no location pack for the grid pass: %s", exc)
        grid_level = getattr(pack, "grid_level", None)
        admin_levels = tuple((getattr(pack, "admin_level_map", {}) or {}).values())
        rows = self.session.execute(text(
            "SELECT location_id, name, location_code, admin_level, country_code, "
            "       centroid_lat, centroid_lon, parent_location_id "
            "FROM dim_location ORDER BY location_id")).mappings().all()
        parent_of = {int(r["location_id"]): r["parent_location_id"] for r in rows}

        def _depth(location_id: int, seen: Optional[Set[int]] = None) -> int:
            seen = seen or set()
            if location_id in seen:
                return 0
            seen.add(location_id)
            parent = parent_of.get(location_id)
            return 0 if parent is None else 1 + _depth(int(parent), seen)

        grid: List[Dict[str, Any]] = []
        admin_records: List[Dict[str, Any]] = []
        no_coords = by_pattern = 0

        for row in rows:
            location_id = int(row["location_id"])
            label = str(row["admin_level"] or "").strip()
            country = str(row["country_code"] or "").strip().upper()
            is_grid = bool(grid_level) and label == grid_level
            if not is_grid and (_GRID_NAME_RE.match(str(row["name"] or ""))
                                or _GRID_NAME_RE.match(
                                    str(row["location_code"] or ""))):
                is_grid = True
                by_pattern += 1
            try:
                flat, flon = float(row["centroid_lat"]), float(row["centroid_lon"])
            except (TypeError, ValueError):
                if is_grid:
                    no_coords += 1
                continue
            if not (np.isfinite(flat) and np.isfinite(flon)):
                if is_grid:
                    no_coords += 1
                continue
            if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
                if is_grid:
                    no_coords += 1
                continue
            record = {"location_id": location_id, "country": country,
                      "lat": flat, "lon": flon,
                      "parent": row["parent_location_id"], "label": label}
            if is_grid:
                grid.append(record)
            elif not label:
                continue
            elif admin_levels:
                if label in admin_levels:
                    admin_records.append(record)
            else:
                admin_records.append(record)

        if not grid:
            logger.info("  no grid cells found (grid_level=%r, %d row(s) examined); "
                        "no parent assigned", grid_level, len(rows))
            return {"status": "no_grid", "grid_level": grid_level,
                    "grid_cells": 0, "assigned": 0}

        candidates: List[Dict[str, Any]] = []
        target_level: Optional[str] = None
        if admin_levels:
            for level_label in reversed(admin_levels):
                pool = [r for r in admin_records if r["label"] == level_label]
                if pool:
                    candidates, target_level = pool, level_label
                    break
        else:
            depths = {r["location_id"]: _depth(r["location_id"])
                      for r in admin_records}
            max_depth = max(depths.values(), default=0)
            candidates = [r for r in admin_records
                          if depths.get(r["location_id"]) == max_depth]
            target_level = f"depth={max_depth}"

        if not candidates:
            logger.warning(
                "  %d grid cell(s) found but no administrative row carries "
                "coordinates (%s requested), so no parent can be assigned. %d "
                "cell(s) also lack their own coordinates.",
                len(grid), list(admin_levels) or "any level", no_coords)
            return {"status": "no_candidates", "grid_level": grid_level,
                    "grid_cells": len(grid), "assigned": 0,
                    "no_coordinates": no_coords}

        by_country: Dict[str, List[Dict[str, Any]]] = {}
        for record in candidates:
            by_country.setdefault(record["country"], []).append(record)

        kept = 0
        updates: List[Dict[str, Any]] = []
        pending: Dict[str, List[Dict[str, Any]]] = {}
        for record in grid:
            if record["parent"] is not None and not force:
                kept += 1
                continue
            pending.setdefault(record["country"], []).append(record)

        for country in sorted(pending):
            group = pending[country]
            pool = by_country.get(country) or candidates
            nearest = _nearest_index(
                [r["lat"] for r in group], [r["lon"] for r in group],
                [r["lat"] for r in pool], [r["lon"] for r in pool])
            for offset, record in enumerate(group):
                parent_id = pool[int(nearest[offset])]["location_id"]
                if parent_id == record["location_id"]:
                    continue
                updates.append({"location_id": record["location_id"],
                                "parent_location_id": parent_id})

        if updates and not dry_run:
            statement = text(
                "UPDATE dim_location SET parent_location_id = :parent_location_id "
                "WHERE location_id = :location_id")
            for start in range(0, len(updates), MAX_IN_VARS):
                self.session.execute(statement, updates[start:start + MAX_IN_VARS])
            self.session.commit()

        by_country_counts: Dict[str, int] = {}
        for record in grid:
            key = record["country"] or "(none)"
            by_country_counts[key] = by_country_counts.get(key, 0) + 1

        logger.info(
            "  grid parents: %d/%d cell(s) assigned to %r (%d by location_code "
            "pattern, %d already parented, %d without coordinates; countries: %s)%s",
            len(updates), len(grid), target_level, by_pattern, kept, no_coords,
            ", ".join(f"{c}={n}" for c, n in sorted(by_country_counts.items())),
            " (dry run)" if dry_run else "")
        return {"status": "dry_run" if dry_run else "ok",
                "grid_level": grid_level, "admin_level": target_level,
                "grid_cells": len(grid), "candidates": len(candidates),
                "assigned": len(updates), "kept": kept,
                "no_coordinates": no_coords, "matched_by_pattern": by_pattern,
                "by_country": by_country_counts, "dry_run": bool(dry_run)}

    # -- dimension merge ---------------------------------------------------

    def _consolidate_dimension(self, model_class, name_col: str, code_col: str,
                               fallback_key_fn=None, pack=None,
                               dry_run: bool = False,
                               group_extra: Tuple[str, ...] = ()) -> Dict[str, Any]:
        """Merge one dimension: concept:<id> -> standard:<code> -> name:<key>.

        A duplicate is deleted only after its facts are re-pointed AND a live count
        confirms nothing still references it; otherwise it is kept.
        """
        table_name = getattr(model_class, "__tablename__", "?")
        try:
            safe_table = require_identifier(table_name, what="table name")
        except ValueError as exc:
            logger.error("  _consolidate_dimension: %s", exc)
            return {"status": "error", "table": table_name, "reason": str(exc)}

        primary_key = list(model_class.__table__.primary_key.columns)
        query = self.session.query(model_class)
        if primary_key:
            query = query.order_by(*primary_key)
        records = query.all()

        empty = {"status": "no_data", "table": table_name, "name_column": name_col,
                 "dimension_key": code_col, "rows_examined": len(records),
                 "duplicates_found": 0, "merged": [], "deleted": 0,
                 "skipped_referenced": 0}
        if not records:
            logger.info("  %s: no rows to merge", table_name)
            return empty

        key_function = (fallback_key_fn if fallback_key_fn is not None
                        else _default_vocab_key)

        def _suffix(record) -> str:
            if not group_extra:
                return ""
            parts = [("" if getattr(record, c, None) is None
                      else str(getattr(record, c)).strip().upper())
                     for c in group_extra]
            return "|" + "|".join(parts)

        def key_of(record) -> Optional[str]:
            concept_id = getattr(record, "concept_id", None)
            if concept_id is not None:
                return f"concept:{concept_id}"
            standard_code = getattr(record, "standard_code", None)
            if standard_code is not None and str(standard_code).strip():
                return f"standard:{str(standard_code).strip()}{_suffix(record)}"
            raw = getattr(record, name_col, None)
            if raw is None:
                return None
            try:
                if raw != raw:  # NaN
                    return None
            except Exception:  # noqa: BLE001
                return None
            text_value = str(raw).strip()
            if not text_value:
                return None
            try:
                normalised = key_function(text_value)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  %s: name key failed for %r (%s: %s)",
                               table_name, text_value, type(exc).__name__, exc)
                normalised = None
            if normalised is None or not str(normalised).strip():
                return None
            return f"name:{str(normalised).strip()}{_suffix(record)}"

        groups: Dict[str, List[Any]] = {}
        for record in records:
            key = key_of(record)
            if key:
                groups.setdefault(key, []).append(record)

        duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not duplicate_groups:
            logger.info("  %s: %d row(s), no duplicate group", table_name,
                        len(records))
            empty["status"] = "ok"
            return empty

        is_vector = table_name == "dim_vector"
        merged: List[Dict[str, Any]] = []
        deleted_total = skipped_total = 0

        for group_key in sorted(duplicate_groups):
            members = duplicate_groups[group_key]
            if is_vector:
                canonical = self._pick_canonical_vector(members, pack)
            else:
                canonical = sorted(members, key=lambda r: (
                    0 if getattr(r, "concept_id", None) is not None else 1,
                    0 if str(getattr(r, "standard_code", None) or "").strip() else 1,
                    str(getattr(r, code_col, ""))))[0]
            if canonical is None:
                logger.warning("  %s: group %r produced no canonical row",
                               table_name, group_key)
                continue

            canonical_key = getattr(canonical, code_col)
            canonical_name = getattr(canonical, name_col, None)
            if getattr(canonical, "concept_id", None) is None:
                donor = next((m for m in members if m is not canonical
                              and getattr(m, "concept_id", None) is not None), None)
                if donor is not None:
                    canonical.concept_id = getattr(donor, "concept_id")
                    if not str(getattr(canonical, "standard_code", None) or "").strip():
                        canonical.standard_code = getattr(donor, "standard_code", None)

            duplicate_keys: List[Any] = []
            kept_keys: List[Any] = []
            for duplicate in members:
                if duplicate is canonical:
                    continue
                duplicate_key = getattr(duplicate, code_col)
                if dry_run:
                    duplicate_keys.append(duplicate_key)
                    continue
                if hasattr(model_class, "parent_location_id"):
                    try:
                        self.session.execute(text(
                            f"UPDATE {safe_table} SET parent_location_id = :new_id "
                            f"WHERE parent_location_id = :old_id"),
                            {"new_id": canonical_key, "old_id": duplicate_key})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("  %s: could not re-parent children of %r: %s",
                                       table_name, duplicate_key, exc)
                try:
                    self._update_fact_references(model_class, duplicate_key,
                                                 canonical_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("  %s: could not re-point facts of %r onto %r: "
                                   "%s; keeping the duplicate.", table_name,
                                   duplicate_key, canonical_key, exc)
                    kept_keys.append(duplicate_key)
                    skipped_total += 1
                    continue
                remaining = self._count_references(model_class, duplicate_key)
                if remaining:
                    logger.warning("  %s: %r is still referenced by %d row(s) after "
                                   "the cascade; kept.", table_name, duplicate_key,
                                   remaining)
                    kept_keys.append(duplicate_key)
                    skipped_total += 1
                    continue
                self.session.delete(duplicate)
                duplicate_keys.append(duplicate_key)
                deleted_total += 1

            if duplicate_keys or kept_keys:
                merged.append({
                    "group_key": group_key, "canonical_code": canonical_key,
                    "canonical_name": canonical_name,
                    "merged_count": len(members),
                    "duplicate_codes": duplicate_keys, "kept_codes": kept_keys,
                    "member_names": [getattr(m, name_col, None) for m in members]})

        if not dry_run:
            try:
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("  %s: the merge commit failed: %s", table_name, exc)
                self.session.rollback()
                raise

        logger.info("  %s: %d row(s), %d duplicate group(s), %d removed%s%s",
                    table_name, len(records), len(duplicate_groups), deleted_total,
                    (f", {skipped_total} kept because facts still referenced them"
                     if skipped_total else ""),
                    " (dry run)" if dry_run else "")
        return {"status": "dry_run" if dry_run else "ok", "table": table_name,
                "name_column": name_col, "dimension_key": code_col,
                "rows_examined": len(records),
                "duplicates_found": len(duplicate_groups), "merged": merged,
                "deleted": deleted_total, "skipped_referenced": skipped_total}

    # -- FK cascade --------------------------------------------------------

    def _count_references(self, model_class, key: Any) -> int:
        if key is None:
            return 0
        total = 0
        for fact_table, fk_column in _dimension_fact_map(model_class):
            try:
                safe_table = require_identifier(fact_table, what="table name")
                safe_column = require_identifier(fk_column, what="column name")
            except ValueError:
                continue
            total += self._scalar_or_zero_raw(
                f"SELECT COUNT(*) FROM {safe_table} WHERE {safe_column} = :k",
                {"k": key})
        return total

    def _update_fact_references(self, model_class, old_key, new_key) -> int:
        """Re-point every fact FK; aggregate when a plain UPDATE would collide."""
        if old_key == new_key:
            return 0
        pairs = _dimension_fact_map(model_class)
        if not pairs:
            _warn_once(f"no-fact-refs:{getattr(model_class, '__tablename__', '?')}",
                       "  %s: no fact table references its primary key; nothing to "
                       "re-point.", getattr(model_class, "__tablename__", "?"))
            return 0
        written = 0
        for fact_table, fk_column in pairs:
            try:
                safe_table = require_identifier(fact_table, what="table name")
                safe_column = require_identifier(fk_column, what="column name")
            except ValueError as exc:
                logger.warning("  cascade: %s", exc)
                continue
            if not self._would_collide(safe_table, safe_column, old_key, new_key):
                try:
                    result = self.session.execute(text(
                        f"UPDATE {safe_table} SET {safe_column} = :new "
                        f"WHERE {safe_column} = :old"),
                        {"new": new_key, "old": old_key})
                    written += int(result.rowcount or 0)
                    continue
                except IntegrityError:
                    self.session.rollback()
                    logger.info("  cascade: %s.%s would collide; aggregating",
                                safe_table, safe_column)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("  cascade: could not update %s.%s: %s",
                                   safe_table, safe_column, exc)
                    continue
            report = self._aggregate_and_reassign_bulk([{
                "table": safe_table, "fk": safe_column,
                "old": old_key, "new": new_key}])
            written += int(report.get("rows_written", 0))
        return written

    def _would_collide(self, table_name: str, fk_column: str,
                       old_key: Any, new_key: Any) -> bool:
        key_columns = list((_FACT_AGGREGATION_SPEC.get(table_name) or {})
                           .get("natural_key") or [])
        if not key_columns:
            return False
        projected = ", ".join(
            f"CASE WHEN {c} = :old THEN :new ELSE {c} END" if c == fk_column else c
            for c in key_columns)
        try:
            groups = self.session.execute(text(
                "SELECT COUNT(*) FROM ("
                f"  SELECT {projected} FROM {table_name} "
                f"  WHERE {fk_column} IN (:old, :new) "
                f"  GROUP BY {projected} HAVING COUNT(*) > 1"
                ") AS colliding"), {"old": old_key, "new": new_key}).scalar() or 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("collision check failed for %s.%s (%s); assuming none",
                         table_name, fk_column, exc)
            return False
        return int(groups) > 0

    # -- aggregation -------------------------------------------------------

    def _validate_aggregation_spec(self, spec: Optional[Mapping[str, Any]] = None
                                   ) -> Dict[str, Dict[str, Any]]:
        """Validate a spec into {table: {key, ops, numeric, derived}}."""
        from etl.loaders.db_loader import NATURAL_KEY_CONFIG
        source = dict(_FACT_AGGREGATION_SPEC if spec is None else spec)
        if not source:
            raise ValueError("the aggregation spec is empty.")
        operators = {
            "sum": "sum", "total": "sum", "add": "sum", "plus": "sum",
            "max": "max", "maximum": "max", "greatest": "max",
            "min": "min", "minimum": "min", "least": "min",
            "first": "first", "earliest": "first", "keep": "first",
            "last": "last", "latest": "last"}

        def _columns(value: Any, table: str, purpose: str) -> Tuple[str, ...]:
            if value is None:
                return ()
            if isinstance(value, str):
                return (value.strip(),) if value.strip() else ()
            if not isinstance(value, (list, tuple, set, frozenset)):
                raise ValueError(f"{table}: {purpose} must be a name or list, got "
                                 f"{type(value).__name__}")
            return tuple(str(i).strip() for i in value if str(i).strip())

        numeric_types = ("INTEGER", "FLOAT", "NUMERIC", "REAL", "DOUBLE")
        normalized: Dict[str, Dict[str, Any]] = {}

        for table_name in sorted(source):
            table_obj = _model_for_table(table_name)
            if table_obj is None:
                raise ValueError(f"{table_name!r} is not a registered table.")
            available = set(table_obj.__table__.columns.keys())
            is_number = {
                c.name: (isinstance(c.type, Integer)
                         or str(c.type).upper().startswith(numeric_types))
                for c in table_obj.__table__.columns}

            entry = source[table_name]
            declared_key: Tuple[str, ...] = ()
            declared_ops: Dict[str, Optional[str]] = {}
            derived: Tuple[str, ...] = ()

            if isinstance(entry, Mapping):
                for key_name in ("key", "natural_key", "keys", "conflict_target"):
                    if key_name in entry:
                        declared_key = _columns(entry[key_name], table_name, key_name)
                        break
                for op_name in ("ops", "aggregate", "aggregations"):
                    block = entry.get(op_name)
                    if block is None:
                        continue
                    if not isinstance(block, Mapping):
                        raise ValueError(f"{table_name}.{op_name} must map column to "
                                         f"operator, got {type(block).__name__}")
                    for column, operator in block.items():
                        token = str(operator).strip().lower()
                        if token not in operators:
                            raise ValueError(
                                f"{table_name}: operator {operator!r} for {column!r} "
                                f"is not one of {sorted(set(operators))}")
                        declared_ops[str(column).strip()] = operators[token]
                for op_name in ("sum", "max", "min", "first", "last"):
                    for column in _columns(entry.get(op_name), table_name, op_name):
                        declared_ops[column] = operators[op_name]
                derived = _columns(entry.get("derived"), table_name, "derived")
                bare = entry.get("columns")
                if bare is not None:
                    for column in _columns(bare, table_name, "columns"):
                        declared_ops.setdefault(column, None)
            elif isinstance(entry, (list, tuple, set, frozenset)):
                declared_ops = {c: None for c in _columns(entry, table_name, "columns")}
            else:
                raise ValueError(f"aggregation spec entry for {table_name!r} must be "
                                 f"a mapping or a list, got {type(entry).__name__}")

            configured = (NATURAL_KEY_CONFIG.get(table_name) or {}).get("columns") or []
            loader_key = tuple(str(c).strip() for c in configured if str(c).strip())
            if loader_key:
                if declared_key and tuple(declared_key) != loader_key:
                    _warn_once(
                        f"aggregate-key:{table_name}",
                        "%s: spec declares key %s but NATURAL_KEY_CONFIG declares "
                        "%s; using the loader's.", table_name, list(declared_key),
                        list(loader_key))
                key_columns = loader_key
            else:
                key_columns = declared_key

            if not key_columns:
                raise ValueError(f"{table_name}: no natural key and no conflict "
                                 f"target; aggregating would duplicate rows.")
            missing_key = sorted(set(key_columns) - available)
            if missing_key:
                raise ValueError(f"{table_name}: natural key names column(s) "
                                 f"{missing_key} the model does not carry")

            ops: Dict[str, str] = {}
            for column in sorted(declared_ops):
                if column in key_columns:
                    _warn_once(f"aggregate-key-column:{table_name}:{column}",
                               "%s: %r is both a key column and an aggregation "
                               "target; the key wins.", table_name, column)
                    continue
                if column not in available:
                    raise ValueError(f"{table_name}: aggregation names column "
                                     f"{column!r}, which the model does not carry")
                operator = declared_ops[column]
                if operator is None:
                    operator = "sum" if is_number.get(column) else "first"
                if operator == "sum" and not is_number.get(column):
                    raise ValueError(f"{table_name}.{column}: 'sum' needs a numeric "
                                     f"column; use first/last/max/min")
                ops[column] = operator

            if not ops:
                _warn_once(f"aggregate-no-ops:{table_name}",
                           "%s: no aggregatable column; a collision can only keep "
                           "the existing row.", table_name)
            missing_derived = sorted(set(derived) - available)
            if missing_derived:
                raise ValueError(f"{table_name}: `derived` names column(s) "
                                 f"{missing_derived} the model does not carry")

            normalized[table_name] = {
                "key": tuple(key_columns), "ops": ops,
                "numeric": tuple(c for c in sorted(ops) if is_number.get(c)),
                "derived": tuple(sorted(set(derived) - set(ops) - set(key_columns)))}
        return normalized

    def _aggregate_and_reassign(self, records: Iterable[Any], *,
                                spec: Optional[Mapping[str, Any]] = None,
                                dry_run: bool = False,
                                stop_on_error: bool = False) -> Dict[str, Any]:
        normalized = self._validate_aggregation_spec(spec)
        totals: Dict[str, Dict[str, Any]] = {}
        seen = errors = 0
        for record in records:
            seen += 1
            try:
                result = self._aggregate_and_reassign_bulk(
                    [record], spec=normalized, dry_run=dry_run)
            except Exception as exc:  
                errors += 1
                logger.error("aggregate: record %d failed (%s: %s)",
                             seen, type(exc).__name__, exc)
                if stop_on_error:
                    raise
                continue
            errors += int(result.get("errors") or 0)
            for table, report in (result.get("tables") or {}).items():
                bucket = totals.setdefault(table, {
                    "rows_in": 0, "rows_written": 0,
                    "merged_with_existing": 0, "keys_skipped": 0, "calls": 0})
                bucket["calls"] += 1
                for field in ("rows_in", "rows_written", "merged_with_existing",
                              "keys_skipped"):
                    bucket[field] += int(report.get(field) or 0)
        return {"tables": {t: totals[t] for t in sorted(totals)},
                "records": seen, "errors": errors, "dry_run": bool(dry_run)}

    def _aggregate_and_reassign_bulk(self, records: Iterable[Mapping[str, Any]], *,
                                     spec: Optional[Mapping[str, Any]] = None,
                                     dry_run: bool = False) -> Dict[str, Any]:
        """Reassign a fact FK, aggregating colliding rows.

        dropna=False is load-bearing: pandas otherwise drops NaN-key groups and
        deletes the rows the merge exists to keep.
        """
        normalized = self._validate_aggregation_spec(spec)
        results: Dict[str, Dict[str, Any]] = {}
        errors = 0
        by_table: Dict[str, List[Mapping[str, Any]]] = {}
        for record in records:
            table_name = str(record.get("table") or "").strip()
            if not table_name:
                errors += 1
                continue
            by_table.setdefault(table_name, []).append(record)

        for table_name in sorted(by_table):
            entry = normalized.get(table_name)
            if entry is None:
                errors += 1
                logger.warning("aggregate: %s is not in the aggregation spec",
                               table_name)
                continue
            try:
                safe_table = require_identifier(table_name, what="table name")
            except ValueError as exc:
                errors += 1
                logger.warning("aggregate: %s", exc)
                continue
            model = _model_for_table(table_name)
            if model is None:
                errors += 1
                continue
            pk_columns = list(model.__table__.primary_key.columns)
            if len(pk_columns) != 1:
                errors += 1
                logger.warning("aggregate: %s has a composite primary key",
                               table_name)
                continue
            pk_name = pk_columns[0].name

            pairs: Dict[Any, Any] = {}
            for record in by_table[table_name]:
                old, new = record.get("old"), record.get("new")
                if old is not None and new is not None and old != new:
                    pairs[old] = new
            if not pairs:
                continue

            fk_columns = {str(r.get("fk") or "").strip() for r in by_table[table_name]}
            if len(fk_columns) != 1:
                errors += 1
                logger.warning("aggregate: %s got %d distinct FK columns",
                               table_name, len(fk_columns))
                continue
            try:
                safe_fk = require_identifier(fk_columns.pop(), what="column name")
            except ValueError as exc:
                errors += 1
                logger.warning("aggregate: %s", exc)
                continue

            key_columns = list(entry["key"])
            ops = dict(entry["ops"])
            derived = list(entry["derived"])
            table_columns = [c.name for c in model.__table__.columns]
            select_columns = [c for c in table_columns if c != pk_name]
            if safe_fk not in select_columns:
                errors += 1
                logger.warning("aggregate: %s has no column %r", table_name, safe_fk)
                continue

            wanted = sorted(pairs, key=str) + [
                v for v in sorted(set(pairs.values()), key=str) if v not in pairs]
            params = {f"v{i}": value for i, value in enumerate(wanted)}
            placeholders = ", ".join(f":v{i}" for i in range(len(wanted)))
            rows = self.session.execute(text(
                f"SELECT {', '.join(select_columns)} FROM {safe_table} "
                f"WHERE {safe_fk} IN ({placeholders}) ORDER BY {pk_name}"
            ), params).mappings().all()
            if not rows:
                continue

            frame = pd.DataFrame([dict(r) for r in rows])
            frame[safe_fk] = frame[safe_fk].map(lambda v: pairs.get(v, v))
            aggregations: Dict[str, str] = {}
            for column in select_columns:
                if column in derived:
                    aggregations[column] = "first"
                elif column in ops:
                    aggregations[column] = ops[column]
                else:
                    aggregations[column] = "first"
            grouped = (frame.groupby(key_columns, dropna=False, sort=True)
                       .agg(aggregations).reset_index())
            for column in derived:
                grouped[column] = None

            deleted_pks = [row[pk_name] for row in rows]
            merged_with_existing = max(0, len(rows) - len(grouped))
            if not dry_run:
                for start in range(0, len(deleted_pks), MAX_IN_VARS):
                    chunk = deleted_pks[start:start + MAX_IN_VARS]
                    pk_params = {f"p{i}": v for i, v in enumerate(chunk)}
                    pk_ph = ", ".join(f":p{i}" for i in range(len(chunk)))
                    self.session.execute(text(
                        f"DELETE FROM {safe_table} WHERE {pk_name} IN ({pk_ph})"
                    ), pk_params)
                insert_columns = [c for c in grouped.columns if c != pk_name]
                statement = text(
                    f"INSERT INTO {safe_table} ({', '.join(insert_columns)}) "
                    f"VALUES ({', '.join(':' + c for c in insert_columns)})")
                payload = []
                for record in grouped.to_dict(orient="records"):
                    cleaned = {}
                    for column in insert_columns:
                        value = record.get(column)
                        if isinstance(value, float) and pd.isna(value):
                            value = None
                        cleaned[column] = value
                    payload.append(cleaned)
                for start in range(0, len(payload), MAX_IN_VARS):
                    self.session.execute(statement,
                                         payload[start:start + MAX_IN_VARS])
                self.session.commit()

            logger.info("  aggregate %s.%s: %d in, %d out (%d merged), %d derived "
                        "cleared%s", table_name, safe_fk, len(rows), len(grouped),
                        merged_with_existing, len(derived),
                        " (dry run)" if dry_run else "")
            results[table_name] = {"rows_in": len(rows),
                                   "rows_written": len(grouped),
                                   "merged_with_existing": merged_with_existing,
                                   "keys_skipped": 0}

        return {"tables": results,
                "rows_written": sum(r["rows_written"] for r in results.values()),
                "errors": errors, "dry_run": bool(dry_run)}

    # -- A8 ----------------------------------------------------------------

    def _match_column(self, table: str, column: str, pack: Any,
                      resolver: ConceptResolver, *, dry_run: bool = False,
                      run_id: Optional[str] = None,
                      settled_values: Optional[Set[str]] = None,
                      context_columns: Tuple[str, ...] = (),
                      stampable: bool = True) -> Dict[str, int]:
        """Reconcile one column against one pack.

        Two writes, two jobs, and both happen here:

        * ``fact_resolution_log`` - append-only, one row per value per run,
          including the alternative candidates. Insert-or-ignore, because a re-run
          of one run id must not rewrite what that run concluded.
        * ``fact_concept_map`` - the current decision, upserted, with a WHERE
          clause that refuses to re-decide a row a human already approved.

        ``stampable=False`` records the decision in ``fact_concept_map`` only: a
        fact table has no ``concept_id``, and A8c rewrites the value from the map.
        """
        from etl.transformers.dimension_index import DimensionKeyIndex, seed_from_rows

        stats: Dict[str, int] = {
            "total": 0, "matched": 0, "exact": 0, "normalized": 0, "alias": 0,
            "fuzzy": 0, "ambiguous": 0, "unresolved": 0, "unmatched": 0,
            "needs_review": 0, "applied": 0, "concept_ids_set": 0, "recorded": 0,
            "ledger": 0, "skipped_settled": 0, "errors": 0}
        try:
            ident_table = require_identifier(table, what="table name")
            ident_column = require_identifier(column, what="column name")
            extra_columns = tuple(
                require_identifier(n, what="column name")
                for n in context_columns if n and n != column)
        except ValueError as exc:
            logger.error("  %s.%s: %s", table, column, exc)
            stats["errors"] += 1
            return stats

        select_list = ", ".join((ident_column,) + extra_columns)
        try:
            rows = self.session.execute(text(
                f"SELECT DISTINCT {select_list} FROM {ident_table} "
                f"WHERE {ident_column} IS NOT NULL ORDER BY {select_list}"
            )).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.error("  could not read distinct values from %s.%s: %s",
                         table, column, exc)
            stats["errors"] += 1
            return stats

        ordered: List[Any] = []
        contexts: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = str(row[0])
            if key not in contexts:
                ordered.append(row[0])
                contexts[key] = {}
            for position, context_name in enumerate(extra_columns, start=1):
                if (contexts[key].get(context_name) is None
                        and row[position] is not None):
                    contexts[key][context_name] = row[position]

        vocabulary_id = getattr(pack, "vocabulary_id", None)
        concept_index = DimensionKeyIndex(
            label=f"dim_concept:{vocabulary_id or 'none'}")
        if vocabulary_id:
            try:
                concept_rows = self.session.execute(text(
                    "SELECT concept_code, concept_id FROM dim_concept "
                    "WHERE vocabulary_id = :v ORDER BY concept_code"
                ), {"v": vocabulary_id}).mappings().all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("  could not read dim_concept for %r: %s.",
                               vocabulary_id, exc)
                concept_rows = []
            if concept_rows:
                seed_from_rows(concept_index, concept_rows,
                               code_field="concept_code", pk_field="concept_id")

        settled = settled_values or set()
        base_context = {"source_table": ident_table, "source_column": ident_column,
                        "target_field": f"{ident_table}.{ident_column}",
                        "run_id": run_id}
        decision_rows: List[Dict[str, Any]] = []
        accepted: Dict[str, List[Any]] = {}

        for value in ordered:
            stats["total"] += 1
            if str(value) in settled:
                stats["skipped_settled"] += 1
                continue
            context = dict(base_context)
            context.update(contexts.get(str(value)) or {})
            try:
                resolution = resolver.resolve(value, pack, context)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  resolution failed for %s.%s = %r: %s: %s",
                               table, column, value, type(exc).__name__, exc)
                stats["errors"] += 1
                continue

            decision = _effective_decision(pack, resolution)
            concept_code = (resolution.concept_code
                            if decision == AUTO_ACCEPTED else None)
            method = resolution.match_method
            if method == METHOD_EXACT:
                stats["exact"] += 1
            elif method == METHOD_NORMALIZED:
                stats["normalized"] += 1
            elif method == METHOD_ALIAS:
                stats["alias"] += 1
            elif method == METHOD_FUZZY:
                stats["fuzzy"] += 1
            elif method == METHOD_AMBIGUOUS:
                stats["ambiguous"] += 1
            elif method == UNRESOLVED:
                stats["unresolved"] += 1
                stats["unmatched"] += 1
            if decision == AUTO_ACCEPTED:
                stats["matched"] += 1
            elif decision == NEEDS_REVIEW:
                stats["needs_review"] += 1

            decision_rows.append({
                "t": ident_table, "c": ident_column, "v": str(value),
                "vocab": resolution.vocabulary_id or vocabulary_id,
                "code": concept_code, "method": method,
                "score": float(resolution.match_score), "decision": decision,
                "vocab_version": (resolution.vocabulary_version
                                  or getattr(pack, "version", None)),
                "resolver_version": resolution.resolver_version, "run_id": run_id,
                "resolved_at": datetime.utcnow()})
            if decision == AUTO_ACCEPTED and concept_code:
                accepted.setdefault(str(concept_code), []).append(value)

        write_failed = False
        if decision_rows and not dry_run:
            batch_size = max(1, min(MAX_IN_VARS, 500))

            # ---- append-only ledger: every decision, every run ------------
            # Written first, with DO NOTHING, because it is the history: a re-run
            # of one run id must not rewrite what that run concluded. This is the
            # write that was missing - the ledger existed, was documented, and
            # nothing in the pipeline populated it, so the database could state
            # the current decision but not the evidence behind it.
            if run_id:
                ledger_statement = text("""
                    INSERT INTO fact_resolution_log
                        (source_table, source_column, source_value, vocabulary_id,
                         vocabulary_version, concept_code, match_method,
                         match_score, decision, resolver_version, run_id,
                         resolved_at)
                    VALUES
                        (:t, :c, :v, :vocab, :vocab_version, :code, :method,
                         :score, :decision, :resolver_version, :run_id,
                         :resolved_at)
                    ON CONFLICT (source_table, source_column, source_value, run_id)
                    DO NOTHING
                """)
                for start in range(0, len(decision_rows), batch_size):
                    chunk = decision_rows[start:start + batch_size]
                    try:
                        self.session.execute(ledger_statement, chunk)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("  could not append %d ledger row(s) for "
                                     "%s.%s: %s", len(chunk), table, column, exc)
                        stats["errors"] += 1
                        self.session.rollback()
                        write_failed = True
                        break
                    stats["ledger"] += len(chunk)
            else:
                _warn_once(
                    f"ledger:no-run-id:{ident_table}.{ident_column}",
                    "  %s.%s: no run id, so %d decision(s) cannot be appended to "
                    "fact_resolution_log (run_id is NOT NULL and part of its "
                    "unique key). The current state is written to "
                    "fact_concept_map; the evidence behind it is lost.",
                    ident_table, ident_column, len(decision_rows))

            # ---- current-state projection: the decision in force ----------
            if not write_failed:
                statement = text("""
                    INSERT INTO fact_concept_map
                        (source_table, source_column, source_value, vocabulary_id,
                         concept_code, match_method, match_score, decision,
                         vocabulary_version, resolver_version, last_run_id)
                    VALUES (:t, :c, :v, :vocab, :code, :method, :score, :decision,
                            :vocab_version, :resolver_version, :run_id)
                    ON CONFLICT (source_table, source_column, source_value)
                    DO UPDATE SET
                        vocabulary_id      = EXCLUDED.vocabulary_id,
                        concept_code       = EXCLUDED.concept_code,
                        match_method       = EXCLUDED.match_method,
                        match_score        = EXCLUDED.match_score,
                        decision           = EXCLUDED.decision,
                        vocabulary_version = EXCLUDED.vocabulary_version,
                        resolver_version   = EXCLUDED.resolver_version,
                        last_run_id        = EXCLUDED.last_run_id
                    WHERE NOT (
                        fact_concept_map.is_approved = 1
                        AND fact_concept_map.concept_code IS NOT NULL
                        AND EXCLUDED.concept_code IS NOT NULL
                        AND fact_concept_map.concept_code <> EXCLUDED.concept_code)
                """)
                for start in range(0, len(decision_rows), batch_size):
                    chunk = decision_rows[start:start + batch_size]
                    try:
                        self.session.execute(statement, chunk)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("  could not record %d decision(s) for "
                                     "%s.%s: %s", len(chunk), table, column, exc)
                        stats["errors"] += 1
                        self.session.rollback()
                        write_failed = True
                        break
                    stats["recorded"] += len(chunk)

        if accepted and not stampable:
            logger.info("    %s.%s: %d accepted concept(s) recorded in "
                        "fact_concept_map only; the table has no concept_id, so A8c "
                        "aligns the value in place.", table, column, len(accepted))
        elif accepted and not dry_run and not write_failed:
            for concept_code in sorted(accepted):
                values = accepted[concept_code]
                entry = concept_index.lookup(concept_code)
                concept_id = entry.pk if entry is not None else None
                if concept_id is None:
                    _warn_once(f"concept-id:{vocabulary_id}:{concept_code}",
                               "  %s.%s: concept %r accepted but absent from "
                               "dim_concept for %r; standard_code written without "
                               "concept_id.", table, column, concept_code,
                               vocabulary_id)
                for start in range(0, len(values), MAX_IN_VARS):
                    chunk = values[start:start + MAX_IN_VARS]
                    stamp = text(
                        f"UPDATE {ident_table} SET standard_code = :code, "
                        f"concept_id = COALESCE(:cid, concept_id) "
                        f"WHERE {ident_column} IN :vals"
                    ).bindparams(bindparam("vals", expanding=True))
                    try:
                        result = self.session.execute(
                            stamp, {"code": concept_code, "cid": concept_id,
                                    "vals": chunk})
                        updated = int(result.rowcount or 0)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("  could not stamp %s.%s with %r: %s",
                                       table, column, concept_code, exc)
                        stats["errors"] += 1
                        continue
                    stats["applied"] += updated
                    if concept_id is not None:
                        stats["concept_ids_set"] += updated

        if not dry_run and not write_failed:
            try:
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("  the commit for %s.%s failed: %s", table, column, exc)
                self.session.rollback()
                stats["errors"] += 1

        logger.info("    %s.%s: %d value(s) vs %s -> %d logged, %d recorded, "
                    "%d applied (%d concept_id), %d needs review, %d unresolved, "
                    "%d error(s)%s",
                    table, column, stats["total"], vocabulary_id or "(no pack)",
                    stats["ledger"], stats["recorded"], stats["applied"],
                    stats["concept_ids_set"], stats["needs_review"],
                    stats["unresolved"], stats["errors"],
                    " (dry run)" if dry_run else "")
        return stats


    def standardize_vocabularies(self, vocab_dir: Optional[str] = None,
                                 fuzzy_threshold: Optional[float] = None,
                                 dry_run: bool = False, *,
                                 registry: Optional[VocabularyRegistry] = None,
                                 run_id: Optional[str] = None) -> Dict[str, Any]:
        """Reconcile every registered field against its pack.

        Country codes are not here: they are static ISO data, standardised at A6a.
        """
        warnings.warn("standardize_vocabularies is the deprecated entry point; "
                      "identity, release, normaliser and thresholds come from the "
                      "pack manifests.", DeprecationWarning, stacklevel=2)

        logger.info("=" * 60)
        logger.info("A8: VOCABULARY RECONCILIATION%s", " (dry run)" if dry_run else "")
        logger.info("=" * 60)
        if vocab_dir is not None:
            logger.warning("  vocab_dir=%r ignored: a vocabulary file is declared "
                           "by the pack manifest that owns it.", vocab_dir)
        if fuzzy_threshold is not None:
            logger.warning("  fuzzy_threshold=%r ignored: the cutoff is "
                           "ConceptPack.fuzzy_threshold, per vocabulary.",
                           fuzzy_threshold)

        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        if resolved_registry is None:
            logger.warning("  no registry available; nothing can be reconciled.")
            return {}
        resolved_run_id = (run_id if run_id is not None
                           else getattr(self, "run_id", None))

        if not dry_run:
            try:
                written = resolved_registry.load_into(self.session)
            except Exception as exc:  # noqa: BLE001
                logger.error("  could not record the registry's packs in "
                             "dim_vocabulary: %s. fact_concept_map.vocabulary_id is "
                             "a foreign key, so reconciliation cannot proceed.", exc)
                self.session.rollback()
                return {}
            logger.info("  registry digest %s: %d pack(s) recorded in dim_vocabulary",
                        resolved_registry.digest()[:12], len(written))

        bindings = resolved_registry.field_bindings()
        if not bindings:
            logger.warning("  the registry binds no field to a pack.")
            return {}

        unbound = [b.target_field for b in iter_bindings()
                   if b.value_type == "text" and b.target_field not in bindings]
        if unbound:
            logger.warning(
                "  %d of %d text field(s) have NO vocabulary pack; their values "
                "are not reconciled, though B5 still merges them on a normalised "
                "key. Each needs one manifest plus one CSV:",
                len(unbound), len(unbound) + len(bindings))
            for field in unbound:
                logger.warning("    %-50s %d distinct value(s)", field,
                               self._distinct_value_count(field))

        def _settled(dimension_table: str, name_column: str) -> Set[str]:
            if not resolved_run_id:
                return set()
            try:
                rows = self.session.execute(text(
                    "SELECT source_value FROM fact_concept_map "
                    "WHERE source_table = :t AND source_column = :c "
                    "AND last_run_id = :r AND decision = :d"
                ), {"t": dimension_table, "c": name_column,
                    "r": resolved_run_id, "d": AUTO_ACCEPTED}).fetchall()
            except Exception as exc:  # noqa: BLE001
                logger.debug("could not read settled values for %s.%s: %s",
                             dimension_table, name_column, exc)
                return set()
            return {str(row[0]) for row in rows if row[0] is not None}

        resolver = ConceptResolver(resolved_registry, self.session)
        results: Dict[str, Any] = {}
        applied_total = 0
        sizes: Dict[Optional[str], int] = {}

        for field in sorted(bindings):
            packs = bindings[field]
            if not packs:
                continue
            pack = packs[0]
            try:
                binding = resolve_field(field)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  the registry binds %r, which FIELD_CATALOG does "
                               "not carry (%s); skipping.", field, exc)
                continue
            name_column = field.split(".", 1)[1]
            vocabulary_id = getattr(pack, "vocabulary_id", None)
            if binding.value_type != "text":
                logger.info("  %s: not reconciled (%s values are not vocabulary "
                            "text).", field, binding.value_type)
                continue
            if vocabulary_id not in sizes:
                sizes[vocabulary_id] = self._vocabulary_size(vocabulary_id)
            if sizes[vocabulary_id] == 0:
                logger.warning(
                    "  %s: pack %r has no concepts in dim_concept "
                    "(vocabulary_id=%r); every value will be 'unresolved'. Load it "
                    "with VocabularyLoader.load_csv(csv, registry.get(pack_id)).",
                    field, getattr(pack, "pack_id", "?"), vocabulary_id)
            if len(packs) > 1:
                logger.info("  %s: %d pack(s) cover this field; %r wins (rank: %s).",
                            field, len(packs), getattr(pack, "pack_id", "?"),
                            ", ".join(getattr(p, "pack_id", "?") for p in packs))

            context_columns = (("country_code",)
                               if binding.dimension_table == "dim_location" else ())
            key = f"{binding.dimension_table}.{name_column}"
            stats = self._match_column(
                binding.dimension_table, name_column, pack, resolver,
                dry_run=dry_run, run_id=resolved_run_id,
                settled_values=_settled(binding.dimension_table, name_column),
                context_columns=context_columns, stampable=can_stamp(field))
            results[key] = stats
            applied_total += int(stats.get("applied", 0))

        if not dry_run:
            try:
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("  the reconciliation commit failed: %s", exc)
                self.session.rollback()

        logger.info("=" * 60)
        logger.info("RECONCILIATION COMPLETE")
        for key in sorted(results):
            s = results[key]
            logger.info("  %s: %d value(s), %d applied (%d exact, %d normalized, "
                        "%d alias, %d fuzzy), %d needs review, %d unresolved, "
                        "%d settled, %d error(s)", key, s["total"], s["applied"],
                        s["exact"], s["normalized"], s["alias"], s["fuzzy"],
                        s["needs_review"], s["unresolved"], s["skipped_settled"],
                        s["errors"])
        logger.info("  rows stamped with a concept: %d", applied_total)
        logger.info("=" * 60)
        return results

    def _vocabulary_size(self, vocabulary_id: Optional[str]) -> int:
        if not vocabulary_id:
            return 0
        try:
            return int(self.session.execute(
                text("SELECT COUNT(*) FROM dim_concept WHERE vocabulary_id = :v"),
                {"v": vocabulary_id}).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not count dim_concept for %s: %s", vocabulary_id, exc)
            return 0

    # -- B5 ----------------------------------------------------------------

    def consolidate_vocabulary(self, merge_policies: Optional[Dict[str, Any]] = None,
                               *, registry: Optional[VocabularyRegistry] = None,
                               run_id: Optional[str] = None,
                               dry_run: bool = False) -> Dict[str, Any]:
        """Merge every dimension in the catalog, not only packed ones.

        A merge is a property of the schema; driving the work list from
        registry.merge_policies() left every dimension with no manifest
        unconsolidated. One table failing rolls back that table and continues.
        """
        logger.info("=" * 60)
        logger.info("B5: MERGE DIMENSION ROWS BY CONCEPT%s",
                    " (dry run)" if dry_run else "")
        logger.info("=" * 60)

        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        by_table: Dict[str, List[str]] = {}
        for field in dimension_fields():
            try:
                binding = resolve_field(field)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  catalog field %r is unreadable (%s); skipping",
                               field, exc)
                continue
            if binding.value_type != "text":
                continue
            by_table.setdefault(binding.dimension_table, []).append(field)
        if not by_table:
            logger.info("  the catalog holds no text dimension field.")
            return {}

        results: Dict[str, Any] = {}
        with_vocabulary: List[str] = []
        without_vocabulary: List[str] = []
        failures: List[str] = []

        for table_name in sorted(by_table):
            field = sorted(by_table[table_name])[0]
            binding = resolve_field(field)
            model_class = _model_for_table(binding.dimension_table)
            if model_class is None:
                logger.warning("  %s: no model for %r; skipping",
                               field, binding.dimension_table)
                continue
            primary_key = list(model_class.__table__.primary_key.columns)
            if len(primary_key) == 1 and isinstance(primary_key[0].type, Integer):
                logger.info("  %s: skipped (surrogate-keyed; A6 merges it on "
                            "standard_code)", binding.dimension_table)
                results[binding.dimension_table] = {
                    "status": "skipped",
                    "reason": "surrogate-keyed dimension; A6 owns it"}
                continue

            pack = None
            if resolved_registry is not None:
                try:
                    pack = resolved_registry.select_pack(field)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("no pack for %r: %s", field, exc)
            key_function = (_pack_key_fn(pack) if pack is not None
                            else _default_vocab_key)
            if pack is None:
                without_vocabulary.append(binding.dimension_table)
            else:
                with_vocabulary.append(
                    f"{binding.dimension_table}={getattr(pack, 'pack_id', '?')}")

            try:
                report = self._consolidate_dimension(
                    model_class, field.split(".", 1)[1], binding.dimension_key,
                    fallback_key_fn=key_function, pack=pack, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                self.session.rollback()
                failures.append(binding.dimension_table)
                logger.error("  %s: merge failed (%s: %s); rolled back, remaining "
                             "phases continue.", binding.dimension_table,
                             type(exc).__name__, exc)
                results[binding.dimension_table] = {
                    "status": "error", "table": binding.dimension_table,
                    "duplicates_found": 0, "deleted": 0,
                    "reason": f"{type(exc).__name__}: {exc}"}
                continue
            report["vocabulary"] = getattr(pack, "pack_id", None)
            results[binding.dimension_table] = report

        if without_vocabulary:
            logger.info("  merged on the generic key (no vocabulary governs): %s",
                        ", ".join(sorted(without_vocabulary)))
        if with_vocabulary:
            logger.info("  merged on the pack's normaliser: %s",
                        ", ".join(sorted(with_vocabulary)))
        if failures:
            logger.warning("  %d table(s) left unmerged because their merge failed: "
                           "%s", len(failures), ", ".join(sorted(failures)))
        if not dry_run:
            try:
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("  the merge-pass commit failed: %s", exc)
                self.session.rollback()
        return results

    def _pick_canonical_vector(self, records: List, pack=None):
        """concept_id, standard_code, pack bonus, name length, fact usage."""
        candidates = sorted(records, key=lambda r: str(getattr(r, "vector_code", "")))
        if not candidates:
            raise ValueError("_pick_canonical_vector needs at least one record")
        if len(candidates) == 1:
            return candidates[0]
        usage: Dict[str, int] = {}
        codes = sorted({str(getattr(r, "vector_code", "")) for r in candidates
                        if getattr(r, "vector_code", None) is not None})
        for start in range(0, len(codes), MAX_IN_VARS):
            statement = text(
                "SELECT vector_code, COUNT(*) FROM fact_vector_observation "
                "WHERE vector_code IN :vals GROUP BY vector_code "
                "ORDER BY vector_code").bindparams(bindparam("vals", expanding=True))
            try:
                for code, count in self.session.execute(
                        statement, {"vals": codes[start:start + MAX_IN_VARS]}).fetchall():
                    usage[str(code)] = int(count or 0)
            except Exception as exc:  # noqa: BLE001
                logger.debug("could not count fact usage: %s", exc)
                usage = {}
                break
        bonuses = [_CANONICAL_FORM_BONUSES[t] for t in _rule_tokens(pack)
                   if t in _CANONICAL_FORM_BONUSES]

        def score(record) -> int:
            total = 0
            if getattr(record, "concept_id", None) is not None:
                total += 1_000_000
            if str(getattr(record, "standard_code", None) or "").strip():
                total += 100_000
            name = str(getattr(record, "species_name", "") or "").strip()
            for bonus in bonuses:
                total += bonus(name)
            total += len(name)
            total += min(usage.get(str(getattr(record, "vector_code", "")), 0), 9_999)
            return total

        return max(candidates, key=score)

    def _normalize_species_name(self, name):
        """Deprecated: delegates to normalizers['species_canon']."""
        warnings.warn("_normalize_species_name is deprecated; use "
                      "utilities.vocabulary.normalizers.NORMALIZERS['species_canon'].",
                      DeprecationWarning, stacklevel=2)
        return _normalizers.NORMALIZERS["species_canon"](name)

    # -- FK alignment ------------------------------------------------------

    def align_all_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """Repair orphans by name before NULLing what is left (not the reverse)."""
        logger.info("=" * 60)
        logger.info("ALIGNING ALL FOREIGN KEYS")
        logger.info("=" * 60)
        results: Dict[str, Any] = {"dimensions": {}, "facts": {}, "summary": {
            "total_fixed": 0, "tables_processed": 0,
            "orphans_found": 0, "quarantined": 0}}
        self._clean_dimension_tables(dry_run)
        fact_alignments = self._align_fact_foreign_keys(dry_run)
        results["facts"] = fact_alignments
        results["dimensions"] = self._align_dimension_foreign_keys(dry_run)
        for _key, stats in fact_alignments.items():
            if isinstance(stats, dict):
                results["summary"]["total_fixed"] += stats.get("fixed", 0)
                results["summary"]["orphans_found"] += stats.get("orphaned", 0)
                results["summary"]["quarantined"] += stats.get("quarantined", 0)
                results["summary"]["tables_processed"] += 1
        logger.info("  Repaired: %d | Orphans: %d | Quarantined: %d | Tables: %d",
                    results["summary"]["total_fixed"],
                    results["summary"]["orphans_found"],
                    results["summary"]["quarantined"],
                    results["summary"]["tables_processed"])
        return results

    def _clean_dimension_tables(self, dry_run: bool = False) -> None:
        """Report (never delete) dimension rows no fact table references."""
        inspector = inspect(self.session.bind)
        all_tables = inspector.get_table_names()
        fact_tables = [t for t in all_tables if t.startswith("fact_")]
        dim_tables = [t for t in all_tables
                      if t.startswith("dim_") and t != "dim_time"]
        for dim_table in dim_tables:
            pk_cols = inspector.get_pk_constraint(dim_table).get(
                "constrained_columns", [])
            if not pk_cols:
                continue
            pk_col = pk_cols[0]
            referencing = []
            for fact_table in fact_tables:
                for fk in inspector.get_foreign_keys(fact_table):
                    if fk.get("referred_table") != dim_table:
                        continue
                    constrained = fk.get("constrained_columns") or []
                    if constrained:
                        referencing.append((fact_table, constrained[0]))
            if not referencing:
                logger.info("  %s has no fact references, keeping all records",
                            dim_table)
                continue
            referenced_ids: Set[Any] = set()
            for fact_table, fk_col in referencing:
                try:
                    referenced_ids.update(
                        row[0] for row in self.session.execute(text(
                            f"SELECT DISTINCT {fk_col} FROM {fact_table} "
                            f"WHERE {fk_col} IS NOT NULL")))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("  Could not query %s.%s: %s",
                                 fact_table, fk_col, exc)
            if not referenced_ids:
                continue
            orphaned = _chunked_in(
                self.session,
                f"SELECT {pk_col} FROM {dim_table} WHERE {pk_col} NOT IN :vals",
                {}, referenced_ids)
            if orphaned and not dry_run:
                logger.info("  %d unreferenced record(s) in %s (kept)",
                            len(orphaned), dim_table)

    
    def _align_dimension_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """NULL fact FKs pointing at a dimension key that does not exist.

        A NOT NULL FK cannot be NULLed, so those columns are reported and left
        alone rather than silently skipped: the row needs a human, and a counter
        that no caller prints is not a report.
        """
        dimension_models = {
            "dim_location": (DimLocation, "location_id"),
            "dim_disease": (DimDisease, "disease_code"),
            "dim_vector": (DimVector, "vector_code"),
            "dim_insecticide": (DimInsecticide, "insecticide_code"),
            "dim_source": (DimSource, "source_code"),
            "dim_identification_method": (DimIdentificationMethod, "method_code"),
            "dim_resistance_test_type": (DimResistanceTestType, "test_type_code"),
            "dim_socioeconomic_indicator": (DimSocioeconomicIndicator,
                                            "indicator_code")}
        inspector = inspect(self.session.bind)
        fact_tables = [t for t in inspector.get_table_names()
                       if t.startswith("fact_")]
        results: Dict[str, Any] = {}
        for dim_name, (model, pk_col) in dimension_models.items():
            results[dim_name] = {"fixed": 0, "errors": 0, "skipped_notnull": 0}
            valid_ids = {getattr(r, pk_col) for r in self.session.query(model).all()
                         if getattr(r, pk_col) is not None}
            if not valid_ids:
                continue
            valid_list = sorted(valid_ids, key=str)
            for fact_table in fact_tables:
                for fk in inspector.get_foreign_keys(fact_table):
                    if fk.get("referred_table") != dim_name:
                        continue
                    constrained = fk.get("constrained_columns") or []
                    if not constrained:
                        continue
                    fk_col = constrained[0]
                    col_info = {c["name"]: c
                                for c in inspector.get_columns(fact_table)}
                    if not col_info.get(fk_col, {}).get("nullable", True):
                        results[dim_name]["skipped_notnull"] += 1
                        # Logged, because the previous version only incremented a
                        # counter that no caller prints. A NOT NULL FK whose
                        # target is missing cannot be cleared, so the row needs
                        # attention and the run said nothing about it.
                        logger.warning(
                            "    %s.%s: NOT NULL foreign key, so an unresolvable "
                            "value cannot be cleared; left as-is for manual "
                            "review.", fact_table, fk_col)
                        continue
                    try:
                        invalid = 0
                        for start in range(0, len(valid_list), MAX_IN_VARS):
                            chunk = valid_list[start:start + MAX_IN_VARS]
                            stmt = text(
                                f"SELECT COUNT(*) FROM {fact_table} WHERE {fk_col} "
                                f"IS NOT NULL AND {fk_col} NOT IN :vals"
                            ).bindparams(bindparam("vals", expanding=True))
                            invalid += int(self.session.execute(
                                stmt, {"vals": chunk}).scalar() or 0)
                        if not invalid:
                            continue
                        if dry_run:
                            results[dim_name]["fixed"] += invalid
                            continue
                        for start in range(0, len(valid_list), MAX_IN_VARS):
                            chunk = valid_list[start:start + MAX_IN_VARS]
                            stmt = text(
                                f"UPDATE {fact_table} SET {fk_col} = NULL "
                                f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals"
                            ).bindparams(bindparam("vals", expanding=True))
                            self.session.execute(stmt, {"vals": chunk})
                        results[dim_name]["fixed"] += invalid
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("    Could not align %s.%s: %s",
                                       fact_table, fk_col, exc)
                        results[dim_name]["errors"] += 1
            if not dry_run:
                self.session.commit()
        return results


    def _align_fact_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """LEGACY: repair orphans by LIKE, then NULL or quarantine the rest.

        Keyed per (table, column); a non-nullable FK moves unresolvable rows to
        _quarantine_<table>, which is a real DELETE, logged loudly.
        """
        from collections import Counter

        _warn_legacy_alignment()
        configs = [
            ("fact_vector_observation", "location_id", "dim_location", "location_id", "name"),
            ("fact_vector_observation", "vector_code", "dim_vector", "vector_code", "species_name"),
            ("fact_vector_observation", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_disease_case", "location_id", "dim_location", "location_id", "name"),
            ("fact_disease_case", "disease_code", "dim_disease", "disease_code", "disease_name"),
            ("fact_disease_case", "identification_method_code", "dim_identification_method", "method_code", "method_name"),
            ("fact_disease_case", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_insecticide_resistance", "location_id", "dim_location", "location_id", "name"),
            ("fact_insecticide_resistance", "vector_code", "dim_vector", "vector_code", "species_name"),
            ("fact_insecticide_resistance", "insecticide_code", "dim_insecticide", "insecticide_code", "insecticide_name"),
            ("fact_insecticide_resistance", "test_type_code", "dim_resistance_test_type", "test_type_code", "test_type_name"),
            ("fact_insecticide_resistance", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_bionomics", "location_id", "dim_location", "location_id", "name"),
            ("fact_bionomics", "vector_code", "dim_vector", "vector_code", "species_name"),
            ("fact_bionomics", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_intervention", "location_id", "dim_location", "location_id", "name"),
            ("fact_intervention", "target_disease_code", "dim_disease", "disease_code", "disease_name"),
            ("fact_intervention", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_socioeconomic_condition", "location_id", "dim_location", "location_id", "name"),
            ("fact_socioeconomic_condition", "indicator_code", "dim_socioeconomic_indicator", "indicator_code", "indicator_name"),
            ("fact_socioeconomic_condition", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_health_facility", "location_id", "dim_location", "location_id", "name"),
            ("fact_health_facility", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_disease_burden", "location_id", "dim_location", "location_id", "name"),
            ("fact_disease_burden", "disease_code", "dim_disease", "disease_code", "disease_name"),
            ("fact_disease_burden", "source_code", "dim_source", "source_code", "source_name"),
            ("fact_environment", "location_id", "dim_location", "location_id", "name"),
            ("fact_environment", "source_code", "dim_source", "source_code", "source_name"),
        ]

        results: Dict[str, Dict[str, Any]] = {}
        seen: Set[Tuple[str, str]] = set()
        inspector = inspect(self.session.bind)

        for table_name, fk_col, dim_table, pk_col, name_col in configs:
            if (table_name, fk_col) in seen:
                continue
            seen.add((table_name, fk_col))
            key = f"{table_name}.{fk_col}"
            results[key] = {"table": table_name, "column": fk_col,
                            "orphaned": 0, "fixed": 0, "still_orphaned": 0,
                            "quarantined": 0, "details": []}
            try:
                valid_values = {row[0] for row in self.session.execute(
                    text(f"SELECT {pk_col} FROM {dim_table}")).fetchall()
                    if row[0] is not None}
            except Exception as exc:  # noqa: BLE001
                logger.warning("    Could not get valid values from %s: %s",
                               dim_table, exc)
                continue
            if not valid_values:
                continue

            valid_list = sorted(valid_values, key=str)
            orphan_counter: Counter = Counter()
            query_error = False
            for start in range(0, len(valid_list), MAX_IN_VARS):
                chunk = valid_list[start:start + MAX_IN_VARS]
                stmt = text(
                    f"SELECT {fk_col}, COUNT(*) FROM {table_name} "
                    f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals "
                    f"GROUP BY {fk_col}").bindparams(bindparam("vals", expanding=True))
                try:
                    for value, count in self.session.execute(
                            stmt, {"vals": chunk}).fetchall():
                        orphan_counter[value] += count
                except Exception as exc:  # noqa: BLE001
                    logger.warning("    Could not query orphans for %s.%s: %s",
                                   table_name, fk_col, exc)
                    query_error = True
                    break
            if query_error or not orphan_counter:
                continue

            candidates = list(orphan_counter.keys())
            truly_orphan: Counter = Counter()
            for start in range(0, len(candidates), MAX_IN_VARS):
                chunk = candidates[start:start + MAX_IN_VARS]
                present = {row[0] for row in self.session.execute(
                    text(f"SELECT {pk_col} FROM {dim_table} WHERE {pk_col} IN :vals")
                    .bindparams(bindparam("vals", expanding=True)),
                    {"vals": chunk}).fetchall()}
                for value in chunk:
                    if value not in present:
                        truly_orphan[value] = orphan_counter[value]
            orphan_counter = truly_orphan
            if not orphan_counter:
                continue

            orphaned_values = list(orphan_counter.keys())
            results[key]["orphaned"] = sum(orphan_counter.values())
            results[key]["details"] = [{"value": v, "count": orphan_counter[v]}
                                       for v in orphaned_values[:100]]
            if dry_run:
                continue

            pk_cols_list = inspector.get_pk_constraint(table_name).get(
                "constrained_columns", [])
            single_pk = pk_cols_list[0] if len(pk_cols_list) == 1 else None
            fixed_values: List[Any] = []
            remaining_values: List[Any] = []

            for orphan_val in orphaned_values:
                new_val = None
                if name_col and isinstance(orphan_val, str):
                    try:
                        match = self.session.execute(text(
                            f"SELECT {pk_col} FROM {dim_table} "
                            f"WHERE LOWER({name_col}) LIKE LOWER(:pat) LIMIT 1"
                        ), {"pat": f"%{orphan_val}%"}).first()
                        if match:
                            new_val = match[0]
                    except Exception:  # noqa: BLE001
                        pass
                if new_val is None:
                    remaining_values.append(orphan_val)
                    continue
                try:
                    self.session.execute(text(
                        f"UPDATE {table_name} SET {fk_col} = :new_val "
                        f"WHERE {fk_col} = :old_val"
                    ), {"new_val": new_val, "old_val": orphan_val})
                    self.session.commit()
                    fixed_values.append(orphan_val)
                    continue
                except IntegrityError:
                    self.session.rollback()

                if single_pk is None:
                    remaining_values.append(orphan_val)
                    continue
                try:
                    row_ids = [r[0] for r in self.session.execute(text(
                        f"SELECT {single_pk} FROM {table_name} "
                        f"WHERE {fk_col} = :old_val"),
                        {"old_val": orphan_val}).fetchall()]
                except Exception:  # noqa: BLE001
                    remaining_values.append(orphan_val)
                    continue
                still_orphan = False
                for row_id in row_ids:
                    try:
                        self.session.execute(text(
                            f"UPDATE {table_name} SET {fk_col} = :new_val "
                            f"WHERE {single_pk} = :row_id"),
                            {"new_val": new_val, "row_id": row_id})
                        self.session.commit()
                    except IntegrityError:
                        self.session.rollback()
                        still_orphan = True
                        break
                if still_orphan:
                    remaining_values.append(orphan_val)
                else:
                    fixed_values.append(orphan_val)

            col_info = {c["name"]: c for c in inspector.get_columns(table_name)}
            fk_nullable = col_info.get(fk_col, {}).get("nullable", True)
            if remaining_values:
                if fk_nullable:
                    for start in range(0, len(remaining_values), MAX_IN_VARS):
                        chunk = remaining_values[start:start + MAX_IN_VARS]
                        stmt = text(
                            f"UPDATE {table_name} SET {fk_col} = NULL "
                            f"WHERE {fk_col} IN :vals"
                        ).bindparams(bindparam("vals", expanding=True))
                        self.session.execute(stmt, {"vals": chunk})
                    logger.warning("    %s.%s: NULLed %d unresolvable FK value(s)",
                                   table_name, fk_col, len(remaining_values))
                else:
                    qname = f"_quarantine_{table_name}"
                    self.session.execute(text(
                        f"CREATE TABLE IF NOT EXISTS {qname} AS SELECT *, "
                        f"CAST(NULL AS TEXT) AS _orphan_fk_col, "
                        f"CAST(NULL AS TEXT) AS _orphan_value, "
                        f"CAST(NULL AS TIMESTAMP) AS _quarantined_at "
                        f"FROM {table_name} WHERE 1=0"))
                    moved = 0
                    for start in range(0, len(remaining_values), MAX_IN_VARS):
                        chunk = remaining_values[start:start + MAX_IN_VARS]
                        self.session.execute(text(
                            f"INSERT INTO {qname} SELECT *, :fk_col_name, "
                            f"CAST({fk_col} AS TEXT), CURRENT_TIMESTAMP "
                            f"FROM {table_name} WHERE {fk_col} IN :vals"
                        ).bindparams(bindparam("vals", expanding=True)),
                        {"vals": chunk, "fk_col_name": fk_col})
                        result = self.session.execute(text(
                            f"DELETE FROM {table_name} WHERE {fk_col} IN :vals"
                        ).bindparams(bindparam("vals", expanding=True)),
                        {"vals": chunk})
                        moved += int(result.rowcount or 0)
                    results[key]["quarantined"] = moved
                    logger.warning(
                        "    %s.%s: MOVED %d row(s) into %s (NOT NULL FK that does "
                        "not resolve). Restore with INSERT INTO %s SELECT ... FROM "
                        "%s.", table_name, fk_col, moved, qname, table_name, qname)
                self.session.commit()

            results[key]["fixed"] = sum(orphan_counter[v] for v in fixed_values)
            remaining_orphans = 0
            for start in range(0, len(valid_list), MAX_IN_VARS):
                chunk = valid_list[start:start + MAX_IN_VARS]
                stmt = text(
                    f"SELECT COUNT(*) FROM {table_name} "
                    f"WHERE {fk_col} IS NOT NULL AND {fk_col} NOT IN :vals"
                ).bindparams(bindparam("vals", expanding=True))
                remaining_orphans += int(self.session.execute(
                    stmt, {"vals": chunk}).scalar() or 0)
            results[key]["still_orphaned"] = remaining_orphans
            if remaining_orphans:
                logger.error("    %s.%s: %d row(s) still carry an unresolvable FK; "
                             "manual review.", table_name, fk_col, remaining_orphans)
            else:
                logger.info("    %s.%s: repair complete (%d fixed, %d orphaned)",
                            table_name, fk_col, results[key]["fixed"],
                            results[key]["orphaned"])
        return results

    # -- audit helpers -----------------------------------------------------

    def validate_country_codes(self) -> Dict[str, Any]:
        """Country coverage, per-code counts, and code/name consistency.

        A non-zero name_mismatches after A6a means something wrote the pair
        directly: country_name is no longer the canonical label for its code.
        """
        _build_country_tables()
        try:
            rows = self.session.execute(text(
                "SELECT country_code, country_name FROM dim_location "
                "ORDER BY location_id")).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read country columns: %s", exc)
            return {"total": 0, "valid": 0, "invalid": 0, "missing": 0,
                    "invalid_values": [], "by_code": {}, "name_mismatches": 0,
                    "name_mismatch_samples": []}

        valid = invalid = missing = 0
        invalid_values: Set[str] = set()
        by_code: Dict[str, int] = {}
        name_mismatch = 0
        mismatch_samples: List[Dict[str, Any]] = []

        for code, name in rows:
            text_value = "" if code is None else str(code).strip().upper()
            if not text_value:
                missing += 1
                continue
            if (text_value in _COUNTRY_BY_ISO2 or text_value in ISO_COUNTRY_CODES
                    or text_value in ISO_TO_COUNTRY_NAME):
                valid += 1
            else:
                invalid += 1
                invalid_values.add(text_value)
            by_code[text_value] = by_code.get(text_value, 0) + 1
            canonical = _country_name_for(text_value)
            if canonical and str(name or "").strip() != canonical:
                name_mismatch += 1
                if len(mismatch_samples) < 10:
                    mismatch_samples.append({"country_code": text_value,
                                             "country_name": name,
                                             "expected": canonical})
        if name_mismatch:
            logger.warning("  country: %d row(s) carry a country_name that is not "
                           "the canonical label for their code; A6a did not cover "
                           "them.", name_mismatch)
        return {"total": len(rows), "valid": valid, "invalid": invalid,
                "missing": missing, "invalid_values": sorted(invalid_values)[:20],
                "by_code": {c: by_code[c] for c in sorted(by_code)},
                "name_mismatches": name_mismatch,
                "name_mismatch_samples": mismatch_samples}

    def _location_vocabulary_id(self, registry: Any = None) -> Optional[str]:
        loaded = _load_registry(registry if registry is not None
                                else getattr(self, "registry", None))
        if loaded is None:
            return None
        try:
            return getattr(loaded.select_pack("dim_location.name"),
                           "vocabulary_id", None)
        except Exception:  # noqa: BLE001
            return None

    def _check_hierarchy(self, registry: Any = None) -> Dict[str, Any]:
        pack = None
        loaded = _load_registry(registry if registry is not None
                                else getattr(self, "registry", None))
        if loaded is not None:
            try:
                pack = loaded.select_pack("dim_location.name")
            except Exception as exc:  # noqa: BLE001
                logger.debug("no location pack for the hierarchy check: %s", exc)
        admin_levels = set((getattr(pack, "admin_level_map", {}) or {}).values())
        rows = self.session.execute(text(
            "SELECT admin_level, parent_location_id FROM dim_location "
            "ORDER BY location_id")).fetchall()
        placed = total = 0
        for admin_level, parent_id in rows:
            if admin_levels and str(admin_level or "") in admin_levels:
                continue
            total += 1
            if parent_id is not None:
                placed += 1
        return {"placed": placed, "total_non_admin": total}

    def _check_remaining_orphans(self) -> Dict[str, int]:
        orphans: Dict[str, int] = {}
        for model_class in _dimension_models():
            table_name = model_class.__tablename__
            pk_columns = list(model_class.__table__.primary_key.columns)
            if len(pk_columns) != 1:
                continue
            pk_column = pk_columns[0].name
            for fact_table, fk_column in _dimension_fact_map(model_class):
                try:
                    safe_table = require_identifier(fact_table, what="table name")
                    safe_column = require_identifier(fk_column, what="column name")
                    safe_dim = require_identifier(table_name, what="table name")
                    safe_pk = require_identifier(pk_column, what="column name")
                except ValueError:
                    continue
                count = self._scalar_or_zero(
                    f"SELECT COUNT(*) FROM {safe_table} f WHERE f.{safe_column} "
                    f"IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {safe_dim} d "
                    f"WHERE d.{safe_pk} = f.{safe_column})")
                if count:
                    orphans[f"{safe_table}.{safe_column}"] = count
        return orphans

    def _check_concept_conflicts(self, vocabulary_id: Optional[str]) -> Dict[str, int]:
        """concept_id vs the concept its own standard_code names.

        The check this replaces compared standard_code to location_code, which are
        never equal, so it flagged every stamped row and measured nothing.
        """
        result = {"stamped": 0, "conflicting": 0, "dangling": 0}
        if not vocabulary_id:
            return result
        result["stamped"] = self._scalar_or_zero_raw(
            "SELECT COUNT(*) FROM dim_location WHERE concept_id IS NOT NULL", {})
        result["conflicting"] = self._scalar_or_zero_raw(
            "SELECT COUNT(*) FROM dim_location l "
            "WHERE l.concept_id IS NOT NULL AND l.standard_code IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM dim_concept c "
            "            WHERE c.vocabulary_id = :v "
            "              AND c.concept_code = l.standard_code "
            "              AND c.concept_id <> l.concept_id)", {"v": vocabulary_id})
        result["dangling"] = self._scalar_or_zero_raw(
            "SELECT COUNT(*) FROM dim_location l "
            "WHERE l.standard_code IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM dim_concept c "
            "                WHERE c.vocabulary_id = :v "
            "                  AND c.concept_code = l.standard_code)",
            {"v": vocabulary_id})
        return result

    # -- burden / resistance ----------------------------------------------

    def fix_orphaned_burden_records(self) -> int:
        logger.info("=" * 60)
        logger.info("FIXING ORPHANED BURDEN RECORDS")
        logger.info("=" * 60)
        valid_ids = [int(r[0]) for r in self.session.execute(
            text("SELECT location_id FROM dim_location ORDER BY location_id")
        ).fetchall()]
        if not valid_ids:
            logger.info("No valid locations found; nothing to fix")
            return 0
        orphaned = _chunked_in(self.session, """
            SELECT burden_id, location_id, year, disease_code
            FROM fact_disease_burden
            WHERE location_id IS NOT NULL AND location_id NOT IN :vals
        """, {}, valid_ids)
        if not orphaned:
            logger.info("No orphaned burden records found")
            return 0
        seen: Set[Any] = set()
        unique_rows = []
        for row in orphaned:
            if row[0] in seen:
                continue
            seen.add(row[0])
            unique_rows.append(row)
        logger.info("Found %d orphaned burden record(s)", len(unique_rows))
        by_code = {str(code): int(loc_id) for loc_id, code in self.session.execute(
            text("SELECT location_id, location_code FROM dim_location "
                 "WHERE location_code IS NOT NULL")).fetchall()}
        reassignments: Dict[Any, Any] = {}
        unresolved: List[Any] = []
        for _burden_id, bad_location_id, _year, _disease in unique_rows:
            target = by_code.get(str(bad_location_id).strip())
            if target is None:
                unresolved.append(bad_location_id)
                continue
            reassignments[bad_location_id] = target
        if unresolved:
            logger.warning("  %d orphan value(s) have no matching location_code; "
                           "left untouched. Samples: %s", len(unresolved),
                           sorted({str(v) for v in unresolved})[:10])
        fixed = 0
        for bad_id, new_id in sorted(reassignments.items(), key=lambda i: str(i[0])):
            if bad_id == new_id:
                continue
            try:
                self._aggregate_and_reassign([{
                    "table": "fact_disease_burden", "fk": "location_id",
                    "old": bad_id, "new": new_id}])
                fixed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("  Could not reassign burden location %r -> %r: %s",
                               bad_id, new_id, exc)
        self.session.commit()
        logger.info("Repaired %d orphan location group(s)", fixed)
        return fixed


    def expand_burden_to_daily(
        self,
        force: bool = False,
        seed: int = 42,
        base_variation: Optional[float] = None,
        *,
        default_population: Optional[float] = None,
        population_indicator: str = "SP.POP.TOTL",
        national_allocation: bool = False,
        population_divisor: Optional[float] = None,
        rate_scale: str = "per_100k",
        prefer: str = _BURDEN_MEASURE_CASES_MEAN,
        group_by_level: Optional[str] = None,
        locations: Optional[Iterable[int]] = None,
        years: Optional[Iterable[int]] = None,
        limit: Optional[int] = None,
        allow_mixed_grain: bool = False,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Expand yearly burden into daily cases summing to each annual total.

        A record carries one of two measures, or both:

        ``cases_mean``
            An absolute case count for the cell-year. Self-contained: no
            population, no allocation, no unit assumption.
        ``incidence_rate``
            A rate whose scale the schema does not document. Converted with
            ``rate / _RATE_SCALES[rate_scale] * population``.

        **``prefer`` decides which is used when both are present**, and it
        defaults to ``cases_mean``. Preferring the rate discards a measured
        absolute count in favour of an assumed population denominator, which is
        what the previous ordering did: every record carrying both was routed to
        the rate branch, found no denominator, and was counted in
        ``no_population``.

        Every choice that changes a stored number is recorded - measure
        preference, rate scale, allocation and divisor, write grain, seed, kernel
        origin and the kernel's own weights - in
        ``run_manifest.parameters.expansion`` (see :mod:`utilities.run_manifest`).
        A deposited database can therefore be attributed to a stated policy, and
        two runs compared without reading code.

        Expanded rows are marked twice: ``identification_method_code =
        'ESTIMATE'`` and ``case_status = 'estimated'``. The second is new.
        Writing ``'confirmed'`` asserted that a modelled figure was an observed
        case, and any analysis filtering on confirmed cases would have counted
        synthetic rows as observations.

        Parameters
        ----------
        base_variation : float, optional
            ``None`` uses the allocation kernel's value, so a caller cannot
            re-introduce a hard-coded sigma by omitting the argument.
        allow_mixed_grain : bool
            Refuse to add a second write grain to the same years unless this is
            set. Two grains coexist harmlessly at the storage level - the case
            keys differ by ``location_id``, so the unique constraint never fires -
            and then every downstream sum double-counts by the grouping factor,
            silently and unrecoverably.
        run_id : str, optional
            For the manifest. Falls back to ``self.run_id``.

        Returns
        -------
        dict
            ``inserted``, ``already_present``, ``skipped``, ``no_population``,
            ``errors`` - the five keys the CLI has always read - plus ``records``,
            ``groups``, ``merged``, ``zero``, ``years_prepared``, ``tiers``,
            ``basis``, ``grain``, ``grain_targets``, ``unparented_cells``,
            ``kernel`` and ``purged``.

        Notes
        -----
        Records that share one ``(location, year, disease, source)`` are **summed
        into one series before writing**. ``uq_fact_case_natural`` has no pathogen
        column, so two burden rows for one disease in one place and year would
        otherwise expand to the same 365 keys and the second insert would violate
        the constraint.

        The delete window includes ``source_code``: it is part of the case key, so
        clearing a window without it would delete another source's rows.

        ``_ensure_dim_time`` runs once per **year**, not once per record.
        """
        if rate_scale not in _RATE_SCALES:
            raise ValueError(
                f"rate_scale must be one of {sorted(_RATE_SCALES)}, got "
                f"{rate_scale!r}")
        if prefer not in _BURDEN_MEASURE_PREFERENCES:
            raise ValueError(
                f"prefer must be one of {list(_BURDEN_MEASURE_PREFERENCES)}, got "
                f"{prefer!r}")
        scale = float(_RATE_SCALES[rate_scale])
        effective_run_id = run_id or getattr(self, "run_id", None)

        logger.info("=" * 60)
        logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS")
        logger.info("  measure: %s (rate scale %r)", prefer, rate_scale)
        logger.info("=" * 60)

        try:
            self.session.execute(text("SELECT case_count FROM fact_disease_case "
                                      "LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            logger.error("case_count column missing; run `alembic upgrade head`.")
            raise RuntimeError("Missing case_count column; run migrations first.") \
                from exc

        self.session.execute(text(
            "INSERT INTO dim_identification_method "
            "(method_code, method_name, method_type) "
            "VALUES ('ESTIMATE', 'Estimated', 'modeled') "
            "ON CONFLICT (method_code) DO NOTHING"))
        self.session.commit()

        empty: Dict[str, Any] = {
            "inserted": 0, "already_present": 0, "skipped": 0,
            "no_population": 0, "errors": 0, "records": 0, "groups": 0,
            "merged": 0, "zero": 0, "years_prepared": 0, "tiers": {},
            "basis": {}, "grain": "grid cell", "grain_targets": 0,
            "unparented_cells": 0, "kernel": FLAT_BURDEN_KERNEL.origin,
            "purged": 0}

        if not self._scalar_or_zero("SELECT COUNT(*) FROM fact_disease_burden"):
            logger.info("No burden records found; nothing to expand.")
            return empty

        # ---- allocation policy -------------------------------------------
        kernel = load_burden_kernel(getattr(self, "registry", None))
        if kernel.is_flat:
            logger.warning(
                "  the daily series is allocated UNIFORMLY: no seasonal policy "
                "pack is in force. A seasonal pattern later recovered from "
                "fact_disease_case under this run does not exist in the data.")
        elif not kernel.source:
            logger.warning(
                "  the allocation kernel %s cites no source. A seasonal pattern "
                "relies on these weights, so a seasonal claim needs the pack to "
                "name where they came from.", kernel.origin)

        # ---- denominators ------------------------------------------------
        index = _PopulationIndex(self.session, population_indicator).load()
        divisors = (self._burden_locations_per_country(index)
                    if national_allocation else {})
        if national_allocation:
            if divisors:
                logger.warning(
                    "  national allocation is ON: a location with no series of its "
                    "own uses its country's population divided by the number of "
                    "burden locations in that country (%s). Totals stay correct; "
                    "the spatial pattern is uniform, not measured.",
                    ", ".join(f"{c}={n}" for c, n in sorted(divisors.items())))
            else:
                logger.warning("  national allocation is ON but no country "
                               "population series could be mapped; it will not "
                               "apply.")

        def _divisor_for(location_id: Any) -> float:
            if population_divisor is not None:
                return max(1e-9, float(population_divisor))
            country = index.country_for(location_id) or "(unknown)"
            return float(max(1, divisors.get(country, 1)))

        # ---- write grain -------------------------------------------------
        case_location_of: Dict[int, int] = {}
        grain_label = "grid cell"
        if group_by_level:
            requested = str(group_by_level).strip().lower()
            if requested in ("", "none", "cell", "grid"):
                pass
            elif requested in ("parent", "parent_location", "one-hop"):
                for location_id, parent_location_id in self.session.execute(text(
                    "SELECT location_id, parent_location_id FROM dim_location "
                    "ORDER BY location_id")).fetchall():
                    if location_id is not None and parent_location_id is not None:
                        case_location_of[int(location_id)] = int(parent_location_id)
                grain_label = "each cell's parent_location_id"
            else:
                case_location_of = self._ancestor_map(requested)
                grain_label = f"admin_level={requested}"
            if case_location_of:
                logger.info("  writing at %s: %d cell(s) map to %d target(s)",
                            grain_label, len(case_location_of),
                            len(set(case_location_of.values())))
            elif requested not in ("", "none", "cell", "grid"):
                logger.warning(
                    "  grouping to %r mapped no grid cell; writing per cell "
                    "instead.", requested)

        # ---- which records ----------------------------------------------
        where: List[str] = [
            "((incidence_rate IS NOT NULL AND incidence_rate > 0) "
            " OR (cases_mean     IS NOT NULL AND cases_mean     > 0))"]
        params: Dict[str, Any] = {}

        wanted_locations = sorted({int(v) for v in (locations or [])
                                   if v is not None})
        if wanted_locations:
            placeholders = ", ".join(f":loc{i}" for i in range(len(wanted_locations)))
            where.append(f"location_id IN ({placeholders})")
            params.update({f"loc{i}": value
                           for i, value in enumerate(wanted_locations)})

        wanted_years = sorted({int(v) for v in (years or []) if v is not None})
        if wanted_years:
            placeholders = ", ".join(f":yr{i}" for i in range(len(wanted_years)))
            where.append(f"year IN ({placeholders})")
            params.update({f"yr{i}": value for i, value in enumerate(wanted_years)})

        sql = ("SELECT burden_id, location_id, year, disease_code, source_code, "
               "       incidence_rate, cases_mean FROM fact_disease_burden "
               f"WHERE {' AND '.join(where)} ORDER BY burden_id")
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = max(1, int(limit))

        burden_records = self.session.execute(text(sql), params).fetchall()
        if not burden_records:
            logger.warning("No burden records matched the selected filters.")
            return empty

        selected_years = sorted({int(row[2]) for row in burden_records
                                 if row[2] is not None})
        first_year = selected_years[0] if selected_years else None
        last_year = selected_years[-1] if selected_years else None

        # ---- refuse to mix grains in the same years ----------------------
        purged = 0
        if first_year is not None:
            purged = self._guard_write_grain(
                first_year, last_year,
                self._expected_admin_levels(case_location_of), grain_label,
                force=force, allow_mixed_grain=allow_mixed_grain)

        # ---- annual totals, grouped by case key -------------------------
        annual_by_key: Dict[Tuple[int, int, str, str], float] = {}
        tier_counts: Dict[str, int] = {}
        basis_counts: Dict[str, int] = {}
        prepared_years: Set[int] = set()
        unparented_cells: Set[int] = set()
        skipped = no_population = 0
        failures = 0
        logged_skips = 0
        logged_allocations = 0
        location_keys = self._location_stable_keys()

        def _prepare_year(year_value: int) -> bool:
            if year_value in prepared_years:
                return True
            try:
                self._ensure_dim_time(int(year_value))
            except Exception as exc:  # noqa: BLE001
                logger.error("  could not extend dim_time to %s: %s",
                             year_value, exc)
                return False
            prepared_years.add(int(year_value))
            return True

        for (burden_id, location_id, year, disease_code,
             source_code, incidence_rate, cases_mean) in burden_records:
            if location_id is None or year is None or disease_code is None:
                skipped += 1
                continue
            try:
                location_key, year_key = int(location_id), int(year)
            except (TypeError, ValueError):
                skipped += 1
                continue

            rate_value = _positive(incidence_rate)
            mean_value = _positive(cases_mean)

            # Which measure this record uses. `prefer` decides when both exist;
            # the loser is not used and is not an error.
            if prefer == _BURDEN_MEASURE_INCIDENCE_RATE:
                use_rate = rate_value is not None
                use_mean = (not use_rate) and mean_value is not None
            else:
                use_mean = mean_value is not None
                use_rate = (not use_mean) and rate_value is not None

            if not use_rate and not use_mean:
                skipped += 1
                continue

            population: Optional[float] = None
            tier = _BURDEN_MEASURE_CASES_MEAN if use_mean else "none"

            if use_rate:
                # The denominator is resolved ONLY for a rate record. This is the
                # ordering the previous version got wrong: it resolved (or skipped
                # resolving) before deciding the measure, so a record carrying both
                # ended up in the rate branch with no population.
                hit = index.resolve(location_key, year_key,
                                    allow_country=national_allocation)
                if hit is not None:
                    population, tier = hit.value, hit.tier
                    if hit.is_country_level:
                        divisor = _divisor_for(location_key)
                        population = population / divisor
                        if logged_allocations < _BURDEN_LOG_SAMPLE:
                            logged_allocations += 1
                            logger.info(
                                "  allocation: burden_id %s (location=%s, year=%s) "
                                "uses %s population %s / %s = %.0f",
                                burden_id, location_key, year_key, hit.tier,
                                f"{hit.value:,.0f}", f"{divisor:,.0f}", population)
                elif default_population is not None:
                    population, tier = float(default_population), "default"

                if population is None or population <= 0:
                    no_population += 1
                    if logged_skips < _BURDEN_LOG_SAMPLE:
                        logged_skips += 1
                        logger.info(
                            "  skipping burden_id %s (location=%s, year=%s, "
                            "disease=%s): no %r row for this location and no "
                            "country series%s.",
                            burden_id, location_key, year_key, disease_code,
                            population_indicator,
                            "" if national_allocation
                            else " (national allocation is OFF)")
                    continue
                annual_cases = (rate_value / scale) * float(population)
            else:
                annual_cases = float(mean_value)

            if not _prepare_year(year_key):
                failures += 1
                continue

            case_location = case_location_of.get(location_key, location_key)
            if case_location == location_key and case_location_of:
                unparented_cells.add(location_key)

            key = (case_location, year_key, str(disease_code),
                   str(source_code or ""))
            annual_by_key[key] = annual_by_key.get(key, 0.0) + annual_cases
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            basis = (_BURDEN_MEASURE_CASES_MEAN if use_mean
                     else _BURDEN_MEASURE_INCIDENCE_RATE)
            basis_counts[basis] = basis_counts.get(basis, 0) + 1

        merged = len(burden_records) - skipped - no_population - len(annual_by_key)
        if merged > 0:
            logger.info(
                "  %d burden record(s) shared a case key with another and were "
                "SUMMED: uq_fact_case_natural has no pathogen column, so one "
                "daily series per (location, year, disease, source) is the only "
                "shape fact_disease_case can hold.", merged)

        # ---- write -------------------------------------------------------
        stats: Dict[str, Any] = {
            "inserted": 0, "already_present": 0, "skipped": skipped,
            "no_population": no_population, "errors": failures,
            "records": len(burden_records), "groups": len(annual_by_key),
            "merged": max(0, merged), "zero": 0,
            "years_prepared": len(prepared_years), "tiers": tier_counts,
            "basis": basis_counts, "grain": grain_label,
            "grain_targets": len(set(case_location_of.values())),
            "unparented_cells": len(unparented_cells),
            "kernel": kernel.origin, "purged": purged}

        if not annual_by_key:
            logger.warning(
                "Nothing to expand: %d record(s) examined, %d had no denominator, "
                "%d were skipped for a missing location/year/disease.",
                len(burden_records), no_population, skipped)
            self._record_expansion_parameters(
                effective_run_id, stats, kernel, prefer, rate_scale,
                national_allocation, population_divisor, population_indicator,
                default_population, seed, group_by_level)
            return stats

        statement = text(
            "INSERT INTO fact_disease_case ("
            "  location_id, date_id_onset, date_id_reported, disease_code, "
            "  identification_method_code, source_code, case_status, host_type, "
            "  age_group, sex, outcome, case_count"
            ") VALUES ("
            "  :loc_id, :date_id, :date_id, :disease, 'ESTIMATE', :src_code, "
            "  'estimated', 'human', '', '', 'unknown', :case_count)")

        window_where = (
            "WHERE location_id = :loc_id "
            "AND date_id_onset >= :start_date AND date_id_onset <= :end_date "
            "AND disease_code = :disease "
            "AND identification_method_code = 'ESTIMATE' "
            "AND ((:src_code IS NULL AND source_code IS NULL) "
            "     OR source_code = :src_code)")
        window_sql = text(f"DELETE FROM fact_disease_case {window_where}")
        count_sql = text(f"SELECT COUNT(*) FROM fact_disease_case {window_where}")

        for key in sorted(annual_by_key):
            location_key, year_key, disease_code, source_code = key
            annual_cases = annual_by_key[key]

            target = int(round(max(0.0, annual_cases)))
            if target <= 0:
                stats["zero"] = int(stats["zero"]) + 1
                logger.debug("  skipping (%s, %s, %s, %s): annual total %.3f "
                             "rounds to 0", location_key, year_key, disease_code,
                             source_code, annual_cases)
                continue

            window = {"loc_id": location_key,
                      "start_date": f"{year_key}-01-01",
                      "end_date": f"{year_key}-12-31",
                      "disease": disease_code,
                      "src_code": source_code or None}
            existing = int(self.session.execute(count_sql, window).scalar() or 0)
            if existing and not force:
                stats["already_present"] = int(stats["already_present"]) + 1
                continue

            daily = _burden_generate_daily_case_counts(
                annual_cases=annual_cases, year=year_key,
                location_id=location_key,
                base_variation=base_variation, seed=seed,
                kernel=kernel,
                stable_key=location_keys.get(location_key))
            try:
                if existing:
                    deleted = self.session.execute(window_sql, window).rowcount or 0
                    logger.debug("    deleted %d existing ESTIMATE row(s)", deleted)
                payload = [{"loc_id": location_key, "date_id": row["date_id"],
                            "disease": disease_code,
                            "src_code": source_code or None,
                            "case_count": int(row["case_count"])}
                           for row in daily.to_dict(orient="records")]
                for start in range(0, len(payload), 5000):
                    self.session.execute(statement, payload[start:start + 5000])
                self.session.commit()
                stats["inserted"] = int(stats["inserted"]) + len(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("    error expanding (%s, %s, %s, %s): %s: %s",
                             location_key, year_key, disease_code, source_code,
                             type(exc).__name__, exc)
                self.session.rollback()
                stats["errors"] = int(stats["errors"]) + 1

        logger.info(
            "BURDEN EXPANSION COMPLETE: %d inserted, %d present, %d zero-total, "
            "%d no-population, %d skipped, %d error(s); %d record(s) -> %d group(s), "
            "%d summed",
            stats["inserted"], stats["already_present"], stats["zero"],
            stats["no_population"], stats["skipped"], stats["errors"],
            stats["records"], stats["groups"], stats["merged"])
        logger.info("  measures: %s", ", ".join(
            f"{name}={count}" for name, count in sorted(basis_counts.items()))
            or "(none)")
        logger.info("  denominators: %s", ", ".join(
            f"{name}={count}" for name, count in sorted(tier_counts.items()))
            or "(none)")
        logger.info("  allocation kernel: %s", kernel.origin)
        if case_location_of:
            logger.info(
                "  written at %s: %d target(s)%s. A large `summed` count here is "
                "NORMAL at a coarser grain (cells sharing a target), not a "
                "duplicate.",
                grain_label, stats["grain_targets"],
                f"; {len(unparented_cells)} cell(s) had no such ancestor and kept "
                f"their own location_id" if unparented_cells else "")

        self._record_expansion_parameters(
            effective_run_id, stats, kernel, prefer, rate_scale,
            national_allocation, population_divisor, population_indicator,
            default_population, seed, group_by_level)

        if stats["no_population"]:
            logger.warning(
                "  %d rate record(s) had no denominator for %r. They are the only "
                "records that need one: a %r record is self-contained. Use "
                "--national-allocation if the population is a national series.",
                stats["no_population"], population_indicator,
                _BURDEN_MEASURE_CASES_MEAN)
        if stats["zero"]:
            logger.warning(
                "  %d group(s) round to 0 cases and were not written. If that is "
                "all of them on a %r run, the rate scale is wrong: try "
                "--rate-scale per_person (cell shares are small, so a per-100k "
                "assumption yields fractions of a case).",
                stats["zero"], _BURDEN_MEASURE_INCIDENCE_RATE)
        return stats


    def _burden_locations_per_country(self, index: "_PopulationIndex") -> Dict[str, int]:
        """How many distinct burden locations each country holds.

        This is the allocation divisor: giving each of N cells ``P/N`` makes the
        sum over those cells exactly ``P``, so the national total is preserved
        even though the spatial pattern is uniform. Computed over **all** burden
        locations, not over the selected subset, so ``--limit`` cannot silently
        change the divisor.
        """
        counts: Dict[str, int] = {}
        try:
            rows = self.session.execute(text(
                "SELECT DISTINCT location_id FROM fact_disease_burden "
                "WHERE location_id IS NOT NULL")).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("  could not count burden locations per country (%s); "
                           "the allocation divisor will be 1.", exc)
            return counts
        for (location_id,) in rows:
            country = index.country_for(location_id) or "(unknown)"
            counts[country] = counts.get(country, 0) + 1
        return counts

    def _ancestor_map(self, target_level: str, *, grid_level: str = "grid",
                      max_hops: int = 8) -> Dict[int, int]:
        """``grid cell -> its nearest ancestor at target_level``.

        Walks ``parent_location_id`` once per cell with a cycle guard. A cell
        whose chain never reaches ``target_level`` is simply absent from the map,
        and the expansion then keeps that cell at its own ``location_id`` - it
        does not drop the record and does not invent an ancestor.

        Read-only, one query. ``max_hops`` bounds a corrupted parent chain: a
        two-node cycle would otherwise loop forever, and a plausible-looking
        wrong ancestor is worse than no ancestor.
        """
        rows = self.session.execute(text(
            "SELECT location_id, admin_level, parent_location_id "
            "FROM dim_location ORDER BY location_id")).fetchall()

        parent_of: Dict[int, Optional[int]] = {}
        level_of: Dict[int, str] = {}
        for location_id, admin_level, parent_location_id in rows:
            if location_id is None:
                continue
            key = int(location_id)
            level_of[key] = str(admin_level or "").strip().lower()
            parent_of[key] = (None if parent_location_id is None
                              else int(parent_location_id))

        wanted = str(target_level).strip().lower()
        start_level = str(grid_level).strip().lower()
        found: Dict[int, int] = {}
        for key, label in level_of.items():
            if label != start_level:
                continue
            current = parent_of.get(key)
            hops = 0
            seen = {key}
            while current is not None and hops < max_hops and current not in seen:
                if level_of.get(current) == wanted:
                    found[key] = current
                    break
                seen.add(current)
                current = parent_of.get(current)
                hops += 1
        return found


    def _location_stable_keys(self) -> Dict[int, int]:
        """``location_id -> stable seed key`` for the whole location dimension.

        One read, because the generator needs it per series and the alternative is
        a query per burden record.
        """
        keys: Dict[int, int] = {}
        try:
            rows = self.session.execute(text(
                "SELECT location_id, location_code, name FROM dim_location "
                "ORDER BY location_id")).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("  could not read location codes (%s); the daily series "
                           "will be seeded on the surrogate key, which is not "
                           "stable across a rebuild.", exc)
            return keys
        for location_id, location_code, name in rows:
            if location_id is None:
                continue
            keys[int(location_id)] = _stable_location_key(location_code, name)
        return keys

    def _expected_admin_levels(self, case_location_of: Mapping[int, int]) -> Set[str]:
        """The ``admin_level`` values the requested write grain produces."""
        if not case_location_of:
            return {"grid"}
        target_ids = sorted(set(case_location_of.values()))
        levels: Set[str] = set()
        try:
            for _location_id, admin_level in _chunked_in(
                    self.session,
                    "SELECT location_id, admin_level FROM dim_location "
                    "WHERE location_id IN :vals ORDER BY location_id",
                    {}, target_ids):
                levels.add(str(admin_level or "").strip().lower() or "(null)")
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read the target admin levels: %s", exc)
        return levels or {"(unknown)"}

    def _guard_write_grain(self, first_year: int, last_year: int,
                           expected_levels: Set[str], grain_label: str,
                           *, force: bool, allow_mixed_grain: bool) -> int:
        """Refuse, or purge, when ESTIMATE rows at another grain exist.

        Two grains in one year do not collide - the case keys differ by
        ``location_id`` - so the database accepts them and every downstream sum
        then double-counts by the grouping factor. The check is cheap and the
        consequence is unrecoverable once the rows are mixed, so it is a guard
        rather than a warning.

        Returns the number of rows purged (0 unless ``force`` replaced them).
        """
        window = {"start": f"{first_year}-01-01", "end": f"{last_year}-12-31"}
        try:
            present = self.session.execute(text(
                "SELECT COALESCE(l.admin_level, '(null)') AS lvl, COUNT(*) "
                "FROM fact_disease_case c "
                "JOIN dim_location l ON l.location_id = c.location_id "
                "WHERE c.identification_method_code = 'ESTIMATE' "
                "  AND c.date_id_onset >= :start AND c.date_id_onset <= :end "
                "GROUP BY 1 ORDER BY 1"), window).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug("grain check could not run: %s", exc)
            return 0

        if not present:
            return 0

        existing = {str(row[0]).strip().lower(): int(row[1]) for row in present}
        mixture = len(existing) > 1
        foreign = set(existing) - {level.lower() for level in expected_levels}
        if not mixture and not foreign:
            return 0

        detail = ", ".join(f"{level}={count}"
                           for level, count in sorted(existing.items()))
        if allow_mixed_grain:
            logger.warning(
                "  estimate rows already exist at %s over %s-%s, and this run "
                "writes at %s (%s). MIXED GRAINS WILL COEXIST and any sum over "
                "fact_disease_case will double-count by the grouping factor. "
                "Proceeding because allow_mixed_grain is set.", detail,
                first_year, last_year, grain_label,
                ", ".join(sorted(expected_levels)))
            return 0

        if not force:
            raise RuntimeError(
                f"fact_disease_case already holds ESTIMATE rows at {detail} over "
                f"{first_year}-{last_year}, while this run writes at {grain_label} "
                f"({', '.join(sorted(expected_levels))}). Mixed grains co-exist "
                f"without violating a constraint and silently double-count "
                f"downstream. Re-run with force=True to replace them, or with "
                f"allow_mixed_grain=True if the mixture is intended.")

        purged = int(self.session.execute(text(
            "DELETE FROM fact_disease_case "
            "WHERE identification_method_code = 'ESTIMATE' "
            "  AND date_id_onset >= :start AND date_id_onset <= :end"),
            window).rowcount or 0)
        self.session.commit()
        logger.warning(
            "  purged %d existing ESTIMATE row(s) at %s over %s-%s before writing "
            "at %s. The previous series is gone; its parameters remain in "
            "run_manifest for the run that wrote it.",
            purged, detail, first_year, last_year, grain_label)
        return purged

    def _record_expansion_parameters(
        self, run_id: Optional[str], stats: Mapping[str, Any],
        kernel: _BurdenKernel, prefer: str, rate_scale: str,
        national_allocation: bool, population_divisor: Optional[float],
        population_indicator: str, default_population: Optional[float],
        seed: int, group_by_level: Optional[str],
    ) -> None:
        """Write the expansion policy to ``run_manifest``. Never raises.

        Everything here changes a stored number, so a database without this row
        cannot be attributed to a procedure. A failure to record is a WARNING on a
        run that continues: losing the rows just inserted because a JSON write
        failed would be the worse outcome.
        """
        if not run_id:
            logger.warning(
                "  no run id available, so this expansion's parameters are NOT "
                "recorded and the modelled rows it just wrote cannot be attributed "
                "to a stated policy. Run through main.py, which sets one, or pass "
                "run_id=.")
            return
        try:
            _record_manifest_parameters(
                self.session, run_id=run_id, namespace="expansion",
                parameters={
                    "phase": "expand_burden_to_daily",
                    "measure_preference": prefer,
                    "rate_scale": rate_scale,
                    "rate_divisor": _RATE_SCALES[rate_scale],
                    "national_allocation": bool(national_allocation),
                    "population_divisor": population_divisor,
                    "population_indicator": population_indicator,
                    "default_population": default_population,
                    "seed": int(seed),
                    "group_by_level": group_by_level,
                    "write_grain": stats.get("grain"),
                    "grain_targets": stats.get("grain_targets"),
                    "kernel": kernel.describe(),
                    "case_status_written": "estimated",
                    "identification_method_code": "ESTIMATE",
                    "records": stats.get("records"),
                    "groups": stats.get("groups"),
                    "inserted": stats.get("inserted"),
                    "purged": stats.get("purged"),
                    "basis": dict(stats.get("basis") or {}),
                    "tiers": dict(stats.get("tiers") or {}),
                })
            self.session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("  could not record the expansion parameters: %s: %s",
                           type(exc).__name__, exc)
            try:
                self.session.rollback()
            except Exception:  # noqa: BLE001
                pass


    def _ensure_dim_time(self, year: int) -> int:
        """Insert missing ``dim_time`` rows for ``year`` (``date_id`` is an FK).

        ``season`` is written as NULL, deliberately. The previous version wrote
        ``'dry'`` from November to March and ``'wet'`` otherwise - a northern- and
        southern-hemisphere heuristic that is wrong for both in East Africa's
        bimodal rainfall regime, and that cannot be right for a table keyed on a
        date alone, since season is a property of a place and a calendar
        together. A column that looks authoritative and is not is worse than an
        empty column, so the field waits for a per-country climatology drawn from
        a cited source.
        """
        from datetime import timedelta

        existing = {row[0] for row in self.session.execute(text(
            "SELECT date_id FROM dim_time WHERE year = :y"), {"y": int(year)})}
        current = datetime(int(year), 1, 1)
        end = datetime(int(year), 12, 31)
        payload = []
        while current <= end:
            date_id = current.strftime("%Y-%m-%d")
            if date_id not in existing:
                payload.append({
                    "date_id": date_id, "date": current.date(),
                    "year": current.year, "month": current.month,
                    "week": current.isocalendar()[1],
                    "day_of_year": current.timetuple().tm_yday})
            current += timedelta(days=1)
        if payload:
            self.session.execute(text(
                "INSERT INTO dim_time (date_id, date, year, month, week, "
                "day_of_year, season, is_holiday) VALUES (:date_id, :date, "
                ":year, :month, :week, :day_of_year, NULL, 0)"), payload)
            self.session.commit()
            logger.debug("  dim_time: added %d row(s) for %s", len(payload), year)
            _warn_once(
                "dim_time:season",
                "  dim_time.season is not populated: a season label needs a "
                "per-country climatology, and a single global dry/wet split is "
                "wrong for bimodal regimes and for either hemisphere. The column "
                "is left NULL rather than filled with an unsupported value.")
        return len(payload)


    def _compute_resistance_status(self) -> Dict[str, Any]:
        """Four passes, strongest signal first; only rows still unclassified."""
        registry = getattr(self, "registry", None)
        susceptible_cutoff, suspected_cutoff = _resistance_thresholds(registry)
        pack_id, pack_version, pack_release = ("(unknown)", "(unknown)",
                                               "(unrecorded)")
        try:
            loaded = _load_registry(registry)
            pack = (loaded.require_parameter_pack(RESISTANCE_THRESHOLDS_KEY)
                    if loaded is not None else None)
            if pack is not None:
                pack_id = getattr(pack, "pack_id", pack_id)
                pack_version = getattr(pack, "version", pack_version)
                pack_release = getattr(pack, "released_with", "") or pack_release
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read the classification pack: %s", exc)

        bands = {
            "susceptible": susceptible_cutoff, "suspected": suspected_cutoff,
            "susceptible_label": RESISTANCE_BANDS["susceptible"],
            "suspected_label": RESISTANCE_BANDS["suspected"],
            "resistant_label": RESISTANCE_BANDS["resistant"]}

        logger.info("=" * 60)
        logger.info("COMPUTING RESISTANCE STATUS")
        logger.info("  pack: %r v%s (released with %s), cut-offs: %s>=%g%%, "
                    "%s>=%g%%, else %s", pack_id, pack_version, pack_release,
                    RESISTANCE_BANDS["susceptible"], susceptible_cutoff,
                    RESISTANCE_BANDS["suspected"], suspected_cutoff,
                    RESISTANCE_BANDS["resistant"])

        total = self._scalar_or_zero("SELECT COUNT(*) FROM fact_insecticide_resistance")
        if not total:
            logger.info("  no resistance records found; nothing to classify")
            return {"thresholds": {"susceptible": susceptible_cutoff,
                                   "suspected": suspected_cutoff},
                    "pack": {"pack_id": pack_id, "version": pack_version,
                             "released_with": pack_release,
                             "parameter_key": RESISTANCE_THRESHOLDS_KEY},
                    "rows_total": 0, "passes": {}, "classified": 0,
                    "unclassified": 0}

        passes: Dict[str, int] = {}
        passes["tested_dead"] = int(self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET mortality = ROUND((n_insect_dead * 100.0) / n_insect_tested, 2),
                resistance_status = CASE
                    WHEN (n_insect_dead * 100.0 / n_insect_tested) >= :susceptible
                        THEN :susceptible_label
                    WHEN (n_insect_dead * 100.0 / n_insect_tested) >= :suspected
                        THEN :suspected_label
                    ELSE :resistant_label END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND n_insect_tested IS NOT NULL AND n_insect_tested > 0
              AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
        """), bands).rowcount or 0)
        self.session.commit()

        passes["tested_resistant"] = int(self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET n_insect_dead = n_insect_tested - n_insect_resistant,
                mortality = ROUND(
                    ((n_insect_tested - n_insect_resistant) * 100.0)
                    / n_insect_tested, 2),
                resistance_status = CASE
                    WHEN ((n_insect_tested - n_insect_resistant) * 100.0
                          / n_insect_tested) >= :susceptible THEN :susceptible_label
                    WHEN ((n_insect_tested - n_insect_resistant) * 100.0
                          / n_insect_tested) >= :suspected THEN :suspected_label
                    ELSE :resistant_label END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND n_insect_tested IS NOT NULL AND n_insect_tested > 0
              AND n_insect_resistant IS NOT NULL
              AND n_insect_resistant <= n_insect_tested
        """), bands).rowcount or 0)
        self.session.commit()

        passes["susceptible_resistant"] = int(self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET n_insect_tested = n_insect_susceptible + n_insect_resistant,
                n_insect_dead = n_insect_susceptible,
                mortality = ROUND((n_insect_susceptible * 100.0)
                    / (n_insect_susceptible + n_insect_resistant), 2),
                resistance_status = CASE
                    WHEN (n_insect_susceptible * 100.0
                          / (n_insect_susceptible + n_insect_resistant))
                         >= :susceptible THEN :susceptible_label
                    WHEN (n_insect_susceptible * 100.0
                          / (n_insect_susceptible + n_insect_resistant))
                         >= :suspected THEN :suspected_label
                    ELSE :resistant_label END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND n_insect_susceptible IS NOT NULL
              AND n_insect_resistant IS NOT NULL
              AND (n_insect_susceptible + n_insect_resistant) > 0
        """), bands).rowcount or 0)
        self.session.commit()

        passes["mortality_only"] = int(self.session.execute(text("""
            UPDATE fact_insecticide_resistance
            SET resistance_status = CASE
                WHEN mortality >= :susceptible THEN :susceptible_label
                WHEN mortality >= :suspected THEN :suspected_label
                ELSE :resistant_label END
            WHERE (resistance_status IS NULL OR resistance_status = '')
              AND mortality IS NOT NULL AND mortality BETWEEN 0 AND 100
        """), bands).rowcount or 0)
        self.session.commit()

        unclassified = self._scalar_or_zero(
            "SELECT COUNT(*) FROM fact_insecticide_resistance "
            "WHERE resistance_status IS NULL OR resistance_status = ''")
        if unclassified:
            logger.warning("  %d of %d row(s) could not be classified: no usable "
                           "counts and no mortality figure.", unclassified, total)
        self._verify_resistance_status()
        return {"thresholds": {"susceptible": susceptible_cutoff,
                               "suspected": suspected_cutoff},
                "pack": {"pack_id": pack_id, "version": pack_version,
                         "released_with": pack_release,
                         "parameter_key": RESISTANCE_THRESHOLDS_KEY},
                "rows_total": total, "passes": passes,
                "classified": max(0, total - unclassified),
                "unclassified": unclassified}

    def _verify_resistance_status(self) -> None:
        distribution = self.session.execute(text(
            "SELECT resistance_status, COUNT(*) FROM fact_insecticide_resistance "
            "WHERE resistance_status IS NOT NULL AND resistance_status != '' "
            "GROUP BY resistance_status ORDER BY resistance_status")).fetchall()
        remaining = self._scalar_or_zero(
            "SELECT COUNT(*) FROM fact_insecticide_resistance "
            "WHERE resistance_status IS NULL OR resistance_status = ''")
        for status, count in distribution:
            logger.info("  %s: %d", status, count)
        if remaining:
            logger.warning("  %d row(s) still have no resistance_status", remaining)

    # -- duplicates --------------------------------------------------------

    def detect_duplicates(self, tables: Optional[Iterable[str]] = None, *,
                          registry: Any = None,
                          max_groups: int = 50) -> Dict[str, Any]:
        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        wanted = ({str(n).strip() for n in tables if str(n).strip()}
                  if tables is not None else None)
        targets: Dict[str, Tuple[str, str, str, Any]] = {}
        for field in dimension_fields():
            try:
                binding = resolve_field(field)
            except Exception as exc:  # noqa: BLE001
                logger.debug("duplicate scan skips %r: %s", field, exc)
                continue
            if binding.value_type != "text":
                continue
            if wanted is not None and binding.dimension_table not in wanted:
                continue
            pack = None
            if resolved_registry is not None:
                try:
                    pack = resolved_registry.select_pack(field)
                except Exception:  # noqa: BLE001
                    pack = None
            targets.setdefault(binding.dimension_table, (
                binding.dimension_table, field.split(".", 1)[1],
                binding.dimension_key, pack))

        results: Dict[str, Any] = {}
        for table_name in sorted(targets):
            _table, name_column, code_column, pack = targets[table_name]
            model = _model_for_table(table_name)
            if model is None:
                continue
            try:
                query = self.session.query(model)
                primary_key = list(model.__table__.primary_key.columns)
                if primary_key:
                    query = query.order_by(*primary_key)
                records = query.all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("duplicate scan: could not read %r: %s",
                               table_name, exc)
                continue
            has_concept = "concept_id" in set(model.__table__.columns.keys())
            key_function = (_pack_key_fn(pack) if pack is not None
                            else _default_vocab_key)
            groups: Dict[str, List[Any]] = {}
            for record in records:
                concept_id = (getattr(record, "concept_id", None)
                              if has_concept else None)
                if concept_id is not None:
                    groups.setdefault(f"concept:{concept_id}", []).append(record)
                    continue
                standard_code = getattr(record, "standard_code", None)
                if standard_code is not None and str(standard_code).strip():
                    groups.setdefault(f"standard:{str(standard_code).strip()}",
                                      []).append(record)
                    continue
                raw = getattr(record, name_column, None)
                if raw is None:
                    continue
                try:
                    if raw != raw:
                        continue
                except Exception:  # noqa: BLE001
                    continue
                text_value = str(raw).strip()
                if not text_value:
                    continue
                try:
                    name_key = key_function(text_value)
                except Exception:  # noqa: BLE001
                    name_key = _default_vocab_key(text_value)
                if not str(name_key or "").strip():
                    continue
                groups.setdefault(f"name:{str(name_key).strip()}",
                                  []).append(record)

            duplicates = {k: v for k, v in groups.items() if len(v) > 1}
            listed = [{"group_key": k, "count": len(duplicates[k]),
                       "codes": sorted(str(getattr(m, code_column, ""))
                                       for m in duplicates[k])}
                      for k in sorted(duplicates)][: max(0, int(max_groups))]
            contradictions = []
            if has_concept:
                by_name: Dict[str, Set[Any]] = {}
                for record in records:
                    concept_id = getattr(record, "concept_id", None)
                    if concept_id is None:
                        continue
                    raw = getattr(record, name_column, None)
                    if raw is None:
                        continue
                    try:
                        label = str(key_function(str(raw).strip()) or "").strip()
                    except Exception:  # noqa: BLE001
                        label = _default_vocab_key(str(raw).strip())
                    if label:
                        by_name.setdefault(label, set()).add(concept_id)
                contradictions = [{"name_key": label,
                                   "concept_ids": sorted(ids, key=str)}
                                  for label, ids in sorted(by_name.items())
                                  if len(ids) > 1]

            results[table_name] = {
                "rows_examined": len(records), "name_column": name_column,
                "dimension_key": code_column,
                "vocabulary": getattr(pack, "pack_id", None),
                "groups": listed, "group_count": len(duplicates),
                "groups_truncated": len(duplicates) > max(0, int(max_groups)),
                "conflicting_concepts": contradictions[: max(0, int(max_groups))],
                "conflict_count": len(contradictions)}
            if duplicates:
                logger.warning("  %s: %d duplicate group(s) across %d row(s)",
                               table_name, len(duplicates), len(records))
        return results

    def resolve_fact_table_duplicates(self) -> Dict[str, int]:
        logger.info("Resolving fact table duplicates...")
        results: Dict[str, int] = {}
        for table_name, spec in sorted(_FACT_AGGREGATION_SPEC.items()):
            model = _model_for_table(table_name)
            key_columns = list(spec.get("natural_key") or [])
            if model is None or not key_columns:
                continue
            group_by = ", ".join(key_columns)
            duplicate_groups = self._scalar_or_zero(
                f"SELECT COUNT(*) FROM (SELECT {group_by} FROM {table_name} "
                f"GROUP BY {group_by} HAVING COUNT(*) > 1) AS dup")
            results[table_name] = int(duplicate_groups)
            if not duplicate_groups:
                logger.info("  %s: no duplicates; untouched", table_name)
                continue
            rows = self.session.execute(text(
                f"SELECT {group_by} FROM {table_name} GROUP BY {group_by} "
                f"HAVING COUNT(*) > 1")).fetchall()
            collapsed = 0
            for row in rows:
                filters = dict(zip(key_columns, row))
                collapsed += self._collapse_group(table_name, model,
                                                  key_columns, filters)
            if collapsed:
                self.session.commit()
                logger.info("  %s: collapsed %d duplicate group(s)",
                            table_name, collapsed)
        return results

    def _collapse_group(self, table_name: str, model, key_columns: List[str],
                        filters: Dict[str, Any]) -> int:
        spec = _FACT_AGGREGATION_SPEC.get(table_name) or {}
        ops: Dict[str, str] = {}
        for column in spec.get("sums") or ():
            ops[column] = "sum"
        for column in spec.get("means") or ():
            ops[column] = "mean"
        for column in spec.get("first") or ():
            ops[column] = "first"
        derived = list(spec.get("derived") or ())
        pk_columns = list(model.__table__.primary_key.columns)
        if len(pk_columns) != 1:
            return 0
        pk_name = pk_columns[0].name

        where, params = _or_where(filters)
        select_columns = [c.name for c in model.__table__.columns
                          if c.name != pk_name]
        rows = self.session.execute(text(
            f"SELECT {', '.join(select_columns)} FROM {table_name} "
            f"WHERE {where} ORDER BY {pk_name}"), params).mappings().all()
        if len(rows) < 2:
            return 0

        frame = pd.DataFrame([dict(r) for r in rows])
        aggregations = {c: ("first" if c in derived else ops.get(c, "first"))
                        for c in select_columns}
        grouped = (frame.groupby(key_columns, dropna=False, sort=True)
                   .agg(aggregations).reset_index())
        for column in derived:
            grouped[column] = None
        for row in rows:
            self.session.execute(text(
                f"DELETE FROM {table_name} WHERE {pk_name} = :pk"),
                {"pk": row[pk_name]})
        insert_columns = [c for c in grouped.columns if c != pk_name]
        payload = []
        for record in grouped.to_dict(orient="records"):
            cleaned = {}
            for column in insert_columns:
                value = record.get(column)
                if isinstance(value, float) and pd.isna(value):
                    value = None
                cleaned[column] = value
            payload.append(cleaned)
        self.session.execute(text(
            f"INSERT INTO {table_name} ({', '.join(insert_columns)}) "
            f"VALUES ({', '.join(':' + c for c in insert_columns)})"), payload)
        return 1

    # -- orchestration -----------------------------------------------------

    def consolidate_all(self, gadm_gpkg: Optional[str] = None,
                        country_codes: Optional[List[str]] = None,
                        dry_run: bool = False, *,
                        registry: Optional[VocabularyRegistry] = None,
                        run_id: Optional[str] = None,
                        name_locations: bool = True,
                        include_grid_names: bool = False,
                        overwrite_human_names: bool = False,
                        audit_country_geometry: bool = False,
                        burden_grain: Optional[str] = None,
                        burden_prefer: str = _BURDEN_MEASURE_CASES_MEAN,
                        burden_rate_scale: str = "per_100k",
                        burden_national_allocation: bool = False,
                        burden_force: bool = False
                        ) -> Dict[str, Any]:
        """Run every phase in the one safe order.

        Country codes are canonicalised at A6a, before any phase that groups or
        filters by country. audit_country_geometry=True also cross-checks rows that
        were already canonical, at one coordinate lookup per row.

        The ``burden_*`` arguments are the expansion policy. They are parameters
        rather than constants because each of them changes a stored number, and
        because the pipeline and the CLI must be able to reach the same code path
        - they previously disagreed silently, one writing per cell and the other
        at admin grain. ``burden_force=False`` by default: overwriting a series
        someone else generated is a decision, not a default.
        """
        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        resolved_run_id = (run_id if run_id is not None
                           else getattr(self, "run_id", None))
        countries = [str(c).strip().upper() for c in (country_codes or [])
                     if str(c).strip()]
        if gadm_gpkg:
            self._boundary_path = str(gadm_gpkg)

        logger.info("=" * 60)
        logger.info("CONSOLIDATING CDM%s", " (dry run)" if dry_run else "")
        logger.info("  run id:    %s", resolved_run_id or "(none)")
        logger.info("  registry:  %s", (resolved_registry.digest()[:12]
                                        if resolved_registry is not None
                                        else "(none)"))
        logger.info("  countries: %s", countries or "(all)")
        logger.info("  boundary:  %s", self._boundary_path or "(none)")
        logger.info("=" * 60)

        results: Dict[str, Any] = {
            "run_id": resolved_run_id,
            "registry_digest": (resolved_registry.digest()
                                if resolved_registry is not None else ""),
            "dry_run": bool(dry_run), "conflicts": 0}

        logger.info("-" * 60)
        logger.info("FK: ALIGN FOREIGN KEYS")
        results["foreign_key_alignment"] = self.align_all_foreign_keys(dry_run)

        logger.info("-" * 60)
        logger.info("A6: CONSOLIDATE LOCATIONS")
        results["locations"] = self.consolidate_locations(
            gadm_gpkg=gadm_gpkg, country_codes=countries, dry_run=dry_run,
            audit_country_geometry=audit_country_geometry)
        results["country_standard"] = ((results["locations"] or {})
                                       .get("country_standard") or {})

        logger.info("-" * 60)
        logger.info("VOCAB: LOAD SHIPPED VOCABULARIES")
        results["vocabulary_load"] = load_shipped_vocabularies(
            self.session, resolved_registry, dry_run=dry_run)

        logger.info("-" * 60)
        logger.info("A8: RECONCILE VOCABULARIES")
        results["vocabulary_standard"] = self.standardize_vocabularies(
            dry_run=dry_run, registry=resolved_registry, run_id=resolved_run_id)

        logger.info("-" * 60)
        logger.info("A8b: ALIGN DIMENSION VALUES TO THEIR CONCEPTS")
        results["value_alignment"] = align_dimension_values(
            self.session, resolved_registry, dry_run=dry_run)

        logger.info("-" * 60)
        logger.info("A8c: ALIGN FACT VALUES TO THEIR CONCEPTS")
        results["fact_value_alignment"] = align_fact_values(
            self.session, resolved_registry, dry_run=dry_run)

        results["alignment_report"] = alignment_report(
            self.session, resolved_registry)

        logger.info("-" * 60)
        logger.info("A9: ATTACH ADMIN CONCEPTS (spatial)")
        admin_result: Dict[str, Any] = {}
        try:
            from utilities.admin_alignment import attach_admin_concepts
            admin_result = attach_admin_concepts(
                self.session, registry=resolved_registry, dry_run=dry_run) or {}
        except RuntimeError as exc:
            logger.warning("  spatial concept attachment skipped: %s", exc)
            admin_result = {"status": "skipped", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("  spatial concept attachment failed: %s: %s",
                           type(exc).__name__, exc)
            admin_result = {"status": "error", "reason": str(exc)}

        location_vocabulary_id = self._location_vocabulary_id(resolved_registry)
        conflicts = ({"stamped": 0, "conflicting": 0, "dangling": 0} if dry_run
                     else self._check_concept_conflicts(location_vocabulary_id))
        admin_result["conflicts"] = conflicts
        results["admin_concepts"] = admin_result
        results["conflicts"] = int(conflicts.get("conflicting", 0))

        if conflicts.get("conflicting"):
            logger.warning(
                "  %d of %d stamped location row(s) carry a concept_id that "
                "disagrees with the concept their own standard_code names; "
                "attach_admin_concepts is the only writer of "
                "dim_location.concept_id, so something else wrote it.",
                conflicts["conflicting"], conflicts["stamped"])
        if conflicts.get("dangling"):
            logger.warning(
                "  %d location row(s) carry a standard_code that is not a concept "
                "in %r, so no concept_id could be stamped from it.",
                conflicts["dangling"], location_vocabulary_id or "(no pack)")

        logger.info("-" * 60)
        logger.info("A9b: NAME LOCATIONS FROM THEIR COORDINATES")
        if name_locations:
            name_report = self.resolve_location_names(
                gadm_gpkg=gadm_gpkg, dry_run=dry_run,
                include_grid=include_grid_names,
                overwrite_human=overwrite_human_names)
        else:
            name_report = {"status": "skipped", "reason": "name_locations=False"}
            logger.info("  skipped (name_locations=False)")
        results["location_names"] = name_report
        if isinstance(results.get("locations"), dict):
            results["locations"]["name_assignment"] = name_report
            if name_report.get("renamed"):
                results["locations"]["name_duplicate_groups"] = \
                    self._location_name_duplicates(limit=50)

        logger.info("-" * 60)
        logger.info("B5: MERGE DIMENSION ROWS BY CONCEPT")
        merge_report = self.consolidate_vocabulary(
            registry=resolved_registry, run_id=resolved_run_id, dry_run=dry_run)
        results["merge"] = merge_report
        results["vocabulary_consolidate"] = {
            table: {"duplicates_found": (report or {}).get("duplicates_found", 0)}
            for table, report in sorted(merge_report.items())}

        logger.info("-" * 60)
        logger.info("FACTS: REASSIGN BY CONCEPT")
        fact_report: Dict[str, Any] = {}
        for table_name in sorted(merge_report):
            report = merge_report[table_name] or {}
            merged = report.get("merged") or []
            if not merged:
                continue
            model_class = _model_for_table(table_name)
            if model_class is None:
                logger.warning("  %s: no model class; fact re-check skipped",
                               table_name)
                continue
            pairs = failed = 0
            for entry in merged:
                canonical = entry.get("canonical_code")
                if canonical is None:
                    continue
                for duplicate in entry.get("duplicate_codes") or ():
                    if duplicate == canonical:
                        continue
                    if dry_run:
                        pairs += 1
                        continue
                    try:
                        self._update_fact_references(model_class, duplicate,
                                                     canonical)
                        pairs += 1
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        logger.warning("  %s: could not re-point %r -> %r: %s",
                                       table_name, duplicate, canonical, exc)
            fact_report[table_name] = {"pairs": pairs, "errors": failed}
        results["facts"] = fact_report

        logger.info("-" * 60)
        logger.info("CASCADES: AUDIT + REMAINING ORPHANS")
        results["remaining_orphans"] = self._check_remaining_orphans()
        if results["remaining_orphans"]:
            for key, count in sorted(results["remaining_orphans"].items()):
                logger.warning("  %s still references %d deleted key(s)", key, count)

        logger.info("-" * 60)
        logger.info("BURDEN: EXPAND TO DAILY")
        burden_stats: Dict[str, Any] = {"inserted": 0, "errors": 0}
        if dry_run:
            logger.info("  (dry run) burden expansion skipped")
        else:
            try:
                # The run id and the policy are both passed through: without the
                # first, the modelled rows cannot be attributed; without the
                # second, the pipeline silently writes at a different grain from
                # the CLI and downstream sums double-count.
                burden_stats = self.expand_burden_to_daily(
                    force=burden_force, prefer=burden_prefer,
                    rate_scale=burden_rate_scale,
                    national_allocation=burden_national_allocation,
                    group_by_level=burden_grain, run_id=resolved_run_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("  burden expansion failed: %s: %s",
                             type(exc).__name__, exc)
                burden_stats = {"inserted": 0, "errors": 1,
                                "reason": f"{type(exc).__name__}: {exc}"}
            try:
                self.fix_orphaned_burden_records()
            except Exception as exc:  # noqa: BLE001
                logger.warning("  orphan burden repair failed: %s", exc)
        results["burden_fixed"] = int(burden_stats.get("inserted", 0))
        results["burden"] = burden_stats

        logger.info("-" * 60)
        logger.info("RESISTANCE: CLASSIFY BIOASSAYS")
        try:
            results["resistance"] = self._compute_resistance_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("  resistance classification failed: %s: %s",
                         type(exc).__name__, exc)
            results["resistance"] = {"status": "error", "reason": str(exc)}

        logger.info("-" * 60)
        logger.info("FACT DUPLICATES + VALIDATION")
        results["duplicates"] = {
            table: {"duplicate_count": count}
            for table, count in sorted(self.resolve_fact_table_duplicates().items())}
        results["validation"] = self.validate_country_codes()
        results["hierarchy"] = self._check_hierarchy(resolved_registry)

        if not dry_run:
            try:
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("  final commit failed: %s", exc)
                self.session.rollback()
                raise

        logger.info("=" * 60)
        logger.info("CONSOLIDATION COMPLETE%s: %d conflict(s), %d orphaned "
                    "reference(s)", " (dry run)" if dry_run else "",
                    results["conflicts"],
                    sum(results["remaining_orphans"].values()))
        logger.info("=" * 60)
        return results


    def summarize_locations(self, *, registry: Any = None,
                            sample_limit: int = 5) -> Dict[str, Any]:
        """Read-only description of dim_location: coverage, hierarchy, names."""
        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        pack = None
        if resolved_registry is not None:
            try:
                pack = resolved_registry.select_pack("dim_location.name")
            except Exception as exc:  # noqa: BLE001
                logger.debug("no location pack for the summary: %s", exc)
        key_function = _pack_key_fn(pack) if pack is not None else _default_vocab_key
        try:
            rows = self.session.execute(text(
                "SELECT location_id, name, location_code, country_code, "
                "country_name, admin_level, centroid_lat, centroid_lon, "
                "standard_code, concept_id, parent_location_id "
                "FROM dim_location ORDER BY location_id")).mappings().all()
        except Exception as exc:  # noqa: BLE001
            logger.error("could not read dim_location: %s", exc)
            return {"rows": 0, "error": f"{type(exc).__name__}: {exc}",
                    "pack": getattr(pack, "pack_id", None)}

        limit = max(0, int(sample_limit))
        known_ids = {int(r["location_id"]) for r in rows}
        by_level: Dict[str, int] = {}
        concept_ids = standard_codes = half_concept = 0
        with_coords = half_coords = with_parent = missing_parent = self_parent = 0
        missing_country = 0
        country_codes: Dict[str, int] = {}
        name_groups: Dict[str, List[Any]] = {}
        unparented, conceptless, broken = [], [], []
        generated_names = 0

        for row in rows:
            label = str(row["admin_level"] or "").strip()
            by_level[label] = by_level.get(label, 0) + 1
            if _looks_generated_name(row["name"], row["location_code"]):
                generated_names += 1
            has_concept = row["concept_id"] is not None
            has_standard = bool(str(row["standard_code"] or "").strip())
            concept_ids += int(has_concept)
            standard_codes += int(has_standard)
            half_concept += int(has_concept != has_standard)

            lat, lon = row["centroid_lat"], row["centroid_lon"]
            lat_ok = lat is not None and -90.0 <= float(lat) <= 90.0
            lon_ok = lon is not None and -180.0 <= float(lon) <= 180.0
            if lat_ok and lon_ok:
                with_coords += 1
            elif lat_ok or lon_ok:
                half_coords += 1

            parent_id = row["parent_location_id"]
            if parent_id is not None:
                with_parent += 1
                parent_id = int(parent_id)
                if parent_id == int(row["location_id"]):
                    self_parent += 1
                    if len(broken) < limit:
                        broken.append({"location_id": int(row["location_id"]),
                                       "name": row["name"],
                                       "reason": "is its own parent"})
                elif parent_id not in known_ids:
                    missing_parent += 1
                    if len(broken) < limit:
                        broken.append({"location_id": int(row["location_id"]),
                                       "name": row["name"],
                                       "parent_location_id": parent_id,
                                       "reason": "parent does not exist"})
            elif len(unparented) < limit:
                unparented.append({"location_id": int(row["location_id"]),
                                   "name": row["name"],
                                   "admin_level": row["admin_level"]})

            country = str(row["country_code"] or "").strip().upper()
            if country:
                country_codes[country] = country_codes.get(country, 0) + 1
            else:
                missing_country += 1

            if not has_concept and len(conceptless) < limit:
                conceptless.append({"location_id": int(row["location_id"]),
                                    "name": row["name"],
                                    "admin_level": row["admin_level"],
                                    "location_code": row["location_code"]})

            name = str(row["name"] or "").strip()
            if name:
                try:
                    key = str(key_function(name) or "").strip()
                except Exception:  # noqa: BLE001
                    key = _default_vocab_key(name)
                if key:
                    name_groups.setdefault(key, []).append(row["location_id"])

        duplicate_names = {k: v for k, v in name_groups.items() if len(v) > 1}
        summary = {
            "rows": len(rows),
            "by_admin_level": {l: by_level[l]
                               for l in sorted(by_level, key=lambda l: (l == "", l))},
            "concept_coverage": {"with_concept_id": concept_ids,
                                 "with_standard_code": standard_codes,
                                 "half_stamped": half_concept,
                                 "without_concept": len(rows) - concept_ids},
            "coordinate_coverage": {
                "with_coordinates": with_coords,
                "half_coordinates": half_coords,
                "without_coordinates": len(rows) - with_coords - half_coords},
            "hierarchy": {"with_parent": with_parent,
                          "without_parent": len(rows) - with_parent,
                          "missing_parent": missing_parent,
                          "self_parent": self_parent, "broken_samples": broken},
            "countries": {"distinct": len(country_codes),
                          "by_code": {c: country_codes[c]
                                      for c in sorted(country_codes)},
                          "without_country_code": missing_country},
            "names": {"generated": generated_names,
                      "human": len(rows) - generated_names},
            "duplicate_names": {
                "group_count": len(duplicate_names),
                "row_count": sum(len(v) for v in duplicate_names.values()),
                "samples": [{"name_key": k, "count": len(duplicate_names[k]),
                             "location_ids": sorted(duplicate_names[k])[:limit]}
                            for k in sorted(duplicate_names)[:limit]]},
            "samples": {"unparented": unparented, "without_concept": conceptless},
            "pack": getattr(pack, "pack_id", None)}

        logger.info("LOCATION SUMMARY (%d row(s)): %d concept, %d complete coords, "
                    "%d parented (%d missing, %d self), %d generated name(s), "
                    "%d duplicate name group(s)", len(rows), concept_ids,
                    with_coords, with_parent, missing_parent, self_parent,
                    generated_names, len(duplicate_names))
        return summary

    
    def close(self) -> Dict[str, Any]:
        """Drain the pending resolution buffer, record the ledger size, close.

        The buffer is kept when its flush fails, so a retry is possible. The
        ledger count is read before the session closes, because after that the
        number is unavailable and a summary that cannot distinguish "the ledger is
        empty" from "the ledger was never consulted" is not a summary.
        """
        if getattr(self, "_closed", False):
            return dict(getattr(self, "_close_report", {}) or {})
        buffer: List[Any] = []
        seen: Set[Tuple[str, str, str, str]] = set()

        def _offer(candidate: Any) -> None:
            if candidate is None:
                return
            key = (str(getattr(candidate, "source_table", "") or "").strip(),
                   str(getattr(candidate, "source_column", "") or "").strip(),
                   "" if getattr(candidate, "source_value", None) is None
                   else str(candidate.source_value),
                   str(getattr(candidate, "run_id", "") or "").strip())
            if key in seen:
                return
            seen.add(key)
            buffer.append(candidate)

        raw_pending = getattr(self, "_pending_resolutions", None)
        if isinstance(raw_pending, Mapping):
            for key in sorted(raw_pending, key=lambda item: str(item)):
                _offer(raw_pending[key])
        elif raw_pending:
            for item in list(raw_pending):
                _offer(item)

        resolver = getattr(self, "resolver", None)
        if resolver is not None and hasattr(resolver, "pending_resolutions"):
            try:
                for item in list(resolver.pending_resolutions() or ()):
                    _offer(item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("close: could not read pending resolutions: %s", exc)

        report: Dict[str, Any] = {"flushed": 0, "pending": len(buffer),
                                  "closed": False, "errors": [],
                                  "ledger_rows": 0}
        if buffer:
            try:
                from etl.loaders.db_loader import DBLoader
                report["flushed"] = int(
                    DBLoader(batch_size=1000, upsert=True)
                    .load_resolutions(buffer) or 0)
                report["pending"] = 0
                self._pending_resolutions = []
                logger.info("close: flushed %d resolution(s) into "
                            "fact_resolution_log / fact_concept_map",
                            report["flushed"])
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                report["errors"].append(message)
                logger.error("close: could not flush %d pending resolution(s) "
                             "(%s); they are KEPT so a retry is possible.",
                             len(buffer), message)
                self._pending_resolutions = list(buffer)

        session = getattr(self, "session", None)
        if session is not None:
            try:
                report["ledger_rows"] = int(session.execute(
                    text("SELECT COUNT(*) FROM fact_resolution_log")).scalar()
                    or 0)
            except Exception as exc:  # noqa: BLE001 - pre-migration database
                logger.debug("close: could not count the resolution ledger: %s", exc)
            try:
                session.close()
                report["closed"] = True
            except Exception as exc:  # noqa: BLE001
                message = f"session close failed: {type(exc).__name__}: {exc}"
                report["errors"].append(message)
                logger.warning("close: %s", message)

        self._closed = True
        self._close_report = dict(report)
        return report


# -- module-level wrapper --------------------------------------------------

def run_consolidation(core_cfg: Optional[Mapping[str, Any]] = None, *,
                      gadm_gpkg: Optional[str] = None,
                      country_codes: Optional[Iterable[str]] = None,
                      dry_run: bool = False, registry: Any = None,
                      run_id: Optional[str] = None,
                      close: bool = True,
                      name_locations: bool = True,
                      include_grid_names: bool = False,
                      overwrite_human_names: bool = False,
                      audit_country_geometry: bool = False,
                      burden_grain: Optional[str] = None,
                      burden_prefer: str = _BURDEN_MEASURE_CASES_MEAN,
                      burden_rate_scale: str = "per_100k",
                      burden_national_allocation: bool = False,
                      burden_force: bool = False) -> Dict[str, Any]:
    """Run consolidation and always close() (the only drain of the buffer).

    core.vocabulary.* keys other than gadm_gpkg are warned about and ignored.

    The ``burden_*`` arguments reach ``expand_burden_to_daily`` unchanged, so an
    orchestrator can select the expansion policy without a second code path.
    """
    config = dict(core_cfg or {})
    admin_boundaries = config.get("admin_boundaries") or {}
    vocabulary = config.get("vocabulary") or {}

    boundary_path = gadm_gpkg or admin_boundaries.get("path") or None
    if not gadm_gpkg:
        for key in sorted(vocabulary, key=str):
            if key == "gadm_gpkg" and not boundary_path:
                boundary_path = vocabulary[key]
                logger.warning("core.vocabulary.gadm_gpkg is deprecated; move it to "
                               "core.admin_boundaries.path.")
                continue
            logger.warning("core.vocabulary.%s is IGNORED (value: %r). Vocabularies "
                           "are chosen by the registry.", key, vocabulary[key])

    if country_codes is not None:
        codes = [str(c).strip() for c in country_codes if str(c).strip()]
    else:
        codes = [str(c).strip() for c in (config.get("countries") or [])
                 if str(c).strip()]

    normalised: List[str] = []
    for code in codes:
        iso2 = get_iso_country_code(code)
        if iso2 is None:
            logger.warning("unrecognised country code %r; passing through so the "
                           "filter is visibly wrong rather than silently wide", code)
            normalised.append(code.upper())
            continue
        normalised.append(ISO_COUNTRY_CODES.get(iso2) or code.upper())

    consolidator = None
    close_report: Optional[Dict[str, Any]] = None
    try:
        consolidator = DataConsolidator()
        if registry is not None:
            consolidator.registry = registry
        if run_id is not None:
            consolidator.run_id = run_id
        results = consolidator.consolidate_all(
            gadm_gpkg=boundary_path, country_codes=normalised or None,
            dry_run=dry_run, registry=registry, run_id=run_id,
            name_locations=name_locations, include_grid_names=include_grid_names,
            overwrite_human_names=overwrite_human_names,
            audit_country_geometry=audit_country_geometry,
            burden_grain=burden_grain, burden_prefer=burden_prefer,
            burden_rate_scale=burden_rate_scale,
            burden_national_allocation=burden_national_allocation,
            burden_force=burden_force)
    except Exception as exc:  # noqa: BLE001
        logger.error("consolidation failed: %s: %s", type(exc).__name__, exc,
                     exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if consolidator is not None and close:
            try:
                close_report = consolidator.close()
            except Exception as exc:  # noqa: BLE001
                logger.error("close: draining the consolidator failed: %s: %s",
                             type(exc).__name__, exc)
                close_report = {"errors": [f"{type(exc).__name__}: {exc}"]}

    if isinstance(results, dict):
        results = dict(results)
        results["close"] = close_report
    return results


__all__ = [
    "DataConsolidator",
    "enrich_location_dataframe",
    "get_country_from_coords",
    "get_country_name",
    "get_iso_country_code",
    "haversine_distance",
    "run_consolidation",
]