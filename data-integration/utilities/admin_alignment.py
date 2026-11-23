# -*- coding: utf-8 -*-
"""Boundary alignment: GID strings first, point-in-polygon second.

Layer names, GID columns, name columns and admin-level labels all come from the
location pack's manifest. Nothing here names a country, a region or a unit.

Two GID grammars are read:

    GADM 3.6   GID_0_KEN  GID_1_KEN.3  GID_2_KEN.3.1
    GADM 4.x   KEN        KEN.3_1      KEN.3.1_1

``dim_location.location_code`` is not a GID. The ingest mappings write
"<name>/<lat>/<lon>" there, deliberately, so a newly seen site keeps its own
identity instead of collapsing into a boundary unit. Matching on GID equality
alone therefore places nothing, which is why the spatial pass exists: every row
that carries coordinates is assigned the deepest polygon containing its
centroid, and its parent is read from that GID's own parent.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import geopandas as gpd
import pandas as pd
from sqlalchemy import bindparam, text

from database.geoutils import haversine_distance
from database.session import require_identifier
from utilities.vocabulary.registry import ConceptPack, VocabularyRegistry
from utilities.constants import GRID_NAME_RE, MAX_IN_VARS

logger = logging.getLogger(__name__)


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
    """The pack that declares the boundary layout."""
    loaded = _load_registry(registry)
    if loaded is None:
        raise RuntimeError(
            f"admin alignment needs the vocabulary registry, and none was supplied "
            f"and none exists at {DEFAULT_REGISTRY_PATH}.")

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
                f"pack {pack.pack_id!r} does not declare layer level(s) {missing}.")
    if not specs:
        raise ValueError(
            f"pack {pack.pack_id!r} yields no layer specs for levels {levels!r}.")

    specs.sort(key=lambda s: s["level"])
    return tuple(specs)


# ---------------------------------------------------------------------------
# GID grammar
# ---------------------------------------------------------------------------

def _gid_level(gid: Any) -> Optional[int]:
    """Boundary level a GID encodes, or None.

    ``GID_2_KEN.3.1`` -> 2, ``KEN.3.1_1`` -> 2, ``KEN`` -> 0.
    """
    value = _text(gid)
    if not value:
        return None

    head, sep, remainder = value.partition("_")
    if sep and head.upper() == "GID":
        number, sep2, path = remainder.partition("_")
        if sep2 and path:
            try:
                return int(number)
            except (TypeError, ValueError):
                return None
        return None

    base, sep, suffix = value.rpartition("_")
    if sep and base and suffix.isdigit() and "." in base:
        return base.count(".")

    return 0


def _gid_country(gid: Any) -> str:
    """ISO-3 country field a GID carries, upper-cased. '' when absent."""
    value = _text(gid)
    if not value:
        return ""

    head, sep, remainder = value.partition("_")
    if sep and head.upper() == "GID":
        path = remainder.partition("_")[2]
        return path.split(".", 1)[0].upper() if path else ""

    return value.split(".", 1)[0].split("_", 1)[0].upper()


def _parent_gid(gid: Any) -> Optional[str]:
    """The GID of ``gid``'s parent, or None for a top-level GID."""
    value = _text(gid)
    if not value:
        return None

    level = _gid_level(value)
    if level is None or level < 1:
        return None

    head, sep, remainder = value.partition("_")
    if sep and head.upper() == "GID":
        number, _, path = remainder.partition("_")
        if level <= 1:
            return f"{head}_0_{path.split('.', 1)[0]}"
        return f"{head}_{level - 1}_{path.rsplit('.', 1)[0]}"

    base, _, suffix = value.rpartition("_")
    if level <= 1:
        return base.split(".", 1)[0]
    return f"{base.rsplit('.', 1)[0]}_{suffix}"


