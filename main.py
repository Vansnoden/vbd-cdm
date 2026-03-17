#!/usr/bin/env python
# main.py

import sys
from pathlib import Path

# Ensure the project root is in sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine, Base, SessionLocal
from etl.loaders import DatabaseLoader
from etl.mappings import GBIF_VECTOR_OBSERVATION_MAPPING, VECTOR_ATLAS_MAPPING, VECTOR_ATLAS_SIMPLE_MAPPING
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_tables_if_not_exist():
    """Create all tables defined in Base.metadata."""
    Base.metadata.create_all(bind=engine)
    print("Tables checked/created.")


def run_etl_pipeline():
    """Run the complete ETL pipeline with proper error handling."""
    
    # Initialize database session
    db = SessionLocal()
    loader = DatabaseLoader(db)
    
    pipeline_results = {}
    
    try:
        # ==================== STEP 1: Load Location Data ====================
        print("\n" + "="*50)
        print("STEP 1: Loading location data")
        print("="*50)
        
        region_gpkg = Path(__file__).parent / "study_areas" / "ghana" / "greater_accra.gpkg"
        if region_gpkg.exists():
            stats = loader.load_location_from_gpkg(
                gpkg_path=region_gpkg,
                admin_level='region'
            )
            pipeline_results['location'] = stats
            print(f"Location loading stats: Inserted: {stats['inserted']}, Updated: {stats['updated']}")
        
        # Commit after successful step
        db.commit()
        print("✓ Step 1 committed successfully")
        
        # ==================== STEP 2: Load Environmental Data ====================
        print("\n" + "="*50)
        print("STEP 2: Loading environmental data from NetCDF")
        print("="*50)
        
        data_root = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/clipped/batch_results"
        env_stats = loader.load_environmental_from_netcdf(data_root)
        pipeline_results['environmental'] = env_stats
        print(f"Environmental loading stats: Processed {env_stats['processed_files']} files")
        
        # Commit after successful step
        db.commit()
        print("✓ Step 2 committed successfully")
        
    except Exception as e:
        print(f"❌ Error in steps 1-2: {e}")
        db.rollback()
        logger.error(f"Rolling back transaction due to: {e}")
        
        # Check if we can continue with remaining steps
        print("\n⚠️  Attempting to continue with remaining steps...")
    
    try:
        # ==================== STEP 3: Load Vector Observation Data ====================
        print("\n" + "="*50)
        print("STEP 3: Loading vector observation data from GBIF / Vector Atlas")
        print("="*50)
        
        # vector_stats = loader.load_vector_from_csv(
        #     data_dir="/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/Ghana/gbif_anopheles_occurrence",
        #     pattern="*.csv",
        #     column_mapping=GBIF_VECTOR_OBSERVATION_MAPPING,
        #     sep='\t'
        # )
        vector_stats = loader.load_vector_from_csv(
            data_dir="/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/va/filteredDataGambiae",
            pattern="*.csv",
            column_mapping=VECTOR_ATLAS_SIMPLE_MAPPING,
            sep='\t'
        )
        pipeline_results['vector'] = vector_stats
        print(f"Vector loading stats: Loaded {vector_stats['loaded_records']} records")
        
        # Commit after successful step
        db.commit()
        print("✓ Step 3 committed successfully")
        
    except Exception as e:
        print(f"❌ Error in step 3: {e}")
        db.rollback()
        logger.error(f"Rolling back transaction due to: {e}")
    
    try:
        # ==================== STEP 4: Load Disease Burden Data ====================
        print("\n" + "="*50)
        print("STEP 4: Loading disease burden data from raster")
        print("="*50)
        
        raster_path = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/map/2025_GBD2024_Global_Pf_Incidence_Count_2000/2025_GBD2024_Global_Pf_Incidence_Count_2024.tif"
        
        if Path(raster_path).exists():
            burden_stats = loader.load_disease_burden_from_raster(
                raster_path=raster_path,
                year=2024,
                disease='malaria',
                source='GBD 2024'
            )
            pipeline_results['disease_burden'] = burden_stats
            print(f"Disease burden loading stats: Loaded {burden_stats['loaded']} locations")
            
            # Commit after successful step
            db.commit()
            print("✓ Step 4 committed successfully")
        else:
            print(f"Raster file not found: {raster_path}")
            
    except Exception as e:
        print(f"❌ Error in step 4: {e}")
        db.rollback()
        logger.error(f"Rolling back transaction due to: {e}")
    
    # ==================== Summary ====================
    print("\n" + "="*50)
    print("LOADING COMPLETE - SUMMARY")
    print("="*50)
    
    if 'location' in pipeline_results:
        stats = pipeline_results['location']
        print(f"✓ Location data: {stats['inserted'] + stats['updated']} features processed")
    
    if 'environmental' in pipeline_results:
        print(f"✓ Environmental data: {pipeline_results['environmental']['processed_files']} NetCDF files")
    
    if 'vector' in pipeline_results:
        print(f"✓ Vector observations: {pipeline_results['vector']['loaded_records']} records")
    
    if 'disease_burden' in pipeline_results:
        print(f"✓ Disease burden: {pipeline_results['disease_burden']['loaded']} locations")
    
    return pipeline_results


def main():
    """Main entry point."""
    # Ensure tables exist
    create_tables_if_not_exist()
    
    # Run the pipeline
    results = run_etl_pipeline()
    
    print("\n✨ ETL pipeline execution completed.")


if __name__ == "__main__":
    main()