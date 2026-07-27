# -*- coding: utf-8 -*-

"""
CDM Integration Framework - Orchestrator
Reads base_config.yaml, runs ETL pipelines for all data sources.
"""

import os
import sys
import logging
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from datetime import datetime
from sqlalchemy import text

import pandas as pd
from dotenv import load_dotenv

from utilities.consolidation import DataConsolidator, run_consolidation, enrich_location_dataframe

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

# Local imports
from database import models
from database.session import SessionLocal
from utilities.data_transform_library import load_yaml_config
from etl.extractors.file_extractor import FileExtractor
from etl.transformers.mapping_transformer import MappingTransformer
from etl.loaders.db_loader import DBLoader
from utilities.database_summary import schema_summary_to_csv, export_all_table_samples

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PROJECT_ROOT / 'logs' / 'etl.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)


def setup_environment():
    """Load environment variables and create necessary directories."""
    load_dotenv()
    (PROJECT_ROOT / 'logs').mkdir(exist_ok=True)
    (PROJECT_ROOT / 'outputs').mkdir(exist_ok=True)


def get_all_files_from_folder(folder_path: str, extension: str) -> List[str]:
    """
    Recursively find all files with a given extension in a folder.

    Args:
        folder_path: Root folder to scan.
        extension: File extension without dot (e.g., 'csv', 'grib').

    Returns:
        List of full file paths.
    """
    ext = extension.lower()
    files = []
    for root, _, filenames in os.walk(folder_path):
        for f in filenames:
            if f.lower().endswith(f'.{ext}'):
                files.append(os.path.join(root, f))
    return files


def get_file_paths(source_config: Dict[str, Any]) -> List[str]:
    """Get list of file paths from source configuration."""
    source_type = source_config.get('type', 'file')
    extension = source_config.get('extension', '').lower()
    source_path = source_config.get('path')
    if not extension:
        raise ValueError(f"Source missing 'extension' field")
    if not source_path:
        raise ValueError(f"Source missing 'path' field")
    if not os.path.isabs(source_path):
        source_path = str(PROJECT_ROOT / source_path)
    if source_type == 'folder':
        file_paths = get_all_files_from_folder(source_path, extension)
        if not file_paths:
            logger.warning(f"No .{extension} files found in folder {source_path}")
        else:
            logger.info(f"Found {len(file_paths)} files in folder {source_path}")
    else:
        file_paths = [source_path]
    return file_paths


def extract_data(file_path: str, source_config: Dict[str, Any]) -> pd.DataFrame:
    """Extract data from a single file."""
    extract_mode = source_config.get('extract_mode', 'single')
    reader_args = source_config.get('extension_options', {})
    if 'variables' in source_config:
        reader_args['variables'] = source_config['variables']
    if 'time_idx' in source_config:
        reader_args['time_idx'] = source_config['time_idx']
    extractor = FileExtractor(extract_mode=extract_mode, large_df_threshold=10_000_000)
    df = extractor.extract(file_path, **reader_args)
    if df.empty:
        logger.warning(f"No data extracted from {file_path}")
    logger.info(f"Extracted columns: {list(df.columns)}")
    logger.info(f"First row: {df.iloc[0].to_dict()}")
    logger.info(f"Number of nulls in 'value': {df['value'].isna().sum() if 'value' in df.columns else 'column missing'}")
    return df


def get_mapping_path(source_config: Dict[str, Any]) -> str:
    """Get and validate mapping CSV path."""
    mapping_path = source_config.get('matching')
    if not mapping_path:
        raise ValueError(f"Source missing 'matching' field")
    if not os.path.isabs(mapping_path):
        mapping_path = str(PROJECT_ROOT / mapping_path)
    return mapping_path


