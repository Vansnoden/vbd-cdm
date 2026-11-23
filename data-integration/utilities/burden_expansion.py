# -*- coding: utf-8 -*-
"""Burden expansion: policy, kernel, generator, writer."""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, text
from utilities.constants import (
    CASE_STATUS_ESTIMATE, ESTIMATE_METHOD, MAX_IN_VARS,
    POPULATION_INDICATOR,
)

from utilities.run_manifest import record_parameters as _record_manifest_parameters

logger = logging.getLogger(__name__)


DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "vocabularies" / "registry"
)

_REGISTRY_CACHE: dict = {}

_WARNED: set = set()


def _warn_once(marker: str, message: str, *args: Any) -> None:
    """Emit a warning the first time ``marker`` is seen."""
    if marker in _WARNED:
        return
    _WARNED.add(marker)
    logger.warning(message, *args)


def _load_registry(registry: Any = None) -> Any:
    """The registry handed in, or the shipped one, cached per path."""
    if registry is not None:
        return registry
    key = str(DEFAULT_REGISTRY_PATH)
    cached = _REGISTRY_CACHE.get(key)
    if cached is not None:
        return cached
    if not DEFAULT_REGISTRY_PATH.is_dir():
        _warn_once(
            "registry:missing",
            "no vocabulary registry at %s; the burden-expansion policy falls "
            "back to its built-in defaults.",
            DEFAULT_REGISTRY_PATH,
        )
        return None
    from utilities.vocabulary.registry import VocabularyRegistry

    loaded = VocabularyRegistry.load_from(DEFAULT_REGISTRY_PATH)
    _REGISTRY_CACHE[key] = loaded
    return loaded


BURDEN_KERNEL_KEY = "burden_expansion"

BURDEN_MEASURE_CASES_MEAN = "cases_mean"
BURDEN_MEASURE_INCIDENCE_RATE = "incidence_rate"
BURDEN_MEASURE_PREFERENCES = (BURDEN_MEASURE_CASES_MEAN,
                              BURDEN_MEASURE_INCIDENCE_RATE)

_RATE_SCALES: Dict[str, float] = {"per_100k": 100_000.0, "per_person": 1.0}

_BURDEN_KERNEL_DEFAULTS: Dict[str, Any] = {
    "intra_month_amplitude": 0.0,
    "base_variation": 0.0,
    "spike_probability": 0.0,
    "spike_mean_excess": 0.5,
}

_KERNEL_KEYS: Tuple[str, ...] = (
    "seasonality",
    "weekly_pattern",
    "holidays",
    "holiday_factors",
    "intra_month_amplitude",
    "base_variation",
    "spike_probability",
    "spike_mean_excess",
)

_POLICY_SECTIONS: Dict[str, Tuple[str, ...]] = {
    "measure": ("prefer", "rate_scale"),
    "denominator": ("indicator", "national_allocation", "default_population",
                    "divisor"),
    "write": ("grain", "seed", "force", "allow_mixed_grain",
              "max_projected_rows"),
    "selection": ("years", "locations", "limit"),
}

_BLOCK_DOCUMENTATION_KEYS: Tuple[str, ...] = ("description", "note", "source")

_HOLIDAY_KEY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

_BURDEN_LOG_SAMPLE = 10

_FETCH_ROWS = 1000

_COMMIT_ROWS = 50_000

_PROGRESS_EVERY = 500

DEFAULT_MAX_PROJECTED_ROWS = 5_000_000

_WEIGHT_CACHE_LIMIT = 512


