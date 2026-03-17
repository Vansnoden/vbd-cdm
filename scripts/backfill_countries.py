#!/usr/bin/env python
# scripts/backfill_countries.py

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.session import SessionLocal
from etl.loaders import DatabaseLoader

def main():
    db = SessionLocal()
    loader = DatabaseLoader(db)
    
    try:
        print("Backfilling country information for all locations...")
        updated = loader._populate_location_countries()
        print(f"Done! Updated {updated} locations.")
    finally:
        db.close()

if __name__ == "__main__":

    main()