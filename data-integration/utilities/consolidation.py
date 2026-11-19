# -*- coding: utf-8 -*-
"""Post-ingestion consolidation phases.

Order: FK alignment -> A6 locations -> A8 vocabulary reconcile -> A9 spatial
concepts -> B5 merge -> facts cascade -> audit -> burden expand -> resistance.

Nothing here redefines a geo or country helper: ``database.geoutils`` owns them.
An earlier revision shadowed ``get_iso_country_code`` below the imports and kept
the first-match substring bug that resolves 'guinea' to Equatorial Guinea.
"""

from __future__ import annotations

import calendar
import logging
import re
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import Integer, bindparam, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.constants import COUNTRY_LOOKUP
from database.geoutils import (
    ISO_COUNTRY_CODES,
    ISO_TO_COUNTRY_NAME,
    get_country_from_coords,
    get_country_name,
    get_iso_country_code,
    haversine_distance,
)
from database.models import (
    DimDisease, DimIdentificationMethod, DimInsecticide, DimLocation,
    DimResistanceTestType, DimSocioeconomicIndicator, DimSource, DimVector,
    FactBionomics, FactDiseaseBurden, FactDiseaseCase, FactEnvironment,
    FactHealthFacility, FactInsecticideResistance, FactIntervention,
    FactSocioeconomicCondition, FactVectorObservation,
)
from database.session import Base, SessionLocal, require_identifier
from etl.transformers.concept_resolver import (
    AUTO_ACCEPTED, METHOD_ALIAS, METHOD_AMBIGUOUS, METHOD_EXACT, METHOD_FUZZY,
    METHOD_NORMALIZED, NEEDS_REVIEW, RESISTANCE_BANDS, UNRESOLVED,
    ConceptResolver, Resolution,
)
from utilities.vocabulary import normalizers as _normalizers
from utilities.vocabulary.field_catalog import DOMAIN_TO_DIM_TABLE, resolve_field
from utilities.vocabulary.registry import VocabularyRegistry

logger = logging.getLogger(__name__)

MAX_IN_VARS = 900
_GRID_NAME_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?\s*$")

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "vocabularies" / "registry"

_REGISTRY_CACHE: dict = {}
_WARNED: set = set()
_LEGACY_ALIGNMENT_WARNED: set = set()


def _warn_once(marker: str, message: str, *args: Any) -> None:
    if marker in _WARNED:
        return
    _WARNED.add(marker)
    logger.warning(message, *args)


def _warn_legacy_alignment() -> None:
    """Emitted once per process. ``_align_fact_foreign_keys`` repairs an orphan
    with a LIKE substring match, records no score and writes no concept-map row,
    so its repairs are unreviewable. Scheduled for removal."""
    if _LEGACY_ALIGNMENT_WARNED:
        return
    _LEGACY_ALIGNMENT_WARNED.add("align_fact_foreign_keys")
    logger.warning(
        "_align_fact_foreign_keys is LEGACY (review C12): substring match, no "
        "score, no fact_concept_map row, no reviewable trace. Do not build on it."
    )


def _load_registry(registry: Any = None) -> Optional[VocabularyRegistry]:
    """Return ``registry``, or load the shipped packs once. A missing directory
    is a warning and None; a malformed manifest raises."""
    if registry is not None:
        return registry
    key = str(DEFAULT_REGISTRY_PATH)
    cached = _REGISTRY_CACHE.get(key)
    if cached is not None:
        return cached
    if not DEFAULT_REGISTRY_PATH.is_dir():
        _warn_once(
            "registry:missing",
            "no vocabulary registry at %s and none supplied; vocabulary "
            "standardisation is skipped for this run.", DEFAULT_REGISTRY_PATH,
        )
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


def _pack_from_policy(registry: Optional[VocabularyRegistry], policy: Any):
    if registry is None or policy is None:
        return None
    try:
        return registry.get(policy.primary)
    except Exception as exc:  # noqa: BLE001
        _warn_once(
            f"pack:{getattr(policy, 'primary', '?')}",
            "merge policy for %r names pack %r, which the registry does not "
            "hold (%s). Consolidating without a vocabulary.",
            getattr(policy, "target_field", "?"),
            getattr(policy, "primary", "?"), exc,
        )
        return None


def _vocabulary_size(session, vocabulary_id: Optional[str]) -> int:
    if not vocabulary_id:
        return 0
    try:
        return int(session.execute(
            text("SELECT COUNT(*) FROM dim_concept WHERE vocabulary_id = :v"),
            {"v": vocabulary_id},
        ).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not count dim_concept for %s: %s", vocabulary_id, exc)
        return 0


def _default_vocab_key(value: Any) -> str:
    if not isinstance(value, str):
        return "" if value is None else str(value).strip().lower()
    return re.sub(r"\s+", " ", value.strip()).lower()


def _pack_key_fn(pack: Any):
    """``str -> str`` grouping key for a pack, degrading to trim/lower."""
    chain = tuple(getattr(pack, "normalizer_chain", ()) or ())
    label = getattr(pack, "pack_id", "?")
    try:
        composed = _normalizers.compose(chain)
    except Exception as exc:  # noqa: BLE001
        _warn_once(f"compose:{label}",
                   "pack %r: normalizer_chain %s could not be composed (%s: %s). "
                   "Grouping falls back to trim/lower.",
                   label, list(chain), type(exc).__name__, exc)
        return _default_vocab_key

    def _guarded(value: Any) -> str:
        try:
            return composed(value)
        except Exception as exc:  # noqa: BLE001
            _warn_once(f"normalise:{label}:{type(exc).__name__}",
                       "pack %r: the normaliser raised %s: %s. Grouping falls "
                       "back to trim/lower.", label, type(exc).__name__, exc)
            return _default_vocab_key(value)

    return _guarded


# ---------------------------------------------------------------------------
# Canonical rules
# ---------------------------------------------------------------------------

def _rule_tokens(pack: Any) -> tuple:
    raw = getattr(pack, "canonical_rule", None)
    if not raw:
        return ()
    return tuple(t for t in re.split(r"[,\s]+", str(raw).strip()) if t)


def _rule_require_spatial_or_human(pack: Any, resolution: "Resolution") -> bool:
    """Location names never auto-apply from a string match."""
    return False


def _rule_never_collapse_hybrids(pack: Any, resolution: "Resolution") -> bool:
    """A match that only survives the fuzzy rung stays in review."""
    return resolution.match_method != METHOD_FUZZY


_CANONICAL_MATCH_RULES = {
    "require_spatial_or_human": _rule_require_spatial_or_human,
    "never_collapse_hybrids": _rule_never_collapse_hybrids,
}


def _bonus_genus_prefix(name: str) -> int:
    return 10_000 if str(name or "").strip().lower().startswith("anopheles") else 0


_CANONICAL_FORM_BONUSES = {"prefer_genus_prefix": _bonus_genus_prefix}


def _may_apply_concept(pack: Any, resolution: "Resolution") -> bool:
    """Unknown rule ids block application and say so once."""
    for token in _rule_tokens(pack):
        predicate = _CANONICAL_MATCH_RULES.get(token)
        if predicate is not None:
            if not predicate(pack, resolution):
                return False
            continue
        if token in _CANONICAL_FORM_BONUSES:
            continue
        _warn_once(f"rule:{token}",
                   "pack %r declares canonical_rule %r, which this build does "
                   "not implement. No concept governed by it is applied "
                   "automatically.", getattr(pack, "pack_id", "?"), token)
        return False
    return True


def _effective_decision(pack: Any, resolution: "Resolution") -> str:
    """The resolver's verdict, narrowed (never widened) by the pack."""
    decision = resolution.decision
    if decision == AUTO_ACCEPTED and not _may_apply_concept(pack, resolution):
        return NEEDS_REVIEW
    return decision


# ---------------------------------------------------------------------------
# Resistance policy
# ---------------------------------------------------------------------------

RESISTANCE_THRESHOLDS_KEY = "resistance_thresholds"
_SUSCEPTIBLE_KEY = "susceptible"
_SUSPECTED_KEY = "suspected"


def _resistance_thresholds(registry=None) -> Tuple[float, float]:
    """``(susceptible, suspected)`` percentages, from the parameter pack.

    Never defaulted: a run that classified under undeclared cut-offs would be
    indistinguishable from one that used the real ones.
    """
    from utilities.vocabulary.registry import RegistryError

    loaded = _load_registry(registry)
    if loaded is None:
        raise RuntimeError(
            "cannot classify resistance: no registry supplied and none loadable. "
            "Cut-offs live in vocabularies/registry/who-resistance/manifest.yaml."
        )
    try:
        pack = loaded.require_parameter_pack(RESISTANCE_THRESHOLDS_KEY)
    except RegistryError as exc:
        raise RuntimeError(f"cannot classify resistance: {exc}") from exc

    block = pack.parameters.get(RESISTANCE_THRESHOLDS_KEY)
    if not isinstance(block, dict):
        raise RuntimeError(
            f"parameter pack {pack.pack_id!r} declares "
            f"{RESISTANCE_THRESHOLDS_KEY!r} as {type(block).__name__}; expected a "
            f"mapping with {_SUSCEPTIBLE_KEY!r} and {_SUSPECTED_KEY!r}."
        )
    missing = [k for k in (_SUSCEPTIBLE_KEY, _SUSPECTED_KEY) if k not in block]
    if missing:
        raise RuntimeError(
            f"parameter pack {pack.pack_id!r} is missing {missing} from its "
            f"{RESISTANCE_THRESHOLDS_KEY!r} block."
        )
    try:
        susceptible = float(block[_SUSCEPTIBLE_KEY])
        suspected = float(block[_SUSPECTED_KEY])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"parameter pack {pack.pack_id!r} has a non-numeric "
            f"{RESISTANCE_THRESHOLDS_KEY!r} value: {block!r}"
        ) from exc
    if not (0.0 <= suspected <= susceptible <= 100.0):
        raise RuntimeError(
            f"parameter pack {pack.pack_id!r} declares an impossible band: "
            f"{_SUSCEPTIBLE_KEY}={susceptible!r}, "
            f"{_SUSPECTED_KEY}={suspected!r}."
        )
    return susceptible, suspected