def init_dim_time(start_date: str, end_date: str):
    """
    Initialize the dim_time table with all dates from start_date to end_date.
    
    Args:
        start_date: Start date in 'YYYY-MM-DD' format
        end_date: End date in 'YYYY-MM-DD' format
    
    Example:
        init_dim_time('2020-01-01', '2025-12-31')
    """
    from datetime import datetime, timedelta
    logger.info(f"Initializing dim_time from {start_date} to {end_date}")
    # Parse dates
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    if start > end:
        raise ValueError(f"start_date ({start_date}) must be before end_date ({end_date})")
    with SessionLocal() as session:
        current = start
        count = 0
        skipped = 0
        while current <= end:
            date_id = current.strftime('%Y-%m-%d')
            # Check if date already exists
            existing = session.query(models.DimTime).filter_by(date_id=date_id).first()
            if existing:
                skipped += 1
                current += timedelta(days=1)
                continue
            # Determine season (adjust based on your region)
            month = current.month
            # For tropical regions: dry season Nov-Mar, wet season Apr-Oct
            if month in [11, 12, 1, 2, 3]:
                season = 'dry'
            else:
                season = 'wet'
            dt = models.DimTime(
                date_id=date_id,
                date=current,
                year=current.year,
                month=current.month,
                week=current.isocalendar()[1],
                day_of_year=current.timetuple().tm_yday,
                season=season,
                is_holiday=False
            )
            session.add(dt)
            count += 1
            # Commit every 1000 records to avoid memory issues
            if count % 1000 == 0:
                session.commit()
                logger.info(f"  Committed {count} records so far...")
            current += timedelta(days=1)
        # Final commit
        session.commit()
        logger.info(f"  Created {count} new time records, {skipped} already existed")


def get_dimension_tables() -> List[str]:
    """Return list of dimension tables in order of dependency."""
    return [
        'dim_source',
        'dim_vector',
        'dim_location',
        'dim_disease',
        'dim_insecticide',
        'dim_identification_method',
        'dim_resistance_test_type',
        'dim_socioeconomic_indicator'
    ]


def transform_dimensions(df: pd.DataFrame, mapping_path: str) -> Dict[str, pd.DataFrame]:
    """Transform source DataFrame to dimension tables only."""
    transformer = MappingTransformer(mapping_path)
    all_tables = transformer.transform(df)
    dim_tables = get_dimension_tables()
    dim_dfs = {t: all_tables[t] for t in dim_tables if t in all_tables and not all_tables[t].empty}
    return dim_dfs


def load_dimensions(dim_dfs: Dict[str, pd.DataFrame], loader: DBLoader) -> int:
    """Load dimension tables into database. Returns total rows loaded."""
    total_rows = 0
    for table_name in get_dimension_tables():
        if table_name not in dim_dfs:
            continue
        dim_df = dim_dfs[table_name]
        logger.info(f"  Loading dimension {table_name} with {len(dim_df)} rows...")
        if table_name == 'dim_location':
            # Fill country_code from lat/lon for rows that miss it
            dim_df = enrich_location_dataframe(dim_df)
            # Optionally drop rows without location_id (if required)
            if 'location_id' in dim_df.columns:
                before = len(dim_df)
                dim_df = dim_df.dropna(subset=['location_id'])
                if len(dim_df) < before:
                    logger.warning(f"  Removed {before - len(dim_df)} rows with NULL location_id")
        rows = loader.load_table(table_name, dim_df)
        total_rows += rows
        logger.info(f"  Loaded {rows} rows into {table_name}")
    return total_rows


def get_fact_tables() -> List[str]:
    """Return list of fact tables."""
    return [
        'fact_vector_observation',
        'fact_disease_case',
        'fact_insecticide_resistance',
        'fact_bionomics',
        'fact_intervention',
        'fact_socioeconomic_condition',
        'fact_health_facility',
        'fact_disease_burden',
        'fact_environment'
    ]


