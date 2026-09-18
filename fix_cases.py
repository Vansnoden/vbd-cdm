# fix_cases.py - Modified version with daily counts stored as a single row

import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

# Add project root to path
project_root = Path(__file__).parent.absolute()
sys.path.insert(0, str(project_root))

from database.session import SessionLocal
from sqlalchemy import text

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ========================= CONFIGURATION =========================

# Seasonal factors by month (tropical regions)
SEASONAL_FACTORS = {
    1: 0.55, 2: 0.50, 3: 0.65, 4: 1.35, 5: 1.50, 6: 1.40,
    7: 1.30, 8: 1.10, 9: 0.85, 10: 0.65, 11: 0.55, 12: 0.50,
}

WEEKLY_PATTERNS = {
    1: 0.95, 2: 1.00, 3: 1.05, 4: 1.10, 5: 1.10, 6: 0.90, 7: 0.85,
}


def get_seasonal_factor(month: int) -> float:
    return SEASONAL_FACTORS.get(month, 1.0)


def get_weekly_factor(date: datetime) -> float:
    return WEEKLY_PATTERNS.get(date.isocalendar()[2], 1.0)


def generate_daily_case_counts(
    annual_cases: float,
    year: int,
    location_id: int,
    base_variation: float = 0.25,
    seed: int = None
) -> pd.DataFrame:
    """
    Generate daily case counts with realistic variation.
    Returns one row per day with the case count.
    """
    if seed is not None:
        np.random.seed(seed + location_id + year)
    
    start_date = datetime(year, 1, 1)
    end_date = datetime(year, 12, 31)
    date_range = pd.date_range(start_date, end_date)
    
    daily_data = []
    
    # Pre-calculate all daily factors to normalize properly
    daily_factors = []
    for date in date_range:
        month = date.month
        
        seasonal_factor = get_seasonal_factor(month)
        weekly_factor = get_weekly_factor(date)
        
        day_in_month = date.day
        days_in_month = (date.replace(month=month % 12 + 1, day=1) - 
                        date.replace(day=1)).days
        intra_month_phase = (day_in_month / days_in_month) * 2 * np.pi
        intra_month_factor = 1.0 + 0.08 * np.sin(intra_month_phase + month * 0.5)
        
        noise = np.random.lognormal(0, base_variation * 0.7)
        
        holiday_factor = 1.0
        if date.month == 12 and date.day in [25, 26]:
            holiday_factor = 0.80
        elif date.month == 1 and date.day == 1:
            holiday_factor = 0.75
        elif date.month in [4, 5]:
            if date.month == 4 and 1 <= date.day <= 10:
                holiday_factor = 0.85
            elif date.month == 5 and date.day <= 5:
                holiday_factor = 0.85
        
        spike_factor = 1.0
        if np.random.random() < 0.005:
            spike_factor = 1.0 + np.random.exponential(0.5)
        
        daily_factor = (
            seasonal_factor * weekly_factor * intra_month_factor * 
            noise * holiday_factor * spike_factor
        )
        daily_factors.append(daily_factor)
    
    # Normalize so sum of factors = number of days (so total = annual_cases)
    total_factor = sum(daily_factors)
    if total_factor > 0:
        daily_factors = [f / total_factor * len(date_range) for f in daily_factors]
    
    # Generate daily case counts
    for date, factor in zip(date_range, daily_factors):
        daily_cases = annual_cases * factor
        daily_cases = max(1, int(round(daily_cases)))
        
        daily_data.append({
            'date_id': date.strftime('%Y-%m-%d'),
            'year': year,
            'month': date.month,
            'case_count': daily_cases,
            'location_id': location_id,
        })
    
    return pd.DataFrame(daily_data)


