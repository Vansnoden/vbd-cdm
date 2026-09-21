#!/usr/bin/env python3
"""
explore-data.py - exploratory visualisation of the VBD CDM database.

Works against whatever DATABASE_URL the project is configured for
(SQLite by default, PostgreSQL if the env vars are set). Reads through
SQLAlchemy, so the same script runs against both backends.

Usage:
    python explore-data.py
    python explore-data.py --url sqlite:///vbd_cdm.db
    python explore-data.py --url postgresql://user:pw@host:5432/vbd_cdm
    python explore-data.py --only vector,environment

Output layout (default: exploration/):
    00_overview.png
    01_locations.png
    02_time_coverage.png
    03_vector_species.png
    04_vector_temporal.png
    05_vector_spatial.png
    06_environment.png
    07_resistance.png
    08_bionomics.png
    09_cases.png
    10_fk_integrity.png
    summary.txt
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# Paths / defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).resolve().parent
DEFAULT_OUT  = SCRIPT_DIR / "exploration"

PALETTE = {
    "blue":   "#3B82F6",
    "dark":   "#1E40AF",
    "light":  "#93C5FD",
    "red":    "#EF4444",
    "green":  "#10B981",
    "amber":  "#F59E0B",
    "gray":   "#6B7280",
    "lgray":  "#E5E7EB",
}

plt.rcParams.update({
    "figure.dpi": 110,
    "savefig.dpi": 130,
    "font.size": 9,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.labelsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": PALETTE["lgray"],
    "grid.linewidth": 0.6,
    "figure.facecolor": "white",
})

# ---------------------------------------------------------------------------
# Engine resolution
#
# Resolution order:
#   1. --url CLI argument
#   2. the project's database.session.DATABASE_URL
#   3. the DATABASE_URL environment variable
#   4. a local ./vbd_cdm.db (SQLite fallback)
# ---------------------------------------------------------------------------

def resolve_engine(explicit_url: str | None = None) -> tuple[Engine, str]:
    if explicit_url:
        return create_engine(explicit_url), "cli --url"

    # Try the project's session module. The project root must be on the
    # import path; add it explicitly so the script works when invoked from
    # any working directory.
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from database.session import DATABASE_URL  # type: ignore
        return create_engine(DATABASE_URL), "project database.session"
    except Exception as e:
        print(f"  [warn] could not import database.session: {e}")

    url = os.getenv("DATABASE_URL")
    if url:
        return create_engine(url), "env DATABASE_URL"

    local = SCRIPT_DIR / "vbd_cdm.db"
    if local.exists():
        return create_engine(f"sqlite:///{local}"), "local vbd_cdm.db"

    sys.exit(
        "Could not resolve a database.\n"
        "  Set --url, or DATABASE_URL, or place vbd_cdm.db next to this script."
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def table_names(engine: Engine) -> list[str]:
    """All tables in the DB, excluding Alembic's bookkeeping table."""
    all_names = inspect(engine).get_table_names()
    return sorted(t for t in all_names if t != "alembic_version")


def q(engine: Engine, sql: str) -> pd.DataFrame:
    """Run a read-only query; return a DataFrame. Never raises."""
    try:
        with engine.connect() as con:
            return pd.read_sql_query(sql, con)
    except Exception as e:
        print(f"  [query error] {type(e).__name__}: {e}")
        return pd.DataFrame()


def save(fig, out_dir: Path, name: str) -> Path:
    path = out_dir / name
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {path.name}")
    return path


def empty_axes(ax, msg: str) -> None:
    ax.axis("off")
    ax.text(0.5, 0.5, msg, ha="center", va="center",
            transform=ax.transAxes, color=PALETTE["gray"], style="italic")


# ---------------------------------------------------------------------------
# 00 - Overview: row counts per table
# ---------------------------------------------------------------------------

