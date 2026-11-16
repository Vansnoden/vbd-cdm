# fix_resistance_status.py
from database.session import SessionLocal
from sqlalchemy import text

session = SessionLocal()
try:
    result = session.execute(text("""
        UPDATE fact_insecticide_resistance
        SET 
            mortality = ROUND((n_insect_dead * 100.0) / n_insect_tested, 2),
            resistance_status = CASE
                WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 98 THEN 'Susceptible'
                WHEN (n_insect_dead * 100.0 / n_insect_tested) >= 90 THEN 'Suspected Resistance'
                ELSE 'Resistant'
            END
        WHERE n_insect_tested IS NOT NULL AND n_insect_tested > 0
          AND n_insect_dead IS NOT NULL AND n_insect_dead >= 0
    """))
    session.commit()
    print(f"Updated {result.rowcount} records")
    
    # Verify
    dist = session.execute(text("""
        SELECT resistance_status, COUNT(*) 
        FROM fact_insecticide_resistance 
        WHERE resistance_status IS NOT NULL 
        GROUP BY resistance_status
    """)).fetchall()
    for status, count in dist:
        print(f"{status}: {count}")
finally:
    session.close()