def get_population_for_location(session, location_id: int, default_population: int = 100000) -> int:
    result = session.execute(text("""
        SELECT indicator_value 
        FROM fact_socioeconomic_condition 
        WHERE location_id = :loc_id 
          AND indicator_code = 'SP.POP.TOTL'
        ORDER BY year DESC
        LIMIT 1
    """), {'loc_id': location_id}).first()
    
    if result and result[0] and result[0] > 0:
        return int(result[0])
    
    result = session.execute(text("""
        SELECT name FROM dim_location WHERE location_id = :loc_id
    """), {'loc_id': location_id}).first()
    
    if result:
        name = result[0].lower()
        if 'county' in name or 'district' in name:
            return 500000
        elif 'sub-county' in name or 'ward' in name:
            return 100000
        elif 'village' in name:
            return 5000
    
    return default_population


def verify_case_data(session):
    """Verify the case data after expansion."""
    
    # Check if case_count column exists
    try:
        # Try to get stats from case_count column
        stats = session.execute(text("""
            SELECT 
                MIN(case_count) as min_cases,
                MAX(case_count) as max_cases,
                AVG(case_count) as avg_cases,
                COUNT(*) as total_records
            FROM fact_disease_case
            WHERE identification_method_code = 'ESTIMATE'
        """)).first()
        
        if stats and stats[0] is not None:
            logger.info(f"Total estimate records: {stats[3]}")
            logger.info(f"Case statistics: min={stats[0]:.0f}, max={stats[1]:.0f}, avg={stats[2]:.1f}")
            
            if stats[0] == stats[1]:
                logger.warning("⚠️  WARNING: All case counts are identical! No variation in the data.")
            else:
                logger.info("✅ Variation detected in case counts!")
            
            # Show sample
            sample = session.execute(text("""
                SELECT date_id_onset, case_count
                FROM fact_disease_case
                WHERE identification_method_code = 'ESTIMATE'
                ORDER BY date_id_onset
                LIMIT 20
            """)).fetchall()
            
            if sample:
                logger.info("Sample of daily case counts (first 20 days):")
                for date_id, count in sample[:10]:
                    logger.info(f"  {date_id}: {count} cases")
            
            return
    
    except Exception as e:
        logger.warning(f"Could not query case_count column: {e}")
    
    # Fallback: count rows per date
    logger.info("Counting cases per date (fallback method)...")
    daily_counts = session.execute(text("""
        SELECT date_id_onset, COUNT(*) as case_count
        FROM fact_disease_case
        WHERE identification_method_code = 'ESTIMATE'
        GROUP BY date_id_onset
        ORDER BY date_id_onset
        LIMIT 100
    """)).fetchall()
    
    if daily_counts:
        counts = [row[1] for row in daily_counts]
        logger.info(f"Daily case counts (sample of 100 days):")
        logger.info(f"  Min: {min(counts)}, Max: {max(counts)}, Avg: {sum(counts)/len(counts):.1f}")
        
        if min(counts) == max(counts):
            logger.warning("⚠️  WARNING: All sampled daily counts are identical! No variation in the data.")
        else:
            logger.info("✅ Variation detected in daily case counts!")