def _positive(value: Any) -> Optional[float]:
    """``float(value)`` when it is a usable positive number, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _chunked_rows(session, sql: str, values: Sequence[Any],
                  params: Optional[Mapping[str, Any]] = None,
                  name: str = "vals",
                  chunk_size: int = MAX_IN_VARS) -> List[Any]:
    """Run an ``IN :vals`` query in chunks. Empty input returns ``[]``."""
    values = list(values)
    if not values:
        return []
    statement = text(sql).bindparams(bindparam(name, expanding=True))
    out: List[Any] = []
    for start in range(0, len(values), chunk_size):
        payload = dict(params or {})
        payload[name] = values[start:start + chunk_size]
        out.extend(session.execute(statement, payload).fetchall())
    return out


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class _BurdenKernel:
    """Weights that distribute an annual total across the days of a year."""

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
    """``{month: weight}`` from a mapping or a 12-element sequence."""
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
    """``{iso_weekday: weight}``. Monday is 1."""
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
    """``{'MM-DD': weight}``; a key that is not a calendar day is dropped."""
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


def _validate_kernel_block(block: Mapping[str, Any], label: str) -> None:
    """Reject any key this module does not consume."""
    allowed = (set(_KERNEL_KEYS) | set(_POLICY_SECTIONS)
               | set(_BLOCK_DOCUMENTATION_KEYS))
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ValueError(
            f"{label}: `{BURDEN_KERNEL_KEY}` has unknown key(s) {unknown}. "
            f"Shape keys: {', '.join(_KERNEL_KEYS)}. "
            f"Policy sections: {', '.join(sorted(_POLICY_SECTIONS))}. "
            f"Documentation keys: {', '.join(_BLOCK_DOCUMENTATION_KEYS)}.")


def _diagnose_missing_kernel_pack(loaded: Any) -> str:
    """Explain a pack that declares the weights but not the block around them."""
    try:
        packs = list(loaded.parameter_packs())
    except Exception:  # noqa: BLE001
        return ""
    offenders = [
        pack for pack in packs
        if "seasonality" in (getattr(pack, "parameters", None) or {})
        and BURDEN_KERNEL_KEY not in (getattr(pack, "parameters", None) or {})
    ]
    if not offenders:
        return ""
    names = ", ".join(
        f"{getattr(pack, 'pack_id', '?')!r} (v{getattr(pack, 'version', '?')})"
        for pack in offenders)
    return (
        f" Pack(s) {names} declare `seasonality` at the top level, but the "
        f"loader only reads a single top-level {BURDEN_KERNEL_KEY!r} mapping. "
        f"Everything outside it is ignored. Indent the weights under "
        f"`{BURDEN_KERNEL_KEY}:`."
    )


def _load_kernel_pack(registry: Any = None
                      ) -> Tuple[Any, Optional[Mapping[str, Any]]]:
    """``(pack, block)`` for the burden-expansion pack, or ``(None, None)``."""
    loaded = _load_registry(registry)
    if loaded is None:
        return None, None
    try:
        pack = loaded.find_parameter_pack(BURDEN_KERNEL_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("  burden-expansion: cannot resolve %r (%s: %s); the "
                       "built-in defaults apply.", BURDEN_KERNEL_KEY,
                       type(exc).__name__, exc)
        return None, None
    if pack is None:
        return None, None
    block = pack.parameters.get(BURDEN_KERNEL_KEY)
    if not isinstance(block, Mapping):
        raise ValueError(
            f"parameter pack {pack.vocabulary_id!r} declares "
            f"{BURDEN_KERNEL_KEY!r} as {type(block).__name__}; expected a "
            f"mapping of allocation keys and policy sections.")
    _validate_kernel_block(block, str(pack.vocabulary_id))
    return pack, block


def load_burden_kernel(registry: Any = None) -> _BurdenKernel:
    """The allocation weights in force; absence means uniform allocation."""
    loaded = _load_registry(registry)
    if loaded is None:
        logger.info("  burden kernel: no registry, allocating uniformly "
                    "(origin=flat).")
        return FLAT_BURDEN_KERNEL

    pack, block = _load_kernel_pack(loaded)
    if pack is None:
        logger.info(
            "  burden kernel: no parameter pack declares a top-level %r block, so "
            "allocation is uniformly flat. Ship "
            "vocabularies/registry/burden-expansion/manifest.yaml with the weights "
            "nested under `%s:`.%s",
            BURDEN_KERNEL_KEY, BURDEN_KERNEL_KEY,
            _diagnose_missing_kernel_pack(loaded))
        return FLAT_BURDEN_KERNEL

    if "seasonality" not in block:
        logger.info("  burden kernel: %s declares no seasonality, so allocation "
                    "is uniformly flat by declaration (origin=flat).",
                    pack.vocabulary_id)
        return FLAT_BURDEN_KERNEL

    monthly = _kernel_month_map(block.get("seasonality"))
    if not monthly:
        logger.warning("  burden kernel: pack %r declares no usable `seasonality` "
                       "block (every weight is absent, zero or negative); "
                       "allocating uniformly.", pack.pack_id)
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
        source=(str(block.get("source")
                    or pack.parameters.get("source") or "").strip() or None),
    )
    logger.info("  burden kernel: %s (%d month(s), %d weekday(s), %d holiday(s), "
                "variation %.3f%s)", kernel.origin, len(kernel.monthly),
                len(kernel.weekly), len(kernel.holidays), kernel.base_variation,
                "" if kernel.source else "; NO SOURCE CITED - a seasonal claim "
                "from this series would be unsupported")
    return kernel


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ExpansionPolicy:
    """Every knob the expansion reads, resolved from the pack."""

    prefer: str
    rate_scale: str
    indicator: str
    national_allocation: bool
    default_population: Optional[float]
    divisor: Optional[float]
    grain: Optional[str]
    seed: int
    force: bool
    allow_mixed_grain: bool
    max_projected_rows: int
    years: Optional[Tuple[int, ...]]
    locations: Optional[Tuple[int, ...]]
    limit: Optional[int]
    origin: str
    pack_id: Optional[str]
    version: Optional[str]
    declared: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        """A JSON-serialisable summary for the run manifest."""
        return {
            "origin": self.origin,
            "pack_id": self.pack_id,
            "version": self.version,
            "declared_keys": list(self.declared),
            "measure": {"prefer": self.prefer, "rate_scale": self.rate_scale},
            "denominator": {
                "indicator": self.indicator,
                "national_allocation": self.national_allocation,
                "default_population": self.default_population,
                "divisor": self.divisor,
            },
            "write": {
                "grain": self.grain,
                "seed": self.seed,
                "force": self.force,
                "allow_mixed_grain": self.allow_mixed_grain,
                "max_projected_rows": self.max_projected_rows,
            },
            "selection": {
                "years": list(self.years) if self.years else None,
                "locations": list(self.locations) if self.locations else None,
                "limit": self.limit,
            },
        }


def _block_mapping(block: Mapping[str, Any], key: str, allowed: Sequence[str],
                   label: str) -> Mapping[str, Any]:
    """The named sub-mapping of ``block``, or ``{}`` when absent."""
    raw = block.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"{label}: `{key}` must be a mapping, got {type(raw).__name__}")
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(
            f"{label}: `{key}` has unknown key(s) {unknown}; "
            f"allowed: {', '.join(allowed)}")
    return raw


def _policy_choice(value: Any, label: str, allowed: Sequence[str]) -> str:
    text_value = str(value).strip()
    if text_value not in allowed:
        raise ValueError(f"{label}: {value!r} is not one of {', '.join(allowed)}")
    return text_value


def _policy_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label}: expected true or false, got {value!r}")


def _policy_positive(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}: expected a positive number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: expected a positive number, "
                         f"got {value!r}") from None
    if not number > 0:
        raise ValueError(f"{label}: expected a positive number, got {value!r}")
    return number


def _policy_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}: expected an integer, got {value!r}")
    return int(value)


def _policy_int_tuple(value: Any, label: str) -> Tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label}: expected a list of integers, got {value!r}")
    numbers = sorted({_policy_int(item, label) for item in value})
    if not numbers:
        raise ValueError(
            f"{label}: an empty list selects nothing; use null instead")
    return tuple(numbers)


def _read_expansion_policy(block: Mapping[str, Any], *, origin: str,
                           pack_id: Optional[str], version: Optional[str],
                           label: str) -> _ExpansionPolicy:
    """Validate the four policy sections into one immutable record."""
    sections = {
        "measure": _block_mapping(block, "measure",
                                  _POLICY_SECTIONS["measure"], label),
        "denominator": _block_mapping(block, "denominator",
                                      _POLICY_SECTIONS["denominator"], label),
        "write": _block_mapping(block, "write",
                                _POLICY_SECTIONS["write"], label),
        "selection": _block_mapping(block, "selection",
                                    _POLICY_SECTIONS["selection"], label),
    }
    declared: List[str] = []

    def _note(section: str, key: str) -> bool:
        """Whether ``section.key`` was declared, recording it if so."""
        if key in sections[section]:
            declared.append(f"{section}.{key}")
            return True
        return False

    prefer = BURDEN_MEASURE_CASES_MEAN
    if _note("measure", "prefer"):
        prefer = _policy_choice(sections["measure"]["prefer"],
                                f"{label}: measure.prefer",
                                BURDEN_MEASURE_PREFERENCES)

    rate_scale = "per_100k"
    if _note("measure", "rate_scale"):
        rate_scale = _policy_choice(sections["measure"]["rate_scale"],
                                    f"{label}: measure.rate_scale",
                                    tuple(sorted(_RATE_SCALES)))

    indicator = "SP.POP.TOTL"
    if _note("denominator", "indicator"):
        indicator = str(sections["denominator"]["indicator"]).strip()
        if not indicator:
            raise ValueError(f"{label}: denominator.indicator is empty")

    national_allocation = False
    if _note("denominator", "national_allocation"):
        national_allocation = _policy_bool(
            sections["denominator"]["national_allocation"],
            f"{label}: denominator.national_allocation")

    default_population: Optional[float] = None
    if _note("denominator", "default_population"):
        raw = sections["denominator"]["default_population"]
        if raw is not None:
            default_population = _policy_positive(
                raw, f"{label}: denominator.default_population")

    divisor: Optional[float] = None
    if _note("denominator", "divisor"):
        raw = sections["denominator"]["divisor"]
        if raw is not None:
            divisor = _policy_positive(raw, f"{label}: denominator.divisor")

    grain: Optional[str] = None
    if _note("write", "grain"):
        raw = sections["write"]["grain"]
        grain = None if raw is None else (str(raw).strip() or None)

    seed = 42
    if _note("write", "seed"):
        seed = _policy_int(sections["write"]["seed"], f"{label}: write.seed")
        if seed < 0:
            raise ValueError(f"{label}: write.seed must be >= 0, got {seed}")

    force = False
    if _note("write", "force"):
        force = _policy_bool(sections["write"]["force"], f"{label}: write.force")

    allow_mixed_grain = False
    if _note("write", "allow_mixed_grain"):
        allow_mixed_grain = _policy_bool(
            sections["write"]["allow_mixed_grain"],
            f"{label}: write.allow_mixed_grain")

    max_projected_rows = DEFAULT_MAX_PROJECTED_ROWS
    if _note("write", "max_projected_rows"):
        max_projected_rows = _policy_int(
            sections["write"]["max_projected_rows"],
            f"{label}: write.max_projected_rows")
        if max_projected_rows <= 0:
            raise ValueError(
                f"{label}: write.max_projected_rows must be positive, got "
                f"{max_projected_rows}")

    years: Optional[Tuple[int, ...]] = None
    if _note("selection", "years") and sections["selection"]["years"] is not None:
        years = _policy_int_tuple(sections["selection"]["years"],
                                  f"{label}: selection.years")

    locations: Optional[Tuple[int, ...]] = None
    if (_note("selection", "locations")
            and sections["selection"]["locations"] is not None):
        locations = _policy_int_tuple(sections["selection"]["locations"],
                                      f"{label}: selection.locations")

    limit: Optional[int] = None
    if _note("selection", "limit") and sections["selection"]["limit"] is not None:
        limit = _policy_int(sections["selection"]["limit"],
                            f"{label}: selection.limit")
        if limit <= 0:
            raise ValueError(f"{label}: selection.limit must be positive")

    return _ExpansionPolicy(
        prefer=prefer, rate_scale=rate_scale, indicator=indicator,
        national_allocation=national_allocation,
        default_population=default_population, divisor=divisor,
        grain=grain, seed=seed, force=force,
        allow_mixed_grain=allow_mixed_grain,
        max_projected_rows=max_projected_rows,
        years=years, locations=locations, limit=limit,
        origin=origin, pack_id=pack_id, version=version,
        declared=tuple(sorted(declared)),
    )


def default_expansion_policy() -> _ExpansionPolicy:
    """The built-in defaults: exactly the behaviour before the pack existed."""
    return _ExpansionPolicy(
        prefer=BURDEN_MEASURE_CASES_MEAN,
        rate_scale="per_100k",
        indicator="SP.POP.TOTL",
        national_allocation=False,
        default_population=None,
        divisor=None,
        grain=None,
        seed=42,
        force=False,
        allow_mixed_grain=False,
        max_projected_rows=DEFAULT_MAX_PROJECTED_ROWS,
        years=None,
        locations=None,
        limit=None,
        origin="built-in defaults",
        pack_id=None,
        version=None,
    )


def load_expansion_policy(registry: Any = None) -> _ExpansionPolicy:
    """The expansion policy in force; absence means the built-in defaults."""
    fallback = default_expansion_policy()

    loaded = _load_registry(registry)
    if loaded is None:
        logger.info("  expansion policy: no registry, using the built-in defaults "
                    "(measure %s, rate scale per_100k, national allocation OFF).",
                    fallback.prefer)
        return fallback

    pack, block = _load_kernel_pack(loaded)
    if pack is None:
        logger.info(
            "  expansion policy: no `%s` parameter pack, using the built-in "
            "defaults (measure %s, rate scale per_100k, national allocation "
            "OFF).%s", BURDEN_KERNEL_KEY, fallback.prefer,
            _diagnose_missing_kernel_pack(loaded))
        return fallback

    policy = _read_expansion_policy(
        block, origin=f"pack:{pack.vocabulary_id}", pack_id=pack.pack_id,
        version=pack.version, label=str(pack.vocabulary_id))

    logger.info(
        "  expansion policy: %s - measure %s (rate scale %r), denominator %r "
        "(national allocation %s%s), grain %s, seed %s, force %s%s; %d key(s) "
        "declared in the manifest",
        policy.origin, policy.prefer, policy.rate_scale, policy.indicator,
        "ON" if policy.national_allocation else "OFF",
        (f", divisor {policy.divisor:g}" if policy.divisor is not None
         else f", default population {policy.default_population:,.0f}"
         if policy.default_population is not None else ""),
        policy.grain or "per grid cell", policy.seed,
        "ON" if policy.force else "OFF",
        ", mixed grains allowed" if policy.allow_mixed_grain else "",
        len(policy.declared))
    return policy


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def _stable_location_key(location_code: Any, name: Any = None) -> int:
    """A 32-bit key from a location's own identifier, not its surrogate."""
    for candidate in (location_code, name):
        text_value = str(candidate or "").strip()
        if text_value:
            digest = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
            return int(digest[:8], 16)
    return 0