# ---------------------------------------------------------------------------
# Burden expansion
# ---------------------------------------------------------------------------

_BURDEN_SEASONAL_FACTORS = {
    1: 0.55, 2: 0.50, 3: 0.65, 4: 1.35, 5: 1.50, 6: 1.40,
    7: 1.30, 8: 1.10, 9: 0.85, 10: 0.65, 11: 0.55, 12: 0.50,
}
_BURDEN_WEEKLY_PATTERNS = {
    1: 0.95, 2: 1.00, 3: 1.05, 4: 1.10, 5: 1.10, 6: 0.90, 7: 0.85,
}


def _burden_seasonal_factor(month: int) -> float:
    return _BURDEN_SEASONAL_FACTORS.get(month, 1.0)


def _burden_weekly_factor(date: "datetime") -> float:
    return _BURDEN_WEEKLY_PATTERNS.get(date.isocalendar()[2], 1.0)


def _burden_generate_daily_case_counts(
    annual_cases: float,
    year: int,
    location_id: int,
    base_variation: float = 0.25,
    seed: Optional[int] = None,
) -> pd.DataFrame:
    """Expand an annual total into a daily series whose sum equals that total.

    Shape = seasonal(month) * weekly(dow) * intra_month(day, month) *
    lognormal(0, base_variation*0.7) * holiday * spike, normalised to sum 1, then
    apportioned by largest remainder so ``sum(case_count) == round(annual_cases)``
    exactly.

    Corrections over the prior revision: factors normalised to 1 (not 365, which
    produced ~365x the source total); ``calendar.monthrange`` (not
    ``month % 12 + 1``, which was negative in December); ``default_rng`` (not the
    process-global ``np.random.seed``); no ``max(1, ...)`` floor.
    """
    rng = np.random.default_rng(
        None if seed is None else int(seed) + int(location_id) + int(year)
    )
    date_range = pd.date_range(datetime(year, 1, 1), datetime(year, 12, 31))

    factors: List[float] = []
    for date in date_range:
        month = date.month
        days_in_month = calendar.monthrange(year, month)[1]
        phase = (date.day / max(days_in_month, 1)) * 2 * np.pi
        intra = 1.0 + 0.08 * np.sin(phase + month * 0.5)
        noise = float(rng.lognormal(0.0, base_variation * 0.7))

        holiday = 1.0
        if month == 12 and date.day in (25, 26):
            holiday = 0.80
        elif month == 1 and date.day == 1:
            holiday = 0.75
        elif month == 4 and 1 <= date.day <= 10:
            holiday = 0.85
        elif month == 5 and date.day <= 5:
            holiday = 0.85

        spike = 1.0
        if rng.random() < 0.005:
            spike = 1.0 + float(rng.exponential(0.5))

        factors.append(
            _burden_seasonal_factor(month) * _burden_weekly_factor(date)
            * intra * noise * holiday * spike
        )

    total = sum(factors)
    factors = (
        [f / total for f in factors] if total > 0
        else [1.0 / len(date_range)] * len(date_range)
    )

    target = int(round(max(0.0, float(annual_cases))))
    raw = [annual_cases * f for f in factors]
    counts = [int(v) for v in raw]

    remainder = target - sum(counts)
    if remainder > 0:
        order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - counts[i]), i))
        for i in order[:remainder]:
            counts[i] += 1
    elif remainder < 0:
        order = sorted(range(len(raw)), key=lambda i: (raw[i] - counts[i], i))
        for i in order[: abs(remainder)]:
            if counts[i] > 0:
                counts[i] -= 1

    return pd.DataFrame({
        "date_id": [d.strftime("%Y-%m-%d") for d in date_range],
        "year": year,
        "month": [d.month for d in date_range],
        "case_count": counts,
        "location_id": location_id,
    })


# ---------------------------------------------------------------------------
# Fact aggregation spec
# ---------------------------------------------------------------------------

_FACT_AGGREGATION_SPEC: Dict[str, Dict[str, Any]] = {
    "fact_vector_observation": {
        "natural_key": ["location_id", "date_id", "vector_code", "source_code",
                        "life_stage", "sex", "collection_method"],
        "sums": ["count"],
        "means": ["density_per_trap", "infection_rate"],
        "first": ["tested_for_pathogen", "infection_status"],
    },
    "fact_insecticide_resistance": {
        "natural_key": ["location_id", "date_id", "vector_code", "insecticide_code",
                        "test_type_code", "source_code", "concentration"],
        "sums": ["n_insect_tested", "n_insect_susceptible", "n_insect_resistant",
                 "n_insect_dead", "vgsc_n_insect_tested",
                 "vgsc995l_vgsc995l_n", "vgsc995l_vgsc995f_n",
                 "vgsc995f_vgsc995f_n", "vgsc995l_vgsc995s_n",
                 "vgsc995s_vgsc995s_n", "vgsc995f_vgsc995s_n"],
        "means": [],
        "first": ["exposure_time", "vgsc_generation"],
        "derived": ["mortality", "resistance_status"],
    },
    "fact_bionomics": {
        "natural_key": ["location_id", "date_id", "vector_code", "source_code"],
        "sums": [],
        "means": ["biting_rate"],
        "first": ["feeding_preference", "biting_location", "resting_behavior",
                  "larval_site_type"],
    },
    "fact_environment": {
        "natural_key": ["location_id", "date_id", "variable"],
        "sums": [],
        "means": ["value"],
        "first": ["source_code"],
    },
    "fact_disease_case": {
        "natural_key": ["location_id", "date_id_onset", "disease_code",
                        "identification_method_code", "source_code",
                        "case_status", "age_group", "sex"],
        "sums": ["case_count"],
        "means": [],
        "first": ["date_id_reported", "host_type", "outcome", "age_years"],
    },
    "fact_disease_burden": {
        "natural_key": ["location_id", "year", "disease_code", "pathogen_species"],
        "sums": ["cases_mean"],
        "means": ["incidence_rate", "prevalence_rate", "mortality_rate"],
        "first": ["source_code"],
    },
    "fact_intervention": {
        "natural_key": ["location_id", "date_start", "target_disease_code",
                        "source_code", "intervention_type"],
        "sums": [],
        "means": ["coverage"],
        "first": ["date_end"],
    },
    "fact_socioeconomic_condition": {
        "natural_key": ["location_id", "year", "indicator_code"],
        "sums": [],
        "means": ["indicator_value"],
        "first": ["source_code"],
    },
}