def fig_overview(engine: Engine, out_dir: Path) -> pd.DataFrame:
    rows = []
    for t in table_names(engine):
        try:
            with engine.connect() as con:
                n = int(pd.read_sql_query(f"SELECT COUNT(*) AS n FROM {t}",
                                          con).iloc[0]["n"])
        except Exception:
            n = -1
        rows.append({"table": t, "rows": n})
    df = pd.DataFrame(rows).sort_values("rows", ascending=True)

    fig, ax = plt.subplots(figsize=(8, max(4, 0.28 * max(len(df), 4))))
    colors = [PALETTE["dark"] if t.startswith("fact_")
              else PALETTE["blue"] if t.startswith("dim_")
              else PALETTE["gray"] for t in df["table"]]
    ax.barh(df["table"], df["rows"], color=colors, alpha=0.85)
    ax.set_xscale("symlog")
    ax.set_xlabel("rows (symlog)")
    ax.set_title("Table row counts  ·  fact = dark, dim = blue, other = gray")
    for i, (_, r) in enumerate(df.iterrows()):
        if r["rows"] > 0:
            ax.text(r["rows"], i, f"  {r['rows']:,}",
                    va="center", fontsize=7, color=PALETTE["gray"])
    save(fig, out_dir, "00_overview.png")
    return df


# ---------------------------------------------------------------------------
# 01 - Locations
# ---------------------------------------------------------------------------

def fig_locations(engine: Engine, out_dir: Path) -> pd.DataFrame:
    df = q(engine, """
        SELECT location_id, name, admin_level, country_code,
               centroid_lat AS lat, centroid_lon AS lon
        FROM dim_location
    """)
    fig = plt.figure(figsize=(12, 5))
    gs  = fig.add_gridspec(1, 2, width_ratios=[1.4, 1])

    ax0 = fig.add_subplot(gs[0, 0])
    ax1 = fig.add_subplot(gs[0, 1])

    if df.empty:
        empty_axes(ax0, "dim_location is empty")
        empty_axes(ax1, "dim_location is empty")
    else:
        wc = df[df["lat"].notna() & df["lon"].notna()].copy()
        wc["admin_level"] = wc["admin_level"].fillna("(none)")
        if wc.empty:
            empty_axes(ax0, "No locations with centroid_lat/lon")
        else:
            top_levels = wc["admin_level"].value_counts().head(8).index.tolist()
            for lvl in top_levels:
                sub = wc[wc["admin_level"] == lvl]
                ax0.scatter(sub["lon"], sub["lat"], s=12, alpha=0.7, label=lvl)
            other = wc[~wc["admin_level"].isin(top_levels)]
            if not other.empty:
                ax0.scatter(other["lon"], other["lat"], s=8, alpha=0.4,
                            color=PALETTE["gray"], label="other")
            ax0.set_xlabel("lon")
            ax0.set_ylabel("lat")
            ax0.set_title(f"Locations with coordinates (n={len(wc):,})")
            ax0.legend(fontsize=7, loc="best", frameon=False)
            ax0.set_aspect("equal", adjustable="datalim")

        top_countries = (df["country_code"].fillna("(none)")
                         .value_counts().head(15))
        ax1.barh(top_countries.index[::-1], top_countries.values[::-1],
                 color=PALETTE["blue"], alpha=0.85)
        ax1.set_title(f"Top countries "
                      f"(n={df['country_code'].nunique(dropna=True)} total)")
        ax1.set_xlabel("locations")

    save(fig, out_dir, "01_locations.png")
    return df


# ---------------------------------------------------------------------------
# 02 - Time coverage per fact table
# ---------------------------------------------------------------------------

def fig_time_coverage(engine: Engine, out_dir: Path) -> pd.DataFrame:
    specs = [
        ("fact_vector_observation",     "date_id"),
        ("fact_environment",            "date_id"),
        ("fact_insecticide_resistance", "date_id"),
        ("fact_bionomics",              "date_id"),
        ("fact_disease_case",           "date_id_reported"),
        ("fact_intervention",           "date_start"),
    ]
    existing = set(table_names(engine))
    rows = []
    for tbl, date_col in specs:
        if tbl not in existing:
            continue
        sql = f"""
            SELECT t.year AS year, COUNT(*) AS n
            FROM {tbl} f
            JOIN dim_time t
              ON f.{date_col} = t.date_id
            GROUP BY t.year
            ORDER BY t.year
        """
        df = q(engine, sql)
        for _, r in df.iterrows():
            rows.append({"table": tbl, "year": int(r["year"]),
                         "count": int(r["n"])})
    cov = pd.DataFrame(rows)

    fig, ax = plt.subplots(figsize=(10, 5))
    if cov.empty:
        empty_axes(ax, "No fact tables with joined date_id rows")
    else:
        pivot = (cov.pivot_table(index="year", columns="table",
                                 values="count", aggfunc="sum")
                 .fillna(0))
        for col in pivot.columns:
            ax.plot(pivot.index, pivot[col], marker="o",
                    label=col.replace("fact_", ""), linewidth=1.6)
        ax.set_yscale("symlog")
        ax.set_xlabel("year")
        ax.set_ylabel("rows (symlog)")
        ax.set_title("Fact table coverage by year")
        ax.legend(fontsize=8, frameon=False)
    save(fig, out_dir, "02_time_coverage.png")
    return cov