@dataclass(frozen=True)
class _YearGrid:
    """Calendar facts for one year, so a series never walks a pandas index."""

    date_ids: Tuple[str, ...]
    months: Tuple[int, ...]
    weekdays: Tuple[int, ...]
    month_days: Tuple[str, ...]
    days: Tuple[int, ...]
    month_lengths: Tuple[int, ...]


_GRID_CACHE: Dict[int, _YearGrid] = {}
_WEIGHT_CACHE: Dict[Tuple[int, tuple], Tuple[float, ...]] = {}


def _days_in_year(year: int) -> int:
    """366 in a leap year, else 365."""
    return 366 if calendar.isleap(int(year)) else 365


def _year_grid(year: int) -> _YearGrid:
    """Calendar data for ``year``, built once and cached."""
    key = int(year)
    cached = _GRID_CACHE.get(key)
    if cached is not None:
        return cached
    dates = pd.date_range(datetime(key, 1, 1), datetime(key, 12, 31))
    months = tuple(int(date.month) for date in dates)
    grid = _YearGrid(
        date_ids=tuple(date.strftime("%Y-%m-%d") for date in dates),
        months=months,
        weekdays=tuple(int(date.isocalendar()[2]) for date in dates),
        month_days=tuple(date.strftime("%m-%d") for date in dates),
        days=tuple(int(date.day) for date in dates),
        month_lengths=tuple(
            calendar.monthrange(key, month)[1] for month in months),
    )
    _GRID_CACHE[key] = grid
    return grid


