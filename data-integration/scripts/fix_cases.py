#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_cases.py - CLI wrapper for burden expansion.

The implementation lives in
``utilities.consolidation.DataConsolidator.expand_burden_to_daily()``. This file
contains no counting logic, no seasonal curve and no distribution of any kind:
it opens a session, calls that method, reports the returned stats and translates
them into an exit status.

Safe to call, and safe to call twice:

    - Without --force, burden records that already have ESTIMATE rows are skipped.
    - With --force, those ESTIMATE rows are deleted and re-inserted.

    The random seed is combined with (location_id, year), so the same
    (location, year) produces the same daily series across runs regardless of
    how many other locations were processed in between.

The two measures
----------------
A burden record carries ``cases_mean`` (an absolute case count for the cell-year)
or ``incidence_rate`` (a rate that needs a population denominator), or both.

``--prefer cases-mean`` is the default, because a measured count needs no
population, no allocation and no assumption about the rate's unit. ``--check``
reports which measure each record uses, and what the rate reading would be under
both plausible scales, so the choice is made on evidence rather than by default.

Why --check exists
------------------
The expansion used to report its problems only in INFO log lines, so "inserted 0"
and "inserted 240,000" looked the same from a shell. ``--check`` resolves every
selected record exactly as a run would - it imports ``_PopulationIndex`` and
``_RATE_SCALES`` rather than re-deriving them - and prints the summary, the write
grain, the projected row count and a per-group verdict.

Usage::

    python scripts/fix_cases.py --check
    python scripts/fix_cases.py --group-by-level admin3 --check
    python scripts/fix_cases.py --group-by-level admin3 --years 2022 --force
    python scripts/fix_cases.py --prefer incidence-rate --national-allocation \\
        --rate-scale per_person --years 2022
    python scripts/fix_cases.py --population 54000000