# ---------------------------------------------------------------------------
# 03 - Vector species composition
# ---------------------------------------------------------------------------

def fig_vector_species(engine: Engine, out_dir: Path) -> pd.DataFrame:
    df = q(engine, """
        SELECT COALESCE(v.species_name, o.vector_code, '(null)') AS species,
               COUNT(*) AS n,
               SUM(o.count) AS total_count
        FROM fact_vector_observation o
        LEFT JOIN dim_vector v ON o.vector_code = v.vector_code
        GROUP BY COALESCE(v.species_name, o.vector_code, '(null)')
        ORDER BY 2 DESC
    """)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    if df.empty:
        for ax in axes:
            empty_axes(ax, "fact_vector_observation is empty")
    else:
        top = df.head(20)
        axes[0].barh(top["species"][::-1], top["n"][::-1],
                     color=PALETTE["blue"], alpha=0.85)
        axes[0].set_title(f"Vector rows by species (top {len(top)})")
        axes[0].set_xlabel("rows")

        top2 = df.sort_values("total_count", ascending=False).head(20)
        top2 = top2[top2["total_count"].fillna(0) > 0]
        if not top2.empty:
            axes[1].barh(top2["species"][::-1], top2["total_count"][::-1],
                         color=PALETTE["dark"], alpha=0.85)
            axes[1].set_title("Total count by species")
            axes[1].set_xlabel("sum(count)")
        else:
            empty_axes(axes[1], "No non-zero count values")
    save(fig, out_dir, "03_vector_species.png")
    return df


# ---------------------------------------------------------------------------
# 04 - Vector observations over time
# ---------------------------------------------------------------------------

def fig_vector_temporal(engine: Engine, out_dir: Path) -> pd.DataFrame:
    df = q(engine, """
        SELECT t.year, t.month, COUNT(*) AS n,
               SUM(o.count) AS total_count
        FROM fact_vector_observation o
        JOIN dim_time t ON o.date_id = t.date_id
        GROUP BY t.year, t.month
        ORDER BY t.year, t.month
    """)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    if df.empty:
        for ax in axes:
            empty_axes(ax, "No vector observations with a valid date")
    else:
        df["ym"] = pd.to_datetime(
            dict(year=df["year"].astype(int),
                 month=df["month"].astype(int), day=1)
        )
        axes[0].bar(df["ym"], df["n"], width=25,
                    color=PALETTE["blue"], alpha=0.85)
        axes[0].set_title("Vector observation rows per month")
        axes[0].set_ylabel("rows")

        axes[1].bar(df["ym"], df["total_count"].fillna(0), width=25,
                    color=PALETTE["dark"], alpha=0.85)
        axes[1].set_title("Total vector count per month")
        axes[1].set_ylabel("sum(count)")
        for ax in axes:
            ax.tick_params(axis="x", labelrotation=30)
    save(fig, out_dir, "04_vector_temporal.png")
    return df


# ---------------------------------------------------------------------------
# 05 - Vector spatial distribution
# ---------------------------------------------------------------------------