def transform_facts(df: pd.DataFrame, mapping_path: str) -> Dict[str, pd.DataFrame]:
    """Transform source DataFrame to fact tables only."""
    transformer = MappingTransformer(mapping_path)
    all_tables = transformer.transform(df)
    fact_tables = get_fact_tables()
    fact_dfs = {t: all_tables[t] for t in fact_tables if t in all_tables and not all_tables[t].empty}
    return fact_dfs


def load_facts(fact_dfs: Dict[str, pd.DataFrame], loader: DBLoader) -> int:
    total_rows = 0
    for table_name in get_fact_tables():
        if table_name not in fact_dfs:
            continue
        fact_df = fact_dfs[table_name]
        logger.info(f"  Processing fact {table_name} with {len(fact_df)} rows...")
        # Drop rows with null value for fact_environment
        if table_name == 'fact_environment':
            if 'value' in fact_df.columns:
                before = len(fact_df)
                fact_df = fact_df.dropna(subset=['value'])
                logger.info(f"    Dropped {before - len(fact_df)} rows with null value")
            if 'location_id' in fact_df.columns:
                before = len(fact_df)
                fact_df = fact_df.dropna(subset=['location_id'])
                logger.info(f"    Dropped {before - len(fact_df)} rows with null location_id")
        if not fact_df.empty:
            rows = loader.load_table(table_name, fact_df)
            total_rows += rows
            logger.info(f"  Loaded {rows} rows into {table_name}")
        else:
            logger.warning(f"No valid rows to load for {table_name}")
    return total_rows


def run_ingestion_for_source(source_config: Dict[str, Any]) -> bool:
    """
    Run the complete ETL pipeline for a single data source.
    """
    import re
    source_name = source_config.get('source_name', 'unknown')
    logger.info(f"===== Processing source: {source_name} =====")
    try:
        # Step 1: Get file paths
        file_paths = get_file_paths(source_config)
        if not file_paths:
            logger.warning(f"No files to process for {source_name}")
            return False
        # Step 2: Get mapping path
        mapping_path = get_mapping_path(source_config)
        # step 2.5: init time dimension
        init_dim_time(start_date="1900-01-01", end_date="2026-12-31")
        
        # Step 3: Create loader
        loader = DBLoader(batch_size=10000, upsert=True, use_copy=True)
        
        # Step 4: Process each file
        total_dim_rows_loaded = 0
        total_fact_rows_loaded = 0
        extract_mode = source_config.get('extract_mode', 'single')
        reader_args = source_config.get('extension_options', {})
        
        for file_path in file_paths:
            logger.info(f"Processing file: {file_path}")
            # Extract year from filename (if present)
            year_match = re.search(r'(\d{4})', os.path.basename(file_path))
            year = int(year_match.group(1)) if year_match else None
            
            # Step 4a: Extract data
            extractor = FileExtractor(extract_mode=extract_mode, large_df_threshold=10_000_000)
            result = extractor.extract(file_path, **reader_args)

            if extract_mode == 'chunked':
                chunk_iter = result
                dims_loaded = False   # flag to skip dims after first chunk
                chunk_count = 0
                for chunk_df in chunk_iter:
                    chunk_count += 1
                    if chunk_count % 10 == 0:
                        logger.info(f"Processed {chunk_count} chunks so far...")
                    if chunk_df.empty:
                        continue
                    if year is not None:
                        chunk_df['year'] = year
                    # Load dimensions only once (from the first chunk)
                    if not dims_loaded:
                        dim_dfs = transform_dimensions(chunk_df, mapping_path)
                        if dim_dfs:
                            total_dim_rows_loaded += load_dimensions(dim_dfs, loader)
                        dims_loaded = True
                    else:
                        # Skip dimensions – they already exist
                        pass
                    # Always process facts (they are unique per chunk)
                    fact_dfs = transform_facts(chunk_df, mapping_path)
                    if fact_dfs:
                        total_fact_rows_loaded += load_facts(fact_dfs, loader)
            else:
                # Normal single DataFrame
                df = result
                if df.empty:
                    continue
                if year is not None:
                    df['year'] = year
                # Transform and load dimensions
                dim_dfs = transform_dimensions(df, mapping_path)
                if dim_dfs:
                    total_dim_rows_loaded += load_dimensions(dim_dfs, loader)
                # Transform and load facts
                fact_dfs = transform_facts(df, mapping_path)
                if fact_dfs:
                    total_fact_rows_loaded += load_facts(fact_dfs, loader)
        
        logger.info(f"Source '{source_name}' completed. Total dim rows loaded: {total_dim_rows_loaded}. "
                     f"Total facts rows loaded: {total_fact_rows_loaded}")
        return True
    except Exception as e:
        logger.error(f"Failed to process source '{source_name}': {e}", exc_info=True)
        return False


