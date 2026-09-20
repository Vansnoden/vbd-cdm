"""
Build a location vocabulary from GADM administrative boundaries.

GADM 4.1 layout (verified against gadm41_KEN.gpkg):
  - Layers:    ADM_ADM_0, ADM_ADM_1, ADM_ADM_2, ADM_ADM_3
  - Level 0:   name lives in COUNTRY  (no NAME_0)
  - Levels 1+  name lives in NAME_<n>, variant in VARNAME_<n>
"""

import geopandas as gpd
import pandas as pd
from typing import Optional


# Column/class spec per administrative level.
GADM_LEVELS = {
    0: {
        "name_candidates": ["COUNTRY", "NAME_0"],
        "gid_column":      "GID_0",
        "concept_class":   "country",
        "varname_column":  None,
    },
    1: {
        "name_candidates": ["NAME_1"],
        "gid_column":      "GID_1",
        "concept_class":   "admin1",
        "varname_column":  "VARNAME_1",
    },
    2: {
        "name_candidates": ["NAME_2"],
        "gid_column":      "GID_2",
        "concept_class":   "admin2",
        "varname_column":  "VARNAME_2",
    },
    3: {
        "name_candidates": ["NAME_3"],
        "gid_column":      "GID_3",
        "concept_class":   "admin3",
        "varname_column":  "VARNAME_3",
    },
}


def _load_level(gpkg_path: str, level: int, country_code: Optional[str]) -> Optional[gpd.GeoDataFrame]:
    """Try the standard layer name, then the per-country fallback."""
    candidates = [f"ADM_ADM_{level}"]
    if country_code:
        candidates.append(f"gadm41_{country_code}_{level}")
    for layer in candidates:
        try:
            return gpd.read_file(gpkg_path, layer=layer)
        except Exception:
            continue
    return None


def _first_present(df: pd.DataFrame, columns):
    """Return the first column name in `columns` that exists in `df`."""
    for col in columns:
        if col in df.columns:
            return col
    return None


def build_location_vocabulary(
    gpkg_path: str,
    output_csv: str,
    country_codes: Optional[list] = None,
) -> pd.DataFrame:
    """
    Extract location concepts from a GADM GeoPackage.

    Produces one row per administrative unit with:
        concept_code, concept_name, concept_class, parent_code,
        centroid_lat, centroid_lon, synonyms, vocabulary_id, domain,
        name_normalized

    Parameters
    ----------
    gpkg_path : str
        Path to the GADM GeoPackage (e.g. gadm41_KEN.gpkg).
    output_csv : str
        Where to write the resulting vocabulary.
    country_codes : list[str] | None
        Optional GID_0 filter, e.g. ['KEN'].
    """
    rows = []
    cc0 = country_codes[0] if country_codes else None

    for level, spec in GADM_LEVELS.items():
        gdf = _load_level(gpkg_path, level, cc0)
        if gdf is None:
            print(f"[level {level}] no matching layer found; skipping")
            continue

        # Country filter (works because GID_0 is in every layer).
        if country_codes and "GID_0" in gdf.columns:
            gdf = gdf[gdf["GID_0"].isin(country_codes)]
            if gdf.empty:
                continue

        name_col = _first_present(gdf, spec["name_candidates"])
        if name_col is None:
            print(
                f"[level {level}] no name column in {list(gdf.columns)}; skipping"
            )
            continue

        gid_col = spec["gid_column"]
        if gid_col not in gdf.columns:
            print(f"[level {level}] missing {gid_col}; skipping")
            continue

        varname_col = spec["varname_column"]
        parent_col = f"GID_{level - 1}" if level > 0 else None

        for _, row in gdf.iterrows():
            try:
                centroid = row.geometry.centroid
                lat, lon = float(centroid.y), float(centroid.x)
            except Exception:
                lat, lon = None, None

            parent_code = row[parent_col] if parent_col and parent_col in gdf.columns else None
            synonyms = row[varname_col] if varname_col and varname_col in gdf.columns else ""

            rows.append({
                "concept_code":  row[gid_col],
                "concept_name":  row[name_col],
                "concept_class": spec["concept_class"],
                "parent_code":   parent_code,
                "centroid_lat":  lat,
                "centroid_lon":  lon,
                "synonyms":      synonyms if pd.notna(synonyms) else "",
            })

    df = pd.DataFrame(rows)
    df["vocabulary_id"] = "GADM"
    df["domain"] = "location"
    df["name_normalized"] = df["concept_name"].astype(str).str.lower().str.strip()

    df.to_csv(output_csv, index=False)
    print(f"Wrote {len(df)} location concepts to {output_csv}")
    return df