Exit status
-----------
0   rows were inserted, or every group was already present (idempotent no-op)
1   the session or the expansion raised; the message is on stderr
2   usage error (argparse)
3   nothing to expand, or every group rounds to 0 cases
4   nothing would be inserted because no denominator is reachable
5   the pass ran but reported one or more record-level errors
"""

from __future__ import annotations

import argparse
import calendar
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Project root
#
# `scripts/fix_cases.py` -> <repo>. NOT Path(__file__).parent: that is the
# scripts/ directory, and inserting it puts `utilities` out of reach.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Loaded before anything that touches `database`: database.session resolves its
# URL once, at import time, from DATABASE_URL / the DB_* tuple / the SQLite
# fallback. A later load_dotenv() is a no-op for this process, so an operator
# with a working .env would otherwise silently get an empty SQLite file.
try:
    from dotenv import load_dotenv

    if (PROJECT_ROOT / ".env").is_file():
        load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
except Exception:  # noqa: BLE001 - python-dotenv is optional
    pass

from sqlalchemy import text  # noqa: E402

from database.session import DATABASE_URL, engine  # noqa: E402
from utilities.consolidation import (  # noqa: E402
    _BURDEN_MEASURE_CASES_MEAN,
    _BURDEN_MEASURE_INCIDENCE_RATE,
    _RATE_SCALES,
    DataConsolidator,
    _PopulationIndex,
    _positive,
)

logger = logging.getLogger("fix_cases")


# ---------------------------------------------------------------------------
# Exit status
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NOTHING_TO_DO = 3
EXIT_NO_POPULATION = 4
EXIT_RUN_ERRORS = 5

#: The socioeconomic indicator the expansion reads a denominator from.
POPULATION_INDICATOR = "SP.POP.TOTL"

#: The identification method the expansion tags its rows with.
ESTIMATE_METHOD = "ESTIMATE"

#: Above this many projected case rows, the operator is told what it will cost.
_VOLUME_WARNING = 5_000_000

#: Sample rows shown in the projection sections.
_SAMPLE_LIMIT = 10

#: argparse names for the two measures.
_PREFER_CHOICES = {
    "cases-mean": _BURDEN_MEASURE_CASES_MEAN,
    "incidence-rate": _BURDEN_MEASURE_INCIDENCE_RATE,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scalar(session, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """``COUNT(*)``-style scalar, or None when the query cannot run."""
    try:
        value = session.execute(text(sql), params or {}).scalar()
    except Exception as exc:  # noqa: BLE001 - a probe must never break a run
        logger.debug("diagnostic query failed (%s): %s", sql.split("\n")[0], exc)
        return None
    return 0 if value is None else int(value)


def _rows(session, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
    try:
        return session.execute(text(sql), params or {}).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("diagnostic query failed (%s): %s", sql.split("\n")[0], exc)
        return []


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """Fixed-width table on stdout. Deterministic, no dependency."""
    if not rows:
        print("    (none)")
        return
    cells = [[("" if value is None else str(value)) for value in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in cells:
        for index, value in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(value))

    print("    " + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)))
    print("    " + "  ".join("-" * width for width in widths))
    for row in cells:
        print("    " + "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))


def _fmt(value: Any) -> str:
    if value is None:
        return "unavailable"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _days(year: Any) -> int:
    try:
        return 366 if calendar.isleap(int(year)) else 365
    except (TypeError, ValueError):
        return 365


def _int_list(raw: Optional[str], label: str) -> Optional[List[int]]:
    """Parse a comma-separated integer list, or None when unset."""
    if not raw:
        return None
    values: List[int] = []
    for piece in str(raw).replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            values.append(int(piece))
        except ValueError:
            raise SystemExit(f"{label}: {piece!r} is not an integer") from None
    return sorted(set(values)) or None


# ---------------------------------------------------------------------------
# Projection - the same resolution a run performs
# ---------------------------------------------------------------------------

def _record_where(locations: Optional[Sequence[int]],
                  years: Optional[Sequence[int]]) -> Tuple[str, Dict[str, Any]]:
    """The WHERE clause of "a record that can be expanded", plus its parameters."""
    where = [
        "((incidence_rate IS NOT NULL AND incidence_rate > 0) "
        " OR (cases_mean     IS NOT NULL AND cases_mean     > 0))"
    ]
    params: Dict[str, Any] = {}
    if locations:
        placeholders = ", ".join(f":loc{i}" for i in range(len(locations)))
        where.append(f"location_id IN ({placeholders})")
        params.update({f"loc{i}": value for i, value in enumerate(locations)})
    if years:
        placeholders = ", ".join(f":yr{i}" for i in range(len(years)))
        where.append(f"year IN ({placeholders})")
        params.update({f"yr{i}": value for i, value in enumerate(years)})
    return " AND ".join(where), params


def _project(consolidator, *, locations, years, limit, indicator,
             default_population, national_allocation, prefer, rate_scale,
             group_by_level, sample_limit=_SAMPLE_LIMIT) -> Dict[str, Any]:
    """Resolve every selected record exactly as the run would, and project it.

    One streamed pass. Produces the measure each record uses, the denominator tier
    behind it, the write grain, and a per-group rounding projection - so the
    verdict here is the run's own arithmetic, not a second implementation of it.
    """
    session = consolidator.session
    where, params = _record_where(locations, years)
    sql = (
        "SELECT burden_id, location_id, year, disease_code, source_code, "
        "       incidence_rate, cases_mean FROM fact_disease_burden "
        f"WHERE {where} ORDER BY burden_id"
    )
    if limit is not None:
        sql += " LIMIT :lim"
        params["lim"] = max(1, int(limit))

    try:
        index = _PopulationIndex(session, indicator).load()
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not build the population index: %s", exc)
        index = None

    divisors: Dict[str, int] = {}
    if national_allocation and index is not None:
        try:
            divisors = consolidator._burden_locations_per_country(index)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not count burden locations per country: %s", exc)
            divisors = {}

    grain_map: Dict[int, int] = {}
    grain_label = "grid cell"
    if group_by_level:
        requested = str(group_by_level).strip().lower()
        if requested in ("", "none", "cell", "grid"):
            pass
        elif requested in ("parent", "parent_location", "one-hop"):
            for location_id, parent_location_id in _rows(
                session,
                "SELECT location_id, parent_location_id FROM dim_location "
                "ORDER BY location_id",
            ):
                if location_id is not None and parent_location_id is not None:
                    grain_map[int(location_id)] = int(parent_location_id)
            grain_label = "each cell's parent_location_id"
        else:
            try:
                grain_map = consolidator._ancestor_map(requested)
            except Exception as exc:  # noqa: BLE001
                logger.debug("could not build the ancestor map: %s", exc)
                grain_map = {}
            grain_label = f"admin_level={requested}"

    scale = float(_RATE_SCALES[rate_scale])
    totals: Dict[Tuple[Any, Any, Any, Any], float] = {}
    basis_counts: Dict[str, int] = {}
    tier_counts: Dict[str, int] = {}
    rate_sum_per_100k = 0.0
    rate_sum_per_person = 0.0
    records = 0
    blocked = 0
    samples: List[List[Any]] = []

    try:
        cursor = session.execute(text(sql), params).yield_per(2000)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    for (burden_id, location_id, year, disease_code, source_code,
         incidence_rate, cases_mean) in cursor:
        records += 1
        try:
            location_key, year_key = int(location_id), int(year)
        except (TypeError, ValueError):
            continue

        rate_value = _positive(incidence_rate)
        mean_value = _positive(cases_mean)

        if prefer == _BURDEN_MEASURE_INCIDENCE_RATE:
            use_rate = rate_value is not None
            use_mean = (not use_rate) and mean_value is not None
        else:
            use_mean = mean_value is not None
            use_rate = (not use_mean) and rate_value is not None

        if not use_rate and not use_mean:
            continue

        population: Optional[float] = None
        tier = _BURDEN_MEASURE_CASES_MEAN if use_mean else "none"
        source_note = "cases_mean" if use_mean else "none"

        if use_rate:
            hit = (index.resolve(location_key, year_key,
                                 allow_country=national_allocation)
                   if index is not None else None)
            if hit is not None:
                population, tier = hit.value, hit.tier
                source_note = f"{hit.tier} ({hit.year})"
                if hit.is_country_level:
                    divisor = float(max(1, divisors.get(
                        index.country_for(location_key) or "(unknown)", 1)))
                    population = population / divisor
                    source_note += f" / {divisor:,.0f}"
            elif default_population is not None:
                population, tier, source_note = (
                    float(default_population), "default", "--population")

            if population is None or population <= 0:
                blocked += 1
                tier_counts["none"] = tier_counts.get("none", 0) + 1
                if len(samples) < sample_limit:
                    samples.append([
                        burden_id, location_key, year, disease_code,
                        f"{rate_value:g}", "-", "-", "BLOCKED"])
                continue

            annual = (rate_value / scale) * float(population)
            # The same record under the other plausible reading, so the operator
            # can see which one produces a sane national total.
            rate_sum_per_100k += (rate_value / _RATE_SCALES["per_100k"]) * float(population)
            rate_sum_per_person += (rate_value / _RATE_SCALES["per_person"]) * float(population)
            basis = _BURDEN_MEASURE_INCIDENCE_RATE
        else:
            annual = float(mean_value)
            basis = _BURDEN_MEASURE_CASES_MEAN

        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        basis_counts[basis] = basis_counts.get(basis, 0) + 1

        target = grain_map.get(location_key, location_key)
        key = (target, year_key, str(disease_code), str(source_code or ""))
        totals[key] = totals.get(key, 0.0) + annual

        if len(samples) < sample_limit:
            samples.append([
                burden_id, location_key, year, disease_code,
                f"{rate_value:g}" if rate_value is not None else "-",
                "-" if population is None else f"{population:,.0f}",
                source_note, f"{annual:,.2f}"])

    # ---- rounding projection -----------------------------------------
    written_groups = 0
    zero_groups = 0
    case_rows = 0
    annual_written = 0.0
    largest: List[Tuple[float, Tuple[Any, Any, Any, Any]]] = []
    for key, annual in totals.items():
        target_cases = int(round(max(0.0, annual)))
        if target_cases <= 0:
            zero_groups += 1
            continue
        written_groups += 1
        case_rows += _days(key[1])
        annual_written += annual
        largest.append((annual, key))
    largest.sort(reverse=True)

    return {
        "records": records,
        "groups": len(totals),
        "written_groups": written_groups,
        "zero_groups": zero_groups,
        "case_rows": case_rows,
        "annual_written": annual_written,
        "annual_total": sum(totals.values()),
        "basis": basis_counts,
        "tiers": tier_counts,
        "blocked": blocked,
        "grain": grain_label,
        "grain_targets": len(set(grain_map.values())),
        "divisors": divisors,
        "samples": samples,
        "largest": [(annual, key) for annual, key in largest[:sample_limit]],
        "rate_sum_per_100k": rate_sum_per_100k,
        "rate_sum_per_person": rate_sum_per_person,
        "population_index": index,
    }


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def report(consolidator, *, locations: Optional[Sequence[int]] = None,
           years: Optional[Sequence[int]] = None, limit: Optional[int] = None,
           indicator: str = POPULATION_INDICATOR,
           default_population: Optional[float] = None,
           national_allocation: bool = False,
           prefer: str = _BURDEN_MEASURE_CASES_MEAN,
           rate_scale: str = "per_100k",
           group_by_level: Optional[str] = None,
           sample_limit: int = _SAMPLE_LIMIT) -> int:
    """Print what the expansion would do. Writes nothing. Returns an exit status."""
    session = consolidator.session

    print("=" * 78)
    print("BURDEN EXPANSION - PREFLIGHT (read-only)")
    print("=" * 78)
    print(f"  database:      {DATABASE_URL}")
    print(f"  file:          {engine.url.database or '(in memory)'}")
    print(f"  measure:       {prefer} (rate scale {rate_scale!r})")
    print(f"  allocation:    {'national (country series / cells)' if national_allocation else 'per-location only'}")
    print(f"  write grain:   {group_by_level or 'grid cell'}")
    print(f"  selection:     locations={locations or 'all'} years={years or 'all'}"
          f" limit={limit or 'none'}")
    print()

    total_burden = _scalar(session, "SELECT COUNT(*) FROM fact_disease_burden")
    if total_burden is None:
        print("  fact_disease_burden is not readable. Run `alembic upgrade head`.")
        return EXIT_FAILURE
    if total_burden == 0:
        print("  fact_disease_burden is empty: nothing to expand.")
        print("  This pass only re-shapes burden rows that already exist; run")
        print("  ingestion first.")
        return EXIT_NOTHING_TO_DO

    where, where_params = _record_where(locations, years)
    matching = _scalar(session,
                       f"SELECT COUNT(*) FROM fact_disease_burden WHERE {where}",
                       where_params)
    rate_only = _scalar(session,
                        "SELECT COUNT(*) FROM fact_disease_burden WHERE "
                        "incidence_rate IS NOT NULL AND incidence_rate > 0 AND "
                        "(cases_mean IS NULL OR cases_mean <= 0)", {})
    mean_present = _scalar(session,
                           "SELECT COUNT(*) FROM fact_disease_burden WHERE "
                           "cases_mean IS NOT NULL AND cases_mean > 0", {})
    both = _scalar(session,
                   "SELECT COUNT(*) FROM fact_disease_burden WHERE "
                   "incidence_rate > 0 AND cases_mean > 0", {})

    print("1. what the records carry")
    _print_table(["measure", "rows"], [
        ["burden rows in the table", _fmt(total_burden)],
        ["matching this selection", _fmt(matching)],
        ["  have cases_mean (absolute count)", _fmt(mean_present)],
        ["  have incidence_rate only (needs a denominator)", _fmt(rate_only)],
        ["  have BOTH (--prefer decides)", _fmt(both)],
    ])
    print()
    if not matching:
        print("  Nothing matches the selection's own clause:")
        print("  `incidence_rate > 0` or `cases_mean > 0`.")
        return EXIT_NOTHING_TO_DO
    if both:
        winner = ("cases_mean, no population needed"
                  if prefer == _BURDEN_MEASURE_CASES_MEAN
                  else "incidence_rate, needs a denominator")
        print(f"  {_fmt(both)} record(s) carry both measures, so they use "
              f"{winner}.")
        print()

    print("2. schema prerequisites")
    try:
        session.execute(text("SELECT case_count FROM fact_disease_case LIMIT 1"))
        print("    fact_disease_case.case_count present")
    except Exception:  # noqa: BLE001
        print("    fact_disease_case.case_count MISSING: the expansion raises before")
        print("    writing. Run `alembic upgrade head`.")
        return EXIT_FAILURE
    method_rows = _scalar(
        session,
        "SELECT COUNT(*) FROM dim_identification_method WHERE method_code = :m",
        {"m": ESTIMATE_METHOD})
    print(f"    dim_identification_method '{ESTIMATE_METHOD}' "
          f"{'present' if method_rows else 'absent (the expansion inserts it)'}")
    print()

    years_present = [int(row[0]) for row in _rows(
        session,
        "SELECT DISTINCT year FROM fact_disease_burden WHERE year IS NOT NULL "
        f"AND {where} ORDER BY year", where_params) if row[0] is not None]
    coverage: List[List[Any]] = []
    gaps = 0
    for year in years_present[:12]:
        expected = _days(year)
        present = _scalar(session, "SELECT COUNT(*) FROM dim_time WHERE year = :y",
                          {"y": year})
        ok = present is not None and present >= expected
        if not ok:
            gaps += 1
        coverage.append([year, expected, _fmt(present), "complete" if ok else "gap"])
    if len(years_present) > 12:
        coverage.append([f"… {len(years_present) - 12} more", "", "", ""])
    print("3. dim_time coverage (date_id is a foreign key)")
    _print_table(["year", "days", "dim_time rows", "status"], coverage)
    if gaps:
        print("    Gaps are not fatal: `_ensure_dim_time(year)` inserts the whole")
        print("    year before the first record that needs it.")
    print()

    print("4. projection (the run's own arithmetic, streamed)")
    projection = _project(
        consolidator, locations=locations, years=years, limit=limit,
        indicator=indicator, default_population=default_population,
        national_allocation=national_allocation, prefer=prefer,
        rate_scale=rate_scale, group_by_level=group_by_level,
        sample_limit=sample_limit)

    if projection.get("error"):
        print(f"    could not read the record set: {projection['error']}")
        return EXIT_FAILURE

    index = projection.get("population_index")
    if index is not None and (projection["basis"].get(_BURDEN_MEASURE_INCIDENCE_RATE)):
        _print_table(["population index", "value"], [
            [f"rows under {indicator!r}", _fmt(index.location_rows)],
            ["locations carrying a series", _fmt(index.series_locations)],
            ["country series", ", ".join(index.countries()) or "(none)"],
        ])
        print()

    if projection["basis"]:
        _print_table(["measure used", "records"],
                     [[name, count]
                      for name, count in sorted(projection["basis"].items())])
        print()
    if projection["tiers"]:
        _print_table(["denominator tier", "records"],
                     [[name, count]
                      for name, count in sorted(projection["tiers"].items())])
        print()

    if projection["blocked"]:
        print(f"    {projection['blocked']} rate record(s) have no reachable")
        print("    denominator under these flags.")
        if not national_allocation:
            print("    --national-allocation lets a location with no series of its")
            print("    own use its country's, divided by the number of burden")
            print("    locations in that country.")
        if default_population is None:
            print("    Or supply one value with --population N (used as given).")
        print()

    divisors = projection.get("divisors") or {}
    if divisors:
        _print_table(["country", "burden locations (divisor)"],
                     [[code, count] for code, count in sorted(divisors.items())])
        print("    The divisor is the number of burden LOCATIONS, so the shares sum")
        print("    back to the national total. It is deliberately not the number of")
        print("    write targets: grouping must not change the total.")
        print()

    print("5. volume after per-group rounding")
    _print_table(["measure", "value"], [
        ["records processed", _fmt(projection["records"])],
        ["groups at this grain", _fmt(projection["groups"])],
        ["  groups written (>= 1 case)", _fmt(projection["written_groups"])],
        ["  groups rounding to 0 cases", _fmt(projection["zero_groups"])],
        ["case rows written", _fmt(projection["case_rows"])],
        ["annual cases written (sum)", f"{projection['annual_written']:,.0f}"],
        ["write targets", _fmt(projection["grain_targets"])
         if group_by_level else "n/a"],
    ])
    if projection["case_rows"] > _VOLUME_WARNING:
        print(f"    More than {_fmt(_VOLUME_WARNING)} rows. Each group commits")
        print("    individually, so expect a long run and a large database.")
        print("    Consider --group-by-level, --years, or PostgreSQL.")
    print()

    if projection["basis"].get(_BURDEN_MEASURE_INCIDENCE_RATE):
        print("6. the rate records, under both plausible scales")
        _print_table(["rate scale", "annual cases these records imply"], [
            ["per 100,000 (the assumed convention)",
             f"{projection['rate_sum_per_100k']:,.0f}"],
            ["per person (cases/person/year)",
             f"{projection['rate_sum_per_person']:,.0f}"],
        ])
        print("    Only one of these is applied, and only to rate-only records.")
        print("    Compare against the country's plausible annual case burden;")
        print("    the wrong reading is off by a factor of 100,000.")
        print()

    print("7. sample records")
    _print_table(
        ["burden_id", "location", "year", "disease", "rate", "population",
         "from", "annual cases"],
        projection["samples"] or [["(none)", "", "", "", "", "", "", ""]])
    print()

    if projection["largest"]:
        print("8. largest groups (a plausibility check on the totals)")
        _print_table(
            ["annual cases", "location (write target)", "year", "disease",
             "source"],
            [[f"{annual:,.0f}", key[0], key[1], key[2], key[3] or "(null)"]
             for annual, key in projection["largest"]])
        print()

    print("=" * 78)
    if projection["written_groups"] == 0:
        if projection["groups"] == 0:
            print("VERDICT: blocked. No record reached a usable measure, so a run")
            print("         would insert 0 rows and exit 4.")
            return EXIT_NO_POPULATION
        print(f"VERDICT: nothing to write. All {projection['groups']} group(s)")
        print("         round to 0 cases.")
        if projection["basis"].get(_BURDEN_MEASURE_INCIDENCE_RATE):
            print("         That means the rate scale or the denominator is wrong:")
            print("         cell shares are small, so a per-100k assumption yields")
            print("         fractions of a case. Try --rate-scale per_person.")
        return EXIT_NOTHING_TO_DO

    print(f"VERDICT: expandable. {_fmt(projection['written_groups'])} group(s), "
          f"{_fmt(projection['case_rows'])} case row(s),")
    print(f"         written at {projection['grain']} and tagged")
    print("         identification_method_code = 'ESTIMATE'.")
    if projection["zero_groups"]:
        print(f"         {_fmt(projection['zero_groups'])} group(s) would round to "
              f"0 and be skipped.")
    print("         Re-run without --check to write them (add --force to replace).")
    print("=" * 78)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Reporting a real run
# ---------------------------------------------------------------------------

def summarise(stats: Dict[str, Any]) -> int:
    """Print the expansion's stats dict and translate it into an exit status."""
    inserted = int(stats.get("inserted") or 0)
    already = int(stats.get("already_present") or 0)
    skipped = int(stats.get("skipped") or 0)
    no_population = int(stats.get("no_population") or 0)
    errors = int(stats.get("errors") or 0)
    zero = int(stats.get("zero") or 0)
    merged = int(stats.get("merged") or 0)
    basis = stats.get("basis") or {}
    tiers = stats.get("tiers") or {}

    print()
    print("=" * 78)
    print("BURDEN EXPANSION RESULT")
    print("=" * 78)
    _print_table(["outcome", "records"], [
        ["burden records examined", _fmt(stats.get("records"))],
        ["daily series written", _fmt(stats.get("groups"))],
        ["  records summed into a coarser grain", _fmt(merged)],
        ["daily rows inserted", f"{inserted:,}"],
        ["skipped: ESTIMATE rows already present", already],
        ["skipped: annual total rounds to 0 cases", zero],
        ["skipped: no usable rate or case mean", skipped],
        ["skipped: no population denominator", no_population],
        ["errors", errors],
    ])
    if stats.get("grain"):
        print()
        _print_table(["write grain", "targets"], [
            [stats["grain"], _fmt(stats.get("grain_targets"))]])
    if basis:
        print()
        _print_table(["measure used", "records"],
                     [[name, count] for name, count in sorted(basis.items())])
    if tiers:
        print()
        _print_table(["denominator tier", "records"],
                     [[name, count] for name, count in sorted(tiers.items())])
    print("=" * 78)

    if errors:
        print("One or more records failed; each message is in the log above.")
        return EXIT_RUN_ERRORS
    if inserted:
        print(f"Inserted {inserted:,} daily case row(s) across "
              f"{_fmt(stats.get('groups'))} series.")
        return EXIT_OK
    if already:
        print(f"Idempotent no-op: {already} series already had ESTIMATE rows.")
        print("Use --force to delete and re-insert them.")
        return EXIT_OK
    if zero:
        print(f"Nothing to write: every group rounds to 0 cases ({zero}).")
        if basis.get(_BURDEN_MEASURE_INCIDENCE_RATE):
            print("For rate records that means the scale or the denominator is")
            print("wrong: try --rate-scale per_person, or --check to compare.")
            print("    python scripts/fix_cases.py --check --rate-scale per_person")
        return EXIT_NOTHING_TO_DO
    if no_population:
        print(f"No denominator: {no_population} rate record(s) had no")
        print(f"{POPULATION_INDICATOR} row of their own and no country series.")
        print("    python scripts/fix_cases.py --national-allocation --check")
        return EXIT_NO_POPULATION
    print("Nothing was written and nothing was already present. Run --check.")
    return EXIT_NOTHING_TO_DO


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fix_cases.py",
        description=(
            "Expand yearly burden records into daily case rows tagged "
            "identification_method_code = 'ESTIMATE'."
        ),
        epilog=(
            "A record carrying cases_mean needs nothing else: it is an absolute "
            "count. A record carrying only incidence_rate needs a population "
            f"denominator from fact_socioeconomic_condition under "
            f"{POPULATION_INDICATOR!r}, and the rate's scale matters - try "
            "--rate-scale per_person if a per-100k reading yields fractions of a "
            "case. Run --check first."
        ),
    )
    parser.add_argument("--check", action="store_true",
                        help="report what would be expanded, and exit; writes nothing")
    parser.add_argument("--force", action="store_true",
                        help="delete and re-insert ESTIMATE rows that already exist")
    parser.add_argument("--prefer", default="cases-mean", choices=sorted(_PREFER_CHOICES),
                        help=("which measure wins when a record carries both "
                              "(default: cases-mean, no population needed)"))
    parser.add_argument("--rate-scale", default="per_100k",
                        choices=sorted(_RATE_SCALES),
                        help=("how to read incidence_rate: 'per_100k' (rate per "
                              "100,000 people) or 'per_person' (cases per person "
                              "per year). Affects rate-only records only "
                              "(default: per_100k)"))
    parser.add_argument("--population", "--default-population",
                        dest="default_population", type=float, default=None,
                        metavar="N",
                        help=("population to use when no location or country series "
                              "applies. Used as given, never divided."))
    parser.add_argument("--population-indicator", default=POPULATION_INDICATOR,
                        metavar="CODE",
                        help=f"indicator code to read (default: {POPULATION_INDICATOR})")
    parser.add_argument("--national-allocation", action="store_true",
                        help=("allow a location with no series of its own to use its "
                              "country's population, divided by the number of burden "
                              "locations in that country"))
    parser.add_argument("--population-divisor", type=float, default=None,
                        metavar="N",
                        help="override the computed per-country divisor")
    parser.add_argument("--group-by-level", default=None, metavar="LEVEL",
                        help=("write the daily series at this admin_level "
                              "(admin1/admin2/admin3, or 'parent' for the cell's "
                              "immediate parent). Measures are summed into the "
                              "target: same totals, far fewer rows."))
    parser.add_argument("--group-by-parent", dest="group_by_level",
                        action="store_const", const="parent",
                        help="shorthand for --group-by-level parent")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="cap records processed, ordered by burden_id (smoke test)")
    parser.add_argument("--years", default=None, metavar="Y1,Y2",
                        help="comma-separated years to expand, e.g. 2022 or 2020,2021")
    parser.add_argument("--locations", default=None, metavar="ID1,ID2",
                        help="comma-separated location_id values to expand")
    parser.add_argument("--seed", type=int, default=42,
                        help="random seed; combined with (location_id, year)")
    parser.add_argument("--variation", type=float, default=0.25,
                        help="base log-normal variation, 0 to 1 (default: 0.25)")
    parser.add_argument("--log-level", default="WARNING",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help=("logging verbosity (default: WARNING: a full run over "
                              "hundreds of thousands of records writes a great many "
                              "INFO lines)"))
    return parser


