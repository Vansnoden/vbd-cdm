# utilities/admin_alignment.py
#
# Coordinate-driven GADM alignment.
#
# Every location — admin unit, site, facility, or grid cell — is placed
# into the GADM hierarchy by point-in-polygon lookup against its
# centroid. Admin units (level 0 through 3) become first-class rows in
# dim_location with a parent chain. Non-admin rows get a parent_location_id
# pointing at the finest GADM unit that contains them.
#
# No code in this module contains the name of any country, region, or
# admin unit. All inputs come from the caller, which reads them from
# base_config.yaml.

import logging
from typing import List, Optional

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GADM layer specification.
#
# GADM 4.x ships each administrative level as a separate vector layer
# inside one .gpkg file. The layer names are stable across releases. The
# name column varies by level: level 0 uses COUNTRY (there is no NAME_0);
# levels 1+ use NAME_<n>. The GID column is stable: GID_<n>.
#
# The parent GID column lets us link level N to level N-1 without
# recomputing spatial containment — GADM already encodes that hierarchy
# in the data.
# ---------------------------------------------------------------------------

_GADM_LAYERS = [
    {
        "level":          0,
        "layer":          "ADM_ADM_0",
        "name_columns":   ["COUNTRY", "NAME_0"],
        "gid_column":     "GID_0",
        "parent_gid":     None,
        "admin_level":    "admin0",
    },
    {
        "level":          1,
        "layer":          "ADM_ADM_1",
        "name_columns":   ["NAME_1"],
        "gid_column":     "GID_1",
        "parent_gid":     "GID_0",
        "admin_level":    "admin1",
    },
    {
        "level":          2,
        "layer":          "ADM_ADM_2",
        "name_columns":   ["NAME_2"],
        "gid_column":     "GID_2",
        "parent_gid":     "GID_1",
        "admin_level":    "admin2",
    },
    {
        "level":          3,
        "layer":          "ADM_ADM_3",
        "name_columns":   ["NAME_3"],
        "gid_column":     "GID_3",
        "parent_gid":     "GID_2",
        "admin_level":    "admin3",
    },
]


# ---------------------------------------------------------------------------
# Step 1 — load the admin hierarchy into dim_location
# ---------------------------------------------------------------------------

def load_admin_hierarchy(
    session: Session,
    gadm_gpkg: str,
    country_codes: Optional[List[str]] = None,
) -> dict:
    """
    Read the GADM levels 0-3 from `gadm_gpkg`, upsert one row per
    administrative unit into dim_location, and set parent_location_id
    so the hierarchy is a chain.

    Parameters
    ----------
    session : Session
        Active SQLAlchemy session.
    gadm_gpkg : str
        Path to the GADM GeoPackage.
    country_codes : list[str], optional
        GADM GID_0 codes to keep (e.g. ['KEN']). If None, every country
        in the file is loaded.

    Returns
    -------
    dict
        {'levels_loaded': {level: row_count}, 'total_rows': int}
    """
    loaded: dict = {}
    total = 0

    for spec in _GADM_LAYERS:
        try:
            gdf = gpd.read_file(gadm_gpkg, layer=spec["layer"])
        except Exception as e:
            logger.info(
                f"  GADM layer {spec['layer']} not present in {gadm_gpkg} "
                f"({e}); skipping."
            )
            continue

        if country_codes and "GID_0" in gdf.columns:
            gdf = gdf[gdf["GID_0"].isin(country_codes)]

        if gdf.empty:
            logger.info(
                f"  GADM {spec['admin_level']}: no rows matched "
                f"countries={country_codes}"
            )
            continue

        name_col = next(
            (c for c in spec["name_columns"] if c in gdf.columns), None
        )
        if name_col is None:
            logger.warning(
                f"  GADM {spec['admin_level']}: no name column "
                f"among {spec['name_columns']}; columns are "
                f"{list(gdf.columns)}"
            )
            continue

        # Build a {GID: location_id} map from the parent level already loaded.
        # Admin0 has no parent.
        parent_map = {}
        if spec["parent_gid"]:
            rows = session.execute(text(
                "SELECT location_code, location_id FROM dim_location "
                "WHERE origin = 'gadm' AND location_code IS NOT NULL"
            )).fetchall()
            parent_map = dict(rows)

        rows_written = _upsert_admin_rows(
            session, gdf, spec, name_col, parent_map
        )
        loaded[spec["admin_level"]] = rows_written
        total += rows_written
        session.commit()

        logger.info(
            f"  GADM {spec['admin_level']}: upserted {rows_written} rows"
        )

    return {"levels_loaded": loaded, "total_rows": total}