def run_consolidation_step():
    """Run the data consolidation step."""
    logger.info("=" * 60)
    logger.info("RUNNING DATA CONSOLIDATION")
    logger.info("=" * 60)
    
    try:
        consolidator = DataConsolidator()
        try:
            # Fix country codes first
            fixed = consolidator.fix_location_country_codes()
            logger.info(f"Fixed country codes for {fixed} locations")
            
            # Run full consolidation
            results = consolidator.consolidate_all()
            
            # Log summary
            loc_results = results.get('locations', {})
            if loc_results and isinstance(loc_results, dict):
                logger.info(f"Location consolidation: {loc_results.get('original_count', 0)} -> "
                           f"{loc_results.get('merged_count', 0)} locations")
            
            vocab_results = results.get('vocabulary', {})
            for table, result in vocab_results.items():
                if isinstance(result, dict):
                    dup_count = result.get('duplicates_found', 0)
                    if dup_count > 0:
                        logger.info(f"Consolidated {dup_count} duplicates in {table}")
            
            dup_results = results.get('duplicates', {})
            for table, result in dup_results.items():
                if isinstance(result, dict):
                    dup_count = result.get('duplicate_count', 0)
                    if dup_count > 0:
                        logger.warning(f"Found {dup_count} duplicates in {table}")
            
            return results
        finally:
            consolidator.close()
    except Exception as e:
        logger.error(f"Consolidation failed: {e}")
        return {'error': str(e)}


def run_burden_expansion():
    """
    Run the burden expansion script to create daily case records.
    This is a standalone function that can be called from main.
    """
    logger.info("=" * 60)
    logger.info("RUNNING BURDEN EXPANSION")
    logger.info("=" * 60)
    
    try:
        from fix_cases import expand_burden_to_daily
        expand_burden_to_daily()
        logger.info("Burden expansion completed successfully")
        return True
    except ImportError:
        logger.warning("fix_cases.py not found, attempting to use consolidation...")
        return False
    except Exception as e:
        logger.error(f"Burden expansion failed: {e}")
        return False


def generate_summary(output_dir: str = "outputs"):
    """Generate database summary reports."""
    logger.info("Generating database summary...")
    output_path = PROJECT_ROOT / output_dir
    output_path.mkdir(exist_ok=True)
    # Schema summary
    schema_csv = output_path / "schema_summary.csv"
    schema_summary_to_csv(str(schema_csv))
    # Table samples
    samples_dir = output_path / "table_samples"
    export_all_table_samples(str(samples_dir), rows_per_table=100)
    logger.info(f"Summary generated in {output_path}")