def fig_vector_spatial(engine: Engine, out_dir: Path) -> pd.DataFrame:
    df = q(engine, """
        SELECT o.location_id,
               l.name AS site_name,
               l.centroid_lat AS lat,
               l.centroid_lon AS lon,
               COUNT(*) AS n,
               SUM(o.count) AS total_count
        FROM fact_vector_observation o
        LEFT JOIN dim_location l ON o.location_id = l.location_id
        GROUP BY o.location_id, l.name, l.centroid_lat, l.centroid_lon
    """)
    df = df[df["lat"].notna() & df["lon"].notna()].copy()

    fig, ax = plt.subplots(figsize=(8, 7))
    if df.empty:
        empty_axes(ax, "No vector sites with usable coordinates")
    else:
        sizes = 10 + 4 * np.sqrt(df["n"])
        sc = ax.scatter(df["lon"], df["lat"], s=sizes,
                        c=np.log10(df["n"] + 1),
                        cmap="viridis", alpha=0.75,
                        edgecolor="white", linewidth=0.4)
        ax.set_xlabel("lon")
        ax.set_ylabel("lat")
        ax.set_title(f"Vector collection sites (n={len(df):,})")
        ax.set_aspect("equal", adjustable="datalim")
        cbar = fig.colorbar(sc, ax=ax, shrink=0.7)
        cbar.set_label("log10(rows + 1)")
    save(fig, out_dir, "05_vector_spatial.png")
    return df


# ---------------------------------------------------------------------------
# 06 - Environment
# ---------------------------------------------------------------------------

def fig_environment(engine: Engine, out_dir: Path) -> pd.DataFrame:
    df = q(engine, """
        SELECT e.variable,
               COUNT(*) AS n,
               MIN(t.date) AS first_date,
               MAX(t.date) AS last_date,
               COUNT(DISTINCT e.location_id) AS n_locations
        FROM fact_environment e
        JOIN dim_time t ON e.date_id = t.date_id
        GROUP BY e.variable
        ORDER BY e.variable
    """)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    if df.empty:
        for ax in axes:
            empty_axes(ax, "fact_environment has no rows joined to dim_time")
    else:
        axes[0].barh(df["variable"], df["n"],
                     color=PALETTE["green"], alpha=0.85)
        axes[0].set_xscale("symlog")
        axes[0].set_xlabel("rows (symlog)")
        axes[0].set_title("Environment rows per variable")

        geo = q(engine, """
            SELECT DISTINCT e.location_id,
                   l.centroid_lat AS lat, l.centroid_lon AS lon
            FROM fact_environment e
            LEFT JOIN dim_location l ON e.location_id = l.location_id
        """)
        geo = geo[geo["lat"].notna() & geo["lon"].notna()]
        if not geo.empty:
            axes[1].scatter(geo["lon"], geo["lat"],
                            s=10, color=PALETTE["green"], alpha=0.6)
            axes[1].set_xlabel("lon")
            axes[1].set_ylabel("lat")
            axes[1].set_title(f"Environment grid cells (n={len(geo):,})")
            axes[1].set_aspect("equal", adjustable="datalim")
        else:
            empty_axes(axes[1], "No environment locations with coordinates")
    save(fig, out_dir, "06_environment.png")
    return df


# ---------------------------------------------------------------------------
# 07 - Resistance
# ---------------------------------------------------------------------------

def fig_resistance(engine: Engine, out_dir: Path) -> pd.DataFrame:
    spec = q(engine, """
        SELECT COALESCE(v.species_name, r.vector_code, '(null)') AS species,
               COUNT(*) AS n
        FROM fact_insecticide_resistance r
        LEFT JOIN dim_vector v ON r.vector_code = v.vector_code
        GROUP BY COALESCE(v.species_name, r.vector_code, '(null)')
        ORDER BY 2 DESC
    """)
    ins = q(engine, """
        SELECT COALESCE(i.insecticide_name, r.insecticide_code, '(null)') AS insecticide,
               COUNT(*) AS n
        FROM fact_insecticide_resistance r
        LEFT JOIN dim_insecticide i
               ON r.insecticide_code = i.insecticide_code
        GROUP BY COALESCE(i.insecticide_name, r.insecticide_code, '(null)')
        ORDER BY 2 DESC
    """)
    status = q(engine, """
        SELECT COALESCE(resistance_status, '(null)') AS status,
               COUNT(*) AS n
        FROM fact_insecticide_resistance
        GROUP BY COALESCE(resistance_status, '(null)')
        ORDER BY 2 DESC
    """)
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    for ax, df, color, title in [
        (axes[0], spec.head(20), PALETTE["blue"],  "Resistance rows by species"),
        (axes[1], ins.head(20),  PALETTE["amber"], "Resistance rows by insecticide"),
    ]:
        if df.empty:
            empty_axes(ax, "No resistance data")
        else:
            ax.barh(df.iloc[:, 0][::-1], df["n"][::-1], color=color, alpha=0.85)
            ax.set_title(title)
            ax.set_xlabel("rows")
    if status.empty:
        empty_axes(axes[2], "No resistance status values")
    else:
        axes[2].barh(status["status"][::-1], status["n"][::-1],
                     color=PALETTE["red"], alpha=0.85)
        axes[2].set_title("Resistance status distribution")
        axes[2].set_xlabel("rows")
    save(fig, out_dir, "07_resistance.png")
    return status