def _upsert_admin_rows(
    session: Session,
    gdf: gpd.GeoDataFrame,
    spec: dict,
    name_col: str,
    parent_map: dict,
) -> int:
    """
    Upsert one row per polygon. Uses `location_code = GID_<n>` as the
    natural key so the operation is idempotent across runs.
    """
    gid_col = spec["gid_column"]
    parent_gid_col = spec["parent_gid"]
    admin_level = spec["admin_level"]

    written = 0
    for _, row in gdf.iterrows():
        gid = row.get(gid_col)
        if gid is None:
            continue

        name = row.get(name_col)
        centroid = row.geometry.centroid
        lat, lon = float(centroid.y), float(centroid.x)

        # Country code/name from the GID_0 column on any level.
        country_code = row.get("GID_0")
        country_name = None
        if country_code:
            # GID_0 is the ISO-3 country code (KEN, TZA, ...). Read the
            # name from the admin0 record if we can, else use the code.
            admin0_row = session.execute(text(
                "SELECT name FROM dim_location "
                "WHERE location_code = :gid AND admin_level = 'admin0'"
            ), {"gid": country_code}).first()
            if admin0_row:
                country_name = admin0_row[0]

        parent_id = None
        if parent_gid_col:
            parent_gid = row.get(parent_gid_col)
            parent_id = parent_map.get(parent_gid)

        session.execute(text("""
            INSERT INTO dim_location
                (location_code, name, country_code, country_name,
                 admin_level, centroid_lat, centroid_lon,
                 parent_location_id, standard_code, origin)
            VALUES
                (:code, :name, :cc, :cn,
                 :al, :lat, :lon,
                 :parent, :std, 'gadm')
            ON CONFLICT (location_code) DO UPDATE SET
                name               = EXCLUDED.name,
                country_code       = EXCLUDED.country_code,
                country_name       = EXCLUDED.country_name,
                admin_level        = EXCLUDED.admin_level,
                centroid_lat       = EXCLUDED.centroid_lat,
                centroid_lon       = EXCLUDED.centroid_lon,
                parent_location_id = EXCLUDED.parent_location_id,
                standard_code      = EXCLUDED.standard_code,
                origin             = 'gadm'
        """), {
            "code":   gid,
            "name":   name,
            "cc":     country_code,
            "cn":     country_name,
            "al":     admin_level,
            "lat":    lat,
            "lon":    lon,
            "parent": parent_id,
            "std":    gid,
        })
        written += 1

    return written


# ---------------------------------------------------------------------------
# Step 2 — place every non-admin location into the hierarchy
# ---------------------------------------------------------------------------

