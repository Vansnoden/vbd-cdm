# -*- coding: utf-8 -*-
"""Coordinate- and GID-driven boundary alignment.

Which layers to open, which column holds the GID and which hold the name all come
from the active location pack's ``layers`` block. Nothing here contains the name
of a country, region or admin unit.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import geopandas as gpd
from sqlalchemy import bindparam, text

from database.geoutils import haversine_distance
from database.session import require_identifier
from utilities.vocabulary.registry import ConceptPack, VocabularyRegistry

logger = logging.getLogger(__name__)

MAX_IN_VARS = 900

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "vocabularies" / "registry"

_REGISTRY_CACHE: dict = {}
_WARNED: set = set()

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _text(value: Any) -> str:
    """``str(value).strip()`` with None/NaN/NA mapped to ''. Never raises."""
    if value is None:
        return ""
    if type(value).__name__ in ("NAType", "NaTType"):
        return ""
    try:
        if value != value:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return str(value).strip()


def _warn_once(marker: str, message: str, *args: Any) -> None:
    if marker in _WARNED:
        return
    _WARNED.add(marker)
    logger.warning(message, *args)


def _load_registry(registry: Any = None) -> Optional[VocabularyRegistry]:
    """Return ``registry``, or load the shipped packs once. Missing dir -> None."""
    if registry is not None:
        return registry
    key = str(DEFAULT_REGISTRY_PATH)
    cached = _REGISTRY_CACHE.get(key)
    if cached is not None:
        return cached
    if not DEFAULT_REGISTRY_PATH.is_dir():
        return None
    loaded = VocabularyRegistry.load_from(DEFAULT_REGISTRY_PATH)
    _REGISTRY_CACHE[key] = loaded
    return loaded


def _active_location_pack(pack_id: Optional[str] = None,
                          registry: Any = None) -> ConceptPack:
    """The pack that declares the boundary layout.

    With ``pack_id`` it is fetched by id and checked for layers. Without one, the
    winner is the highest ``rank_key()`` among packs that *declare* ``layers``, so
    a non-boundary pack can never win by accident. No silent fallback to a
    hard-coded layout.
    """
    loaded = _load_registry(registry)
    if loaded is None:
        raise RuntimeError(
            f"admin alignment needs the vocabulary registry, and none was supplied "
            f"and none exists at {DEFAULT_REGISTRY_PATH}. Pass a registry, or "
            f"build the packs.")

    if pack_id is not None:
        pack = loaded.get(pack_id)
        if not getattr(pack, "layers", ()):
            raise RuntimeError(
                f"pack {pack_id!r} declares no `layers` block, so it cannot "
                f"describe a boundary hierarchy.")
        return pack

    candidates = [p for p in loaded.packs() if getattr(p, "layers", ())]
    if not candidates:
        raise RuntimeError(
            "no loaded vocabulary pack declares a `layers` block, so there is no "
            "boundary layout to read.")
    return max(candidates, key=ConceptPack.rank_key)


def _layer_specs(pack: ConceptPack,
                 levels: Optional[Iterable[int]] = None) -> Tuple[Dict[str, Any], ...]:
    """The pack's layer specs, normalised and ordered by level.

    Each returned dict carries the four declared fields plus the ``admin_level``
    the pack maps that level to, so a caller cannot forget to consult
    ``admin_level_map`` separately.
    """
    wanted = None if levels is None else {int(level) for level in levels}
    specs: List[Dict[str, Any]] = []

    for spec in pack.layers:
        level = int(spec["level"])
        if wanted is not None and level not in wanted:
            continue
        admin_level = pack.admin_level_for(level)
        if not admin_level:
            raise RuntimeError(
                f"pack {pack.pack_id!r} declares layer level {level} but has no "
                f"entry for it in admin_level_map; the rows could not be labelled.")
        specs.append({
            "level": level,
            "layer": str(spec["layer"]),
            "gid_column": str(spec["gid_column"]),
            "name_columns": tuple(str(n) for n in spec["name_columns"]),
            "admin_level": admin_level,
        })

    if wanted is not None:
        missing = sorted(wanted - {s["level"] for s in specs})
        if missing:
            raise ValueError(
                f"pack {pack.pack_id!r} does not declare layer level(s) {missing}; "
                f"it declares {sorted(int(s['level']) for s in pack.layers)}.")
    if not specs:
        raise ValueError(
            f"pack {pack.pack_id!r} yields no layer specs for levels {levels!r}.")

    specs.sort(key=lambda s: s["level"])
    return tuple(specs)


def _parent_gid(gid: str) -> Optional[str]:
    """The GID of ``gid``'s parent, or None for a top-level GID.

    GADM-style identifiers are hierarchical: ``GID_2_KEN.3.1``'s parent is
    ``GID_1_KEN.3`` — decrement the level, drop the last dotted segment. This is
    the only place the hierarchy rule lives.
    """
    value = _text(gid)
    if not value:
        return None

    head, _, remainder = value.partition("_")
    number, _, path = remainder.partition("_")
    if not head or not path:
        return None
    try:
        level = int(number)
    except (TypeError, ValueError):
        return None
    if level <= 0:
        return None

    parent_path = path.rsplit(".", 1)[0] if "." in path else path
    return f"{head}_{level - 1}_{parent_path}"


def _location_fact_tables() -> Tuple[str, ...]:
    """Every ``fact_*`` table carrying a ``location_id`` FK, sorted.

    Derived from ``Base.metadata`` so a new fact table needs no edit here.
    """
    from database.session import Base

    return tuple(sorted(
        table.name for table in Base.metadata.tables.values()
        if table.name.startswith("fact_") and "location_id" in table.columns))


# ---------------------------------------------------------------------------
# load_admin_hierarchy
# ---------------------------------------------------------------------------

def load_admin_hierarchy(gpkg_path: Union[str, Path], *,
                         pack_id: Optional[str] = None,
                         levels: Optional[Iterable[int]] = None,
                         registry: Any = None) -> Dict[int, "gpd.GeoDataFrame"]:
    """Open the boundary file and return ``{level: GeoDataFrame}``, keys ascending.

    A layer absent from the file, or lacking a declared column, is reported by
    name and skipped; a file from which no declared layer opens raises.
    """
    path = Path(gpkg_path)
    if not path.exists():
        raise ValueError(f"boundary file not found: {path}")

    pack = _active_location_pack(pack_id, registry)
    specs = _layer_specs(pack, levels=levels)

    available: Optional[set] = None
    try:
        listing = gpd.list_layers(path)
        column = "name" if "name" in listing.columns else listing.columns[0]
        available = {str(value) for value in listing[column]}
    except Exception as exc:  # noqa: BLE001 - older geopandas / no driver
        logger.debug("could not list layers in %s: %s", path, exc)

    hierarchy: Dict[int, "gpd.GeoDataFrame"] = {}
    skipped: List[str] = []

    for spec in specs:
        level, layer = spec["level"], spec["layer"]
        if available is not None and layer not in available:
            skipped.append(f"level {level}: no layer {layer!r} in {path.name}")
            continue
        try:
            frame = gpd.read_file(path, layer=layer)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"level {level}: could not read {layer!r} "
                           f"({type(exc).__name__}: {exc})")
            continue

        required = (spec["gid_column"],) + tuple(spec["name_columns"])
        absent = [c for c in required if c not in frame.columns]
        if absent:
            skipped.append(f"level {level}: layer {layer!r} is missing {absent}")
            continue
        if frame.empty:
            skipped.append(f"level {level}: layer {layer!r} is empty")
            continue

        hierarchy[level] = frame
        logger.info("  %s level %d -> %r: %d row(s), GID %r",
                    pack.pack_id, level, layer, len(frame), spec["gid_column"])

    for note in skipped:
        _warn_once(f"layer:{path.name}:{note}", "  skipped %s", note)

    if not hierarchy:
        raise ValueError(
            f"no declared layer could be opened from {path}. Pack "
            f"{pack.pack_id!r} declares {[s['layer'] for s in specs]}. Reasons: "
            + "; ".join(skipped))

    return {level: hierarchy[level] for level in sorted(hierarchy)}


# ---------------------------------------------------------------------------
# resolve_parent_locations
# ---------------------------------------------------------------------------

def resolve_parent_locations(session, *,
                             gpkg_path: Optional[Union[str, Path]] = None,
                             hierarchy: Optional[Dict[int, "gpd.GeoDataFrame"]] = None,
                             pack_id: Optional[str] = None,
                             registry: Any = None,
                             country_codes: Optional[Iterable[str]] = None,
                             levels: Optional[Iterable[int]] = None,
                             dry_run: bool = False) -> Dict[str, Any]:
    """Set ``admin_level`` and ``parent_location_id`` from GIDs.

    Each boundary row is indexed by GID; each ``dim_location`` row whose
    ``location_code`` is a known GID gets its ``admin_level`` and its parent's
    ``location_id``. The parent is found through the identifier, not a name or a
    geometry, so two districts called "Central" cannot be confused.
    """
    pack = _active_location_pack(pack_id, registry)
    specs = {spec["level"]: spec for spec in _layer_specs(pack, levels=levels)}

    if hierarchy is None:
        if gpkg_path is None:
            raise ValueError(
                "resolve_parent_locations needs `hierarchy` or `gpkg_path`.")
        hierarchy = load_admin_hierarchy(
            gpkg_path, pack_id=pack.pack_id, levels=levels, registry=registry)

    allowed_countries = None
    if country_codes:
        allowed_countries = {_text(c).upper() for c in country_codes if _text(c)}

    boundary: Dict[str, Dict[str, Any]] = {}
    countries_skipped = 0

    for level in sorted(hierarchy):
        spec = specs.get(level) or _layer_specs(pack, levels=[level])[0]
        for record in hierarchy[level].to_dict("records"):
            gid = _text(record.get(spec["gid_column"]))
            if not gid:
                continue
            if allowed_countries is not None:
                country = gid.split("_", 2)[-1].split(".", 1)[0].upper()
                if country not in allowed_countries:
                    countries_skipped += 1
                    continue

            name = next((_text(record.get(c)) for c in spec["name_columns"]
                         if _text(record.get(c))), None)
            boundary[gid] = {"level": level,
                             "admin_level": spec["admin_level"], "name": name}

    empty = {"rows_examined": 0, "admin_level_set": 0, "parents_set": 0,
             "unmatched": 0, "countries_skipped": countries_skipped,
             "dry_run": bool(dry_run)}
    if not boundary:
        logger.warning("  no boundary rows indexed (pack %r, levels %s)",
                       pack.pack_id, sorted(specs))
        return empty

    rows = session.execute(text(
        "SELECT location_id, location_code, admin_level, parent_location_id "
        "FROM dim_location WHERE location_code IS NOT NULL")).fetchall()

    id_by_gid = {
        _text(code): int(loc_id) for loc_id, code, _a, _p in rows
        if _text(code) and _text(code) in boundary
    }

    updates: List[Dict[str, Any]] = []
    unmatched = admin_level_set = parents_set = 0

    for location_id, location_code, admin_level, parent_location_id in rows:
        gid = _text(location_code)
        info = boundary.get(gid)
        if info is None:
            unmatched += 1
            continue

        new_level = info["admin_level"]
        parent_gid = _parent_gid(gid)
        new_parent_id = id_by_gid.get(parent_gid) if parent_gid else None

        fields: Dict[str, Any] = {}
        if new_level and new_level != _text(admin_level):
            fields["admin_level"] = new_level
            admin_level_set += 1
        if new_parent_id is not None and new_parent_id != parent_location_id:
            fields["parent_location_id"] = new_parent_id
            parents_set += 1
        if not fields:
            continue
        fields["location_id"] = int(location_id)
        updates.append(fields)

    if updates and not dry_run:
        statement = text(
            "UPDATE dim_location "
            "SET admin_level = COALESCE(:admin_level, admin_level), "
            "    parent_location_id = COALESCE(:parent_location_id, parent_location_id) "
            "WHERE location_id = :location_id")
        for start in range(0, len(updates), MAX_IN_VARS):
            chunk = updates[start:start + MAX_IN_VARS]
            session.execute(statement, [
                {"location_id": row["location_id"],
                 "admin_level": row.get("admin_level"),
                 "parent_location_id": row.get("parent_location_id")}
                for row in chunk])
        session.commit()

    logger.info("  parent resolution (pack %r): %d examined, %d labelled, %d "
                "parented, %d unmatched%s", pack.pack_id, len(rows),
                admin_level_set, parents_set, unmatched,
                " (dry run)" if dry_run else "")
    return {"rows_examined": len(rows), "admin_level_set": admin_level_set,
            "parents_set": parents_set, "unmatched": unmatched,
            "countries_skipped": countries_skipped, "dry_run": bool(dry_run)}


# ---------------------------------------------------------------------------
# attach_admin_concepts
# ---------------------------------------------------------------------------

def attach_admin_concepts(session, pack_id: str = "", *, registry: Any = None,
                          dry_run: bool = False) -> Dict[str, Any]:
    """Stamp ``concept_id`` and ``standard_code`` on ``dim_location``.

    ``location_code`` *is* the boundary GID, which is also the vocabulary's
    ``concept_code``, so attachment is a key join: no name comparison, no fuzzy
    matching, no ambiguity. This is the **only** writer of
    ``dim_location.concept_id`` — string matching is unsafe for place names, so
    the reconciliation pass records those matches as ``needs_review`` and never
    stamps the column.
    """
    pack = _active_location_pack(pack_id or None, registry)
    vocabulary_id = pack.vocabulary_id

    concept_rows = session.execute(text(
        "SELECT concept_code, concept_id FROM dim_concept "
        "WHERE vocabulary_id = :v ORDER BY concept_code"), {"v": vocabulary_id}
    ).fetchall()
    concept_id_by_code = {
        _text(code): int(concept_id) for code, concept_id in concept_rows
        if _text(code) and concept_id is not None}

    if not concept_id_by_code:
        _warn_once(
            f"admin-concepts-empty:{vocabulary_id}",
            "  %r has no concepts in dim_concept (vocabulary_id=%r), so no "
            "location can be stamped. Load that vocabulary under its "
            "version-qualified id.", pack.pack_id, vocabulary_id)
        return {"pack_id": pack.pack_id, "vocabulary_id": vocabulary_id,
                "concepts": 0, "rows_examined": 0, "attached": 0,
                "unchanged": 0, "unmatched": 0, "dry_run": bool(dry_run)}

    rows = session.execute(text(
        "SELECT location_id, location_code, concept_id, standard_code "
        "FROM dim_location WHERE location_code IS NOT NULL")).fetchall()

    updates: List[Dict[str, Any]] = []
    unmatched = unchanged = 0

    for location_id, location_code, concept_id, standard_code in rows:
        code = _text(location_code)
        new_concept_id = concept_id_by_code.get(code)
        if new_concept_id is None:
            unmatched += 1
            continue
        if concept_id == new_concept_id and _text(standard_code) == code:
            unchanged += 1
            continue
        updates.append({"location_id": int(location_id),
                        "concept_id": new_concept_id, "standard_code": code})

    if updates and not dry_run:
        statement = text(
            "UPDATE dim_location SET concept_id = :concept_id, "
            "standard_code = :standard_code WHERE location_id = :location_id")
        for start in range(0, len(updates), MAX_IN_VARS):
            session.execute(statement, updates[start:start + MAX_IN_VARS])
        session.commit()

    logger.info("  admin concepts (pack %r, %d concept(s)): %d/%d attached, "
                "%d unchanged, %d unmatched%s", pack.pack_id,
                len(concept_id_by_code), len(updates), len(rows), unchanged,
                unmatched, " (dry run)" if dry_run else "")
    return {"pack_id": pack.pack_id, "vocabulary_id": vocabulary_id,
            "concepts": len(concept_id_by_code), "rows_examined": len(rows),
            "attached": len(updates), "unchanged": unchanged,
            "unmatched": unmatched, "dry_run": bool(dry_run)}


# ---------------------------------------------------------------------------
# reassign_grid_facts_to_admin
# ---------------------------------------------------------------------------

def reassign_grid_facts_to_admin(session, *, pack_id: Optional[str] = None,
                                 registry: Any = None, dry_run: bool = False,
                                 prune_grid_locations: bool = False
                                 ) -> Dict[str, Any]:
    """Point every fact row attached to a grid cell at the nearest admin unit.

    A grid cell is a synthetic carrier of coordinates, not an analysis unit. Grid
    level and the admin levels come from the pack (``grid_level``,
    ``admin_level_map``), not from a literal ``'grid'``.

    Determinism: the location query is ``ORDER BY location_id`` and the
    comparison is strict ``<``, so an exact distance tie is won by the **lowest
    ``location_id``**. That is a stated rule, not "whichever row the engine
    returned first".
    """
    pack = _active_location_pack(pack_id, registry)

    grid_level = pack.grid_level
    if not grid_level:
        raise RuntimeError(
            f"pack {pack.pack_id!r} declares no `grid_level`, so grid facts cannot "
            f"be identified.")

    admin_levels: Tuple[str, ...] = tuple(pack.admin_level_map.values())
    if not admin_levels:
        raise RuntimeError(
            f"pack {pack.pack_id!r} declares no `admin_level_map`, so there is no "
            f"administrative level to reassign to.")

    rows = session.execute(text(
        "SELECT location_id, name, admin_level, centroid_lat, centroid_lon "
        "FROM dim_location ORDER BY location_id")).fetchall()

    by_level: Dict[str, List[Tuple[int, float, float]]] = {}
    grid_locations: List[Tuple[int, float, float]] = []

    for location_id, _name, admin_level, centroid_lat, centroid_lon in rows:
        label = _text(admin_level)
        try:
            lat, lon = float(centroid_lat), float(centroid_lon)
        except (TypeError, ValueError):
            continue
        if lat != lat or lon != lon:  # NaN
            continue
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            continue
        if label == grid_level:
            grid_locations.append((int(location_id), lat, lon))
        elif label in admin_levels:
            by_level.setdefault(label, []).append((int(location_id), lat, lon))

    base = {"grid_level": grid_level, "tie_break": "location_id ascending",
            "dry_run": bool(dry_run)}
    if not grid_locations:
        logger.info("  no grid locations found (grid_level=%r)", grid_level)
        return dict(base, grid_locations=0, admin_locations=0, reassigned=0,
                    unmatched=0, pruned=0)

    targets: List[Tuple[int, float, float]] = []
    target_level: Optional[str] = None
    for level_label in reversed(admin_levels):
        candidates = by_level.get(level_label) or []
        if candidates:
            targets, target_level = candidates, level_label
            break

    if not targets:
        _warn_once(
            f"grid-targets:{pack.pack_id}",
            "  %d grid location(s) found but no location carries a configured "
            "administrative level (%s); no grid fact reassigned.",
            len(grid_locations), list(admin_levels))
        return dict(base, grid_locations=len(grid_locations), admin_locations=0,
                    reassigned=0, unmatched=len(grid_locations), pruned=0)

    nearest: Dict[int, int] = {}
    unmatched = 0
    for grid_id, grid_lat, grid_lon in grid_locations:
        best_id: Optional[int] = None
        best_distance = float("inf")
        for candidate_id, candidate_lat, candidate_lon in targets:
            distance = haversine_distance(grid_lat, grid_lon,
                                          candidate_lat, candidate_lon)
            if distance < best_distance:
                best_distance, best_id = distance, candidate_id
        if best_id is None:
            unmatched += 1
        elif best_id != grid_id:
            nearest[grid_id] = best_id

    fact_tables = _location_fact_tables()
    reassigned = 0

    if nearest and not dry_run:
        ordered_pairs = sorted(nearest.items())
        for table_name in fact_tables:
            if not _SAFE_TABLE.match(table_name):
                raise ValueError(f"refusing to interpolate {table_name!r}")
            statement = text(
                f"UPDATE {table_name} SET location_id = :new_id "
                f"WHERE location_id = :old_id")
            for old_id, new_id in ordered_pairs:
                result = session.execute(statement,
                                         {"new_id": new_id, "old_id": old_id})
                reassigned += int(result.rowcount or 0)
        session.commit()

    pruned = 0
    if prune_grid_locations and not dry_run and nearest:
        orphan_ids = sorted(nearest)
        still_referenced: set = set()
        for table_name in fact_tables:
            if not _SAFE_TABLE.match(table_name):
                continue
            for start in range(0, len(orphan_ids), MAX_IN_VARS):
                chunk = orphan_ids[start:start + MAX_IN_VARS]
                statement = text(
                    f"SELECT DISTINCT location_id FROM {table_name} "
                    f"WHERE location_id IN :vals ORDER BY location_id"
                ).bindparams(bindparam("vals", expanding=True))
                for (referenced,) in session.execute(
                    statement, {"vals": chunk}).fetchall():
                    still_referenced.add(int(referenced))

        deletable = [i for i in orphan_ids if i not in still_referenced]
        if deletable:
            statement = text(
                "DELETE FROM dim_location WHERE location_id IN :vals"
            ).bindparams(bindparam("vals", expanding=True))
            for start in range(0, len(deletable), MAX_IN_VARS):
                session.execute(statement,
                                {"vals": deletable[start:start + MAX_IN_VARS]})
            session.commit()
            pruned = len(deletable)

    logger.info("  grid reassignment (%r -> %r): %d/%d cell(s), %d fact row(s) "
                "moved, %d unmatched, %d pruned, tie-break location_id ascending%s",
                grid_level, target_level, len(nearest), len(grid_locations),
                reassigned, unmatched, pruned, " (dry run)" if dry_run else "")
    return dict(base, grid_locations=len(grid_locations),
                admin_locations=len(targets), reassigned=reassigned,
                unmatched=unmatched, pruned=pruned)


__all__ = [
    "attach_admin_concepts",
    "load_admin_hierarchy",
    "reassign_grid_facts_to_admin",
    "resolve_parent_locations",
]