_DIMENSION_FACT_MAP: Dict[Any, Dict[str, List[str]]] = {
    DimDisease: {
        "fact_tables": ["fact_disease_case", "fact_disease_burden",
                        "fact_intervention"],
        "fk_columns": ["disease_code", "disease_code", "target_disease_code"],
    },
    DimVector: {
        "fact_tables": ["fact_vector_observation",
                        "fact_insecticide_resistance", "fact_bionomics"],
        "fk_columns": ["vector_code", "vector_code", "vector_code"],
    },
    DimInsecticide: {
        "fact_tables": ["fact_insecticide_resistance"],
        "fk_columns": ["insecticide_code"],
    },
    DimLocation: {
        "fact_tables": ["fact_vector_observation", "fact_disease_case",
                        "fact_insecticide_resistance", "fact_bionomics",
                        "fact_intervention", "fact_socioeconomic_condition",
                        "fact_health_facility", "fact_disease_burden",
                        "fact_environment"],
        "fk_columns": ["location_id"] * 9,
    },
    DimSource: {
        "fact_tables": ["fact_vector_observation", "fact_disease_case",
                        "fact_insecticide_resistance", "fact_bionomics",
                        "fact_intervention", "fact_socioeconomic_condition",
                        "fact_health_facility", "fact_disease_burden",
                        "fact_environment"],
        "fk_columns": ["source_code"] * 9,
    },
}


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def enrich_location_dataframe(dim_df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing ``country_code``/``country_name`` from centroid coordinates.

    Never overwrites a country the source declared. Returns a new frame.
    """
    if dim_df is None or dim_df.empty:
        return dim_df
    if "centroid_lat" not in dim_df.columns or "centroid_lon" not in dim_df.columns:
        return dim_df

    frame = dim_df.copy()
    for column in ("country_code", "country_name"):
        if column not in frame.columns:
            frame[column] = None

    needs = frame["country_code"].isna() | (
        frame["country_code"].astype(str).str.strip() == ""
    )
    if not needs.any():
        return frame

    filled = 0
    for index in frame.index[needs]:
        lat, lon = frame.at[index, "centroid_lat"], frame.at[index, "centroid_lon"]
        if pd.isna(lat) or pd.isna(lon):
            continue
        try:
            name, code = get_country_from_coords(float(lat), float(lon))
        except Exception as exc:  # noqa: BLE001
            logger.debug("country lookup failed for (%s, %s): %s", lat, lon, exc)
            continue
        if code:
            frame.at[index, "country_code"] = code
            frame.at[index, "country_name"] = name
            filled += 1

    if filled:
        logger.info("  filled country_code for %d location row(s)", filled)
    return frame


def _chunked_in(session, base_sql: str, static_params: dict, values,
                expanding_name: str = "vals", chunk_size: int = MAX_IN_VARS):
    """Run a statement containing ``IN :vals`` in chunks."""
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
    """``WHERE a = :a AND b IS NULL`` built from a mapping, skipping nothing."""
    clauses, params = [], {}
    for name, value in pairs.items():
        if value is None:
            clauses.append(f"{name} IS NULL")
        else:
            params[name] = value
            clauses.append(f"{name} = :{name}")
    return (" AND ".join(clauses) or "1=1"), params


def _nearest_index(glat, glon, alat, alon, chunk: int = 2000):
    """Index of the nearest candidate for each point. Equirectangular, chunked.

    ``dlat^2 + (dlon * cos(mean_lat))^2``. Adequate for nearest-neighbour at
    regional scale, deterministic, and free of a projection dependency. Ties
    resolve to the first candidate, and candidates arrive ordered by
    ``location_id``, so the winner is the lowest id.
    """
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


# ---------------------------------------------------------------------------
# DataConsolidator
# ---------------------------------------------------------------------------

class DataConsolidator:
    """Consolidates and standardizes data in the CDM.

    ``registry`` and ``run_id`` are attached by ``run_consolidation_step`` or
    passed to ``consolidate_all``; a directly-constructed consolidator works with
    no registry at all.
    """

    def __init__(self, session: Optional[Session] = None):
        self.session = session or SessionLocal()
        self._country_lookup = COUNTRY_LOOKUP
        self._closed = False
        self._close_report: Dict[str, Any] = {}
        self._pending_resolutions: List[Any] = []

    def _scalar_or_zero(self, sql: str) -> int:
        """``SELECT COUNT(*)`` or 0. A failed diagnostic must not fail a run."""
        try:
            return int(self.session.execute(text(sql)).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("diagnostic count failed (%s): %s", sql, exc)
            return 0

    # -- A6 ---------------------------------------------------------------

    def fix_location_country_codes(self) -> int:
        """Fill ``dim_location.country_code`` from coordinates. Returns rows fixed."""
        rows = self.session.execute(text(
            "SELECT location_id, centroid_lat, centroid_lon FROM dim_location "
            "WHERE country_code IS NULL AND centroid_lat IS NOT NULL "
            "AND centroid_lon IS NOT NULL ORDER BY location_id"
        )).fetchall()
        if not rows:
            return 0

        updates = []
        for location_id, lat, lon in rows:
            try:
                name, code = get_country_from_coords(float(lat), float(lon))
            except Exception as exc:  # noqa: BLE001
                logger.debug("country lookup failed for %s: %s", location_id, exc)
                continue
            if code:
                updates.append({"location_id": int(location_id),
                                "country_code": code, "country_name": name})

        if updates:
            statement = text(
                "UPDATE dim_location SET country_code = :country_code, "
                "country_name = :country_name WHERE location_id = :location_id"
            )
            for start in range(0, len(updates), MAX_IN_VARS):
                self.session.execute(statement, updates[start:start + MAX_IN_VARS])
            self.session.commit()

        logger.info("  country codes: filled %d of %d location(s)",
                    len(updates), len(rows))
        return len(updates)

    def consolidate_locations(self, *, gadm_gpkg: Optional[str] = None,
                              country_codes: Iterable[str] = (),
                              dry_run: bool = False) -> Dict[str, Any]:
        """De-duplicate ``dim_location`` and place every row in the hierarchy.

        Order: fill country codes -> resolve boundary hierarchy (if a boundary
        file is given) -> detach dangling parents -> merge on normalised name
        within ``(country_code, admin_level)`` -> rebuild parents from
        ``standard_code``.

        The name key carries ``country_code``/``admin_level`` because a bare name
        would merge every "Central" on the continent.
        """
        logger.info("=" * 60)
        logger.info("A6: CONSOLIDATE LOCATIONS%s", " (dry run)" if dry_run else "")
        logger.info("=" * 60)

        codes = [str(c).strip().upper() for c in (country_codes or []) if str(c).strip()]
        before = self._scalar_or_zero("SELECT COUNT(*) FROM dim_location")
        filled = 0 if dry_run else self.fix_location_country_codes()

        parents_resolved: Dict[str, Any] = {}
        if gadm_gpkg and not dry_run:
            try:
                from utilities.admin_alignment import resolve_parent_locations
                parents_resolved = resolve_parent_locations(
                    self.session, gpkg_path=gadm_gpkg,
                    registry=getattr(self, "registry", None),
                    country_codes=codes or None, dry_run=False,
                ) or {}
            except Exception as exc:  # noqa: BLE001 - geography is optional
                logger.warning("  boundary hierarchy skipped: %s: %s",
                               type(exc).__name__, exc)
                parents_resolved = {"status": "skipped", "reason": str(exc)}
        elif gadm_gpkg is None:
            logger.info("  no boundary file configured; skipping the hierarchy pass")

        registry = _load_registry(getattr(self, "registry", None))
        repaired = self._repair_dangling_parents(dry_run=dry_run)

        policy = None
        if registry is not None:
            policy = registry.merge_policies().get("dim_location.name")
        merge_report = self._consolidate_dimension(
            DimLocation, "name", "location_id",
            fallback_key_fn=_pack_key_fn(_pack_from_policy(registry, policy)),
            pack=None, dry_run=dry_run,
            group_extra=("country_code", "admin_level"),
        )
        rebuilt = self._rebuild_admin_hierarchy_from_standard_code(dry_run=dry_run)
        grid_parents = self._assign_grid_parents(dry_run=dry_run)
        
        grid_level = None
        if registry is not None:
            try:
                grid_level = getattr(registry.select_pack("dim_location.name"),
                                     "grid_level", None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("no location pack for the grid count: %s", exc)

        grid_count = 0
        if grid_level:
            params = {"lvl": grid_level}
            grid_count = self._scalar_or_zero_raw(
                "SELECT COUNT(*) FROM dim_location WHERE admin_level = :lvl", params
            )

        after = self._scalar_or_zero("SELECT COUNT(*) FROM dim_location")
        merged_groups = (merge_report or {}).get("merged") or []
        merged_count = sum(len(e.get("duplicate_codes") or ()) for e in merged_groups)

        summary = {
            "original_count": before,
            "final_count": after,
            "country_codes_filled": filled,
            "parents_resolved": parents_resolved,
            "repaired_parents": repaired,
            "hierarchy_rebuilt": rebuilt,
            "grid_count": grid_count,
            "grid_parents": grid_parents,
            "non_grid_count": max(0, after - grid_count),
            "cluster_results": {"merged_count": merged_count,
                                "group_count": len(merged_groups),
                                "groups": merged_groups},
            "name_duplicate_groups": [
                e.get("group_key") for e in merged_groups
                if str(e.get("group_key", "")).startswith("name:")
            ],
            "final_country_fixes": filled,
            "merge_report": merge_report,
            "dry_run": bool(dry_run),
        }

        logger.info("  locations: %d -> %d row(s) (%d merged across %d group(s)), "
                    "%d country code(s) filled, %d parent(s) repaired%s",
                    before, after, merged_count, len(merged_groups), filled,
                    repaired.get("total", 0), " (dry run)" if dry_run else "")
        return summary

    def _scalar_or_zero_raw(self, sql: str, params: Dict[str, Any]) -> int:
        try:
            return int(self.session.execute(text(sql), params).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("diagnostic count failed (%s): %s", sql, exc)
            return 0

    def _repair_dangling_parents(self, dry_run: bool = False) -> Dict[str, Any]:
        """NULL a parent pointer that does not resolve, and break self-parents.

        Both conditions break any recursive hierarchy query. NULL is honest here;
        the nearest ancestor of a broken pointer is a guess.
        """
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
                "AND parent_location_id NOT IN (SELECT location_id FROM dim_location))"
            ))
            self.session.commit()

        if dangling:
            logger.warning(
                "  %d location row(s) had a dangling or self-referential parent and "
                "were detached (%d self, %d missing target)%s",
                dangling, self_parent, missing, " (dry run)" if dry_run else "")

        return {"total": int(dangling), "self_parent": int(self_parent),
                "missing_parent": int(missing),
                "repaired": 0 if dry_run else int(dangling)}

    def _rebuild_admin_hierarchy_from_standard_code(
        self, dry_run: bool = False
    ) -> Dict[str, Any]:
        """Set ``parent_location_id`` from a GADM-style ``standard_code``.

        A GID encodes its own parent (decrement the level, drop the last dotted
        segment), so a run with no GeoPackage still gets a hierarchy. Only
        unparented rows are touched: an existing parent was decided by the
        spatial pass, which knows more.
        """
        rows = self.session.execute(text(
            "SELECT location_id, standard_code FROM dim_location "
            "WHERE parent_location_id IS NULL AND standard_code IS NOT NULL "
            "ORDER BY location_id"
        )).fetchall()
        if not rows:
            return {"examined": 0, "placed": 0}

        from utilities.admin_alignment import _parent_gid

        id_by_code = {
            str(code): int(loc_id)
            for loc_id, code in self.session.execute(text(
                "SELECT location_id, location_code FROM dim_location "
                "WHERE location_code IS NOT NULL"
            )).fetchall()
        }

        updates = []
        for location_id, standard_code in rows:
            parent_gid = _parent_gid(str(standard_code or ""))
            if not parent_gid:
                continue
            parent_id = id_by_code.get(parent_gid)
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
        """Set ``parent_location_id`` on every grid cell.

        A grid cell is a raster pixel: its ``location_code`` is "<lon>/<lat>", it
        appears in no boundary product, and it carries no ``standard_code``. So
        neither the GID join nor the ``standard_code`` rebuild can place it, and
        it stays orphaned — unreachable from the R analysis, which finds
        study-area cells by walking ``parent_location_id`` downward from the
        administrative rows the config names by name.

        A cell is recognised by **either** signal, so the pass cannot be disabled
        by a missing manifest key:

        * ``admin_level == pack.grid_level``, when the pack declares one;
        * ``location_code`` matching "<lon>/<lat>", which is what every raster
          mapping writes.

        Each cell is assigned to the deepest administrative level that carries
        centroid coordinates, restricted to the cell's own ``country_code`` so a
        Kenyan pixel is never filed under a Ugandan district. When the pack
        declares no ``admin_level_map``, the target level is the deepest one by
        observed parent-chain depth instead.

        Idempotent: by default only cells with a NULL parent are considered, so a
        re-run is cheap and a manual override survives. ``force=True`` recomputes
        every cell.
        """
        registry = _load_registry(getattr(self, "registry", None))
        pack = None
        if registry is not None:
            try:
                pack = registry.select_pack("dim_location.name")
            except Exception as exc:  # noqa: BLE001 - no pack is not a failure
                logger.debug("no location pack for the grid pass: %s", exc)

        grid_level = getattr(pack, "grid_level", None)
        admin_levels = tuple((getattr(pack, "admin_level_map", {}) or {}).values())

        rows = self.session.execute(text(
            "SELECT location_id, name, admin_level, country_code, centroid_lat, "
            "       centroid_lon, parent_location_id "
            "FROM dim_location ORDER BY location_id")).fetchall()

        parent_of = {int(r[0]): r[6] for r in rows}

        def _depth(loc_id: int, seen: Optional[Set[int]] = None) -> int:
            """Ancestor count, with a cycle guard."""
            seen = seen or set()
            if loc_id in seen:
                return 0
            seen.add(loc_id)
            parent = parent_of.get(loc_id)
            return 0 if parent is None else 1 + _depth(int(parent), seen)

        # record = (location_id, country, lat, lon, parent, label)
        grid: List[Tuple[int, str, float, float, Any, str]] = []
        admin_records: List[Tuple[int, str, float, float, Any, str]] = []
        no_coords = 0
        by_pattern = 0

        for loc_id, name, level, country, lat, lon, parent in rows:
            label = "" if level is None else str(level).strip()
            is_grid = bool(grid_level) and label == grid_level
            if not is_grid and _GRID_NAME_RE.match(str(name or "")):
                is_grid = True
                by_pattern += 1

            try:
                flat, flon = float(lat), float(lon)
            except (TypeError, ValueError):
                if is_grid:
                    no_coords += 1
                continue
            if flat != flat or flon != flon:  # NaN
                if is_grid:
                    no_coords += 1
                continue
            if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
                if is_grid:
                    no_coords += 1
                continue

            code = (country or "").strip().upper()
            if is_grid:
                grid.append((int(loc_id), code, flat, flon, parent, label))
            elif not label:
                continue
            elif admin_levels:
                if label in admin_levels:
                    admin_records.append(
                        (int(loc_id), code, flat, flon, parent, label))
            else:
                admin_records.append(
                    (int(loc_id), code, flat, flon, parent, label))

        if not grid:
            logger.info("  no grid cells found (grid_level=%r, %d row(s) examined); "
                        "no parent assigned", grid_level, len(rows))
            return {"status": "no_grid", "grid_level": grid_level,
                    "grid_cells": 0, "assigned": 0}

        # ---- pick the target level --------------------------------------
        candidates: List[Tuple[int, str, float, float, Any, str]] = []
        target_level: Optional[str] = None

        if admin_levels:
            for level_label in reversed(admin_levels):
                pool = [r for r in admin_records if r[5] == level_label]
                if pool:
                    candidates, target_level = pool, level_label
                    break
        else:
            depths = {r[0]: _depth(r[0]) for r in admin_records}
            max_depth = max(depths.values(), default=0)
            candidates = [r for r in admin_records if depths.get(r[0]) == max_depth]
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

        by_country: Dict[str, List[Tuple[int, str, float, float, Any, str]]] = {}
        for record in candidates:
            by_country.setdefault(record[1], []).append(record)

        kept = 0
        updates: List[Dict[str, Any]] = []
        pending: Dict[str, List[Tuple[int, str, float, float, Any, str]]] = {}
        for record in grid:
            if record[4] is not None and not force:
                kept += 1
                continue
            pending.setdefault(record[1], []).append(record)

        for country in sorted(pending):
            group = pending[country]
            pool = by_country.get(country) or candidates
            nearest = _nearest_index(
                [r[2] for r in group], [r[3] for r in group],
                [r[2] for r in pool], [r[3] for r in pool])
            for offset, record in enumerate(group):
                parent_id = pool[int(nearest[offset])][0]
                if parent_id == record[0]:
                    continue
                updates.append({"location_id": record[0],
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
            by_country_counts[record[1] or "(none)"] = (
                by_country_counts.get(record[1] or "(none)", 0) + 1)

        logger.info(
            "  grid parents: %d/%d cell(s) assigned to %r (%d recognised by "
            "location_code pattern, %d already parented, %d without coordinates; "
            "countries: %s)%s",
            len(updates), len(grid), target_level, by_pattern, kept, no_coords,
            ", ".join(f"{c}={n}" for c, n in sorted(by_country_counts.items())),
            " (dry run)" if dry_run else "")

        return {"status": "dry_run" if dry_run else "ok",
                "grid_level": grid_level, "admin_level": target_level,
                "grid_cells": len(grid), "candidates": len(candidates),
                "assigned": len(updates), "kept": kept,
                "no_coordinates": no_coords,
                "matched_by_pattern": by_pattern,
                "by_country": by_country_counts,
                "dry_run": bool(dry_run)}

    # -- dimension merge ---------------------------------------------------

    def _consolidate_dimension(self, model_class, name_col: str, code_col: str,
                               fallback_key_fn=None, pack=None,
                               dry_run: bool = False,
                               group_extra: Tuple[str, ...] = ()) -> Dict[str, Any]:
        """Merge duplicate rows of one dimension.

        Grouping key ladder: ``concept:<id>`` -> ``standard:<code>`` ->
        ``name:<normalised>[|extra...]``. ``group_extra`` appends columns to the
        name branch; ``dim_location`` passes country + admin level, which is what
        makes a bare name key safe.

        Canonical selection: a row carrying ``concept_id`` outranks one that does
        not; then ``standard_code``; then lowest code. The canonical row adopts a
        concept from a member before that member is deleted.

        Each duplicate is re-pointed through ``_update_fact_references`` (and
        its children re-parented) before the delete, so no fact row is ever left
        referencing a removed key.
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
                 "duplicates_found": 0, "merged": [], "deleted": 0}
        if not records:
            logger.info("  %s: no rows to merge", table_name)
            return empty

        def _suffix(record) -> str:
            if not group_extra:
                return ""
            parts = []
            for column in group_extra:
                value = getattr(record, column, None)
                parts.append("" if value is None else str(value).strip().upper())
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
            if fallback_key_fn is None:
                return f"name:{text_value}{_suffix(record)}"
            try:
                normalised = fallback_key_fn(text_value)
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
            logger.info("  %s: %d row(s), no duplicate group", table_name, len(records))
            empty["status"] = "ok"
            return empty

        is_vector = table_name == "dim_vector"
        merged: List[Dict[str, Any]] = []
        deleted_total = 0

        for group_key in sorted(duplicate_groups):
            members = duplicate_groups[group_key]
            if is_vector:
                canonical = self._pick_canonical_vector(members, pack)
            else:
                canonical = sorted(members, key=lambda r: (
                    0 if getattr(r, "concept_id", None) is not None else 1,
                    0 if str(getattr(r, "standard_code", None) or "").strip() else 1,
                    str(getattr(r, code_col, "")),
                ))[0]
            if canonical is None:
                logger.warning("  %s: group %r produced no canonical row", table_name,
                               group_key)
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
                            f"WHERE parent_location_id = :old_id"
                        ), {"new_id": canonical_key, "old_id": duplicate_key})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("  %s: could not re-parent children of %r: %s",
                                       table_name, duplicate_key, exc)

                try:
                    self._update_fact_references(model_class, duplicate_key,
                                                 canonical_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "  %s: could not re-point the facts of %r onto %r: %s. "
                        "Keeping the duplicate rather than orphaning them.",
                        table_name, duplicate_key, canonical_key, exc)
                    continue

                self.session.delete(duplicate)
                duplicate_keys.append(duplicate_key)
                deleted_total += 1

            merged.append({
                "group_key": group_key,
                "canonical_code": canonical_key,
                "canonical_name": canonical_name,
                "merged_count": len(members),
                "duplicate_codes": duplicate_keys,
                "member_names": [getattr(m, name_col, None) for m in members],
            })

        if not dry_run:
            try:
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("  %s: the merge commit failed: %s", table_name, exc)
                self.session.rollback()
                raise

        logger.info("  %s: %d row(s), %d duplicate group(s), %d removed%s",
                    table_name, len(records), len(duplicate_groups), deleted_total,
                    " (dry run)" if dry_run else "")
        return {"status": "dry_run" if dry_run else "ok", "table": table_name,
                "name_column": name_col, "dimension_key": code_col,
                "rows_examined": len(records),
                "duplicates_found": len(duplicate_groups),
                "merged": merged, "deleted": deleted_total}

    # -- FK cascade --------------------------------------------------------

    def _update_fact_references(self, model_class, old_key, new_key) -> int:
        """Rewrite every fact FK pointing at ``old_key`` to ``new_key``.

        Uses one ``UPDATE`` when that cannot collide; otherwise routes through
        ``_aggregate_and_reassign_bulk`` so colliding rows are aggregated rather
        than deleted. Returns fact rows written.
        """
        mapping = _DIMENSION_FACT_MAP.get(model_class)
        if not mapping or old_key == new_key:
            return 0

        written = 0
        for fact_table, fk_column in zip(mapping["fact_tables"], mapping["fk_columns"]):
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
                        f"WHERE {safe_column} = :old"
                    ), {"new": new_key, "old": old_key})
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
                "old": old_key, "new": new_key,
            }])
            written += int(report.get("rows_written", 0))
        return written

    def _would_collide(self, table_name: str, fk_column: str,
                       old_key: Any, new_key: Any) -> bool:
        """Whether substituting the FK would break a natural-key unique index."""
        key_columns = list((_FACT_AGGREGATION_SPEC.get(table_name) or {})
                           .get("natural_key") or [])
        if not key_columns:
            return False

        projected = ", ".join(
            f"CASE WHEN {c} = :old THEN :new ELSE {c} END" if c == fk_column else c
            for c in key_columns
        )
        try:
            groups = self.session.execute(text(
                "SELECT COUNT(*) FROM ("
                f"  SELECT {projected} FROM {table_name} "
                f"  WHERE {fk_column} IN (:old, :new) "
                f"  GROUP BY {projected} HAVING COUNT(*) > 1"
                ") AS colliding"
            ), {"old": old_key, "new": new_key}).scalar() or 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("collision check failed for %s.%s (%s); assuming none",
                         table_name, fk_column, exc)
            return False
        return int(groups) > 0

    # -- aggregation -------------------------------------------------------

    def _validate_aggregation_spec(self, spec: Optional[Mapping[str, Any]] = None
                                   ) -> Dict[str, Dict[str, Any]]:
        """Validate an aggregation spec into ``{table: {key, ops, numeric, derived}}``.

        The natural key comes from ``DBLoader.NATURAL_KEY_CONFIG`` where it exists,
        because that is the real conflict target. A spec that disagrees is
        overruled and the disagreement is logged once.
        """
        from etl.loaders.db_loader import NATURAL_KEY_CONFIG

        source = dict(_FACT_AGGREGATION_SPEC if spec is None else spec)
        if not source:
            raise ValueError("the aggregation spec is empty.")

        operators = {
            "sum": "sum", "total": "sum", "add": "sum", "plus": "sum",
            "max": "max", "maximum": "max", "greatest": "max",
            "min": "min", "minimum": "min", "least": "min",
            "first": "first", "earliest": "first", "keep": "first",
            "last": "last", "latest": "last",
        }

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
                for c in table_obj.__table__.columns
            }

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
                                f"{table_name}: operator {operator!r} for "
                                f"{column!r} is not one of "
                                f"{sorted(set(operators))}")
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
                        "%s. Using the loader's; the conflict target must be a real "
                        "unique constraint.", table_name, list(declared_key),
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
                "key": tuple(key_columns),
                "ops": ops,
                "numeric": tuple(c for c in sorted(ops) if is_number.get(c)),
                "derived": tuple(sorted(set(derived) - set(ops) - set(key_columns))),
            }

        return normalized

    def _aggregate_and_reassign(self, records: Iterable[Any], *,
                                spec: Optional[Mapping[str, Any]] = None,
                                dry_run: bool = False,
                                stop_on_error: bool = False) -> Dict[str, Any]:
        """Per-record entry point: one bulk call each, so one bad record does not
        lose the rest. Record shape ``{table, fk, old, new}``."""
        normalized = self._validate_aggregation_spec(spec)
        totals: Dict[str, Dict[str, Any]] = {}
        seen = errors = 0

        for record in records:
            seen += 1
            try:
                result = self._aggregate_and_reassign_bulk(
                    [record], spec=normalized, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
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
        """Reassign a fact FK, aggregating rows that would collide.

        Read the affected rows, substitute the FK in memory, group on the natural
        key with ``dropna=False``, NULL every ``derived`` column, delete the old
        rows and insert the aggregates.

        ``dropna=False`` is load-bearing: pandas otherwise drops groups whose key
        contains NaN, deleting the rows the merge exists to preserve. ``derived``
        columns are NULLed because they were computed from the pre-aggregation
        counts and are recomputed by the caller.
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
                logger.warning("aggregate: %s has a composite primary key", table_name)
                continue
            pk_name = pk_columns[0].name

            pairs: Dict[Any, Any] = {}
            for record in by_table[table_name]:
                old, new = record.get("old"), record.get("new")
                if old is not None and new is not None and old != new:
                    pairs[old] = new
            if not pairs:
                continue

            fk_columns = {str(r.get("fk") or "").strip()
                          for r in by_table[table_name]}
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
            select_columns = sorted({pk_name, safe_fk, *key_columns,
                                     *ops.keys(), *derived})

            wanted = sorted(pairs, key=str) + [
                v for v in sorted(set(pairs.values()), key=str) if v not in pairs
            ]
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

            aggregations = {
                column: {"sum": "sum", "mean": "mean", "min": "min", "max": "max",
                         "first": "first", "last": "last"}[operator]
                for column, operator in ops.items()
            }
            for column in derived:
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
                    self.session.execute(statement, payload[start:start + MAX_IN_VARS])
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
                      context_columns: Tuple[str, ...] = ()) -> Dict[str, int]:
        """Reconcile one dimension column against one pack.

        Writes ``decision``, never ``is_approved``: the machine's confidence is not
        a human verdict. Writes ``concept_code`` only when the effective decision
        is ``auto_accepted``. Records decisions in batches; stamps the dimension
        with one ``UPDATE`` per concept code. Reads distinct values ``ORDER BY`` so
        ``map_id`` is a property of the data.
        """
        from etl.transformers.dimension_index import DimensionKeyIndex, seed_from_rows

        stats: Dict[str, int] = {
            "total": 0, "matched": 0, "exact": 0, "normalized": 0, "alias": 0,
            "fuzzy": 0, "ambiguous": 0, "unresolved": 0, "unmatched": 0,
            "needs_review": 0, "applied": 0, "concept_ids_set": 0, "recorded": 0,
            "skipped_settled": 0, "errors": 0,
        }

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
                if contexts[key].get(context_name) is None and row[position] is not None:
                    contexts[key][context_name] = row[position]

        vocabulary_id = getattr(pack, "vocabulary_id", None)
        concept_index = DimensionKeyIndex(label=f"dim_concept:{vocabulary_id or 'none'}")
        if vocabulary_id:
            try:
                concept_rows = self.session.execute(text(
                    "SELECT concept_code, concept_id FROM dim_concept "
                    "WHERE vocabulary_id = :v ORDER BY concept_code"
                ), {"v": vocabulary_id}).mappings().all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("  could not read dim_concept for %r: %s. "
                               "standard_code written without concept_id.",
                               vocabulary_id, exc)
                concept_rows = []
            if concept_rows:
                seed_from_rows(concept_index, concept_rows,
                               code_field="concept_code", pk_field="concept_id")

        settled = settled_values or set()
        target_field = f"{ident_table}.{ident_column}"
        base_context = {"source_table": ident_table, "source_column": ident_column,
                        "target_field": target_field, "run_id": run_id}

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
            })
            if decision == AUTO_ACCEPTED and concept_code:
                accepted.setdefault(str(concept_code), []).append(value)

        write_failed = False
        if decision_rows and not dry_run:
            batch_size = max(1, min(MAX_IN_VARS, 500))
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
                    AND fact_concept_map.concept_code <> EXCLUDED.concept_code
                )
            """)
            for start in range(0, len(decision_rows), batch_size):
                chunk = decision_rows[start:start + batch_size]
                try:
                    self.session.execute(statement, chunk)
                except Exception as exc:  # noqa: BLE001
                    logger.error("  could not record %d decision(s) for %s.%s: %s",
                                 len(chunk), table, column, exc)
                    stats["errors"] += 1
                    self.session.rollback()
                    write_failed = True
                    break
                stats["recorded"] += len(chunk)

        if accepted and not dry_run and not write_failed:
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

        logger.info(
            "    %s.%s: %d value(s) vs %s -> %d recorded, %d applied (%d "
            "concept_id), %d needs review, %d unresolved, %d error(s)%s",
            table, column, stats["total"], vocabulary_id or "(no pack)",
            stats["recorded"], stats["applied"], stats["concept_ids_set"],
            stats["needs_review"], stats["unresolved"], stats["errors"],
            " (dry run)" if dry_run else "")
        return stats

    def standardize_vocabularies(self, vocab_dir: Optional[str] = None,
                                 fuzzy_threshold: Optional[float] = None,
                                 dry_run: bool = False, *,
                                 registry: Optional[VocabularyRegistry] = None,
                                 run_id: Optional[str] = None) -> Dict[str, Any]:
        """Reconcile raw values against the registry's packs.

        Target, vocabulary, release, normaliser and thresholds all come from
        ``registry.field_bindings()``. Reconciles only values this run has not
        already settled. ``vocab_dir`` and ``fuzzy_threshold`` are accepted
        positionally for one release and ignored (with a warning): a CWD-relative
        vocabulary path and one global threshold are the two parameters the review
        singles out.
        """
        warnings.warn(
            "standardize_vocabularies is the deprecated entry point; vocabulary "
            "identity, release, normaliser and thresholds now come from the pack "
            "manifests.", DeprecationWarning, stacklevel=2)

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
            except Exception as exc:  # noqa: BLE001 - pre-migration database
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
                sizes[vocabulary_id] = _vocabulary_size(self.session, vocabulary_id)
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
                context_columns=context_columns)
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
                        "%d settled, %d error(s)",
                        key, s["total"], s["applied"], s["exact"], s["normalized"],
                        s["alias"], s["fuzzy"], s["needs_review"], s["unresolved"],
                        s["skipped_settled"], s["errors"])
        logger.info("  rows stamped with a concept: %d", applied_total)
        logger.info("=" * 60)
        return results

    # -- B5 ----------------------------------------------------------------

    def consolidate_vocabulary(self, merge_policies: Optional[Dict[str, Any]] = None,
                               *, registry: Optional[VocabularyRegistry] = None,
                               run_id: Optional[str] = None,
                               dry_run: bool = False) -> Dict[str, Any]:
        """Merge duplicate dimension rows, per the registry's merge policies.

        A domain with no pack is not name-merged (logged once); a surrogate-keyed
        dimension is skipped because its concept is the parent unit a row sits in,
        not the row's identity.
        """
        logger.info("=" * 60)
        logger.info("B5: MERGE PASS%s", " (dry run)" if dry_run else "")
        logger.info("=" * 60)

        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        resolved_run_id = (run_id if run_id is not None
                           else getattr(self, "run_id", None))

        policies = dict(merge_policies or {})
        if not policies:
            if resolved_registry is None:
                logger.warning("  no registry: no merge policy, no dimension merged.")
                return {}
            policies = resolved_registry.merge_policies()

        by_table: Dict[str, List[Tuple[str, Any]]] = {}
        for field in sorted(policies):
            try:
                binding = resolve_field(field)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  a merge policy names %r, which FIELD_CATALOG does "
                               "not carry (%s); skipping.", field, exc)
                continue
            if binding.value_type != "text":
                continue
            by_table.setdefault(binding.dimension_table, []).append(
                (field, policies[field]))

        for domain in sorted(DOMAIN_TO_DIM_TABLE):
            table = DOMAIN_TO_DIM_TABLE[domain]
            if table in by_table or table == "dim_location":
                continue
            _warn_once(
                f"merge-policy:{resolved_run_id}:{domain}",
                "  domain %r (%s) has no pack, so it is NOT merged this run: no "
                "normaliser, threshold or precedence to merge on. Declare a "
                "manifest under vocabularies/registry/.", domain, table)

        if not by_table:
            logger.info("  no dimension table has a text-field merge policy.")
            return {}

        results: Dict[str, Any] = {}
        for table_name in sorted(by_table):
            field, policy = sorted(by_table[table_name])[0]
            try:
                binding = resolve_field(field)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  %s: %s", field, exc)
                continue

            model_class = _model_for_table(binding.dimension_table)
            if model_class is None:
                logger.warning("  %s: no model for %r; skipping.",
                               field, binding.dimension_table)
                continue

            primary_key = list(model_class.__table__.primary_key.columns)
            if len(primary_key) == 1 and isinstance(primary_key[0].type, Integer):
                logger.info("  %s: skipped (surrogate-keyed; owned by A6).",
                            binding.dimension_table)
                results[binding.dimension_table] = {
                    "status": "skipped", "reason": "surrogate-keyed dimension"}
                continue

            pack = _pack_from_policy(resolved_registry, policy)
            results[binding.dimension_table] = self._consolidate_dimension(
                model_class, field.split(".", 1)[1], binding.dimension_key,
                fallback_key_fn=_pack_key_fn(pack), pack=pack, dry_run=dry_run)

        if not dry_run:
            try:
                self.session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("  the merge-pass commit failed: %s", exc)
                self.session.rollback()
                raise
        return results

    def _pick_canonical_vector(self, records: List, pack=None):
        """Surviving vector row for a merge group.

        Score: concept_id (+1e6), standard_code (+1e5), pack-declared name bonus,
        name length, fact usage (capped). The candidate list is sorted by
        ``vector_code`` and ``max`` keeps the first maximal element, so ties never
        depend on row order.
        """
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
                "ORDER BY vector_code"
            ).bindparams(bindparam("vals", expanding=True))
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
        """Deprecated. Delegates to ``normalizers``; never re-implements the regex."""
        warnings.warn(
            "DataConsolidator._normalize_species_name is deprecated; use "
            "utilities.vocabulary.normalizers.NORMALIZERS['species_canon'].",
            DeprecationWarning, stacklevel=2)
        return _normalizers.NORMALIZERS["species_canon"](name)

    # -- FK alignment ------------------------------------------------------

    def align_all_foreign_keys(self, dry_run: bool = False) -> Dict[str, Any]:
        """Align every fact-to-dimension foreign key.

        Step 2 *repairs* orphans by name before step 3 NULLs what is left. The
        reverse order (an earlier revision) nulled every orphan before the repair
        pass could see it.
        """
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
        """Report dimension rows no fact table references. Writes nothing: an
        unreferenced dimension row is reference data, not garbage."""
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
        """NULL fact FKs pointing at a dimension key that does not exist."""
        dimension_models = {
            "dim_location": (DimLocation, "location_id"),
            "dim_disease": (DimDisease, "disease_code"),
            "dim_vector": (DimVector, "vector_code"),
            "dim_insecticide": (DimInsecticide, "insecticide_code"),
            "dim_source": (DimSource, "source_code"),
            "dim_identification_method": (DimIdentificationMethod, "method_code"),
            "dim_resistance_test_type": (DimResistanceTestType, "test_type_code"),
            "dim_socioeconomic_indicator": (DimSocioeconomicIndicator,
                                            "indicator_code"),
        }
        inspector = inspect(self.session.bind)
        fact_tables = [t for t in inspector.get_table_names() if t.startswith("fact_")]
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
        """Legacy. Repair orphans by name, then NULL or quarantine the rest.

        Results are keyed per ``(table, column)``: keying per table overwrote every
        column's counters with the last column's, so the headline "repaired"
        number undercounted by however many FK columns a fact table has.

        A non-nullable FK cannot be NULLed, so unresolvable rows move to
        ``_quarantine_<table>`` — a real DELETE, logged at WARNING with the count
        and a restore hint.
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
                    f"GROUP BY {fk_col}"
                ).bindparams(bindparam("vals", expanding=True))
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

            # `NOT IN :vals` is chunked: a value counted as an orphan in one
            # chunk may exist in another. Confirm against the dimension first.
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
            results[key]["details"] = [
                {"value": v, "count": orphan_counter[v]} for v in orphaned_values[:100]]
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
                        f"SELECT {single_pk} FROM {table_name} WHERE {fk_col} = :old_val"
                    ), {"old_val": orphan_val}).fetchall()]
                except Exception:  # noqa: BLE001
                    remaining_values.append(orphan_val)
                    continue

                still_orphan = False
                for row_id in row_ids:
                    try:
                        self.session.execute(text(
                            f"UPDATE {table_name} SET {fk_col} = :new_val "
                            f"WHERE {single_pk} = :row_id"
                        ), {"new_val": new_val, "row_id": row_id})
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
                        "    %s.%s: MOVED %d row(s) into %s (column is NOT NULL and "
                        "the FK does not resolve). This is a real DELETE from the "
                        "fact table; restore with INSERT INTO %s SELECT ... FROM %s.",
                        table_name, fk_col, moved, qname, table_name, qname)
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
                logger.error("    %s.%s: %d row(s) still carry an unresolvable FK "
                             "after repair and NULL/quarantine. Manual review.",
                             table_name, fk_col, remaining_orphans)
            else:
                logger.info("    %s.%s: repair complete (%d fixed, %d orphaned)",
                            table_name, fk_col, results[key]["fixed"],
                            results[key]["orphaned"])
        return results

    # -- audit helpers -----------------------------------------------------

    def validate_country_codes(self) -> Dict[str, Any]:
        """Count locations by country-code state. Read-only."""
        rows = self.session.execute(text(
            "SELECT country_code FROM dim_location ORDER BY location_id")).fetchall()
        valid = invalid = missing = 0
        invalid_values: Set[str] = set()
        for (code,) in rows:
            text_value = "" if code is None else str(code).strip().upper()
            if not text_value:
                missing += 1
            elif text_value in ISO_COUNTRY_CODES or text_value in ISO_TO_COUNTRY_NAME:
                valid += 1
            else:
                invalid += 1
                invalid_values.add(text_value)
        return {"total": len(rows), "valid": valid, "invalid": invalid,
                "missing": missing, "invalid_values": sorted(invalid_values)[:20]}

    def _check_hierarchy(self, registry: Any = None) -> Dict[str, Any]:
        """Count how many non-admin locations have a parent. Read-only."""
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
        """Fact FKs that still reference a missing dimension key.

        Derived from ``_DIMENSION_FACT_MAP`` so a new FK column is covered without
        editing this method.
        """
        orphans: Dict[str, int] = {}
        for model_class, mapping in _DIMENSION_FACT_MAP.items():
            pk_col = list(model_class.__table__.primary_key.columns)[0].name
            for fact_table, fk_column in zip(mapping["fact_tables"],
                                             mapping["fk_columns"]):
                try:
                    safe_table = require_identifier(fact_table, what="table name")
                    safe_column = require_identifier(fk_column, what="column name")
                except ValueError:
                    continue
                count = self._scalar_or_zero(
                    f"SELECT COUNT(*) FROM {safe_table} f WHERE f.{safe_column} "
                    f"IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
                    f"{model_class.__tablename__} d WHERE d.{pk_col} = f.{safe_column})")
                if count:
                    orphans[f"{safe_table}.{safe_column}"] = count
        return orphans

    # -- burden / resistance ----------------------------------------------

    def fix_orphaned_burden_records(self) -> int:
        """Repair burden rows whose ``location_id`` does not resolve.

        Targets ``location_code`` only; an orphan with no matching code is left
        untouched and counted, rather than dumped into the most common case
        location. The previous version tested ``isinstance(bad_location_id, str)``
        on an Integer FK, so that branch was dead and every orphan was moved to one
        arbitrary district.
        """
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

    def expand_burden_to_daily(self, force: bool = False, seed: int = 42,
                               base_variation: float = 0.25, *,
                               default_population: Optional[float] = None
                               ) -> Dict[str, int]:
        """Expand yearly burden rows into daily case records.

        ``default_population`` defaults to None, which **skips** a location with no
        ``SP.POP.TOTL`` row and counts it. The previous revision assumed one
        million people for every such location, so a rural district and a megacity
        produced identical counts with nothing saying why.

        The daily series sums to the annual total, so ``case_count`` stays a real
        case count. ``dim_time`` is extended to cover the year before inserting,
        because ``date_id`` is a foreign key.
        """
        logger.info("=" * 60)
        logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS")
        logger.info("=" * 60)

        try:
            self.session.execute(text(
                "SELECT case_count FROM fact_disease_case LIMIT 1"))
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

        if not self._scalar_or_zero("SELECT COUNT(*) FROM fact_disease_burden"):
            logger.info("No burden records found; nothing to expand.")
            return {"inserted": 0, "already_present": 0, "skipped": 0,
                    "no_population": 0, "errors": 0}

        population_map = {
            int(row[0]): float(row[1])
            for row in self.session.execute(text(
                "SELECT location_id, indicator_value "
                "FROM fact_socioeconomic_condition "
                "WHERE indicator_code = 'SP.POP.TOTL'")).fetchall()
            if row[1] is not None
        }

        burden_records = self.session.execute(text(
            "SELECT burden_id, location_id, year, disease_code, source_code, "
            "incidence_rate, cases_mean FROM fact_disease_burden "
            "WHERE (incidence_rate IS NOT NULL AND incidence_rate > 0) "
            "   OR (cases_mean IS NOT NULL AND cases_mean > 0) "
            "ORDER BY burden_id")).fetchall()
        if not burden_records:
            logger.warning("No burden records with valid data found.")
            return {"inserted": 0, "already_present": 0, "skipped": 0,
                    "no_population": 0, "errors": 0}

        stats = {"inserted": 0, "already_present": 0, "skipped": 0,
                 "no_population": 0, "errors": 0}
        statement = text(
            "INSERT INTO fact_disease_case ("
            "  location_id, date_id_onset, date_id_reported, disease_code, "
            "  identification_method_code, source_code, case_status, host_type, "
            "  age_group, sex, outcome, case_count"
            ") VALUES ("
            "  :loc_id, :date_id, :date_id, :disease, 'ESTIMATE', :src_code, "
            "  'confirmed', 'human', 'all', 'unknown', 'unknown', :case_count"
            ")")
        window_sql = text(
            "DELETE FROM fact_disease_case WHERE location_id = :loc_id "
            "AND date_id_onset >= :start_date AND date_id_onset <= :end_date "
            "AND disease_code = :disease AND identification_method_code = 'ESTIMATE'")
        count_sql = text(
            "SELECT COUNT(*) FROM fact_disease_case WHERE location_id = :loc_id "
            "AND date_id_onset >= :start_date AND date_id_onset <= :end_date "
            "AND disease_code = :disease AND identification_method_code = 'ESTIMATE'")

        for (burden_id, location_id, year, disease_code,
             source_code, incidence_rate, cases_mean) in burden_records:
            population = population_map.get(int(location_id))
            if incidence_rate is not None and incidence_rate > 0:
                if population is None:
                    population = default_population
                if population is None or population <= 0:
                    stats["no_population"] += 1
                    logger.info("  skipping burden_id %s (location=%s, year=%s): no "
                                "population and no default_population given.",
                                burden_id, location_id, year)
                    continue
                annual_cases = (float(incidence_rate) / 100_000.0) * float(population)
            elif cases_mean is not None and cases_mean > 0:
                annual_cases = float(cases_mean)
            else:
                stats["skipped"] += 1
                continue

            window = {"loc_id": location_id, "start_date": f"{year}-01-01",
                      "end_date": f"{year}-12-31", "disease": disease_code}

            try:
                self._ensure_dim_time(year)
            except Exception as exc:  # noqa: BLE001
                logger.error("  could not extend dim_time to %s: %s", year, exc)
                stats["errors"] += 1
                continue

            existing = int(self.session.execute(count_sql, window).scalar() or 0)
            if existing and not force:
                logger.info("  skipping burden_id %s (location=%s, year=%s, "
                            "disease=%s): %d ESTIMATE row(s) present.",
                            burden_id, location_id, year, disease_code, existing)
                stats["already_present"] += 1
                continue

            daily = _burden_generate_daily_case_counts(
                annual_cases=annual_cases, year=int(year),
                location_id=int(location_id),
                base_variation=base_variation, seed=seed)
            logger.info("  burden_id %s (location=%s, year=%s): %d day(s), "
                        "sum=%d (annual %.0f), mean=%.1f, max=%d",
                        burden_id, location_id, year, len(daily),
                        int(daily["case_count"].sum()), annual_cases,
                        float(daily["case_count"].mean()),
                        int(daily["case_count"].max()))

            try:
                if existing:
                    deleted = self.session.execute(window_sql, window).rowcount or 0
                    logger.info("    deleted %d existing ESTIMATE row(s)", deleted)

                payload = [{"loc_id": int(location_id), "date_id": row["date_id"],
                            "disease": disease_code, "src_code": source_code,
                            "case_count": int(row["case_count"])}
                           for row in daily.to_dict(orient="records")]
                for start in range(0, len(payload), 5000):
                    self.session.execute(statement, payload[start:start + 5000])
                self.session.commit()
                stats["inserted"] += len(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("    Error expanding burden_id %s: %s: %s",
                             burden_id, type(exc).__name__, exc)
                self.session.rollback()
                stats["errors"] += 1

        logger.info("BURDEN EXPANSION COMPLETE: %d inserted, %d present, %d skipped, "
                    "%d no-population, %d error(s)", stats["inserted"],
                    stats["already_present"], stats["skipped"],
                    stats["no_population"], stats["errors"])
        return stats

    def _ensure_dim_time(self, year: int) -> int:
        """Insert any missing ``dim_time`` rows for ``year``. Returns rows added.

        ``fact_disease_case.date_id`` is a foreign key, so a burden year outside the
        seeded horizon fails on PostgreSQL instead of passing silently.
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
                    "day_of_year": current.timetuple().tm_yday,
                    "season": ("dry" if current.month in (11, 12, 1, 2, 3)
                               else "wet"),
                })
            current += timedelta(days=1)

        if payload:
            self.session.execute(text(
                "INSERT INTO dim_time (date_id, date, year, month, week, "
                "day_of_year, season, is_holiday) VALUES (:date_id, :date, :year, "
                ":month, :week, :day_of_year, :season, 0)"), payload)
            self.session.commit()
            logger.debug("  dim_time: added %d row(s) for %s", len(payload), year)
        return len(payload)

    def _compute_resistance_status(self) -> Dict[str, Any]:
        """Classify every bioassay into the WHO mortality bands.

        Cut-offs come from the ``who-resistance`` parameter pack, never a literal.
        Band labels come from ``concept_resolver.RESISTANCE_BANDS``, so
        ``'Susceptible'`` is spelled once — an earlier revision defined this method
        twice with two label sources, and the second shadowed the first.

        Four passes, strongest signal first: tested+dead, tested+resistant,
        susceptible+resistant, mortality only. Each touches only rows whose
        ``resistance_status`` is still NULL or blank, so the method is idempotent.
        """
        registry = getattr(self, "registry", None)
        susceptible_cutoff, suspected_cutoff = _resistance_thresholds(registry)

        pack_id, pack_version, pack_release = "(unknown)", "(unknown)", "(unrecorded)"
        try:
            loaded = _load_registry(registry)
            pack = (loaded.require_parameter_pack(RESISTANCE_THRESHOLDS_KEY)
                    if loaded is not None else None)
            if pack is not None:
                pack_id = getattr(pack, "pack_id", pack_id)
                pack_version = getattr(pack, "version", pack_version)
                pack_release = getattr(pack, "released_with", "") or pack_release
        except Exception as exc:  # noqa: BLE001 - a log line must not kill the pass
            logger.debug("could not read the classification pack: %s", exc)

        bands = {
            "susceptible": susceptible_cutoff,
            "suspected": suspected_cutoff,
            "susceptible_label": RESISTANCE_BANDS["susceptible"],
            "suspected_label": RESISTANCE_BANDS["suspected"],
            "resistant_label": RESISTANCE_BANDS["resistant"],
        }

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
        """Report the dimension rows a merge would collapse. Read-only.

        Also reports **contradictions**: rows whose normalised name agrees but
        whose ``concept_id`` disagrees. That is not a duplicate — merging it would
        delete a row the vocabulary has already contradicted.
        """
        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        wanted = ({str(n).strip() for n in tables if str(n).strip()}
                  if tables is not None else None)

        targets: Dict[str, Tuple[str, str, str, Any]] = {}
        if resolved_registry is not None:
            for field, policy in sorted(resolved_registry.merge_policies().items()):
                try:
                    binding = resolve_field(field)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("duplicate scan skips %r: %s", field, exc)
                    continue
                if binding.value_type != "text":
                    continue
                if wanted is not None and binding.dimension_table not in wanted:
                    continue
                targets[binding.dimension_table] = (
                    binding.dimension_table, field.split(".", 1)[1],
                    binding.dimension_key,
                    _pack_from_policy(resolved_registry, policy))

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

            attributes = set(model.__table__.columns.keys())
            has_concept = "concept_id" in attributes
            key_function = (_pack_key_fn(pack) if pack is not None
                            else _default_vocab_key)

            groups: Dict[str, List[Any]] = {}
            for record in records:
                concept_id = getattr(record, "concept_id", None) if has_concept else None
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
                groups.setdefault(f"name:{str(name_key).strip()}", []).append(record)

            duplicates = {k: v for k, v in groups.items() if len(v) > 1}
            listed = [{
                "group_key": k,
                "count": len(duplicates[k]),
                "codes": sorted(str(getattr(m, code_column, ""))
                                for m in duplicates[k]),
            } for k in sorted(duplicates)][: max(0, int(max_groups))]

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
                contradictions = [
                    {"name_key": label, "concept_ids": sorted(ids, key=str)}
                    for label, ids in sorted(by_name.items()) if len(ids) > 1
                ]

            results[table_name] = {
                "rows_examined": len(records),
                "name_column": name_column,
                "dimension_key": code_column,
                "groups": listed,
                "group_count": len(duplicates),
                "groups_truncated": len(duplicates) > max(0, int(max_groups)),
                "conflicting_concepts": contradictions[: max(0, int(max_groups))],
                "conflict_count": len(contradictions),
            }
            if duplicates:
                logger.warning("  %s: %d duplicate group(s) across %d row(s)",
                               table_name, len(duplicates), len(records))
        return results

    def resolve_fact_table_duplicates(self) -> Dict[str, int]:
        """Aggregate duplicates that already exist in the fact tables.

        Each table is guarded by an explicit duplicate count, so a clean database
        touches nothing. Every colliding group is collapsed through the same
        operators the FK cascade uses, so the two paths cannot disagree.
        """
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
        """Aggregate one duplicate group in place; NULL every ``derived`` column."""
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
        select_columns = sorted({pk_name, *key_columns, *ops.keys(), *derived})
        rows = self.session.execute(text(
            f"SELECT {', '.join(select_columns)} FROM {table_name} "
            f"WHERE {where} ORDER BY {pk_name}"), params).mappings().all()
        if len(rows) < 2:
            return 0

        frame = pd.DataFrame([dict(r) for r in rows])
        aggregations = {column: ops[column] for column in ops}
        for column in derived:
            aggregations[column] = "first"
        grouped = (frame.groupby(key_columns, dropna=False, sort=True)
                   .agg(aggregations).reset_index())
        for column in derived:
            grouped[column] = None

        for row in rows:
            self.session.execute(text(
                f"DELETE FROM {table_name} WHERE {pk_name} = :pk"), {"pk": row[pk_name]})

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
                        run_id: Optional[str] = None) -> Dict[str, Any]:
        """Run every consolidation phase, in the one order that is safe.

        FK alignment -> A6 locations -> A8 reconcile -> A9 spatial concepts ->
        B5 merge -> facts sweep -> orphan audit -> burden expand -> resistance ->
        duplicate scan -> country/hierarchy validation.

        The return dict carries every key ``_log_consolidation_summary`` reads
        (``locations``, ``foreign_key_alignment``, ``vocabulary_standard``,
        ``vocabulary_consolidate``, ``duplicates``, ``validation``,
        ``hierarchy``, ``remaining_orphans``, ``burden_fixed``). An earlier
        revision returned aliases for none of them, so the summary printed zeros
        while claiming success.
        """
        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        resolved_run_id = (run_id if run_id is not None
                           else getattr(self, "run_id", None))
        countries = [str(c).strip().upper() for c in (country_codes or [])
                     if str(c).strip()]

        logger.info("=" * 60)
        logger.info("CONSOLIDATING CDM%s", " (dry run)" if dry_run else "")
        logger.info("  run id:   %s", resolved_run_id or "(none)")
        logger.info("  registry: %s", (resolved_registry.digest()[:12]
                                       if resolved_registry is not None else "(none)"))
        logger.info("  countries: %s", countries or "(all)")
        logger.info("=" * 60)

        results: Dict[str, Any] = {
            "run_id": resolved_run_id,
            "registry_digest": (resolved_registry.digest()
                                if resolved_registry is not None else ""),
            "dry_run": bool(dry_run),
            "conflicts": 0,
        }

        logger.info("-" * 60)
        logger.info("FK: ALIGN FOREIGN KEYS")
        results["foreign_key_alignment"] = self.align_all_foreign_keys(dry_run)

        logger.info("-" * 60)
        logger.info("A6: CONSOLIDATE LOCATIONS")
        results["locations"] = self.consolidate_locations(
            gadm_gpkg=gadm_gpkg, country_codes=countries, dry_run=dry_run)

        logger.info("-" * 60)
        logger.info("A8: RECONCILE VOCABULARIES")
        results["vocabulary_standard"] = self.standardize_vocabularies(
            dry_run=dry_run, registry=resolved_registry, run_id=resolved_run_id)

        logger.info("-" * 60)
        logger.info("A9: ATTACH ADMIN CONCEPTS (spatial)")
        conflicting_sql = (
            "SELECT COUNT(*) FROM dim_location WHERE concept_id IS NOT NULL "
            "AND location_code IS NOT NULL "
            "AND (standard_code IS NULL OR standard_code <> location_code)")
        before_conflicts = 0 if dry_run else self._scalar_or_zero(conflicting_sql)
        admin_result: Dict[str, Any] = {}
        try:
            from utilities.admin_alignment import attach_admin_concepts
            admin_result = attach_admin_concepts(
                self.session, registry=resolved_registry, dry_run=dry_run) or {}
        except RuntimeError as exc:
            logger.warning("  spatial concept attachment skipped: %s", exc)
            admin_result = {"status": "skipped", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - geography is optional
            logger.warning("  spatial concept attachment failed: %s: %s",
                           type(exc).__name__, exc)
            admin_result = {"status": "error", "reason": str(exc)}
        results["admin_concepts"] = admin_result

        after_conflicts = 0 if dry_run else self._scalar_or_zero(conflicting_sql)
        results["conflicts"] = int(before_conflicts) + int(after_conflicts)
        if results["conflicts"]:
            logger.warning(
                "  C7 conflict count: %d location row(s) carried a concept the "
                "spatial pass did not produce. dim_location.concept_id has exactly "
                "one writer; check the location pack's canonical_rule.",
                results["conflicts"])

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
                        self._update_fact_references(
                            model_class, duplicate, canonical)
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
        burden_stats = {"inserted": 0, "errors": 0}
        if dry_run:
            logger.info("  (dry run) burden expansion skipped")
        else:
            try:
                burden_stats = self.expand_burden_to_daily()
            except Exception as exc:  # noqa: BLE001
                logger.error("  burden expansion failed: %s: %s",
                             type(exc).__name__, exc)
                burden_stats = {"inserted": 0, "errors": 1}
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
                    results["conflicts"], sum(results["remaining_orphans"].values()))
        logger.info("=" * 60)
        return results

    def summarize_locations(self, *, registry: Any = None,
                            sample_limit: int = 5) -> Dict[str, Any]:
        """Read-only description of ``dim_location``: coverage, hierarchy health,
        duplicate names. Duplicate counts are what a name-based merge *would*
        touch, not a recommendation."""
        resolved_registry = _load_registry(
            registry if registry is not None else getattr(self, "registry", None))
        pack = None
        if resolved_registry is not None:
            try:
                pack = _pack_from_policy(
                    resolved_registry,
                    resolved_registry.merge_policies().get("dim_location.name"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("no location merge policy for the summary: %s", exc)
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
        known_ids = {int(row["location_id"]) for row in rows}
        by_level: Dict[str, int] = {}
        concept_ids = standard_codes = half_concept = 0
        with_coords = half_coords = with_parent = missing_parent = self_parent = 0
        missing_country = 0
        country_codes: Dict[str, int] = {}
        name_groups: Dict[str, List[Any]] = {}
        unparented, conceptless, broken = [], [], []

        for row in rows:
            label = str(row["admin_level"] or "").strip()
            by_level[label] = by_level.get(label, 0) + 1

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
        ordered_levels = sorted(by_level, key=lambda l: (l == "", l))

        summary = {
            "rows": len(rows),
            "by_admin_level": {l: by_level[l] for l in ordered_levels},
            "concept_coverage": {
                "with_concept_id": concept_ids,
                "with_standard_code": standard_codes,
                "half_stamped": half_concept,
                "without_concept": len(rows) - concept_ids,
            },
            "coordinate_coverage": {
                "with_coordinates": with_coords,
                "half_coordinates": half_coords,
                "without_coordinates": len(rows) - with_coords - half_coords,
            },
            "hierarchy": {
                "with_parent": with_parent,
                "without_parent": len(rows) - with_parent,
                "missing_parent": missing_parent,
                "self_parent": self_parent,
                "broken_samples": broken,
            },
            "countries": {
                "distinct": len(country_codes),
                "by_code": {c: country_codes[c] for c in sorted(country_codes)},
                "without_country_code": missing_country,
            },
            "duplicate_names": {
                "group_count": len(duplicate_names),
                "row_count": sum(len(v) for v in duplicate_names.values()),
                "samples": [{"name_key": k, "count": len(duplicate_names[k]),
                             "location_ids": sorted(duplicate_names[k])[:limit]}
                            for k in sorted(duplicate_names)[:limit]],
            },
            "samples": {"unparented": unparented, "without_concept": conceptless},
            "pack": getattr(pack, "pack_id", None),
        }

        logger.info("LOCATION SUMMARY (%d row(s)): %d concept, %d complete coords, "
                    "%d parented (%d missing, %d self), %d duplicate name group(s)",
                    len(rows), concept_ids, with_coords, with_parent,
                    missing_parent, self_parent, len(duplicate_names))
        return summary

    def close(self) -> Dict[str, Any]:
        """Drain the pending resolution buffer, then close the session.

        Drain first (``load_resolutions`` opens its own sessions), commit nothing
        (every phase commits its own work; committing here would make an
        uncommitted merge permanent just for reaching a ``finally``), then close.
        A failed drain leaves the buffer in place and reports it.
        """
        if getattr(self, "_closed", False):
            return dict(getattr(self, "_close_report", {}) or {})

        buffer: List[Any] = []
        seen: Set[Tuple[str, str, str, str]] = set()

        def _offer(candidate: Any) -> None:
            if candidate is None:
                return
            key = (
                str(getattr(candidate, "source_table", "") or "").strip(),
                str(getattr(candidate, "source_column", "") or "").strip(),
                "" if getattr(candidate, "source_value", None) is None
                else str(candidate.source_value),
                str(getattr(candidate, "run_id", "") or "").strip(),
            )
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
                                  "closed": False, "errors": []}

        if buffer:
            try:
                from etl.loaders.db_loader import DBLoader
                report["flushed"] = int(
                    DBLoader(batch_size=1000, upsert=True).load_resolutions(buffer) or 0)
                report["pending"] = 0
                self._pending_resolutions = []
                logger.info("close: flushed %d resolution(s) into "
                            "fact_resolution_log / fact_concept_map",
                            report["flushed"])
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                report["errors"].append(message)
                logger.error("close: could not flush %d pending resolution(s) (%s). "
                             "They are KEPT so a retry is possible.",
                             len(buffer), message)
                self._pending_resolutions = list(buffer)

        session = getattr(self, "session", None)
        if session is not None:
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


# ---------------------------------------------------------------------------
# Module-level wrapper
# ---------------------------------------------------------------------------

def run_consolidation(core_cfg: Optional[Mapping[str, Any]] = None, *,
                      gadm_gpkg: Optional[str] = None,
                      country_codes: Optional[Iterable[str]] = None,
                      dry_run: bool = False, registry: Any = None,
                      run_id: Optional[str] = None,
                      close: bool = True) -> Dict[str, Any]:
    """Run the whole consolidation step and close the consolidator.

    ``close()`` is called from a ``finally`` so it runs on the failure path too:
    it is the only drain for the resolution buffer, and skipping it on an exception
    would lose exactly the evidence a failed run most needs.

    ``core.vocabulary.*`` keys other than ``gadm_gpkg`` are named in a warning and
    ignored — four of them looked like configuration and were read by nothing.
    """
    config = dict(core_cfg or {})
    admin_boundaries = config.get("admin_boundaries") or {}
    vocabulary = config.get("vocabulary") or {}

    boundary_path = gadm_gpkg or admin_boundaries.get("path") or None
    if not gadm_gpkg:
        for key in sorted(vocabulary, key=str):
            if key == "gadm_gpkg" and not boundary_path:
                boundary_path = vocabulary[key]
                logger.warning(
                    "core.vocabulary.gadm_gpkg is deprecated; move it to "
                    "core.admin_boundaries.path. Vocabulary selection is no "
                    "longer configurable.")
                continue
            logger.warning(
                "core.vocabulary.%s is IGNORED (value: %r). Vocabularies are "
                "chosen by the registry; nothing reads this key. Delete it.",
                key, vocabulary[key])

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
            gadm_gpkg=boundary_path,
            country_codes=normalised or None,
            dry_run=dry_run, registry=registry, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - a wrapper reports, it does not trace
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