def _kernel_fingerprint(kernel: _BurdenKernel) -> tuple:
    """The kernel fields that change a day's weight, in hashable form."""
    return (
        tuple(sorted(kernel.monthly.items())),
        tuple(sorted(kernel.weekly.items())),
        tuple(sorted(kernel.holidays.items())),
        float(kernel.intra_amplitude),
    )


def _base_weights(year: int, kernel: _BurdenKernel) -> Tuple[float, ...]:
    """Per-day weights from the kernel alone, cached per (year, kernel)."""
    key = (int(year), _kernel_fingerprint(kernel))
    cached = _WEIGHT_CACHE.get(key)
    if cached is not None:
        return cached

    grid = _year_grid(year)
    weights: List[float] = []
    for index, month in enumerate(grid.months):
        weight = float(kernel.monthly.get(month, 1.0))
        weight *= float(kernel.weekly.get(grid.weekdays[index], 1.0))
        if kernel.intra_amplitude > 0:
            phase = ((grid.days[index] / max(grid.month_lengths[index], 1))
                     * 2 * np.pi)
            weight *= 1.0 + kernel.intra_amplitude * np.sin(phase + month * 0.5)
        weight *= float(kernel.holidays.get(grid.month_days[index], 1.0))
        weights.append(weight)

    if len(_WEIGHT_CACHE) >= _WEIGHT_CACHE_LIMIT:
        _WEIGHT_CACHE.clear()
    frozen = tuple(weights)
    _WEIGHT_CACHE[key] = frozen
    return frozen


def _apply_deviation(base: Tuple[float, ...], variation: float,
                     kernel: _BurdenKernel, rng: Any) -> List[float]:
    """Multiply in the seeded deviation and any spike, in the original draw order."""
    if variation <= 0:
        if kernel.spike_probability <= 0:
            return list(base)
        weights: List[float] = []
        for weight in base:
            value = float(weight)
            if rng.random() < kernel.spike_probability:
                value *= 1.0 + float(rng.exponential(kernel.spike_mean_excess))
            weights.append(value)
        return weights

    if kernel.spike_probability <= 0:
        noise = rng.lognormal(0.0, variation * 0.7, size=len(base))
        return (np.asarray(base, dtype=float) * noise).tolist()

    weights = []
    for weight in base:
        value = float(weight) * float(rng.lognormal(0.0, variation * 0.7))
        if rng.random() < kernel.spike_probability:
            value *= 1.0 + float(rng.exponential(kernel.spike_mean_excess))
        weights.append(value)
    return weights


def project_case_rows(groups_by_year: Mapping[int, int]) -> int:
    """Daily rows a write would produce: one series per group, leap-adjusted."""
    return sum(_days_in_year(year) * int(count)
               for year, count in groups_by_year.items())


def guard_projected_volume(groups_by_year: Mapping[int, int], *,
                           grain_label: str,
                           max_projected_rows: int) -> int:
    """Refuse a run whose projection exceeds the cap, naming both levers."""
    projected = project_case_rows(groups_by_year)
    limit = int(max_projected_rows)
    if projected <= limit:
        return projected
    series = sum(int(count) for count in groups_by_year.values())
    raise RuntimeError(
        f"burden expansion projects {projected:,} daily case rows across "
        f"{series:,} series at the current grain ({grain_label}), above "
        f"max_projected_rows = {limit:,}. Lower it with "
        f"burden_expansion.write.grain (write at a coarser location) or "
        f"burden_expansion.selection.years (expand fewer years). "
        f"Nothing was written.")