def run_analysis_pipelines(pipelines: List[str], config: Dict[str, Any], filter_str: Optional[str] = None):
    """
    Run the standardized analysis pipelines.
    
    Args:
        pipelines: List of pipeline names to run
        config: Full configuration dictionary
        filter_str: Optional filter string (e.g., "dim_location.country_code = 'KE'")
    """
    from analysis.pipeline import run_analysis
    
    logger.info(f"Running analysis pipelines: {pipelines}")
    if filter_str:
        logger.info(f"With filter: {filter_str}")
    
    # Parse analysis parameters from config
    params = _parse_analysis_params(config, filter_str)
    
    # Get analysis flags from config
    process_cfg = config.get('process', {})
    analyze_cfg = process_cfg.get('analyze', {})
    
    # Base analysis configuration
    base_params = {
        'study_name': params.get('study_name', f"{params['location']}_analysis"),
        'location': params['location'],
        'country': params['country'],
        'start_year': params['start_year'],
        'end_year': params['end_year'],
        'output_dir': f"outputs/analysis/{params.get('output_subdir', '')}",
        'environmental_variables': analyze_cfg.get('environmental_variables', ['t2m', 'tp']),
        'max_lag_months': analyze_cfg.get('max_lag_months', 6),
    }
    
    # Pipeline to analysis flag mapping
    pipeline_flags = {
        'seasonal_patterns': {
            'run_seasonal': True, 
            'run_climate_correlation': False, 
            'run_lag_analysis': False, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': False,
            'run_vector_disease': False,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
        'climate_vector_correlation': {
            'run_seasonal': False, 
            'run_climate_correlation': True,
            'run_lag_analysis': False, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': True,
            'run_vector_disease': True, 
            'run_vector_occurrence': True,
            'run_vector_clustering': False
        },
        'lag_analysis': {
            'run_seasonal': False, 
            'run_climate_correlation': False,
            'run_lag_analysis': True, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': False,
            'run_vector_disease': False,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
        'intervention_impact': {
            'run_seasonal': False, 
            'run_climate_correlation': False,
            'run_lag_analysis': False, 
            'run_intervention_impact': True,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': False,
            'run_vector_disease': False,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
        'resistance_trends': {
            'run_seasonal': False, 
            'run_climate_correlation': False,
            'run_lag_analysis': False, 
            'run_intervention_impact': False,
            'run_resistance_trends': True, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': False,
            'run_vector_disease': False,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
        'early_warning_indicators': {
            'run_seasonal': True, 
            'run_climate_correlation': True,
            'run_lag_analysis': True, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': True,
            'run_vector_disease': True,
            'run_vector_occurrence': True,
            'run_vector_clustering': False
        },
        'vector_climate_analysis': {
            'run_seasonal': False, 
            'run_climate_correlation': False,
            'run_lag_analysis': False, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': True,
            'run_vector_disease': False,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
        'vector_disease_analysis': {
            'run_seasonal': False, 
            'run_climate_correlation': False,
            'run_lag_analysis': False, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': False,
            'run_vector_climate': False,
            'run_vector_disease': True,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
        'spatial_analysis': {
            'run_seasonal': False, 
            'run_climate_correlation': False,
            'run_lag_analysis': False, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': True,
            'run_risk_modeling': False,
            'run_vector_climate': False,
            'run_vector_disease': False,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
        'integrated_risk_model': {
            'run_seasonal': False, 
            'run_climate_correlation': False,
            'run_lag_analysis': False, 
            'run_intervention_impact': False,
            'run_resistance_trends': False, 
            'run_spatial_analysis': False,
            'run_risk_modeling': True,
            'run_vector_climate': False,
            'run_vector_disease': False,
            'run_vector_occurrence': False,
            'run_vector_clustering': False
        },
    }
    
    # Get custom pipeline parameters from config
    custom_pipelines = analyze_cfg.get('custom_pipelines', [])
    custom_params = {cp['name']: cp.get('params', {}) for cp in custom_pipelines}
    
    # Run each pipeline
    for pipeline in pipelines:
        logger.info(f"Executing analysis pipeline: {pipeline}")
        try:
            # Get flags for this pipeline
            flags = pipeline_flags.get(pipeline, {})
            
            # Merge with any custom params
            custom = custom_params.get(pipeline, {})
            
            # Start with base params and add flags
            pipeline_params = base_params.copy()
            
            # Add flags (these don't conflict with base_params)
            for key, value in flags.items():
                pipeline_params[key] = value
            
            # Set study name with pipeline suffix
            study_name = params.get('study_name', params['location'])
            pipeline_params['study_name'] = f"{study_name}_{pipeline}"
            
            # Set output subdirectory
            pipeline_params['output_dir'] = f"outputs/analysis/{pipeline}"
            
            # Add custom parameters
            if 'max_lag_months' in custom:
                pipeline_params['max_lag_months'] = custom['max_lag_months']
            if 'variables' in custom:
                pipeline_params['environmental_variables'] = custom['variables']
            if 'features' in custom:
                pipeline_params['features'] = custom['features']
            if 'target' in custom:
                pipeline_params['target'] = custom['target']
            
            # Run the analysis - pass all params as keyword arguments
            results = run_analysis(**pipeline_params)
            
            # Check for errors
            if results and isinstance(results, dict) and 'error' in results:
                logger.error(f"Analysis {pipeline} returned error: {results['error']}")
            else:
                logger.info(f"Completed: {pipeline}")
                
        except Exception as e:
            logger.error(f"Failed: {pipeline} - {e}", exc_info=True)


def _parse_analysis_params(config: Dict[str, Any], filter_str: Optional[str]) -> Dict[str, Any]:
    """
    Extract analysis parameters from config and filter string.
    
    Args:
        config: Full configuration dictionary
        filter_str: Optional filter string
        
    Returns:
        Dictionary with analysis parameters
    """
    import re
    
    # Get core config
    core = config.get('core', {})
    study_region = core.get('study_region', '')
    study_period = core.get('study_period', '')
    
    # Parse period
    start_year, end_year = None, None
    if study_period:
        parts = study_period.split('-')
        if len(parts) == 2:
            try:
                start_year, end_year = int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                start_year, end_year = 2019, 2024
    else:
        start_year, end_year = 2019, 2024
    
    # Parse location and country from filter or study_region
    location, country, study_name = _extract_location_info(filter_str, study_region)
    
    # If location is still "ALL" and country is "ALL", use all available data
    if location == "ALL" and country == "ALL":
        logger.info("No specific filters found. Analysis will use all available data.")
    
    return {
        'study_name': study_name or f"{location}_{country}",
        'location': location,
        'country': country,
        'start_year': start_year,
        'end_year': end_year,
        'output_subdir': f"{location}_{country}".lower().replace(' ', '_')
    }


def _extract_location_info(filter_str: Optional[str], study_region: str) -> tuple:
    """
    Extract location name, country code, and study name from filter or study_region.
    
    Args:
        filter_str: Optional filter string
        study_region: Study region from config
        
    Returns:
        Tuple of (location_name, country_code, study_name)
    """
    import re
    
    location = "ALL"
    country = "ALL"
    study_name = None
    
    if filter_str:
        # Extract country code
        country_match = re.search(r"country_code\s*=\s*'([^']+)'", filter_str)
        if country_match:
            country = country_match.group(1)
        
        # Extract location name
        name_match = re.search(r"name\s+LIKE\s+'%([^%]+)%'", filter_str)
        if name_match:
            location = name_match.group(1)
        else:
            name_match2 = re.search(r"name\s*=\s*'([^']+)'", filter_str)
            if name_match2:
                location = name_match2.group(1)
    
    # If location is still "ALL", try to extract from study_region
    if location == "ALL" and study_region:
        # Try parsing "Siaya County, Kenya" format
        parts = [p.strip() for p in study_region.split(',')]
        if parts:
            # Remove common suffixes
            location = parts[0].replace(' County', '').replace(' District', '').replace(' Region', '').strip()
            study_name = location
            
            if len(parts) > 1:
                country_name = parts[1].strip()
                country_map = {
                    'Kenya': 'KE', 'Ghana': 'GH', 'Nigeria': 'NG', 
                    'South Africa': 'ZA', 'Tanzania': 'TZ', 'Uganda': 'UG',
                    'Ethiopia': 'ET', 'Rwanda': 'RW', 'ALL': 'ALL'
                }
                country = country_map.get(country_name, country)
        else:
            # If no comma, treat as just location
            location = study_region.replace(' County', '').replace(' District', '').strip()
            study_name = location
    
    # If study_name is still None, use location
    if study_name is None:
        study_name = location if location != "ALL" else "National"
    
    return location, country, study_name


def main():
    parser = argparse.ArgumentParser(description="CDM Integration Framework Orchestrator")
    parser.add_argument("--config", default="base_config.yaml", help="Path to YAML configuration file")
    parser.add_argument("--skip-ingest", action="store_true", help="Skip data ingestion")
    parser.add_argument("--skip-summary", action="store_true", help="Skip database summary generation")
    parser.add_argument("--skip-analysis", action="store_true", help="Skip analysis pipelines")
    parser.add_argument("--skip-consolidation", action="store_true", help="Skip data consolidation")
    parser.add_argument("--expand-burden", action="store_true", help="Force burden expansion before analysis")
    parser.add_argument("--source", help="Run only for a specific source name (optional)")
    args = parser.parse_args()
    
    setup_environment()
    
    # Load configuration
    config_path = PROJECT_ROOT / args.config
    if not config_path.exists():
        logger.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
    
    config = load_yaml_config(str(config_path))
    logger.info(f"Loaded configuration from {config_path}")
    
    # Extract process steps
    process_cfg = config.get('process', {})
    steps = process_cfg.get('steps', [])
    
    # Determine which steps to run
    run_ingest = not args.skip_ingest and 'ingest_data' in steps
    run_consolidate = not args.skip_consolidation and 'consolidate' in steps
    run_summary = not args.skip_summary and 'generate_summary' in steps
    run_analysis = not args.skip_analysis and 'analyze' in steps
    
    # --- 0. Check case data before consolidation ---
    if run_analysis and not run_consolidate:
        logger.info("Skipping consolidation, but checking case data...")
        try:
            from database.session import SessionLocal
            session = SessionLocal()
            case_count = session.execute(text("SELECT COUNT(*) FROM fact_disease_case")).scalar()
            session.close()
            logger.info(f"Current case records: {case_count}")
            
            if case_count < 1000:
                logger.warning("Few case records found. Consider running consolidation or burden expansion.")
                if args.expand_burden or case_count < 100:
                    logger.info("Running burden expansion...")
                    run_burden_expansion()
        except Exception as e:
            logger.warning(f"Could not check case data: {e}")
    
    # --- 1. Ingest data ---
    if run_ingest:
        data_sources = config.get('data_sources', {})
        if not data_sources:
            logger.warning("No data sources defined in configuration.")
        else:
            for source_name, source_cfg in data_sources.items():
                if args.source and source_name != args.source:
                    continue
                run_ingestion_for_source(source_cfg)
    
    # 1.5. Consolidate data
    if run_consolidate:
        run_consolidation_step()
    
    # --- 2. Generate summary ---
    if run_summary:
        generate_summary()
    
    # --- 2.5. Force burden expansion if requested ---
    if args.expand_burden and not run_consolidate:
        logger.info("Force expanding burden data...")
        run_burden_expansion()
    
    # --- 3. Run analysis ---
    if run_analysis:
        analysis_cfg = process_cfg.get('analyze', {})
        filter_str = analysis_cfg.get('filter')
        pipelines = analysis_cfg.get('standard_pipelines', [])
        
        if pipelines:
            # Pass the full config to allow custom parameters
            run_analysis_pipelines(pipelines, config, filter_str)
        else:
            logger.info("No analysis pipelines defined.")


if __name__ == "__main__":
    main()