def _known_admin_levels() -> Set[str]:
    """The admin_level values dim_location actually carries, lower-cased."""
    try:
        with engine.connect() as connection:
            return {str(row[0]).strip().lower() for row in connection.execute(
                text("SELECT DISTINCT admin_level FROM dim_location "
                     "WHERE admin_level IS NOT NULL")) if row[0]}
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read admin_level values: %s", exc)
        return set()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (0.0 <= args.variation <= 1.0):
        parser.error("--variation must be between 0 and 1 (it is a log-normal sigma)")
    if args.default_population is not None and args.default_population <= 0:
        parser.error("--population must be a positive number")
    if args.population_divisor is not None and args.population_divisor <= 0:
        parser.error("--population-divisor must be a positive number")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")

    if args.group_by_level:
        level = str(args.group_by_level).strip().lower()
        if level not in ("parent", "parent_location", "one-hop", "none", "cell",
                         "grid"):
            known = _known_admin_levels()
            if known and level not in known:
                parser.error(
                    f"--group-by-level {level!r}: dim_location holds no such "
                    f"admin_level. Present: {', '.join(sorted(known))}")

    years = _int_list(args.years, "--years")
    locations = _int_list(args.locations, "--locations")
    prefer = _PREFER_CHOICES[args.prefer]

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stdout,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    print(f"database:  {DATABASE_URL}")
    print(f"file:      {engine.url.database or '(in memory)'}")
    print(f"mode:      {'CHECK (read-only)' if args.check else 'WRITE'}"
          f" | measure {prefer}"
          f"{' + national allocation' if args.national_allocation else ''}"
          f"{f' + grain {args.group_by_level}' if args.group_by_level else ''}"
          f"{f' limit={args.limit}' if args.limit else ''}"
          f"{f' years={years}' if years else ''}")
    print()

    consolidator = None
    try:
        consolidator = DataConsolidator()

        if args.check:
            return report(
                consolidator,
                locations=locations,
                years=years,
                limit=args.limit,
                indicator=args.population_indicator,
                default_population=args.default_population,
                national_allocation=args.national_allocation,
                prefer=prefer,
                rate_scale=args.rate_scale,
                group_by_level=args.group_by_level,
            )

        stats = consolidator.expand_burden_to_daily(
            force=args.force,
            seed=args.seed,
            base_variation=args.variation,
            default_population=args.default_population,
            population_indicator=args.population_indicator,
            national_allocation=args.national_allocation,
            population_divisor=args.population_divisor,
            rate_scale=args.rate_scale,
            prefer=prefer,
            group_by_level=args.group_by_level,
            locations=locations,
            years=years,
            limit=args.limit,
        )
        return summarise(stats)

    except KeyboardInterrupt:
        logger.error("interrupted; nothing further was written to the database")
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not trace
        logger.error("burden expansion failed: %s: %s", type(exc).__name__, exc)
        if args.log_level == "DEBUG":
            logger.exception("traceback")
        return EXIT_FAILURE
    finally:
        if consolidator is not None:
            try:
                close_report = consolidator.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("close failed: %s: %s", type(exc).__name__, exc)
            else:
                for message in (close_report or {}).get("errors") or ():
                    logger.warning("resolution drain: %s", message)


if __name__ == "__main__":
    raise SystemExit(main())