def _location_fact_tables() -> Tuple[str, ...]:
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
    """Open the boundary file and return ``{level: GeoDataFrame}``.

    A layer is kept when its GID column is present. A declared *name* column that
    is absent is reported and the layer kept: the hierarchy is built from the GID,
    and dropping the level would lose its rows from the parent index. GADM 4.x
    puts the country name in ``COUNTRY`` rather than ``NAME_0``, which is the case
    this tolerates.
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

        if spec["gid_column"] not in frame.columns:
            skipped.append(
                f"level {level}: layer {layer!r} has no GID column "
                f"{spec['gid_column']!r}; columns are {sorted(frame.columns)[:12]}")
            continue

        absent_names = [c for c in spec["name_columns"] if c not in frame.columns]
        if absent_names:
            _warn_once(
                f"layer-names:{path.name}:{level}",
                "  level %d: layer %r has no declared name column(s) %s; keeping "
                "the layer, but those rows carry no name. Columns present: %s.",
                level, layer, absent_names, sorted(frame.columns)[:12])

        if frame.empty:
            skipped.append(f"level {level}: layer {layer!r} is empty")
            continue

        hierarchy[level] = frame
        logger.info("  %s level %d -> %r: %d row(s), GID %r, geometry %r",
                    pack.pack_id, level, layer, len(frame), spec["gid_column"],
                    frame.geometry.name if frame.geometry.name else None)

    for note in skipped:
        _warn_once(f"layer:{path.name}:{note}", "  skipped %s", note)

    if not hierarchy:
        raise ValueError(
            f"no declared layer could be opened from {path}. Pack "
            f"{pack.pack_id!r} declares {[s['layer'] for s in specs]}. Reasons: "
            + "; ".join(skipped))

    return {level: hierarchy[level] for level in sorted(hierarchy)}


def _boundary_index(hierarchy: Dict[int, "gpd.GeoDataFrame"],
                    specs: Dict[int, Dict[str, Any]],
                    allowed_countries: Optional[set],
                    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int], int]:
    """``({gid: info}, {country: count}, skipped_by_country)``."""
    boundary: Dict[str, Dict[str, Any]] = {}
    seen_countries: Dict[str, int] = {}
    skipped = 0

    for level in sorted(hierarchy):
        spec = specs.get(level)
        if spec is None:
            continue
        for record in hierarchy[level].to_dict("records"):
            gid = _text(record.get(spec["gid_column"]))
            if not gid:
                continue

            country = _gid_country(gid)
            seen_countries[country or "(none)"] = (
                seen_countries.get(country or "(none)", 0) + 1)

            if allowed_countries is not None and country not in allowed_countries:
                skipped += 1
                continue

            name = next((_text(record.get(c)) for c in spec["name_columns"]
                         if _text(record.get(c))), None)
            boundary[gid] = {"level": level,
                             "admin_level": spec["admin_level"], "name": name}
    return boundary, seen_countries, skipped


def _points_frame(frame: "pd.DataFrame") -> "gpd.GeoDataFrame":
    """Rows with usable coordinates as an EPSG:4326 point layer."""
    if frame.empty:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    usable = frame.dropna(subset=["centroid_lat", "centroid_lon"])
    inside = usable[
        usable["centroid_lat"].between(-90.0, 90.0)
        & usable["centroid_lon"].between(-180.0, 180.0)]
    if inside.empty:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(
        inside.copy(),
        geometry=gpd.points_from_xy(inside["centroid_lon"], inside["centroid_lat"]),
        crs="EPSG:4326")


def _containing_gid(points: "gpd.GeoDataFrame",
                    hierarchy: Dict[int, "gpd.GeoDataFrame"],
                    specs: Dict[int, Dict[str, Any]],
                    ) -> Dict[int, Tuple[str, str]]:
    """``{location_id: (gid, admin_level)}`` from the deepest containing polygon."""
    found: Dict[int, Tuple[str, str]] = {}
    if points is None or points.empty:
        return found

    for level in sorted(hierarchy, reverse=True):
        spec = specs.get(level)
        if spec is None:
            continue
        layer = hierarchy[level]
        gid_column = spec["gid_column"]
        if gid_column not in layer.columns or layer.geometry.name not in layer.columns:
            continue

        frame = layer[[gid_column, layer.geometry.name]].rename(
            columns={gid_column: "_gid"})
        if frame.crs is not None and points.crs is not None \
                and frame.crs != points.crs:
            try:
                frame = frame.to_crs(points.crs)
            except Exception as exc:  # noqa: BLE001
                logger.debug("could not reproject level %d: %s", level, exc)
                continue

        try:
            joined = gpd.sjoin(points, frame, how="left", predicate="within")
        except TypeError:
            try:
                joined = gpd.sjoin(points, frame, how="left", op="within")
            except Exception as exc:  # noqa: BLE001
                logger.debug("sjoin at level %d failed: %s", level, exc)
                continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("sjoin at level %d failed: %s", level, exc)
            continue

        joined = joined[joined["_gid"].notna()]
        for location_id, gid in zip(joined["location_id"], joined["_gid"]):
            key = int(location_id)
            if key in found:
                continue
            found[key] = (str(gid), spec["admin_level"])
    return found


def _apply_updates(session, updates: List[Dict[str, Any]], dry_run: bool) -> int:
    if not updates or dry_run:
        return len(updates)
    statement = text(
        "UPDATE dim_location "
        "SET admin_level = COALESCE(:admin_level, admin_level), "
        "    standard_code = COALESCE(:standard_code, standard_code), "
        "    parent_location_id = COALESCE(:parent_location_id, parent_location_id) "
        "WHERE location_id = :location_id")
    for start in range(0, len(updates), MAX_IN_VARS):
        session.execute(statement, updates[start:start + MAX_IN_VARS])
    session.commit()
    return len(updates)


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
                             dry_run: bool = False,
                             spatial: bool = True) -> Dict[str, Any]:
    """Label ``admin_level``, stamp ``standard_code`` and set ``parent_location_id``.
    """
    pack = _active_location_pack(pack_id, registry)
    specs = {spec["level"]: spec for spec in _layer_specs(pack, levels=levels)}

    if hierarchy is None:
        if gpkg_path is None:
            raise ValueError("resolve_parent_locations needs `hierarchy` or "
                             "`gpkg_path`.")
        hierarchy = load_admin_hierarchy(
            gpkg_path, pack_id=pack.pack_id, levels=levels, registry=registry)

    allowed_countries = None
    if country_codes:
        allowed = {_text(c).upper() for c in country_codes if _text(c)}
        allowed_countries = allowed or None

    boundary, seen_countries, countries_skipped = _boundary_index(
        hierarchy, specs, allowed_countries)

    if not boundary:
        logger.warning(
            "  no boundary rows indexed (pack %r, levels %s, requested countries "
            "%s). Country fields found in the boundary file: %s.",
            pack.pack_id, sorted(specs), sorted(allowed_countries or ()) or "(all)",
            ", ".join(f"{c}={n}" for c, n in sorted(seen_countries.items()))
            or "(none)")
        return {"rows_examined": 0, "admin_level_set": 0, "parents_set": 0,
                "unmatched": 0, "countries_skipped": countries_skipped,
                "boundary_indexed": 0, "placed_by_gid": 0, "placed_by_geometry": 0,
                "dry_run": bool(dry_run)}

    rows = session.execute(text(
        "SELECT location_id, name, location_code, admin_level, country_code, "
        "       centroid_lat, centroid_lon, parent_location_id, standard_code "
        "FROM dim_location ORDER BY location_id")).mappings().all()
    if not rows:
        return {"rows_examined": 0, "admin_level_set": 0, "parents_set": 0,
                "unmatched": 0, "countries_skipped": countries_skipped,
                "boundary_indexed": len(boundary), "placed_by_gid": 0,
                "placed_by_geometry": 0, "dry_run": bool(dry_run)}

    frame = pd.DataFrame([dict(row) for row in rows])
    frame["centroid_lat"] = pd.to_numeric(frame["centroid_lat"], errors="coerce")
    frame["centroid_lon"] = pd.to_numeric(frame["centroid_lon"], errors="coerce")

    grid_level = getattr(pack, "grid_level", None)

    def _is_grid(record: "pd.Series") -> bool:
        label = _text(record.get("admin_level"))
        if grid_level and label == grid_level:
            return True
        return bool(GRID_NAME_RE.match(_text(record.get("name"))))

    frame["_is_grid"] = frame.apply(_is_grid, axis=1)
    existing_parent = dict(zip(frame["location_id"].astype(int),
                              frame["parent_location_id"]))

    # ---- 1. GID equality ------------------------------------------------
    placed: Dict[int, Tuple[str, str]] = {}
    by_gid = 0
    for record in frame.to_dict("records"):
        gid = _text(record.get("location_code"))
        info = boundary.get(gid)
        if info is None:
            continue
        placed[int(record["location_id"])] = (gid, info["admin_level"])
        by_gid += 1

    # ---- 2. Geometry for everything a GID did not place ------------------
    by_geometry = 0
    if spatial:
        pending_admin = frame[
            (~frame["_is_grid"]) & (~frame["location_id"].astype(int).isin(placed))]
        admin_points = _points_frame(pending_admin)
        if not admin_points.empty:
            spatial_admin = _containing_gid(admin_points, hierarchy, specs)
            for location_id, (gid, admin_level) in spatial_admin.items():
                placed[location_id] = (gid, admin_level)
                by_geometry += 1

    gid_to_location: Dict[str, int] = {}
    for location_id, (gid, _label) in sorted(placed.items()):
        gid_to_location.setdefault(gid, location_id)

    def _parent_of_gid(gid: str, hops: int = 6) -> Tuple[Optional[int], Optional[str]]:
        current = _parent_gid(gid)
        for _ in range(hops):
            if not current:
                return None, None
            location_id = gid_to_location.get(current)
            if location_id is not None:
                return location_id, current
            current = _parent_gid(current)
        return None, None

    updates: List[Dict[str, Any]] = []
    admin_level_set = parents_set = 0

    for location_id, (gid, admin_level) in sorted(placed.items()):
        current = frame.loc[frame["location_id"].astype(int) == location_id].iloc[0]
        parent_id, _parent_code = _parent_of_gid(gid)
        entry: Dict[str, Any] = {"location_id": location_id,
                                 "admin_level": None,
                                 "standard_code": None,
                                 "parent_location_id": None}
        if admin_level and admin_level != _text(current.get("admin_level")):
            entry["admin_level"] = admin_level
            admin_level_set += 1
        if _text(current.get("standard_code")) != gid:
            entry["standard_code"] = gid
        if parent_id is not None and parent_id != existing_parent.get(location_id):
            entry["parent_location_id"] = parent_id
            parents_set += 1
        if any(entry[key] is not None for key in
               ("admin_level", "standard_code", "parent_location_id")):
            updates.append(entry)

    # ---- 3. Grid cells: parent from the containing polygon ---------------
    grid_parented = 0
    if spatial and not frame.empty:
        pending_grid = frame[frame["_is_grid"]]
        if not pending_grid.empty:
            grid_points = _points_frame(pending_grid)
            if not grid_points.empty:
                for location_id, (gid, _label) in sorted(
                        _containing_gid(grid_points, hierarchy, specs).items()):
                    if existing_parent.get(location_id) is not None:
                        continue
                    parent_id, _parent_code = _parent_of_gid(gid)
                    if parent_id is None or parent_id == location_id:
                        continue
                    updates.append({
                        "location_id": location_id, "admin_level": None,
                        "standard_code": None, "parent_location_id": parent_id})
                    grid_parented += 1

    written = _apply_updates(session, updates, dry_run)

    unmatched = len(frame) - len(placed) - int(frame["_is_grid"].sum())
    logger.info(
        "  parent resolution (pack %r): %d boundary GID(s) indexed, %d row(s) "
        "examined, %d placed by GID, %d placed by geometry, %d labelled, "
        "%d parented, %d grid cell(s) parented, %d unmatched, %d skipped by "
        "country, %d update(s)%s",
        pack.pack_id, len(boundary), len(frame), by_gid, by_geometry,
        admin_level_set, parents_set, grid_parented, max(0, unmatched),
        countries_skipped, written, " (dry run)" if dry_run else "")

    if by_gid == 0 and by_geometry == 0:
        logger.warning(
            "  nothing was placed. No location_code is a boundary GID and no "
            "administrative row carries usable centroid_lat/centroid_lon. Check: "
            "SELECT admin_level, COUNT(*), SUM(centroid_lat IS NULL) FROM "
            "dim_location GROUP BY admin_level;")

    return {"rows_examined": len(frame), "admin_level_set": admin_level_set,
            "parents_set": parents_set, "grid_parented": grid_parented,
            "unmatched": max(0, unmatched), "countries_skipped": countries_skipped,
            "boundary_indexed": len(boundary), "placed_by_gid": by_gid,
            "placed_by_geometry": by_geometry, "updates": written,
            "dry_run": bool(dry_run)}


# ---------------------------------------------------------------------------
# attach_admin_concepts
# ---------------------------------------------------------------------------

def attach_admin_concepts(session, pack_id: str = "", *, registry: Any = None,
                          dry_run: bool = False) -> Dict[str, Any]:
    """Stamp ``concept_id`` on ``dim_location`` from its boundary GID.

    Joins on ``standard_code`` first — the column ``resolve_parent_locations``
    writes — and falls back to ``location_code`` for a database whose codes are
    already GIDs. No name comparison, no fuzzy matching. The only writer of
    ``dim_location.concept_id``.
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
        "SELECT location_id, location_code, standard_code, concept_id "
        "FROM dim_location ORDER BY location_id")).fetchall()

    updates: List[Dict[str, Any]] = []
    unmatched = unchanged = 0

    for location_id, location_code, standard_code, concept_id in rows:
        code = _text(standard_code) or _text(location_code)
        new_concept_id = concept_id_by_code.get(code)
        if new_concept_id is None:
            unmatched += 1
            continue
        if concept_id == new_concept_id:
            unchanged += 1
            continue
        updates.append({"location_id": int(location_id),
                        "concept_id": new_concept_id})

    if updates and not dry_run:
        statement = text(
            "UPDATE dim_location SET concept_id = :concept_id "
            "WHERE location_id = :location_id")
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

    Determinism: the location query is ``ORDER BY location_id`` and the comparison
    is strict ``<``, so an exact distance tie is won by the lowest
    ``location_id``.
    """
    pack = _active_location_pack(pack_id, registry)

    grid_level = pack.grid_level
    if not grid_level:
        raise RuntimeError(
            f"pack {pack.pack_id!r} declares no `grid_level`, so grid facts cannot "
            f"be identified.")

    admin_levels: Tuple[str, ...] = tuple(pack.admin_level_map.values())
    if not admin_levels:
        raise RuntimeError(f"pack {pack.pack_id!r} declares no `admin_level_map`.")

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
        if lat != lat or lon != lon:
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