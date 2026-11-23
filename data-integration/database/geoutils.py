# -*- coding: utf-8 -*-
"""Country resolution from coordinates: one polygon lookup, no heuristics."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from math import asin, cos, isfinite, radians, sin, sqrt
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import reverse_geocode as rg

logger = logging.getLogger(__name__)

BOUNDARY_ENV = "VBD_ADMIN_BOUNDARIES"

ISO_COUNTRY_CODES = {
    'KE': 'KEN', 'GH': 'GHA', 'NG': 'NGA', 'ZA': 'ZAF', 'TZ': 'TZA',
    'UG': 'UGA', 'ET': 'ETH', 'DZ': 'DZA', 'AO': 'AGO', 'BJ': 'BEN',
    'BW': 'BWA', 'BF': 'BFA', 'BI': 'BDI', 'CV': 'CPV', 'CM': 'CMR',
    'CF': 'CAF', 'TD': 'TCD', 'KM': 'COM', 'CG': 'COG', 'CI': 'CIV',
    'DJ': 'DJI', 'EG': 'EGY', 'GQ': 'GNQ', 'ER': 'ERI', 'SZ': 'SWZ',
    'GA': 'GAB', 'GM': 'GMB', 'GN': 'GIN', 'GW': 'GNB', 'LS': 'LSO',
    'LR': 'LBR', 'LY': 'LBY', 'MG': 'MDG', 'MW': 'MWI', 'ML': 'MLI',
    'MR': 'MRT', 'MU': 'MUS', 'MA': 'MAR', 'MZ': 'MOZ', 'NA': 'NAM',
    'NE': 'NER', 'RW': 'RWA', 'ST': 'STP', 'SN': 'SEN', 'SC': 'SYC',
    'SL': 'SLE', 'SO': 'SOM', 'SS': 'SSD', 'SD': 'SDN', 'TG': 'TGO',
    'TN': 'TUN', 'ZM': 'ZMB', 'ZW': 'ZWE'
}

COUNTRY_NAME_TO_ISO = {
    'Kenya': 'KE', 'Ghana': 'GH', 'Nigeria': 'NG', 'South Africa': 'ZA',
    'Tanzania': 'TZ', 'Uganda': 'UG', 'Ethiopia': 'ET', 'Algeria': 'DZ',
    'Angola': 'AO', 'Benin': 'BJ', 'Botswana': 'BW', 'Burkina Faso': 'BF',
    'Burundi': 'BI', 'Cabo Verde': 'CV', 'Cameroon': 'CM',
    'Central African Republic': 'CF', 'Chad': 'TD', 'Comoros': 'KM',
    'Congo': 'CG', "Côte d'Ivoire": 'CI', 'Djibouti': 'DJ', 'Egypt': 'EG',
    'Equatorial Guinea': 'GQ', 'Eritrea': 'ER', 'Eswatini': 'SZ',
    'Gabon': 'GA', 'Gambia': 'GM', 'Guinea': 'GN', 'Guinea-Bissau': 'GW',
    'Lesotho': 'LS', 'Liberia': 'LR', 'Libya': 'LY', 'Madagascar': 'MG',
    'Malawi': 'MW', 'Mali': 'ML', 'Mauritania': 'MR', 'Mauritius': 'MU',
    'Morocco': 'MA', 'Mozambique': 'MZ', 'Namibia': 'NA', 'Niger': 'NE',
    'Rwanda': 'RW', 'São Tomé and Príncipe': 'ST', 'Senegal': 'SN',
    'Seychelles': 'SC', 'Sierra Leone': 'SL', 'Somalia': 'SO',
    'South Sudan': 'SS', 'Sudan': 'SD', 'Togo': 'TG', 'Tunisia': 'TN',
    'Zambia': 'ZM', 'Zimbabwe': 'ZW'
}

COUNTRY_ALIASES: Dict[str, str] = {
    "IVORY COAST": "CI",
    "COTE D IVOIRE": "CI",
    "CÔTE D'IVOIRE": "CI",
    "SWAZILAND": "SZ",
    "CAPE VERDE": "CV",
    "CABO VERDE": "CV",
    "UNITED REPUBLIC OF TANZANIA": "TZ",
    "TANZANIA UNITED REPUBLIC OF": "TZ",
    "THE GAMBIA": "GM",
    "GAMBIA THE": "GM",
    "SAO TOME AND PRINCIPE": "ST",
    "SÃO TOMÉ AND PRÍNCIPE": "ST",
    "EGYPT ARAB REPUBLIC OF": "EG",
    "LIBYA STATE OF": "LY",
    "CONGO REPUBLIC OF": "CG",
    "CONGO BRAZZAVILLE": "CG",
}

_ISO3_TO_ISO2: Dict[str, str] = {iso3: iso2 for iso2, iso3 in ISO_COUNTRY_CODES.items()}

_BOUNDARY_PATH: Optional[Path] = None
_BOUNDARY_WARNED: set = set()
_POLYGON_INDEX_CACHE: Dict[str, Optional["_CountryPolygonIndex"]] = {}
_POLYGON_LOOKUP_CACHE: Dict[Tuple[float, float, str], Optional[Tuple[str, str]]] = {}
_POLYGON_LOOKUP_LIMIT = 50_000

_ISO3_RE = re.compile(r"^[A-Z]{3}$")


def _fold_country(value: Any) -> str:
    """Fold a country name or code to its comparison form."""
    if value is None:
        return ""
    text_value = str(value).upper()
    text_value = text_value.replace(",", " ").replace(".", " ")
    return re.sub(r"\s+", " ", text_value).strip()


_FOLDED_COUNTRY_NAMES: Dict[str, str] = {
    _fold_country(name): code for name, code in COUNTRY_NAME_TO_ISO.items()
}

ISO_TO_COUNTRY_NAME = {v: k for k, v in COUNTRY_NAME_TO_ISO.items()}


def _warn_once(marker: str, message: str, *args: Any) -> None:
    """Emit a warning the first time ``marker`` is seen."""
    if marker in _BOUNDARY_WARNED:
        return
    _BOUNDARY_WARNED.add(marker)
    logger.warning(message, *args)


def set_boundary_path(path: Any) -> Optional[Path]:
    """Set the GADM file country resolution reads, and drop cached lookups."""
    global _BOUNDARY_PATH
    _BOUNDARY_PATH = None if path is None else Path(str(path))
    get_country_from_coords.cache_clear()
    _POLYGON_LOOKUP_CACHE.clear()
    if _BOUNDARY_PATH is not None:
        logger.info("country resolution boundary: %s", _BOUNDARY_PATH)
    return _BOUNDARY_PATH


def boundary_path() -> Optional[Path]:
    """The configured GADM file, from a setter call or ``VBD_ADMIN_BOUNDARIES``."""
    if _BOUNDARY_PATH is not None:
        return _BOUNDARY_PATH
    env_value = os.getenv(BOUNDARY_ENV)
    return Path(env_value) if env_value else None


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    radius = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return radius * 2 * asin(sqrt(a))


@dataclass(frozen=True)
class _CountryPolygon:
    """One level-0 boundary unit: its ISO2 code, its label and its geometry."""

    iso2: str
    name: str
    geometry: Any


class _CountryPolygonIndex:
    """Point-in-polygon over country boundaries; STRtree when available."""

    def __init__(self, polygons: list) -> None:
        self.polygons = [p for p in polygons if p.geometry is not None]
        self._tree = None
        self._positions: Dict[int, int] = {}
        if not self.polygons:
            return
        try:
            from shapely.strtree import STRtree
            geometries = [p.geometry for p in self.polygons]
            self._tree = STRtree(geometries)
            self._positions = {id(g): i for i, g in enumerate(geometries)}
        except Exception as exc:  # noqa: BLE001
            logger.debug("no STRtree for country polygons (%s); scanning boxes",
                         type(exc).__name__)

    def _candidates(self, point) -> list:
        """Polygons whose bounding box could hold ``point``."""
        if self._tree is not None:
            try:
                found = self._tree.query(point)
            except Exception:  # noqa: BLE001
                return list(range(len(self.polygons)))
            positions = []
            for item in found:
                if isinstance(item, int):
                    positions.append(item)
                    continue
                position = self._positions.get(id(item))
                if position is not None:
                    positions.append(position)
            return positions
        return [
            index for index, polygon in enumerate(self.polygons)
            if polygon.geometry.bounds[0] <= point.x <= polygon.geometry.bounds[2]
            and polygon.geometry.bounds[1] <= point.y <= polygon.geometry.bounds[3]
        ]

    def at(self, lat: float, lon: float) -> Optional[_CountryPolygon]:
        """The country holding the point, or None. Touching a border counts."""
        if not self.polygons:
            return None
        try:
            from shapely.geometry import Point
            point = Point(float(lon), float(lat))
        except Exception:  # noqa: BLE001
            return None
        if not (isfinite(point.x) and isfinite(point.y)):
            return None
        for position in self._candidates(point):
            if position >= len(self.polygons):
                continue
            polygon = self.polygons[position]
            try:
                if polygon.geometry.contains(point) or polygon.geometry.touches(point):
                    return polygon
            except Exception:  # noqa: BLE001
                continue
        return None


def _polygon_index(path: Path) -> Optional[_CountryPolygonIndex]:
    """The cached level-0 polygon index for ``path``, or None when unreadable."""
    key = str(path)
    if key in _POLYGON_INDEX_CACHE:
        return _POLYGON_INDEX_CACHE[key]

    index: Optional[_CountryPolygonIndex] = None
    if not path.exists():
        _warn_once(f"beoutils:missing:{key}",
                   "no boundary file at %s, so country resolution returns nothing "
                   "for a coordinate the reverse geocoder does not know. Set "
                   "core.admin_boundaries.path or %s.", path, BOUNDARY_ENV)
        _POLYGON_INDEX_CACHE[key] = None
        return None

    try:
        from utilities.admin_alignment import (_active_location_pack, _layer_specs,
                                               _gid_country, load_admin_hierarchy)
        pack = _active_location_pack(None)
        specs = _layer_specs(pack, levels=[0])
        hierarchy = load_admin_hierarchy(path, pack_id=pack.pack_id, levels=[0],
                                         registry=None)
    except Exception as exc:  # noqa: BLE001
        _warn_once(f"geoutils:load:{key}",
                   "cannot read level-0 boundaries from %s (%s: %s); country "
                   "resolution falls back to the reverse geocoder alone.", path,
                   type(exc).__name__, exc)
        _POLYGON_INDEX_CACHE[key] = None
        return None

    frame = hierarchy.get(0)
    if frame is None or frame.empty:
        _warn_once(f"geoutils:empty:{key}",
                   "%s has no level-0 layer with rows; country resolution falls "
                   "back to the reverse geocoder alone.", path)
        _POLYGON_INDEX_CACHE[key] = None
        return None

    spec = specs[0]
    gid_column = spec["gid_column"]
    geometry_column = getattr(frame.geometry, "name", "geometry")
    name_columns = list(spec["name_columns"]) + ["COUNTRY", "NAME_0", "NAME"]

    polygons = []
    for record in frame.to_dict("records"):
        iso2 = _ISO3_TO_ISO2.get(_gid_country(record.get(gid_column)))
        if not iso2:
            continue
        geometry = record.get(geometry_column)
        if geometry is None:
            continue
        label = next((str(record[c]).strip() for c in name_columns
                      if c in record and str(record.get(c) or "").strip()),
                     ISO_TO_COUNTRY_NAME.get(iso2, iso2))
        polygons.append(_CountryPolygon(iso2=iso2, name=label, geometry=geometry))

    if not polygons:
        _warn_once(f"geoutils:noiso:{key}",
                   "%s yielded no level-0 polygon with a recognised ISO-3 code; "
                   "country resolution falls back to the reverse geocoder alone.",
                   path)
        _POLYGON_INDEX_CACHE[key] = None
        return None

    index = _CountryPolygonIndex(polygons)
    logger.info("country resolution: %d level-0 polygon(s) indexed from %s",
                len(index.polygons), path)
    _POLYGON_INDEX_CACHE[key] = index
    return index


def _country_from_polygon(lat: float, lon: float) -> Optional[Tuple[str, str]]:
    """``(name, iso2)`` from the configured boundaries, or None."""
    path = boundary_path()
    if path is None:
        _warn_once("geoutils:nopath",
                   "no boundary file configured (core.admin_boundaries.path or "
                   "%s), so a coordinate outside the reverse geocoder's knowledge "
                   "resolves to no country rather than to a guessed one.",
                   BOUNDARY_ENV)
        return None

    cache_key = (round(float(lat), 6), round(float(lon), 6), str(path))
    if cache_key in _POLYGON_LOOKUP_CACHE:
        return _POLYGON_LOOKUP_CACHE[cache_key]

    index = _polygon_index(path)
    hit = None if index is None else index.at(lat, lon)
    result = None if hit is None else (hit.name, hit.iso2)

    if len(_POLYGON_LOOKUP_CACHE) >= _POLYGON_LOOKUP_LIMIT:
        _POLYGON_LOOKUP_CACHE.clear()
    _POLYGON_LOOKUP_CACHE[cache_key] = result
    return result


@lru_cache(maxsize=10000)
def get_country_from_coords(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """Country name and ISO2 for a coordinate, or ``(None, None)``.

    Point-in-polygon against the configured level-0 boundaries first; the reverse
    geocoder only when no boundary index is loadable.
    """
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
        return None, None

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        logger.warning("Invalid coordinates: (%s, %s)", lat, lon)
        return None, None

    from_polygon = _country_from_polygon(lat, lon)
    if from_polygon is not None:
        return from_polygon

    if boundary_path() is None or _polygon_index(boundary_path()) is None:
        try:
            result = rg.search([(lat, lon)])
            if result:
                country_code = str(result[0].get("country_code") or "").upper()
                if len(country_code) == 3:
                    country_code = _ISO3_TO_ISO2.get(country_code, "")
                if len(country_code) == 2 and country_code in COUNTRY_NAME_TO_ISO.values():
                    return ISO_TO_COUNTRY_NAME.get(country_code), country_code
        except Exception as exc:  # noqa: BLE001
            logger.debug("Reverse geocode failed for (%s, %s): %s", lat, lon, exc)

    logger.warning("No country found for coordinates (%s, %s)", lat, lon)
    return None, None


def get_iso_country_code(country: str) -> Optional[str]:
    """ISO 3166-1 alpha-2 code for a name, alias, or alpha-2/alpha-3 code.

    ``'Kenya'``, ``'KEN'``, ``'ke'`` and ``'Kenya (Siaya)'`` all give ``'KE'``.
    """
    if country is None:
        return None

    folded = _fold_country(country)
    if not folded:
        return None

    if folded in ISO_COUNTRY_CODES:
        return folded

    iso2 = _ISO3_TO_ISO2.get(folded)
    if iso2:
        return iso2

    alias = COUNTRY_ALIASES.get(folded)
    if alias:
        return alias

    exact = _FOLDED_COUNTRY_NAMES.get(folded)
    if exact:
        return exact

    best: Optional[str] = None
    for folded_name in sorted(_FOLDED_COUNTRY_NAMES):
        if not folded_name or folded_name not in folded:
            continue
        if best is None or len(folded_name) > len(best):
            best = folded_name
    if best is None:
        return None
    return _FOLDED_COUNTRY_NAMES[best]


def get_country_name(code: str) -> Optional[str]:
    """Country name for an ISO alpha-2 code, or None."""
    if not code:
        return None
    return ISO_TO_COUNTRY_NAME.get(str(code).upper())


__all__ = [
    "BOUNDARY_ENV",
    "COUNTRY_ALIASES",
    "COUNTRY_NAME_TO_ISO",
    "ISO_COUNTRY_CODES",
    "ISO_TO_COUNTRY_NAME",
    "boundary_path",
    "get_country_from_coords",
    "get_country_name",
    "get_iso_country_code",
    "haversine_distance",
    "set_boundary_path",
]