#!/usr/bin/env python


#!/usr/bin/env python
# main.py
import sys
from pathlib import Path

from etl.mappings import GBIF_VECTOR_OBSERVATION_MAPPING, VECTOR_OBSERVATION_MAPPING
from load_case_data import load_case_files
from load_environmental_data import load_environmental_factor
from load_location_data import load_gpkg_to_location
from load_vector_data import load_vector_files

# Ensure the project root is in sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine, Base
from database.utils import ensure_dates_exist
from etl.loaders import DatabaseLoader
from database.session import SessionLocal
from load_annual_netcdf import load_annual_netcdf_files


def create_tables_if_not_exist():
    """Create all tables defined in Base.metadata."""
    # This will create tables only if they don't exist
    Base.metadata.create_all(bind=engine)
    print("Tables checked/created.")


def main():
    # Ensure tables exist
    create_tables_if_not_exist()

    # Run Location data loader
    # base = Path(__file__).parent / "study_areas" / "kenya"
    # mombasa_gpkg = base / "mombasa_county" / "mombasa_county.gpkg"   # adjust filename
    # siaya_gpkg = base / "siaya_county" / "siaya_county.gpkg"

    base = Path(__file__).parent / "study_areas" / "ghana"
    region_gpkg = base / "greater_accra.gpkg"   # adjust filename
    # siaya_gpkg = base / "siaya_county" / "siaya_county.gpkg"

    # Load for each study site
    # if mombasa_gpkg.exists():
    #     load_gpkg_to_location(mombasa_gpkg, admin_level='county')
    # if siaya_gpkg.exists():
    #     load_gpkg_to_location(siaya_gpkg, admin_level='county')

    if region_gpkg.exists():
        load_gpkg_to_location(region_gpkg, admin_level='region')
   
    # Run the annual environmental data loader (processes all NetCDF files)
    data_root = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results"
    load_annual_netcdf_files(data_root)   # <-- this loads both t2m and tp

    # Load vector observation data
    load_vector_files(
        data_dir="/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/Ghana/gbif_anopheles_occurrence",
        pattern="*.csv",
        column_mapping=GBIF_VECTOR_OBSERVATION_MAPPING,
        sep='\t' 
    )

    
if __name__ == "__main__":
    main()