def resolve_parent_locations(
    session: Session,
    gadm_gpkg: str,
    country_codes: Optional[List[str]] = None,
    finest_level: int = 3,
) -> dict:
    """
    For every non-GADM location that has coordinates and no parent, find
    the finest GADM polygon that contains it and set parent_location_id.

    Walks the hierarchy from finest to coarsest (admin3 -> admin2 -> admin1
    -> admin0). A point that falls outside every admin3 polygon is tried
    against admin2, then admin1, then admin0. This is the fallback the
    previous single-level version lacked: on the Kenya case study, 509 of
    762 grid cells were left unparented because their centroid landed in
    an admin3 gap (protected area, water body, urban boundary) that a
    coarser level would have covered.

    Parameters
    ----------
    session : Session
    gadm_gpkg : str
    country_codes : list[str], optional
        GID_0 filter for the GADM polygons.
    finest_level : int
        Start at this level and walk down (default 3).

    Returns
    -------
    dict
        {'assigned': int, 'unplaced': int, 'levels_used': {level: n}}
    """
    # ---- Load every GADM level once, finest to coarsest. ----
    levels: list = []
    for level in range(finest_level, -1, -1):
        spec = next(s for s in _GADM_LAYERS if s["level"] == level)
        try:
            gdf = gpd.read_file(gadm_gpkg, layer=spec["layer"])
        except Exception as e:
            logger.info(
                f"  GADM layer {spec['layer']} not available ({e}); skipping."
            )
            continue
        if country_codes and "GID_0" in gdf.columns:
            gdf = gdf[gdf["GID_0"].isin(country_codes)]
        if gdf.empty:
            continue

        gid_col = spec["gid_column"]
        if gid_col not in gdf.columns:
            continue

        admin_map = dict(session.execute(text(
            "SELECT location_code, location_id FROM dim_location "
            "WHERE admin_level = :al AND location_code IS NOT NULL"
        ), {"al": spec["admin_level"]}).fetchall())

        gdf = gdf.to_crs("EPSG:4326")
        levels.append((spec, gdf, admin_map))

    if not levels:
        logger.warning("  No GADM polygon layer available; skipping.")
        return {"assigned": 0, "unplaced": 0, "levels_used": {}}

    # ---- Load every non-GADM location with coordinates and no parent. ----
    rows = session.execute(text("""
        SELECT location_id, centroid_lat, centroid_lon
        FROM dim_location
        WHERE parent_location_id IS NULL
          AND (origin IS NULL OR origin <> 'gadm')
          AND centroid_lat IS NOT NULL
          AND centroid_lon IS NOT NULL
    """)).fetchall()

    if not rows:
        logger.info("  No unplaced locations to resolve.")
        return {"assigned": 0, "unplaced": 0, "levels_used": {}}

    pending = gpd.GeoDataFrame(
        {"location_id": [r[0] for r in rows]},
        geometry=[Point(r[2], r[1]) for r in rows],
        crs="EPSG:4326",
    )

    # ---- Cascade through levels, finest first. ----
    levels_used: dict = {}
    updates: list = []

    for spec, gdf, admin_map in levels:
        if pending.empty:
            break

        gid_col = spec["gid_column"]
        joined = gpd.sjoin(
            pending[["location_id", "geometry"]],
            gdf[[gid_col, "geometry"]],
            how="left",
            predicate="within",
        )

        # Deduplicate by location_id: a point can be within more than one
        # polygon at the same level if the polygons overlap slightly.
        placed: dict = {}
        for _, jrow in joined.iterrows():
            parent_gid = jrow.get(gid_col)
            if pd.isna(parent_gid):
                continue
            parent_id = admin_map.get(parent_gid)
            if parent_id is None:
                continue
            loc_id = int(jrow["location_id"])
            if loc_id in placed:
                continue
            placed[loc_id] = int(parent_id)

        for loc_id, parent_id in placed.items():
            updates.append({"loc_id": loc_id, "parent": parent_id})

        levels_used[spec["admin_level"]] = len(placed)

        if placed:
            pending = pending[~pending["location_id"].isin(list(placed.keys()))].copy()

    # ---- Write. ----
    if updates:
        for chunk in _chunked(updates, 500):
            session.execute(
                text("""
                    UPDATE dim_location SET parent_location_id = :parent
                    WHERE location_id = :loc_id
                """),
                chunk,
            )
        session.commit()

    assigned = len(updates)
    unplaced = int(len(pending))

    logger.info(
        f"  Placed {assigned} locations across "
        f"{len(levels_used)} admin level(s); {unplaced} unplaced"
    )
    for lvl in ("admin3", "admin2", "admin1", "admin0"):
        n = levels_used.get(lvl, 0)
        if n:
            logger.info(f"    {lvl}: {n}")

    return {
        "assigned":    assigned,
        "unplaced":    unplaced,
        "levels_used": levels_used,
    }

# ---------------------------------------------------------------------------
# Step 3 — attach GADM concepts to locations that resolved
# ---------------------------------------------------------------------------

def attach_admin_concepts(session: Session) -> int:
    """
    For every admin-origin location, set concept_id to the matching
    GADM concept, if the vocabulary loader has populated dim_concept
    with that GID.

    Returns the number of rows updated.
    """
    # Map GID -> concept_id from dim_concept where vocabulary = GADM.
    gid_to_concept = dict(session.execute(text("""
        SELECT concept_code, concept_id
        FROM dim_concept
        WHERE vocabulary_id = 'GADM'
    """)).fetchall())

    if not gid_to_concept:
        logger.info("  No GADM concepts loaded; skipping concept attach.")
        return 0

    updated = 0
    for gid, cid in gid_to_concept.items():
        result = session.execute(text("""
            UPDATE dim_location
            SET concept_id = :cid
            WHERE location_code = :gid
              AND (concept_id IS NULL OR concept_id <> :cid)
        """), {"cid": cid, "gid": gid})
        updated += result.rowcount or 0

    session.commit()
    logger.info(f"  Attached GADM concepts to {updated} locations")
    return updated


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _chunked(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]