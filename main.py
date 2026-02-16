#!/usr/bin/env python


#!/usr/bin/env python
# main.py
import sys
from pathlib import Path

from load_environmental_data import load_environmental_factor
from load_location_data import load_gpkg_to_location

# Ensure the project root is in sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine, Base
from database.utils import ensure_dates_exist
from etl.loaders import DatabaseLoader
from database.session import SessionLocal


def create_tables_if_not_exist():
    """Create all tables defined in Base.metadata."""
    # This will create tables only if they don't exist
    Base.metadata.create_all(bind=engine)
    print("Tables checked/created.")


def main():
    # Ensure tables exist
    create_tables_if_not_exist()

    # Run Location data loader
    base = Path(__file__).parent / "study_areas" / "kenya"
    mombasa_gpkg = base / "mombasa_county" / "mombasa_county.gpkg"   # adjust filename
    siaya_gpkg = base / "siaya_county" / "siaya_county.gpkg"
    # Load for each county
    if mombasa_gpkg.exists():
        load_gpkg_to_location(mombasa_gpkg, admin_level='county')
    if siaya_gpkg.exists():
        load_gpkg_to_location(siaya_gpkg, admin_level='county')
   
    # Run the environmental data loader
    load_environmental_factor(
        start_date = '2024-01-01', 
        end_date = '2024-12-31',
        data_dir = '/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results/2024_data'
    )
    load_environmental_factor(
        start_date = '2025-01-01', 
        end_date = '2025-12-31',
        data_dir = '/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results/2025_data'
    )


if __name__ == "__main__":
    main()