def generate_daily_case_counts(
    annual_cases: float, year: int, location_id: int,
    base_variation: Optional[float] = None, seed: Optional[int] = None,
    *,
    kernel: Optional[_BurdenKernel] = None,
    stable_key: Optional[int] = None,
) -> pd.DataFrame:
    """Daily series whose sum equals ``round(annual_cases)``, exactly.

    Weights are cached per (year, kernel); only the seeded deviation is per call.
    """
    active = kernel or FLAT_BURDEN_KERNEL
    variation = (active.base_variation if base_variation is None
                 else float(base_variation))

    key = int(location_id if stable_key is None else stable_key)
    rng = np.random.default_rng(
        np.random.SeedSequence([0 if seed is None else int(seed), key, int(year)]))

    grid = _year_grid(year)
    weights = _apply_deviation(_base_weights(year, active), variation, active, rng)

    total = sum(weights)
    shares = ([weight / total for weight in weights] if total > 0
              else [1.0 / len(weights)] * len(weights))

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
        "date_id": list(grid.date_ids),
        "year": int(year),
        "month": list(grid.months),
        "case_count": counts,
        "location_id": location_id,
    })


# ---------------------------------------------------------------------------
# The population index
# ---------------------------------------------------------------------------


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
    """Population denominators, per location and per country."""

    _LEVEL_RANK: Mapping[str, int] = {
        "country": 0, "nation": 0,
        "admin1": 1, "region": 1, "province": 1, "county": 1, "state": 1,
        "admin2": 2, "district": 2,
        "admin3": 3, "ward": 3, "division": 3,
        "admin4": 4, "admin5": 5, "admin6": 6,
        "site": 7, "occurrence": 8, "grid": 9, "cell": 9, "pixel": 9,
    }

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

    @classmethod
    def _rank(cls, label: Any) -> int:
        return cls._LEVEL_RANK.get(str(label or "").strip().lower(), 5)

    def load(self) -> "_PopulationIndex":
        """Read the population rows and the location->country map. Idempotent."""
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

    @staticmethod
    def _nearest_year(series: Mapping[int, float],
                      year: Optional[int]) -> Optional[int]:
        """The year closest to ``year``; ties go to the earlier year."""
        if not series:
            return None
        if year is None:
            return max(series)
        return min(series, key=lambda candidate: (abs(candidate - year), candidate))

    def resolve(self, location_id: Any, year: Any, *,
                allow_country: bool = False) -> Optional[_PopulationHit]:
        """The denominator for one (location, year), or None."""
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


# ---------------------------------------------------------------------------
# dim_time
# ---------------------------------------------------------------------------