def expand_burden_to_daily(
    dry_run: bool = False,
    specific_location_id: int = None,
    seed: int = 42,
    base_variation: float = 0.25,
    force: bool = False
):
    """
    Expand yearly disease burden data to daily records.

    Each day gets a single row in fact_disease_case with the daily case count
    stored in the case_count column.

    Parameters
    ----------
    dry_run : bool
        If True, generate data and log it, but don't write to the database.
    specific_location_id : int, optional
        If provided, only expand burden records for this location.
    seed : int
        Random seed used when generating daily variation.
    base_variation : float
        Base lognormal variation for the daily noise term (0-1, typical 0.2-0.4).
    force : bool
        If True, delete and re-insert existing ESTIMATE rows for a burden record.
        If False (default), skip burden records that already have ESTIMATE rows.
    """
    logger.info("=" * 60)
    logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS (WITH VARIATION)")
    logger.info("=" * 60)

    if dry_run:
        logger.info("*** DRY RUN - No data will be inserted ***")

    session = SessionLocal()

    try:
        # CHANGED: Removed the ad-hoc ALTER TABLE block.
        # The case_count column is now created by an Alembic migration
        # (see alembic/versions/*_add_case_count_to_fact_disease_case.py).
        # If the column is missing, fail loudly instead of silently patching.
        try:
            session.execute(text("SELECT case_count FROM fact_disease_case LIMIT 1"))
            logger.info("✓ case_count column exists")
        except Exception as e:
            logger.error(
                "case_count column missing from fact_disease_case. "
                "Run `alembic upgrade head` before running this script."
            )
            raise RuntimeError("Missing case_count column; run migrations first.") from e

        total_burden = session.execute(
            text("SELECT COUNT(*) FROM fact_disease_burden")
        ).scalar()
        logger.info(f"Total burden records: {total_burden}")

        if total_burden == 0:
            logger.info("No burden records found to expand")
            return

        burden_query = """
            SELECT burden_id, location_id, year, disease_code, source_code,
                   incidence_rate, cases_mean
            FROM fact_disease_burden
            WHERE (incidence_rate IS NOT NULL AND incidence_rate > 0)
               OR (cases_mean IS NOT NULL AND cases_mean > 0)
        """
        if specific_location_id:
            burden_query += f" AND location_id = {int(specific_location_id)}"

        burden_records = session.execute(text(burden_query)).fetchall()

        if not burden_records:
            logger.warning("No burden records with valid data found.")
            return

        logger.info(f"Found {len(burden_records)} burden records to expand")

        # Pre-compute populations once (avoids N+1 lookups)
        location_ids = {rec[1] for rec in burden_records}
        logger.info(f"Processing {len(location_ids)} locations")

        population_map = {}
        for loc_id in location_ids:
            population_map[loc_id] = get_population_for_location(session, loc_id)

        inserted_count = 0
        skipped_count = 0
        already_present_count = 0     # CHANGED: track idempotent skips separately
        error_count = 0

        for record in burden_records:
            (burden_id, location_id, year, disease_code,
             source_code, incidence_rate, cases_mean) = record

            population = population_map.get(location_id, 100000)

            if incidence_rate is not None and incidence_rate > 0 and population > 0:
                annual_cases = (incidence_rate / 100000) * population
            elif cases_mean is not None and cases_mean > 0:
                annual_cases = cases_mean
            else:
                logger.warning(f"  Skipping burden_id {burden_id}: no valid data")
                skipped_count += 1
                continue

            annual_cases = max(annual_cases, 1)

            start_date = f"{year}-01-01"
            end_date = f"{year}-12-31"

            # CHANGED: Idempotency guard. If ESTIMATE rows already exist for
            # (location, year, disease) and force=False, skip entirely.
            existing = session.execute(text("""
                SELECT COUNT(*) FROM fact_disease_case
                WHERE location_id = :loc_id
                  AND date_id_onset >= :start_date
                  AND date_id_onset <= :end_date
                  AND disease_code = :disease
                  AND identification_method_code = 'ESTIMATE'
            """), {
                'loc_id': location_id,
                'start_date': start_date,
                'end_date': end_date,
                'disease': disease_code,
            }).scalar()

            if existing and existing > 0 and not force:
                logger.info(
                    f"  Skipping burden_id {burden_id} (location={location_id}, "
                    f"year={year}, disease={disease_code}): "
                    f"{existing} ESTIMATE rows already exist. "
                    f"Use force=True to overwrite."
                )
                already_present_count += 1
                continue

            logger.info(
                f"  Processing burden_id {burden_id}: "
                f"location={location_id}, year={year}, "
                f"annual_cases={annual_cases:.0f}"
            )

            daily_df = generate_daily_case_counts(
                annual_cases=annual_cases,
                year=year,
                location_id=location_id,
                base_variation=base_variation,
                seed=seed,
            )

            logger.info(f"    Generated {len(daily_df)} daily records")
            logger.info(
                f"    Mean: {daily_df['case_count'].mean():.1f}, "
                f"Min: {daily_df['case_count'].min()}, "
                f"Max: {daily_df['case_count'].max()}, "
                f"Std: {daily_df['case_count'].std():.1f}"
            )

            if dry_run:
                sample = daily_df.head(10)[['date_id', 'case_count']]
                logger.info(f"    Sample (first 10 days):\n{sample.to_string()}")
                continue

            try:
                # Delete existing ESTIMATE rows if force=True (or if they were
                # partially present). If existing == 0, this is a no-op.
                if existing and existing > 0:
                    delete_result = session.execute(text("""
                        DELETE FROM fact_disease_case
                        WHERE location_id = :loc_id
                          AND date_id_onset >= :start_date
                          AND date_id_onset <= :end_date
                          AND disease_code = :disease
                          AND identification_method_code = 'ESTIMATE'
                    """), {
                        'loc_id': location_id,
                        'start_date': start_date,
                        'end_date': end_date,
                        'disease': disease_code,
                    })
                    logger.info(f"    Deleted {delete_result.rowcount} existing ESTIMATE records")

                # Insert daily records with case_count populated.
                batch_size = 5000
                total_days = len(daily_df)
                records_to_insert = []
                is_postgres = 'postgresql' in str(session.bind.url)

                for idx, row in daily_df.iterrows():
                    records_to_insert.append({
                        'loc_id': location_id,
                        'date_id': row['date_id'],
                        'disease': disease_code,
                        'src_code': source_code,
                        'case_count': int(row['case_count']),   # CHANGED: explicit int
                    })

                    is_last = (idx == total_days - 1)
                    if len(records_to_insert) >= batch_size or is_last:
                        if is_postgres:
                            # PostgreSQL: executemany-style with a list of dicts.
                            session.execute(text("""
                                INSERT INTO fact_disease_case (
                                    location_id, date_id_onset, date_id_reported,
                                    disease_code, identification_method_code,
                                    source_code, case_status, host_type,
                                    age_group, sex, outcome, case_count
                                ) VALUES (
                                    :loc_id, :date_id, :date_id,
                                    :disease, 'ESTIMATE',
                                    :src_code, 'confirmed', 'human',
                                    'all', 'unknown', 'unknown', :case_count
                                )
                            """), records_to_insert)
                        else:
                            # SQLite: loop (SQLAlchemy will not expand the list
                            # into multiple VALUES rows with this driver in the
                            # same way).
                            for rec in records_to_insert:
                                session.execute(text("""
                                    INSERT INTO fact_disease_case (
                                        location_id, date_id_onset, date_id_reported,
                                        disease_code, identification_method_code,
                                        source_code, case_status, host_type,
                                        age_group, sex, outcome, case_count
                                    ) VALUES (
                                        :loc_id, :date_id, :date_id,
                                        :disease, 'ESTIMATE',
                                        :src_code, 'confirmed', 'human',
                                        'all', 'unknown', 'unknown', :case_count
                                    )
                                """), rec)
                        records_to_insert = []

                session.commit()
                inserted_count += total_days
                logger.info(
                    f"  ✓ Expanded burden_id {burden_id} ({year}) "
                    f"into {total_days} daily records"
                )

            except Exception as e:
                logger.error(f"  ✗ Error expanding burden_id {burden_id}: {e}")
                session.rollback()
                error_count += 1
                continue

        if not dry_run:
            logger.info("=" * 60)
            logger.info("EXPANSION COMPLETE")
            logger.info(f"  Inserted:        {inserted_count} daily records")
            logger.info(f"  Already present: {already_present_count} burden records skipped (use force=True to overwrite)")
            logger.info(f"  Skipped (no data): {skipped_count} records")
            logger.info(f"  Errors:          {error_count}")
            logger.info("=" * 60)

            logger.info("Verifying case data...")
            verify_case_data(session)
        else:
            logger.info("=" * 60)
            logger.info("DRY RUN COMPLETE - No data inserted")
            logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Error: {e}")
        session.rollback()
        raise
    finally:
        session.close()

def main():
    parser = argparse.ArgumentParser(
        description="Expand disease burden data to daily records with realistic variation"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without inserting")
    parser.add_argument("--location", type=int, help="Only process a specific location ID")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--variation", type=float, default=0.25, help="Base variation (0-1, default: 0.25)")
    
    args = parser.parse_args()
    
    expand_burden_to_daily(
        dry_run=args.dry_run,
        specific_location_id=args.location,
        seed=args.seed,
        base_variation=args.variation
    )


if __name__ == "__main__":
    main()