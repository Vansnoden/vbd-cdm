#!/usr/bin/env python3
"""
export_to_csv.py - Export the VBD CDM to CSV for sharing.

Memory model
------------
This script streams. Peak memory is one chunk (default 50,000 rows) plus
a file handle, regardless of how large the database grows.

Output modes
------------
  --single-csv    One wide-union CSV. Columns that do not apply to a
                  given row are blank. Simple to open and filter in
                  Excel/pandas; larger on disk.
  --per-table     One CSV per fact table, plus a README, zipped. Smaller
                  file, cleaner headers, but the reader has to unpack.

Default is --single-csv. Use --per-table when size matters.

Usage
-----
    python export_to_csv.py
    python export_to_csv.py --per-table
    python export_to_csv.py --out /path/to/cdm_export.csv
    python export_to_csv.py --no-dims          # raw FK codes, no joins
    python export_to_csv.py --limit 5000       # 5,000 rows per fact table
    python export_to_csv.py --chunk-size 20000
"""

import argparse
import logging
import sys
import zipfile
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from database.session import engine, DATABASE_URL  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical column order for the wide-union CSV. Columns not listed here
# are appended alphabetically after these, so a new fact-specific column
# always lands at the tail.
# ---------------------------------------------------------------------------

COMMON_COLUMNS: List[str] = [
    "fact_table",
    "fact_id",

    "location_id", "location_name",
    "country_code", "country_name", "admin_level",
    "centroid_lat", "centroid_lon",

    "date_id", "date", "year", "month", "week", "season",
    "date_id_secondary",

    "vector_code", "species_name", "genus", "family",

    "disease_code", "disease_name", "pathogen_species",

    "source_code", "source_name",
]


# ---------------------------------------------------------------------------
# SQL builders.
#
# Each returns the SQL string for one fact table. `limit=None` means no
# limit; `limit=0` is used during schema discovery to fetch zero rows
# while still getting the correct column metadata.
# ---------------------------------------------------------------------------

def _limit_clause(limit: Optional[int]) -> str:
    if limit is None:
        return ""
    return f" LIMIT {int(limit)}"