def ensure_dim_time(session, year: int) -> int:
    """Insert missing ``dim_time`` rows for ``year`` (``date_id`` is an FK).

    ``season`` is written as NULL: a season label needs a per-country climatology,
    and a single global dry/wet split is wrong for bimodal regimes.
    """
    existing = {row[0] for row in session.execute(text(
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
        session.execute(text(
            "INSERT INTO dim_time (date_id, date, year, month, week, "
            "day_of_year, season, is_holiday) VALUES (:date_id, :date, "
            ":year, :month, :week, :day_of_year, NULL, 0)"), payload)
        session.commit()
        logger.debug("  dim_time: added %d row(s) for %s", len(payload), year)
        _warn_once(
            "dim_time:season",
            "  dim_time.season is not populated: a season label needs a "
            "per-country climatology, and a single global dry/wet split is wrong "
            "for bimodal regimes and for either hemisphere. The column is left "
            "NULL rather than filled with an unsupported value.")
    return len(payload)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


class BurdenExpander:
    """One burden-expansion pass over one session."""

    def __init__(self, session, *, registry: Any = None,
                 run_id: Optional[str] = None) -> None:
        self.session = session
        self.registry = registry
        self.run_id = run_id

    def _scalar_or_zero(self, sql: str,
                        params: Optional[Mapping[str, Any]] = None) -> int:
        """A ``COUNT(*)`` scalar, or 0 when the query cannot run."""
        try:
            return int(self.session.execute(
                text(sql), dict(params or {})).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("diagnostic count failed (%s): %s", sql, exc)
            return 0

    def burden_locations_per_country(self,
                                     index: _PopulationIndex) -> Dict[str, int]:
        """How many distinct burden locations each country holds."""
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

    def ancestor_map(self, target_level: str, *, grid_level: str = "grid",
                     max_hops: int = 8) -> Dict[int, int]:
        """``grid cell -> its nearest ancestor at target_level``."""
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

    def location_stable_keys(self) -> Dict[int, int]:
        """``location_id -> stable seed key`` for the whole location dimension."""
        keys: Dict[int, int] = {}
        try:
            rows = self.session.execute(text(
                "SELECT location_id, location_code, name FROM dim_location "
                "ORDER BY location_id")).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("  could not read location codes (%s); the daily "
                           "series will be seeded on the surrogate key, which is "
                           "not stable across a rebuild.", exc)
            return keys
        for location_id, location_code, name in rows:
            if location_id is None:
                continue
            keys[int(location_id)] = _stable_location_key(location_code, name)
        return keys

    def expected_admin_levels(self,
                              case_location_of: Mapping[int, int]) -> Set[str]:
        """The ``admin_level`` values the requested write grain produces."""
        if not case_location_of:
            return {"grid"}
        target_ids = sorted(set(case_location_of.values()))
        levels: Set[str] = set()
        try:
            for _location_id, admin_level in _chunked_rows(
                    self.session,
                    "SELECT location_id, admin_level FROM dim_location "
                    "WHERE location_id IN :vals ORDER BY location_id",
                    target_ids):
                levels.add(str(admin_level or "").strip().lower() or "(null)")
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read the target admin levels: %s", exc)
        return levels or {"(unknown)"}

    def guard_write_grain(self, first_year: int, last_year: int,
                          expected_levels: Set[str], grain_label: str,
                          *, force: bool, allow_mixed_grain: bool) -> int:
        """Refuse, or purge, when ESTIMATE rows at another grain exist."""
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
                "Proceeding because write.allow_mixed_grain is set.", detail,
                first_year, last_year, grain_label,
                ", ".join(sorted(expected_levels)))
            return 0

        if not force:
            raise RuntimeError(
                f"fact_disease_case already holds ESTIMATE rows at {detail} over "
                f"{first_year}-{last_year}, while this run writes at {grain_label} "
                f"({', '.join(sorted(expected_levels))}). Mixed grains co-exist "
                f"without violating a constraint and silently double-count "
                f"downstream. Re-run with write.force=true (or --force) to replace "
                f"them, or write.allow_mixed_grain=true if the mixture is "
                f"intended.")

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

    def record_parameters(self, run_id: Optional[str], stats: Mapping[str, Any],
                          kernel: _BurdenKernel, policy: _ExpansionPolicy,
                          prefer: str, rate_scale: str,
                          national_allocation: bool,
                          population_divisor: Optional[float],
                          population_indicator: str,
                          default_population: Optional[float],
                          seed: int, group_by_level: Optional[str]) -> None:
        """Write the resolved policy to ``run_manifest``. Never raises."""
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
                    "policy": policy.describe(),
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

    def expand(
        self,
        force: Optional[bool] = None,
        seed: Optional[int] = None,
        base_variation: Optional[float] = None,
        *,
        default_population: Optional[float] = None,
        population_indicator: Optional[str] = None,
        national_allocation: Optional[bool] = None,
        population_divisor: Optional[float] = None,
        rate_scale: Optional[str] = None,
        prefer: Optional[str] = None,
        group_by_level: Optional[str] = None,
        locations: Optional[Iterable[int]] = None,
        years: Optional[Iterable[int]] = None,
        limit: Optional[int] = None,
        allow_mixed_grain: Optional[bool] = None,
        max_projected_rows: Optional[int] = None,
        dry_run: bool = False,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Expand yearly burden into daily cases summing to each annual total.

        Records sharing one (location, year, disease, source) are summed first, since
        uq_fact_case_natural has no pathogen column to tell them apart.
        """
        policy = load_expansion_policy(self.registry)
        kernel = load_burden_kernel(self.registry)

        prefer = policy.prefer if prefer is None else prefer
        rate_scale = policy.rate_scale if rate_scale is None else rate_scale
        population_indicator = (policy.indicator if population_indicator is None
                                else population_indicator)
        national_allocation = (policy.national_allocation
                               if national_allocation is None
                               else national_allocation)
        default_population = (policy.default_population
                              if default_population is None
                              else default_population)
        population_divisor = (policy.divisor if population_divisor is None
                              else population_divisor)
        group_by_level = (policy.grain if group_by_level is None
                          else group_by_level)
        seed = policy.seed if seed is None else seed
        force = policy.force if force is None else force
        allow_mixed_grain = (policy.allow_mixed_grain
                             if allow_mixed_grain is None
                             else allow_mixed_grain)
        max_projected_rows = (policy.max_projected_rows
                              if max_projected_rows is None
                              else max_projected_rows)
        if years is None:
            years = policy.years
        if locations is None:
            locations = policy.locations
        if limit is None:
            limit = policy.limit

        if prefer not in BURDEN_MEASURE_PREFERENCES:
            raise ValueError(
                f"prefer must be one of {list(BURDEN_MEASURE_PREFERENCES)}, got "
                f"{prefer!r} (from {policy.origin})")
        if rate_scale not in _RATE_SCALES:
            raise ValueError(
                f"rate_scale must be one of {sorted(_RATE_SCALES)}, got "
                f"{rate_scale!r} (from {policy.origin})")
        if limit is not None:
            limit = max(1, int(limit))
        scale = float(_RATE_SCALES[rate_scale])
        effective_run_id = run_id or self.run_id

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

        if dry_run:
            logger.info("  dry run: the ESTIMATE method row and everything below "
                        "is computed, nothing is written")
        else:
            self.session.execute(text(
                "INSERT INTO dim_identification_method "
                "(method_code, method_name, method_type) "
                f"VALUES ('{ESTIMATE_METHOD}', 'Estimated', 'modeled') "
                "ON CONFLICT (method_code) DO NOTHING"))
            self.session.commit()

        empty: Dict[str, Any] = {
            "inserted": 0, "already_present": 0, "skipped": 0,
            "no_population": 0, "errors": 0, "records": 0, "groups": 0,
            "merged": 0, "zero": 0, "years_prepared": 0, "tiers": {},
            "basis": {}, "grain": "grid cell", "grain_targets": 0,
            "unparented_cells": 0, "kernel": kernel.origin,
            "policy": policy.describe(), "purged": 0}

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

        index = _PopulationIndex(self.session, population_indicator).load()
        divisors = (self.burden_locations_per_country(index)
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
                case_location_of = self.ancestor_map(requested)
                grain_label = f"admin_level={requested}"
            if case_location_of:
                logger.info("  writing at %s: %d cell(s) map to %d target(s)",
                            grain_label, len(case_location_of),
                            len(set(case_location_of.values())))
            elif requested not in ("", "none", "cell", "grid"):
                logger.warning(
                    "  grouping to %r mapped no grid cell; writing per cell "
                    "instead. A grain of 'grid' forces per-cell explicitly.",
                    requested)

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

        clause = " AND ".join(where)
        selected_sql = (f"SELECT year FROM fact_disease_burden WHERE {clause}"
                        + (" ORDER BY burden_id LIMIT :lim"
                           if limit is not None else ""))
        if limit is not None:
            params["lim"] = int(limit)
        bounds = self.session.execute(text(
            f"SELECT COUNT(*), MIN(year), MAX(year) FROM ({selected_sql}) AS s"),
            params).first()
        available = 0 if bounds is None else int(bounds[0] or 0)
        if not available:
            logger.warning("No burden records matched the selected filters.")
            return empty

        first_year = None if bounds[1] is None else int(bounds[1])
        last_year = None if bounds[2] is None else int(bounds[2])

        purged = 0
        if first_year is not None:
            purged = self.guard_write_grain(
                first_year, last_year,
                self.expected_admin_levels(case_location_of), grain_label,
                force=force and not dry_run,
                allow_mixed_grain=allow_mixed_grain or dry_run)

        select_sql = (
            "SELECT burden_id, location_id, year, disease_code, source_code, "
            "       incidence_rate, cases_mean FROM fact_disease_burden "
            f"WHERE {clause} ORDER BY burden_id")
        if limit is not None:
            select_sql += " LIMIT :lim"

        annual_by_key: Dict[Tuple[int, int, str, str], float] = {}
        groups_by_year: Dict[int, int] = {}
        tier_counts: Dict[str, int] = {}
        basis_counts: Dict[str, int] = {}
        prepared_years: Set[int] = set()
        unparented_cells: Set[int] = set()
        skipped = no_population = failures = records = 0
        logged_skips = logged_allocations = 0
        location_keys = self.location_stable_keys()

        def _prepare_year(year_value: int) -> bool:
            if year_value in prepared_years:
                return True
            if dry_run:
                prepared_years.add(int(year_value))
                return True
            try:
                ensure_dim_time(self.session, int(year_value))
            except Exception as exc:  # noqa: BLE001
                logger.error("  could not extend dim_time to %s: %s",
                             year_value, exc)
                return False
            prepared_years.add(int(year_value))
            return True

        cursor = self.session.execute(text(select_sql), params).yield_per(
            _FETCH_ROWS)
        for (burden_id, location_id, year, disease_code,
             source_code, incidence_rate, cases_mean) in cursor:
            records += 1
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

            if prefer == BURDEN_MEASURE_INCIDENCE_RATE:
                use_rate = rate_value is not None
                use_mean = (not use_rate) and mean_value is not None
            else:
                use_mean = mean_value is not None
                use_rate = (not use_mean) and rate_value is not None

            if not use_rate and not use_mean:
                skipped += 1
                continue

            population: Optional[float] = None
            tier = BURDEN_MEASURE_CASES_MEAN if use_mean else "none"

            if use_rate:
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
                            else " (national allocation is OFF - set "
                                 "denominator.national_allocation, or run "
                                 "fix_cases.py --national-allocation)")
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
            if key not in annual_by_key:
                groups_by_year[year_key] = groups_by_year.get(year_key, 0) + 1
            annual_by_key[key] = annual_by_key.get(key, 0.0) + annual_cases
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            basis = (BURDEN_MEASURE_CASES_MEAN if use_mean
                     else BURDEN_MEASURE_INCIDENCE_RATE)
            basis_counts[basis] = basis_counts.get(basis, 0) + 1

        # failures counts records that reached a measure but could not be prepared.
        merged = max(0, records - skipped - no_population - failures
                     - len(annual_by_key))
        if merged > 0:
            logger.info(
                "  %d burden record(s) shared a case key with another and were "
                "SUMMED: uq_fact_case_natural has no pathogen column, so one "
                "daily series per (location, year, disease, source) is the only "
                "shape fact_disease_case can hold.", merged)

        stats: Dict[str, Any] = {
            "inserted": 0, "already_present": 0, "skipped": skipped,
            "no_population": no_population, "errors": failures,
            "records": records, "groups": len(annual_by_key),
            "merged": max(0, merged), "zero": 0,
            "years_prepared": len(prepared_years), "tiers": tier_counts,
            "basis": basis_counts, "grain": grain_label,
            "grain_targets": len(set(case_location_of.values())),
            "unparented_cells": len(unparented_cells),
            "kernel": kernel.origin, "policy": policy.describe(),
            "purged": purged, "dry_run": False, "case_rows": 0}

        if not annual_by_key:
            logger.warning(
                "Nothing to expand: %d record(s) examined, %d had no denominator, "
                "%d were skipped for a missing location/year/disease.",
                records, no_population, skipped)
            self.record_parameters(
                effective_run_id, stats, kernel, policy, prefer, rate_scale,
                national_allocation, population_divisor, population_indicator,
                default_population, seed, group_by_level)
            return stats

        guard_projected_volume(groups_by_year, grain_label=grain_label,
                               max_projected_rows=int(max_projected_rows))

        projected_rows = guard_projected_volume(
            groups_by_year, grain_label=grain_label,
            max_projected_rows=int(max_projected_rows))

        if dry_run:
            stats["dry_run"] = True
            stats["case_rows"] = projected_rows
            logger.info(
                "  dry run: %d series over %s at %s would write %d daily row(s). "
                "Nothing was written and run_manifest is not updated.",
                len(annual_by_key),
                ", ".join(str(y) for y in sorted(groups_by_year)), grain_label,
                projected_rows)
            return stats

        existing_keys = {
            (int(key_location), int(key_year), str(key_disease),
             "" if key_source is None else str(key_source))
            for key_location, key_year, key_disease, key_source
            in self.session.execute(text(
                "SELECT location_id, "
                "       CAST(SUBSTR(date_id_onset, 1, 4) AS INTEGER) AS yr, "
                "       disease_code, source_code "
                "FROM fact_disease_case "
                "WHERE identification_method_code = 'ESTIMATE'")
            ).yield_per(_FETCH_ROWS)}

        statement = text(
            "INSERT INTO fact_disease_case ("
            "  location_id, date_id_onset, date_id_reported, disease_code, "
            "  identification_method_code, source_code, case_status, host_type, "
            "  age_group, sex, outcome, case_count"
            ") VALUES ("
            "  :loc_id, :date_id, :date_id, :disease, 'ESTIMATE', :src_code, "
            "  'estimated', 'human', '', '', 'unknown', :case_count)")

        window_sql = text(
            "DELETE FROM fact_disease_case "
            "WHERE location_id = :loc_id "
            "AND date_id_onset >= :start_date AND date_id_onset <= :end_date "
            "AND disease_code = :disease "
            "AND identification_method_code = 'ESTIMATE' "
            "AND ((:src_code IS NULL AND source_code IS NULL) "
            "     OR source_code = :src_code)")

        total_series = len(annual_by_key)
        written = buffered = done = 0
        started = time.monotonic()

        for key in sorted(annual_by_key):
            location_key, year_key, disease_code, source_code = key
            annual_cases = annual_by_key[key]
            done += 1

            target = int(round(max(0.0, annual_cases)))
            if target <= 0:
                stats["zero"] = int(stats["zero"]) + 1
                logger.debug("  skipping (%s, %s, %s, %s): annual total %.3f "
                             "rounds to 0", location_key, year_key, disease_code,
                             source_code, annual_cases)
                continue

            present = key in existing_keys
            if present and not force:
                stats["already_present"] = int(stats["already_present"]) + 1
                continue

            daily = generate_daily_case_counts(
                annual_cases=annual_cases, year=year_key,
                location_id=location_key,
                base_variation=base_variation, seed=seed,
                kernel=kernel,
                stable_key=location_keys.get(location_key))
            payload = [{"loc_id": location_key, "date_id": row["date_id"],
                        "disease": disease_code,
                        "src_code": source_code or None,
                        "case_count": int(row["case_count"])}
                       for row in daily.to_dict(orient="records")]
            try:
                # A savepoint keeps one bad series from discarding buffered rows.
                with self.session.begin_nested():
                    if present:
                        deleted = self.session.execute(window_sql, {
                            "loc_id": location_key,
                            "start_date": f"{year_key}-01-01",
                            "end_date": f"{year_key}-12-31",
                            "disease": disease_code,
                            "src_code": source_code or None}).rowcount or 0
                        logger.debug("    deleted %d existing ESTIMATE row(s)",
                                     deleted)
                    self.session.execute(statement, payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("    error expanding (%s, %s, %s, %s): %s: %s",
                             location_key, year_key, disease_code, source_code,
                             type(exc).__name__, exc)
                stats["errors"] = int(stats["errors"]) + 1
                continue

            stats["inserted"] = int(stats["inserted"]) + len(payload)
            written += len(payload)
            buffered += len(payload)
            if buffered >= _COMMIT_ROWS:
                self.session.commit()
                buffered = 0

            if done % _PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - started
                logger.info("  series %d/%d, %d rows written, %.1fs elapsed, "
                            "%.0f rows/s", done, total_series, written, elapsed,
                            written / elapsed if elapsed > 0 else 0.0)

        if buffered:
            self.session.commit()

        elapsed = time.monotonic() - started
        logger.info(
            "BURDEN EXPANSION COMPLETE: %d inserted, %d present, %d zero-total, "
            "%d no-population, %d skipped, %d error(s); %d record(s) -> %d group(s), "
            "%d summed, %.1fs (%.0f rows/s)",
            stats["inserted"], stats["already_present"], stats["zero"],
            stats["no_population"], stats["skipped"], stats["errors"],
            stats["records"], stats["groups"], stats["merged"], elapsed,
            written / elapsed if elapsed > 0 else 0.0)
        logger.info("  measures: %s", ", ".join(
            f"{name}={count}" for name, count in sorted(basis_counts.items()))
            or "(none)")
        logger.info("  denominators: %s", ", ".join(
            f"{name}={count}" for name, count in sorted(tier_counts.items()))
            or "(none)")
        logger.info("  allocation kernel: %s | policy: %s", kernel.origin,
                    policy.origin)
        if case_location_of:
            logger.info(
                "  written at %s: %d target(s)%s. A large `summed` count here is "
                "NORMAL at a coarser grain (cells sharing a target), not a "
                "duplicate.",
                grain_label, stats["grain_targets"],
                f"; {len(unparented_cells)} cell(s) had no such ancestor and kept "
                f"their own location_id" if unparented_cells else "")

        self.record_parameters(
            effective_run_id, stats, kernel, policy, prefer, rate_scale,
            national_allocation, population_divisor, population_indicator,
            default_population, seed, group_by_level)

        if stats["no_population"]:
            logger.warning(
                "  %d rate record(s) had no denominator for %r. They are the only "
                "records that need one: a %r record is self-contained. Allow the "
                "country series with denominator.national_allocation (or "
                "fix_cases.py --national-allocation) if the population is national.",
                stats["no_population"], population_indicator,
                BURDEN_MEASURE_CASES_MEAN)
        if stats["zero"]:
            logger.warning(
                "  %d group(s) round to 0 cases and were not written. If that is "
                "all of them on a %r run, the rate scale is wrong: the schema does "
                "not record the unit, and a per-100k reading of a per-1,000 rate "
                "yields fractions of a case. Compare measure.rate_scale settings.",
                stats["zero"], BURDEN_MEASURE_INCIDENCE_RATE)
        return stats


def expand_burden_to_daily(session: Any, *, registry: Any = None,
                           run_id: Optional[str] = None,
                           **overrides: Any) -> Dict[str, Any]:
    """One-shot expansion; omitted keywords come from the manifest."""
    return BurdenExpander(session, registry=registry, run_id=run_id).expand(
        **overrides)


_BURDEN_MEASURE_CASES_MEAN = BURDEN_MEASURE_CASES_MEAN
_BURDEN_MEASURE_INCIDENCE_RATE = BURDEN_MEASURE_INCIDENCE_RATE
_BURDEN_MEASURE_PREFERENCES = BURDEN_MEASURE_PREFERENCES

_burden_generate_daily_case_counts = generate_daily_case_counts
_ensure_dim_time = ensure_dim_time
_load_burden_registry = _load_registry


__all__ = [
    "BURDEN_KERNEL_KEY",
    "BURDEN_MEASURE_CASES_MEAN",
    "BURDEN_MEASURE_INCIDENCE_RATE",
    "BURDEN_MEASURE_PREFERENCES",
    "BurdenExpander",
    "DEFAULT_MAX_PROJECTED_ROWS",
    "DEFAULT_REGISTRY_PATH",
    "FLAT_BURDEN_KERNEL",
    "MAX_IN_VARS",
    "default_expansion_policy",
    "ensure_dim_time",
    "expand_burden_to_daily",
    "generate_daily_case_counts",
    "guard_projected_volume",
    "load_burden_kernel",
    "load_expansion_policy",
    "project_case_rows",
    "_BURDEN_KERNEL_DEFAULTS",
    "_BURDEN_LOG_SAMPLE",
    "_BURDEN_MEASURE_CASES_MEAN",
    "_BURDEN_MEASURE_INCIDENCE_RATE",
    "_BURDEN_MEASURE_PREFERENCES",
    "_BurdenKernel",
    "_ExpansionPolicy",
    "_PopulationHit",
    "_PopulationIndex",
    "_RATE_SCALES",
    "_burden_generate_daily_case_counts",
    "_days_in_year",
    "_ensure_dim_time",
    "_load_burden_registry",
    "_positive",
    "_stable_location_key",
]