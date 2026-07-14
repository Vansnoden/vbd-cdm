# fix_cases.py
"""
Standalone script to expand disease burden data to daily case records.
Run this after consolidation if case data is still only 366 records.

Usage:
    python fix_cases.py
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np

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


def expand_burden_to_daily():
    """
    Expand yearly disease burden data (fact_disease_burden) to daily records.
    Uses incidence_rate to estimate cases, or cases_mean if available.
    """
    logger.info("=" * 60)
    logger.info("EXPANDING BURDEN DATA TO DAILY RECORDS")
    logger.info("=" * 60)
    
    session = SessionLocal()
    
    try:
        # First, check what's in the burden table
        total_burden = session.execute(text("SELECT COUNT(*) FROM fact_disease_burden")).scalar()
        logger.info(f"Total burden records: {total_burden}")
        
        if total_burden == 0:
            logger.info("No burden records found to expand")
            return
        
        # Check sample burden records
        sample = session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, 
                   cases_mean, incidence_rate, prevalence_rate
            FROM fact_disease_burden 
            LIMIT 5
        """)).fetchall()
        logger.info(f"Sample burden records: {[(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in sample]}")
        
        # Get population estimates for each location
        logger.info("Estimating population for locations...")
        
        # Try to get population from socioeconomic data
        pop_query = """
            SELECT location_id, indicator_value 
            FROM fact_socioeconomic_condition 
            WHERE indicator_code = 'SP.POP.TOTL'
        """
        pop_results = session.execute(text(pop_query)).fetchall()
        population_map = {r[0]: r[1] for r in pop_results}
        
        # Get all locations
        loc_query = "SELECT location_id FROM dim_location"
        loc_results = session.execute(text(loc_query)).fetchall()
        all_location_ids = [r[0] for r in loc_results]
        
        # For locations without population data, use default
        default_population = 1000000  # 1 million
        for loc_id in all_location_ids:
            if loc_id not in population_map or population_map[loc_id] is None or population_map[loc_id] <= 0:
                population_map[loc_id] = default_population
                logger.debug(f"  Using default population {default_population} for location_id {loc_id}")
        
        logger.info(f"Population map has {len(population_map)} locations")
        
        # Get all burden records with valid data
        burden_records = session.execute(text("""
            SELECT burden_id, location_id, year, disease_code, source_code, 
                   incidence_rate, prevalence_rate, mortality_rate, cases_mean
            FROM fact_disease_burden
            WHERE incidence_rate IS NOT NULL AND incidence_rate > 0
               OR cases_mean IS NOT NULL AND cases_mean > 0
        """)).fetchall()
        
        if not burden_records:
            logger.warning("No burden records with valid data found.")
            return
        
        logger.info(f"Found {len(burden_records)} burden records to expand")
        
        # Seasonal factors for malaria in Kenya (monthly distribution)
        # Wet season (April-July) has higher malaria incidence
        monthly_factors = {
            1: 0.6, 2: 0.5, 3: 0.7, 4: 1.4, 5: 1.5, 6: 1.3,
            7: 1.2, 8: 1.0, 9: 0.8, 10: 0.6, 11: 0.5, 12: 0.5
        }
        
        # Normalize factors so sum = 12 (average of 1)
        total_factor = sum(monthly_factors.values())
        normalized_factors = {m: f * 12 / total_factor for m, f in monthly_factors.items()}
        logger.info(f"Normalized monthly factors: {normalized_factors}")
        
        inserted_count = 0
        skipped_count = 0
        error_count = 0
        
        # Detect database type
        is_sqlite = 'sqlite' in str(session.bind.url).lower()
        logger.info(f"Database type: {'SQLite' if is_sqlite else 'PostgreSQL/Other'}")
        
        for record in burden_records:
            burden_id, location_id, year, disease_code, source_code, incidence_rate, prevalence_rate, mortality_rate, cases_mean = record
            
            # Get population for this location
            population = population_map.get(location_id, default_population)
            
            # Calculate annual cases from incidence rate
            # incidence_rate is per 100,000 population
            if incidence_rate is not None and incidence_rate > 0 and population > 0:
                annual_cases = (incidence_rate / 100000) * population
            elif cases_mean is not None and cases_mean > 0:
                annual_cases = cases_mean
            else:
                logger.warning(f"  Skipping burden_id {burden_id}: no valid incidence_rate or cases_mean")
                skipped_count += 1
                continue
            
            annual_cases = max(annual_cases, 1)
            
            logger.info(f"  Processing burden_id {burden_id}: location_id={location_id}, year={year}, "
                       f"annual_cases={annual_cases:.0f}")
            
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31)
            current_date = start_date
            daily_cases_list = []
            
            while current_date <= end_date:
                month = current_date.month
                if month == 12:
                    days_in_month = 31
                else:
                    next_month = datetime(year, month + 1, 1)
                    days_in_month = (next_month - datetime(year, month, 1)).days
                
                # Monthly total cases = annual cases * monthly factor
                monthly_cases = annual_cases * normalized_factors[month]
                
                # Daily cases = monthly cases / days in month
                daily_cases = monthly_cases / days_in_month
                
                # Add small random variation (±15%) for realism
                variation = 0.85 + 0.3 * np.random.random()
                daily_cases = daily_cases * variation
                daily_cases = max(1, int(round(daily_cases)))
                
                daily_cases_list.append({
                    'location_id': location_id,
                    'date_id': current_date.strftime('%Y-%m-%d'),
                    'disease_code': disease_code,
                    'source_code': source_code
                })
                current_date += timedelta(days=1)
            
            if daily_cases_list:
                try:
                    # Delete existing estimates for this location/year/disease
                    delete_result = session.execute(text("""
                        DELETE FROM fact_disease_case
                        WHERE location_id = :loc_id
                          AND date_id_onset >= :start_date
                          AND date_id_onset <= :end_date
                          AND disease_code = :disease
                          AND identification_method_code = 'ESTIMATE'
                    """), {
                        'loc_id': location_id,
                        'start_date': start_date.strftime('%Y-%m-%d'),
                        'end_date': end_date.strftime('%Y-%m-%d'),
                        'disease': disease_code
                    })
                    
                    if delete_result.rowcount > 0:
                        logger.info(f"    Deleted {delete_result.rowcount} existing estimate records")
                    
                    # Insert new daily records
                    for daily in daily_cases_list:
                        session.execute(text("""
                            INSERT INTO fact_disease_case (
                                location_id, date_id_onset, date_id_reported, disease_code,
                                identification_method_code, source_code, case_status,
                                host_type, age_group, sex, outcome
                            ) VALUES (
                                :loc_id, :date_id, :date_id, :disease,
                                'ESTIMATE', :src_code, 'confirmed', 'human',
                                'all', 'unknown', 'unknown'
                            )
                        """), {
                            'loc_id': daily['location_id'],
                            'date_id': daily['date_id'],
                            'disease': daily['disease_code'],
                            'src_code': daily['source_code']
                        })
                    
                    session.commit()
                    inserted_count += len(daily_cases_list)
                    logger.info(f"  Expanded burden_id {burden_id} ({year}) into {len(daily_cases_list)} daily records")
                    
                except Exception as e:
                    logger.error(f"  Error expanding burden_id {burden_id}: {e}")
                    session.rollback()
                    error_count += 1
                    continue
        
        logger.info("=" * 60)
        logger.info(f"EXPANSION COMPLETE")
        logger.info(f"  Inserted: {inserted_count} daily records")
        logger.info(f"  Skipped: {skipped_count} records")
        logger.info(f"  Errors: {error_count}")
        logger.info("=" * 60)
        
        # Verify the results
        logger.info("Verifying case data...")
        case_count = session.execute(text("""
            SELECT COUNT(*) FROM fact_disease_case
            WHERE identification_method_code = 'ESTIMATE'
        """)).scalar()
        logger.info(f"Total estimate records in fact_disease_case: {case_count}")
        
        # Show date range
        date_range = session.execute(text("""
            SELECT MIN(date_id_onset), MAX(date_id_onset)
            FROM fact_disease_case
            WHERE identification_method_code = 'ESTIMATE'
        """)).first()
        if date_range and date_range[0] and date_range[1]:
            logger.info(f"Date range: {date_range[0]} to {date_range[1]}")
        
        # Show distribution by year
        year_dist = session.execute(text("""
            SELECT SUBSTR(date_id_onset, 1, 4) as year, COUNT(*) as count
            FROM fact_disease_case
            WHERE identification_method_code = 'ESTIMATE'
            GROUP BY year
            ORDER BY year
        """)).fetchall()
        
        if year_dist:
            logger.info("Year distribution:")
            for year, count in year_dist:
                logger.info(f"  {year}: {count} records")
        
    except Exception as e:
        logger.error(f"Error: {e}")
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    expand_burden_to_daily()