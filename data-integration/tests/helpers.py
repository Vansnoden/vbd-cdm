import calendar
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd
from sqlalchemy import text


@dataclass(frozen=True)
class FakePack:
    """Minimal stand-in for ConceptPack: only what the ladder reads."""

    vocabulary_id: str
    pack_id: str = "test"
    version: str = "1.0"
    fuzzy_threshold: float = 0.80
    fuzzy_margin: float = 0.05
    case_sensitive: bool = False
    domain: Optional[str] = None

    def normalizer(self):
        from utilities.vocabulary import normalizers

        return normalizers.compose(("generic",))


class PackRegistry:
    """Registry-shaped stub: deterministic, no manifests, no disk access."""

    def __init__(self, packs=()):
        self._packs = list(packs)

    def packs(self):
        return list(self._packs)

    def parameter_packs(self):
        return list(self._packs)

    def find_parameter_pack(self, key):
        for pack in self._packs:
            if key in (pack.parameters or {}):
                return pack
        return None

    def require_parameter_pack(self, key):
        found = self.find_parameter_pack(key)
        if found is None:
            raise KeyError(key)
        return found


def parameter_pack(parameters: Mapping[str, Any], pack_id: str = "burden-expansion",
                   version: str = "1.0") -> Any:
    @dataclass(frozen=True)
    class _ParameterPack:
        parameters: Mapping[str, Any]
        pack_id: str
        version: str

        @property
        def vocabulary_id(self) -> str:
            return f"{self.pack_id}@{self.version}"

    return _ParameterPack(parameters=dict(parameters), pack_id=pack_id,
                          version=version)


def add_vocabulary(session, vocabulary_id: str) -> None:
    session.execute(
        text(
            "INSERT INTO dim_vocabulary (vocabulary_id, vocabulary_name, version) "
            "VALUES (:v, :v, '1.0') "
            "ON CONFLICT (vocabulary_id) DO NOTHING"
        ),
        {"v": vocabulary_id},
    )
    session.commit()


def add_concept(session, vocabulary_id: str, code: str, name: str, *,
                synonyms=None, domain=None, concept_class=None) -> None:
    add_vocabulary(session, vocabulary_id)
    extra = None
    if synonyms:
        extra = json.dumps({"synonyms": list(synonyms)}, sort_keys=True,
                           separators=(",", ":"))
    session.execute(
        text(
            "INSERT INTO dim_concept (vocabulary_id, concept_code, concept_name, "
            "concept_class, domain, parent_code, extra, concept_norm) "
            "VALUES (:v, :c, :n, :k, :d, NULL, :e, NULL)"
        ),
        {"v": vocabulary_id, "c": code, "n": name, "k": concept_class,
         "d": domain, "e": extra},
    )
    session.commit()


def add_source(session, code: str = "SRC-1") -> str:
    session.execute(
        text(
            "INSERT INTO dim_source (source_code, source_name) VALUES (:c, :n) "
            "ON CONFLICT (source_code) DO NOTHING"
        ),
        {"c": code, "n": code},
    )
    session.commit()
    return code


def add_location(session, name: str = "Kisumu", code: str = "loc-1",
                 country_code: str = "KE") -> int:
    session.execute(
        text(
            "INSERT INTO dim_location (name, location_code, country_code, "
            "country_name, admin_level, origin) "
            "VALUES (:n, :c, :cc, 'Kenya', 'site', 'source') "
            "ON CONFLICT (location_code) DO NOTHING"
        ),
        {"n": name, "c": code, "cc": country_code},
    )
    session.commit()
    return int(session.execute(
        text("SELECT location_id FROM dim_location WHERE location_code = :c"),
        {"c": code},
    ).scalar())


def add_day(session, date_id: str) -> str:
    moment = datetime.strptime(date_id, "%Y-%m-%d")
    session.execute(
        text(
            "INSERT INTO dim_time (date_id, date, year, month, week, "
            "day_of_year, season, is_holiday) "
            "VALUES (:d, :dt, :y, :m, :w, :doy, NULL, 0) "
            "ON CONFLICT (date_id) DO NOTHING"
        ),
        {"d": date_id, "dt": moment.date(), "y": moment.year,
         "m": moment.month, "w": moment.isocalendar()[1],
         "doy": moment.timetuple().tm_yday},
    )
    session.commit()
    return date_id


def add_disease(session, code: str = "MALARIA") -> str:
    session.execute(
        text(
            "INSERT INTO dim_disease (disease_code, disease_name) "
            "VALUES (:c, :c) ON CONFLICT (disease_code) DO NOTHING"
        ),
        {"c": code},
    )
    session.commit()
    return code


def add_burden(session, *, location_id: int, year: int, disease_code: str,
               source_code: str, cases_mean: Optional[float] = None,
               pathogen_species: Optional[str] = None,
               incidence_rate: Optional[float] = None) -> None:
    session.execute(
        text(
            "INSERT INTO fact_disease_burden (location_id, year, disease_code, "
            "pathogen_species, source_code, incidence_rate, cases_mean) "
            "VALUES (:l, :y, :d, :p, :s, :r, :c)"
        ),
        {"l": location_id, "y": year, "d": disease_code, "p": pathogen_species,
         "s": source_code, "r": incidence_rate, "c": cases_mean},
    )
    session.commit()


def row_count(session, table: str) -> int:
    return int(session.execute(
        text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def reference_daily_counts(annual_cases: float, year: int, location_id: int,
                           base_variation: Optional[float], seed: Optional[int],
                           kernel) -> list:
    """Frozen copy of the pre-item-12 algorithm, used as a bit-exact oracle."""
    active = kernel
    variation = (active.base_variation if base_variation is None
                 else float(base_variation))
    key = int(location_id)
    rng = np.random.default_rng(
        np.random.SeedSequence([0 if seed is None else int(seed), key, int(year)]))

    date_range = pd.date_range(datetime(year, 1, 1), datetime(year, 12, 31))
    weights = []
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
    return counts