# ---------------------------------------------------------------------------
# 08 - Bionomics
# ---------------------------------------------------------------------------

def fig_bionomics(engine: Engine, out_dir: Path) -> pd.DataFrame:
    fields = [
        ("feeding_preference", "Feeding preference"),
        ("biting_location",    "Biting location"),
        ("resting_behavior",   "Resting behaviour"),
        ("larval_site_type",   "Larval site type"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    axes = axes.flatten()
    any_data = False
    for i, (col, title) in enumerate(fields):
        df = q(engine, f"""
            SELECT COALESCE({col}, '(null)') AS value, COUNT(*) AS n
            FROM fact_bionomics
            GROUP BY COALESCE({col}, '(null)')
            ORDER BY 2 DESC
        """)
        if df.empty:
            empty_axes(axes[i], f"{title} - no rows")
            continue
        any_data = True
        axes[i].barh(df["value"][::-1], df["n"][::-1],
                     color=PALETTE["blue"], alpha=0.85)
        axes[i].set_title(title)
        axes[i].set_xlabel("rows")
    if not any_data:
        fig.suptitle("fact_bionomics is empty or missing categorical fields",
                     fontsize=11, color=PALETTE["gray"])
    save(fig, out_dir, "08_bionomics.png")
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# 09 - Cases
# ---------------------------------------------------------------------------

def fig_cases(engine: Engine, out_dir: Path) -> pd.DataFrame:
    comp = q(engine, """
        SELECT COALESCE(identification_method_code, '(null)') AS method,
               COUNT(*) AS n
        FROM fact_disease_case
        GROUP BY COALESCE(identification_method_code, '(null)')
        ORDER BY 2 DESC
    """)
    per_year = q(engine, """
        SELECT t.year, COUNT(*) AS n
        FROM fact_disease_case c
        JOIN dim_time t ON c.date_id_reported = t.date_id
        GROUP BY t.year ORDER BY t.year
    """)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    if comp.empty:
        empty_axes(axes[0], "fact_disease_case is empty")
    else:
        axes[0].barh(comp["method"][::-1], comp["n"][::-1],
                     color=PALETTE["blue"], alpha=0.85)
        axes[0].set_title("Case rows by identification method")
        axes[0].set_xlabel("rows")

    if per_year.empty:
        empty_axes(axes[1], "No case rows joinable to dim_time")
    else:
        axes[1].bar(per_year["year"], per_year["n"],
                    color=PALETTE["blue"], alpha=0.85)
        axes[1].set_title("Case rows per year")
        axes[1].set_xlabel("year")
        axes[1].set_ylabel("rows")
    save(fig, out_dir, "09_cases.png")
    return comp


# ---------------------------------------------------------------------------
# 10 - FK integrity / orphan counts
# ---------------------------------------------------------------------------

def fig_fk_integrity(engine: Engine, out_dir: Path) -> pd.DataFrame:
    checks = [
        ("fact_vector_observation",     "location_id",     "dim_location",             "location_id"),
        ("fact_vector_observation",     "vector_code",     "dim_vector",               "vector_code"),
        ("fact_vector_observation",     "source_code",     "dim_source",               "source_code"),
        ("fact_insecticide_resistance", "location_id",     "dim_location",             "location_id"),
        ("fact_insecticide_resistance", "insecticide_code","dim_insecticide",          "insecticide_code"),
        ("fact_insecticide_resistance", "test_type_code",  "dim_resistance_test_type", "test_type_code"),
        ("fact_insecticide_resistance", "vector_code",     "dim_vector",               "vector_code"),
        ("fact_bionomics",              "location_id",     "dim_location",             "location_id"),
        ("fact_bionomics",              "vector_code",     "dim_vector",               "vector_code"),
        ("fact_environment",            "location_id",     "dim_location",             "location_id"),
        ("fact_disease_case",           "location_id",     "dim_location",             "location_id"),
        ("fact_disease_case",           "disease_code",    "dim_disease",              "disease_code"),
        ("fact_disease_burden",         "location_id",     "dim_location",             "location_id"),
    ]
    existing = set(table_names(engine))
    rows = []
    for fact, fk, dim, pk in checks:
        if fact not in existing or dim not in existing:
            continue
        try:
            with engine.connect() as con:
                total = int(pd.read_sql_query(
                    f"SELECT COUNT(*) AS n FROM {fact}", con).iloc[0]["n"])
                orphan = int(pd.read_sql_query(f"""
                    SELECT COUNT(*) AS n FROM {fact} f
                    LEFT JOIN {dim} d ON f.{fk} = d.{pk}
                    WHERE f.{fk} IS NOT NULL AND d.{pk} IS NULL
                """, con).iloc[0]["n"])
        except Exception:
            continue
        rows.append({
            "fact": fact, "fk": fk, "dim": dim,
            "total": total, "orphan": orphan,
            "rate": (orphan / total) if total else 0.0,
        })
    df = pd.DataFrame(rows)

    fig, ax = plt.subplots(figsize=(9, max(3, 0.32 * max(len(df), 4))))
    if df.empty:
        empty_axes(ax, "No FK checks could be run")
    else:
        df = df.sort_values("rate")
        labels = [f"{r.fact}.{r.fk} → {r.dim}" for r in df.itertuples()]
        colors = [PALETTE["red"] if r > 0.01 else PALETTE["green"]
                  for r in df["rate"]]
        ax.barh(labels, df["rate"] * 100, color=colors, alpha=0.85)
        ax.set_xlabel("orphan rate (%)")
        ax.set_title("Foreign key integrity (green = <1%, red = ≥1%)")
        for i, (_, r) in enumerate(df.iterrows()):
            ax.text(r["rate"] * 100, i,
                    f"  {r['orphan']:,} / {r['total']:,}",
                    va="center", fontsize=7, color=PALETTE["gray"])
    save(fig, out_dir, "10_fk_integrity.png")
    return df


# ---------------------------------------------------------------------------
# Summary text
# ---------------------------------------------------------------------------

def write_summary(out_dir: Path,
                  db_source: str, dialect: str,
                  overview: pd.DataFrame,
                  locations: pd.DataFrame,
                  time_cov: pd.DataFrame,
                  vector_species: pd.DataFrame,
                  env_vars: pd.DataFrame,
                  resistance_status: pd.DataFrame,
                  cases: pd.DataFrame,
                  fk: pd.DataFrame) -> Path:

    lines: list[str] = []
    def emit(s: str = "") -> None:
        lines.append(s)

    emit("VBD CDM - exploratory summary")
    emit("=" * 60)
    emit(f"Database source: {db_source}")
    emit(f"Dialect:         {dialect}")
    emit()

    def section(title: str) -> None:
        emit(title)
        emit("-" * 60)

    section("Tables")
    if overview.empty:
        emit("  (none)")
    else:
        for _, r in overview.sort_values("rows", ascending=False).iterrows():
            emit(f"  {r['table']:35s} {r['rows']:>12,}")
    emit()

    section("Locations")
    if locations.empty:
        emit("  (none)")
    else:
        n = len(locations)
        n_coord = int((locations["lat"].notna() &
                       locations["lon"].notna()).sum())
        emit(f"  total:        {n:,}")
        emit(f"  with coords:  {n_coord:,} ({n_coord/n:.0%})")
        emit(f"  countries:    {locations['country_code'].nunique(dropna=True)}")
        emit()
        emit("  top countries:")
        for cc, c in (locations["country_code"].fillna("(none)")
                      .value_counts().head(10).items()):
            emit(f"    {cc:8s} {c:>6,}")
    emit()

    section("Vector species (top 15 by row count)")
    if vector_species.empty:
        emit("  (none)")
    else:
        for _, r in vector_species.head(15).iterrows():
            tc = r["total_count"]
            tc_s = f"{int(tc):>10,}" if pd.notna(tc) else f"{'-':>10s}"
            emit(f"  {r['species']:40s} {r['n']:>8,} rows  count={tc_s}")
    emit()

    section("Environment variables")
    if env_vars.empty:
        emit("  (none)")
    else:
        for _, r in env_vars.iterrows():
            emit(f"  {r['variable']:20s} n={r['n']:>10,}  "
                 f"locations={r['n_locations']:>5,}  "
                 f"{r['first_date']} → {r['last_date']}")
    emit()

    section("Resistance status")
    if resistance_status.empty:
        emit("  (none)")
    else:
        for _, r in resistance_status.iterrows():
            emit(f"  {r['status']:30s} {r['n']:>8,}")
    emit()

    section("Cases by identification method")
    if cases.empty:
        emit("  (no case rows)")
    else:
        for _, r in cases.iterrows():
            emit(f"  {r['method']:30s} {r['n']:>8,}")
    emit()

    section("Foreign key integrity")
    if fk.empty:
        emit("  (no checks run)")
    else:
        for _, r in fk.sort_values("rate", ascending=False).iterrows():
            emit(f"  {r['fact']:30s} . {r['fk']:20s} → {r['dim']:25s} "
                 f"{r['orphan']:>7,} / {r['total']:>10,}  ({r['rate']:.2%})")
    emit()

    path = out_dir / "summary.txt"
    path.write_text("\n".join(lines) + "\n")
    print(f"  wrote {path.name}")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url",  type=str, default=None,
                   help="SQLAlchemy URL. Overrides the project's database.session.")
    p.add_argument("--out",  type=Path, default=DEFAULT_OUT,
                   help=f"Output directory (default: {DEFAULT_OUT})")
    p.add_argument("--only", type=str, default=None,
                   help="Comma-separated subset: overview,locations,time,"
                        "vector_species,vector_temporal,vector_spatial,"
                        "environment,resistance,bionomics,cases,fk")
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    engine, source = resolve_engine(args.url)
    dialect = engine.dialect.name

    print(f"DB source: {source}")
    print(f"Dialect:   {dialect}")
    print(f"URL:       {engine.url.render_as_string(hide_password=True)}")
    print(f"Out:       {args.out}")
    print()

    wanted = set(args.only.split(",")) if args.only else None
    def want(name: str) -> bool:
        return wanted is None or name in wanted

    try:
        overview = locations = time_cov = pd.DataFrame()
        vec_spec = env_vars = res_status = cases = fk = pd.DataFrame()

        if want("overview"):         print("00 overview");         overview = fig_overview(engine, args.out)
        if want("locations"):        print("01 locations");        locations = fig_locations(engine, args.out)
        if want("time"):             print("02 time coverage");    time_cov = fig_time_coverage(engine, args.out)
        if want("vector_species"):   print("03 vector species");   vec_spec = fig_vector_species(engine, args.out)
        if want("vector_temporal"):  print("04 vector temporal");  fig_vector_temporal(engine, args.out)
        if want("vector_spatial"):   print("05 vector spatial");   fig_vector_spatial(engine, args.out)
        if want("environment"):      print("06 environment");      env_vars = fig_environment(engine, args.out)
        if want("resistance"):       print("07 resistance");       res_status = fig_resistance(engine, args.out)
        if want("bionomics"):        print("08 bionomics");        fig_bionomics(engine, args.out)
        if want("cases"):            print("09 cases");            cases = fig_cases(engine, args.out)
        if want("fk"):               print("10 FK integrity");     fk = fig_fk_integrity(engine, args.out)

        print("summary")
        write_summary(args.out, source, dialect,
                      overview, locations, time_cov,
                      vec_spec, env_vars, res_status, cases, fk)
    finally:
        engine.dispose()

    print()
    print(f"Done. See {args.out}/")


if __name__ == "__main__":
    main()