def _sql_vector_observation(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_vector_observation' AS fact_table,
                o.observation_id          AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                t.date_id, t.date, t.year, t.month, t.week, t.season,
                NULL AS date_id_secondary,
                v.vector_code, v.species_name, v.genus, v.family,
                NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                o.life_stage, o.sex, o.collection_method,
                o.count, o.density_per_trap,
                o.tested_for_pathogen, o.infection_status, o.infection_rate
            FROM fact_vector_observation o
            LEFT JOIN dim_location l ON o.location_id = l.location_id
            LEFT JOIN dim_time     t ON o.date_id     = t.date_id
            LEFT JOIN dim_vector   v ON o.vector_code = v.vector_code
            LEFT JOIN dim_source   s ON o.source_code = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_vector_observation' AS fact_table,
                o.observation_id          AS fact_id,
                o.location_id, o.date_id, o.vector_code, o.source_code,
                o.life_stage, o.sex, o.collection_method,
                o.count, o.density_per_trap,
                o.tested_for_pathogen, o.infection_status, o.infection_rate
            FROM fact_vector_observation o
        """
    return sql + _limit_clause(limit)


def _sql_disease_case(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_disease_case'       AS fact_table,
                c.case_id                 AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                t.date_id, t.date, t.year, t.month, t.week, t.season,
                c.date_id_reported        AS date_id_secondary,
                NULL AS vector_code, NULL AS species_name,
                NULL AS genus, NULL AS family,
                d.disease_code, d.disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                c.identification_method_code,
                c.case_status, c.host_type,
                c.age_years, c.age_group, c.sex,
                c.outcome, c.case_count
            FROM fact_disease_case c
            LEFT JOIN dim_location l ON c.location_id    = l.location_id
            LEFT JOIN dim_time     t ON c.date_id_onset  = t.date_id
            LEFT JOIN dim_disease  d ON c.disease_code   = d.disease_code
            LEFT JOIN dim_source   s ON c.source_code    = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_disease_case'       AS fact_table,
                c.case_id                 AS fact_id,
                c.location_id, c.date_id_onset AS date_id,
                c.date_id_reported        AS date_id_secondary,
                c.disease_code, c.source_code,
                c.identification_method_code,
                c.case_status, c.host_type,
                c.age_years, c.age_group, c.sex,
                c.outcome, c.case_count
            FROM fact_disease_case c
        """
    return sql + _limit_clause(limit)


def _sql_insecticide_resistance(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_insecticide_resistance' AS fact_table,
                r.resistance_id               AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                t.date_id, t.date, t.year, t.month, t.week, t.season,
                NULL AS date_id_secondary,
                v.vector_code, v.species_name, v.genus, v.family,
                NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                r.insecticide_code, i.insecticide_name, i.chemical_class,
                r.test_type_code, tt.test_type_name,
                r.concentration, r.exposure_time,
                r.n_insect_tested, r.n_insect_susceptible,
                r.n_insect_resistant, r.n_insect_dead,
                r.vgsc_n_insect_tested,
                r.vgsc995l_vgsc995l_n, r.vgsc995l_vgsc995f_n,
                r.vgsc995f_vgsc995f_n, r.vgsc995l_vgsc995s_n,
                r.vgsc995s_vgsc995s_n, r.vgsc995f_vgsc995s_n,
                r.vgsc_generation,
                r.mortality, r.resistance_status
            FROM fact_insecticide_resistance r
            LEFT JOIN dim_location             l  ON r.location_id      = l.location_id
            LEFT JOIN dim_time                 t  ON r.date_id          = t.date_id
            LEFT JOIN dim_vector               v  ON r.vector_code      = v.vector_code
            LEFT JOIN dim_insecticide          i  ON r.insecticide_code = i.insecticide_code
            LEFT JOIN dim_resistance_test_type tt ON r.test_type_code   = tt.test_type_code
            LEFT JOIN dim_source               s  ON r.source_code      = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_insecticide_resistance' AS fact_table,
                r.resistance_id               AS fact_id,
                r.location_id, r.date_id, r.vector_code,
                r.insecticide_code, r.test_type_code, r.source_code,
                r.concentration, r.exposure_time,
                r.n_insect_tested, r.n_insect_susceptible,
                r.n_insect_resistant, r.n_insect_dead,
                r.vgsc_n_insect_tested,
                r.vgsc995l_vgsc995l_n, r.vgsc995l_vgsc995f_n,
                r.vgsc995f_vgsc995f_n, r.vgsc995l_vgsc995s_n,
                r.vgsc995s_vgsc995s_n, r.vgsc995f_vgsc995s_n,
                r.vgsc_generation,
                r.mortality, r.resistance_status
            FROM fact_insecticide_resistance r
        """
    return sql + _limit_clause(limit)


def _sql_bionomics(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_bionomics'          AS fact_table,
                b.bionomics_id            AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                t.date_id, t.date, t.year, t.month, t.week, t.season,
                NULL AS date_id_secondary,
                v.vector_code, v.species_name, v.genus, v.family,
                NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                b.biting_rate, b.feeding_preference, b.biting_location,
                b.resting_behavior, b.larval_site_type
            FROM fact_bionomics b
            LEFT JOIN dim_location l ON b.location_id = l.location_id
            LEFT JOIN dim_time     t ON b.date_id     = t.date_id
            LEFT JOIN dim_vector   v ON b.vector_code = v.vector_code
            LEFT JOIN dim_source   s ON b.source_code = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_bionomics'          AS fact_table,
                b.bionomics_id            AS fact_id,
                b.location_id, b.date_id, b.vector_code, b.source_code,
                b.biting_rate, b.feeding_preference, b.biting_location,
                b.resting_behavior, b.larval_site_type
            FROM fact_bionomics b
        """
    return sql + _limit_clause(limit)


def _sql_intervention(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_intervention'       AS fact_table,
                i.intervention_id         AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                t.date_id, t.date, t.year, t.month, t.week, t.season,
                i.date_end                AS date_id_secondary,
                NULL AS vector_code, NULL AS species_name,
                NULL AS genus, NULL AS family,
                d.disease_code, d.disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                i.intervention_type, i.coverage
            FROM fact_intervention i
            LEFT JOIN dim_location l ON i.location_id         = l.location_id
            LEFT JOIN dim_time     t ON i.date_start          = t.date_id
            LEFT JOIN dim_disease  d ON i.target_disease_code = d.disease_code
            LEFT JOIN dim_source   s ON i.source_code         = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_intervention'       AS fact_table,
                i.intervention_id         AS fact_id,
                i.location_id,
                i.date_start              AS date_id,
                i.date_end                AS date_id_secondary,
                i.target_disease_code     AS disease_code,
                i.source_code,
                i.intervention_type, i.coverage
            FROM fact_intervention i
        """
    return sql + _limit_clause(limit)


def _sql_socioeconomic(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_socioeconomic_condition' AS fact_table,
                sc.socio_id                    AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                NULL AS date_id, NULL AS date,
                sc.year, NULL AS month, NULL AS week, NULL AS season,
                NULL AS date_id_secondary,
                NULL AS vector_code, NULL AS species_name,
                NULL AS genus, NULL AS family,
                NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                sc.indicator_code, si.indicator_name, si.unit,
                sc.indicator_value
            FROM fact_socioeconomic_condition sc
            LEFT JOIN dim_location                l  ON sc.location_id    = l.location_id
            LEFT JOIN dim_socioeconomic_indicator si ON sc.indicator_code = si.indicator_code
            LEFT JOIN dim_source                  s  ON sc.source_code    = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_socioeconomic_condition' AS fact_table,
                sc.socio_id                    AS fact_id,
                sc.location_id, sc.year,
                sc.indicator_code, sc.source_code,
                sc.indicator_value
            FROM fact_socioeconomic_condition sc
        """
    return sql + _limit_clause(limit)


def _sql_health_facility(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_health_facility'    AS fact_table,
                h.facility_id             AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                NULL AS date_id, NULL AS date,
                NULL AS year, NULL AS month, NULL AS week, NULL AS season,
                NULL AS date_id_secondary,
                NULL AS vector_code, NULL AS species_name,
                NULL AS genus, NULL AS family,
                NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                h.facility_code, h.name AS facility_name,
                h.lat, h.lon, h.type, h.catchment_population
            FROM fact_health_facility h
            LEFT JOIN dim_location l ON h.location_id = l.location_id
            LEFT JOIN dim_source   s ON h.source_code = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_health_facility'    AS fact_table,
                h.facility_id             AS fact_id,
                h.location_id, h.source_code,
                h.facility_code, h.name AS facility_name,
                h.lat, h.lon, h.type, h.catchment_population
            FROM fact_health_facility h
        """
    return sql + _limit_clause(limit)


def _sql_disease_burden(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_disease_burden'     AS fact_table,
                b.burden_id               AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                NULL AS date_id, NULL AS date,
                b.year, NULL AS month, NULL AS week, NULL AS season,
                NULL AS date_id_secondary,
                NULL AS vector_code, NULL AS species_name,
                NULL AS genus, NULL AS family,
                d.disease_code, d.disease_name, b.pathogen_species,
                s.source_code, s.source_name,
                b.incidence_rate, b.prevalence_rate,
                b.mortality_rate, b.cases_mean
            FROM fact_disease_burden b
            LEFT JOIN dim_location l ON b.location_id = l.location_id
            LEFT JOIN dim_disease  d ON b.disease_code = d.disease_code
            LEFT JOIN dim_source   s ON b.source_code = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_disease_burden'     AS fact_table,
                b.burden_id               AS fact_id,
                b.location_id, b.year,
                b.disease_code, b.pathogen_species, b.source_code,
                b.incidence_rate, b.prevalence_rate,
                b.mortality_rate, b.cases_mean
            FROM fact_disease_burden b
        """
    return sql + _limit_clause(limit)


def _sql_environment(join_dims: bool, limit: Optional[int]) -> str:
    if join_dims:
        sql = """
            SELECT
                'fact_environment'        AS fact_table,
                e.fact_env_id             AS fact_id,
                l.location_id, l.name AS location_name,
                l.country_code, l.country_name, l.admin_level,
                l.centroid_lat, l.centroid_lon,
                t.date_id, t.date, t.year, t.month, t.week, t.season,
                NULL AS date_id_secondary,
                NULL AS vector_code, NULL AS species_name,
                NULL AS genus, NULL AS family,
                NULL AS disease_code, NULL AS disease_name, NULL AS pathogen_species,
                s.source_code, s.source_name,
                e.variable, e.value
            FROM fact_environment e
            LEFT JOIN dim_location l ON e.location_id = l.location_id
            LEFT JOIN dim_time     t ON e.date_id     = t.date_id
            LEFT JOIN dim_source   s ON e.source_code = s.source_code
        """
    else:
        sql = """
            SELECT
                'fact_environment'        AS fact_table,
                e.fact_env_id             AS fact_id,
                e.location_id, e.date_id, e.source_code,
                e.variable, e.value
            FROM fact_environment e
        """
    return sql + _limit_clause(limit)


# Registry of (fact_table_name, sql_builder).
FACT_QUERIES: List[Tuple[str, callable]] = [
    ("fact_vector_observation",      _sql_vector_observation),
    ("fact_disease_case",            _sql_disease_case),
    ("fact_insecticide_resistance",  _sql_insecticide_resistance),
    ("fact_bionomics",               _sql_bionomics),
    ("fact_intervention",            _sql_intervention),
    ("fact_socioeconomic_condition", _sql_socioeconomic),
    ("fact_health_facility",         _sql_health_facility),
    ("fact_disease_burden",          _sql_disease_burden),
    ("fact_environment",             _sql_environment),
]


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

def _iter_chunks(sql: str, chunk_size: int) -> Iterator[pd.DataFrame]:
    """
    Yield the result of `sql` in chunks of `chunk_size` rows. Uses
    pandas' SQLite/GENERIC chunksize iterator, which pulls rows from the
    DBAPI cursor with fetchmany() and never materialises more than one
    chunk at a time.
    """
    with engine.connect() as con:
        reader = pd.read_sql_query(text(sql), con, chunksize=chunk_size)
        for chunk in reader:
            yield chunk


def _discover_columns(join_dims: bool) -> List[str]:
    """
    Determine the union of columns across all fact queries by running
    each with LIMIT 0. This is a header-only read; no rows are fetched.
    """
    seen: set = set()
    for table_name, builder in FACT_QUERIES:
        sql = builder(join_dims, 0)
        try:
            with engine.connect() as con:
                df = pd.read_sql_query(text(sql), con)
            seen.update(df.columns)
        except Exception as e:
            logger.error(f"  {table_name}: column discovery failed — {e}")

    common = [c for c in COMMON_COLUMNS if c in seen]
    extras = sorted(c for c in seen if c not in COMMON_COLUMNS)
    return common + extras


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------

def export_single_csv(out_path: Path, join_dims: bool,
                      limit: Optional[int], chunk_size: int) -> None:
    """
    Write a single wide-union CSV. Peak memory is one chunk.
    """
    logger.info("Discovering columns (LIMIT 0 read on each fact table)...")
    all_columns = _discover_columns(join_dims)
    logger.info(f"  Union of {len(all_columns)} columns")

    logger.info(f"Streaming rows (chunk_size={chunk_size:,}) → {out_path}")

    header_written = False
    total_rows = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        for table_name, builder in FACT_QUERIES:
            sql = builder(join_dims, limit)
            table_rows = 0
            try:
                for chunk in _iter_chunks(sql, chunk_size):
                    # Reindex to the full column set. Missing columns become
                    # NaN and are written as blank cells. This is a
                    # per-chunk copy, not a global one.
                    chunk = chunk.reindex(columns=all_columns)
                    chunk.to_csv(f, index=False, header=not header_written)
                    header_written = True
                    table_rows += len(chunk)
                    total_rows += len(chunk)
            except Exception as e:
                logger.error(f"  {table_name}: query failed — {e}")
                continue
            logger.info(f"  {table_name:<32s} {table_rows:>12,} rows")

    if not header_written:
        logger.warning("No rows returned from any fact table; nothing written.")
        return

    size_mb = out_path.stat().st_size / 1e6
    logger.info(f"Done. {total_rows:,} rows, {size_mb:.1f} MB → {out_path}")


def export_per_table_zip(out_path: Path, join_dims: bool,
                         limit: Optional[int], chunk_size: int) -> None:
    """
    Write one CSV per fact table and bundle them into a zip. Each CSV is
    streamed to disk with its own header; peak memory is one chunk.
    """
    tmp_dir = out_path.parent / (out_path.stem + "_tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    manifest: List[str] = ["CDM export", "=" * 60, ""]

    for table_name, builder in FACT_QUERIES:
        sql = builder(join_dims, limit)
        csv_path = tmp_dir / f"{table_name}.csv"
        table_rows = 0
        header_written = False

        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                for chunk in _iter_chunks(sql, chunk_size):
                    chunk.to_csv(f, index=False, header=not header_written)
                    header_written = True
                    table_rows += len(chunk)
        except Exception as e:
            logger.error(f"  {table_name}: query failed — {e}")
            continue

        if table_rows == 0:
            # Keep a header-only CSV so downstream code can still read the schema.
            try:
                with engine.connect() as con:
                    empty = pd.read_sql_query(text(builder(join_dims, 0)), con)
                empty.to_csv(csv_path, index=False)
            except Exception:
                pass

        written.append(csv_path)
        manifest.append(f"{table_name:<32s} {table_rows:>12,} rows")
        logger.info(f"  {table_name:<32s} {table_rows:>12,} rows")

    if not written:
        logger.warning("No rows to write; aborting.")
        return

    readme = tmp_dir / "README.txt"
    readme.write_text(
        "\n".join(manifest)
        + "\n\nEach CSV corresponds to one fact table from the VBD CDM.\n"
        + "Dimension values are joined in; raw FK columns are preserved.\n"
        + "Blank cells indicate columns that do not apply to that table.\n"
    )

    logger.info(f"Zipping {len(written)} CSVs → {out_path}")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in written:
            zf.write(p, arcname=p.name)
        zf.write(readme, arcname=readme.name)

    for p in written + [readme]:
        p.unlink()
    tmp_dir.rmdir()

    size_mb = out_path.stat().st_size / 1e6
    logger.info(f"Done. {out_path} ({size_mb:.1f} MB, {len(written)} CSVs)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out", type=Path,
        default=PROJECT_ROOT / "outputs" / "cdm_export.csv",
        help="Output path (default: outputs/cdm_export.csv)",
    )
    parser.add_argument(
        "--per-table", action="store_true",
        help="Write one CSV per fact table and zip them (smaller output).",
    )
    parser.add_argument(
        "--no-dims", action="store_true",
        help="Skip dimension joins; export raw fact columns only.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap rows per fact table (useful for smoke tests).",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=50_000,
        help="Rows per streaming chunk (default: 50,000).",
    )
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Database URL: {DATABASE_URL}")
    logger.info(f"Output:       {args.out}")
    logger.info(f"Mode:         {'per-table zip' if args.per_table else 'single CSV'}")

    if args.per_table:
        if args.out.suffix.lower() != ".zip":
            args.out = args.out.with_suffix(".zip")
        export_per_table_zip(args.out, not args.no_dims, args.limit, args.chunk_size)
    else:
        export_single_csv(args.out, not args.no_dims, args.limit, args.chunk_size)


if __name__ == "__main__":
    main()