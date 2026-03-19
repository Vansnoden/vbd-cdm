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
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_tables_if_not_exist():
    """Create all tables defined in Base.metadata."""
    Base.metadata.create_all(bind=engine)
    print("Tables checked/created.")


def load_map_rasters(loader, map_data_dir, years=None, disease='malaria', source='Malaria Atlas Project'):
    """
    Helper function to load multiple MAP raster files.
    
    Args:
        loader: DatabaseLoader instance
        map_data_dir: Base directory containing MAP raster files
        years: List of years to load (if None, attempts to load all years found)
        disease: Disease name
        source: Data source name
    
    Returns:
        Dictionary with loading statistics for each year
    """
    map_data_dir = Path(map_data_dir)
    
    # Common MAP file naming patterns - adjust these to match your files
    patterns = [
        "*_Incidence_*.tif",
        "*_Pf_Incidence_*.tif",
        "*_incidence_*.tif",
        "*_Incidence_Count_*.tif"
    ]
    
    if not map_data_dir.exists():
        print(f"MAP data directory not found: {map_data_dir}")
        return {}
    
    # Find all raster files
    raster_files = []
    for pattern in patterns:
        raster_files.extend(list(map_data_dir.rglob(pattern)))
    
    if not raster_files:
        print(f"No MAP raster files found in {map_data_dir}")
        return {}
    
    print(f"Found {len(raster_files)} MAP raster files")
    
    # Group files by year (attempt to extract year from filename)
    files_by_year = {}
    for raster_file in raster_files:
        # Try to extract year from filename (look for 4-digit number)
        import re
        year_matches = re.findall(r'((?:19|20)\d{2})', raster_file.name)
        if year_matches:
            year = int(year_matches[len(year_matches) - 1])
            if years is None or year in years:
                files_by_year[year] = raster_file
        else:
            print(f"Could not extract year from filename: {raster_file.name}")
    
    if not files_by_year:
        print("No files with identifiable years found")
        return {}
    
    # Load each year's raster
    results = {}
    for year, raster_file in sorted(files_by_year.items()):
        print(f"\nLoading MAP data for year {year} from {raster_file.name}")
        
        try:
            stats = loader.load_map_raster_to_disease_burden(
                raster_path=raster_file,
                year=year,
                disease=disease,
                source=source,
                version=f"MAP_{year}",
                metric_type='incidence_rate',
                stat='mean'
            )
            results[year] = stats
            print(f"  ✓ Loaded {stats['loaded']} locations for {year}")
            
        except Exception as e:
            print(f"  ❌ Failed to load {year}: {e}")
            results[year] = {'error': str(e)}
    
    return results


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
        
        # GBIF data
        gbif_stats = loader.load_vector_from_csv(
            data_dir="/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/gbif/africa",
            pattern="*.csv",
            column_mapping=GBIF_VECTOR_OBSERVATION_MAPPING,
            sep='\t'
        )
        pipeline_results['vector_gbif'] = gbif_stats
        print(f"GBIF vector loading stats: Loaded {gbif_stats['loaded_records']} records")
        
        # Vector Atlas data (commented out by default, uncomment if needed)
        # va_stats = loader.load_vector_from_csv(
        #     data_dir="/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/va/filteredDataGambiae",
        #     pattern="*.csv",
        #     column_mapping=VECTOR_ATLAS_SIMPLE_MAPPING,
        #     sep='\t'
        # )
        # pipeline_results['vector_atlas'] = va_stats
        # print(f"Vector Atlas loading stats: Loaded {va_stats['loaded_records']} records")
        
        # Commit after successful step
        db.commit()
        print("✓ Step 3 committed successfully")
        
    except Exception as e:
        print(f"❌ Error in step 3: {e}")
        db.rollback()
        logger.error(f"Rolling back transaction due to: {e}")
    
    try:
        # ==================== STEP 4: Load Disease Burden Data (single raster) ====================
        print("\n" + "="*50)
        print("STEP 4: Loading disease burden data from single raster")
        print("="*50)
        
        raster_path = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/map/2025_GBD2024_Global_Pf_Incidence_Count_2000/2025_GBD2024_Global_Pf_Incidence_Count_2024.tif"
        
        if Path(raster_path).exists():
            burden_stats = loader.load_disease_burden_from_raster(
                raster_path=raster_path,
                year=2024,
                disease='malaria',
                source='GBD 2024'
            )
            pipeline_results['disease_burden_2024'] = burden_stats
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
    
    try:
        # ==================== STEP 5: Load Multiple MAP Rasters ====================
        print("\n" + "="*50)
        print("STEP 5: Loading multiple MAP raster time series")
        print("="*50)
        
        # Base directory for MAP data
        map_base_dir = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/map"
        
        # Define specific years to load (adjust based on your available data)
        target_years = list(range(2000,2025))
        
        # Option 1: Load from a specific subdirectory
        map_incidence_dir = Path(map_base_dir) / "incidence"
        if map_incidence_dir.exists():
            map_results = load_map_rasters(
                loader=loader,
                map_data_dir=map_incidence_dir,
                years=target_years,
                disease='malaria',
                source='Malaria Atlas Project'
            )
            pipeline_results['map_time_series'] = map_results
            
            # Count total loaded across years
            total_loaded = sum(stats.get('loaded', 0) for stats in map_results.values() if isinstance(stats, dict))
            print(f"\nMAP time series loading complete: {total_loaded} total location-year records")
            
            # Commit after successful step
            db.commit()
            print("✓ Step 5 committed successfully")
        else:
            print(f"MAP incidence directory not found: {map_incidence_dir}")
            
    except Exception as e:
        print(f"❌ Error in step 5: {e}")
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
    
    if 'vector_gbif' in pipeline_results:
        print(f"✓ GBIF vector observations: {pipeline_results['vector_gbif']['loaded_records']} records")
    
    if 'vector_atlas' in pipeline_results:
        print(f"✓ Vector Atlas observations: {pipeline_results['vector_atlas']['loaded_records']} records")
    
    if 'disease_burden_2024' in pipeline_results:
        print(f"✓ Disease burden 2024: {pipeline_results['disease_burden_2024']['loaded']} locations")
    
    if 'map_time_series' in pipeline_results:
        total_map = sum(stats.get('loaded', 0) for stats in pipeline_results['map_time_series'].values() 
                       if isinstance(stats, dict))
        years_loaded = len([y for y, s in pipeline_results['map_time_series'].items() 
                           if isinstance(s, dict) and s.get('loaded', 0) > 0])
        print(f"✓ MAP time series: {total_map} records across {years_loaded} years")
    
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