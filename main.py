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
from typing import Dict, Any, List, Optional

import pandas as pd
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

# Local imports
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


def run_ingestion_for_source(source_config: Dict[str, Any]) -> bool:
    """
    Run the complete ETL pipeline for a single data source.

    Args:
        source_config: Configuration dictionary for one source (from YAML).

    Returns:
        True if successful, False otherwise.
    """
    source_name = source_config.get('source_name', 'unknown')
    logger.info(f"===== Processing source: {source_name} =====")

    try:
        # 1. Determine file(s) to process
        source_type = source_config.get('type', 'file')
        extension = source_config.get('extension', '').lower()
        if not extension:
            raise ValueError(f"Source '{source_name}' missing 'extension' field")

        source_path = source_config.get('path')
        if not source_path:
            raise ValueError(f"Source '{source_name}' missing 'path' field")

        # Resolve relative path to absolute
        if not os.path.isabs(source_path):
            source_path = str(PROJECT_ROOT / source_path)

        if source_type == 'folder':
            file_paths = get_all_files_from_folder(source_path, extension)
            if not file_paths:
                logger.warning(f"No .{extension} files found in folder {source_path}")
                return False
            logger.info(f"Found {len(file_paths)} files in folder {source_path}")
        else:  # single file
            file_paths = [source_path]

        # 2. Extract options
        extract_mode = source_config.get('extract_mode', 'single')
        reader_args = source_config.get('extension_options', {})
        # For NetCDF/GRIB, we may want to extract specific variables/time
        if 'variables' in source_config:
            reader_args['variables'] = source_config['variables']
        if 'time_idx' in source_config:
            reader_args['time_idx'] = source_config['time_idx']

        # 3. Create Extractor and Transformer (shared across files for this source)
        extractor = FileExtractor(extract_mode=extract_mode, large_df_threshold=10_000_000)

        mapping_path = source_config.get('matching')
        if not mapping_path:
            raise ValueError(f"Source '{source_name}' missing 'matching' field (path to mapping CSV)")

        if not os.path.isabs(mapping_path):
            mapping_path = str(PROJECT_ROOT / mapping_path)

        transformer = MappingTransformer(mapping_path)

        # 4. Loader (common for all files)
        loader = DBLoader(batch_size=10000, upsert=True, use_copy=True)

        # 5. Process each file
        total_rows_loaded = 0
        for file_path in file_paths:
            logger.info(f"Processing file: {file_path}")
            # Extract
            df = extractor.extract(file_path, **reader_args)
            if df.empty:
                logger.warning(f"No data extracted from {file_path}")
                continue

            # Transform
            table_dfs = transformer.transform(df)
            if not table_dfs:
                logger.warning(f"No tables produced from {file_path}")
                continue

            # Load
            rows_loaded = loader.load_all(table_dfs)
            total_rows_loaded += sum(rows_loaded.values())
            logger.info(f"Loaded {sum(rows_loaded.values())} rows from {file_path}")

        logger.info(f"Source '{source_name}' completed. Total rows loaded: {total_rows_loaded}")
        return True

    except Exception as e:
        logger.error(f"Failed to process source '{source_name}': {e}", exc_info=True)
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


def run_analysis_pipelines(pipelines: List[str], filter_str: Optional[str] = None):
    """Placeholder for analysis pipelines."""
    logger.info(f"Running analysis pipelines: {pipelines}")
    if filter_str:
        logger.info(f"With filter: {filter_str}")
    # TODO: Implement actual analysis calls
    # For now, just log
    for pipeline in pipelines:
        logger.info(f"Executing analysis pipeline: {pipeline}")


def main():
    parser = argparse.ArgumentParser(description="CDM Integration Framework Orchestrator")
    parser.add_argument("--config", default="base_config.yaml", help="Path to YAML configuration file")
    parser.add_argument("--skip-ingest", action="store_true", help="Skip data ingestion")
    parser.add_argument("--skip-summary", action="store_true", help="Skip database summary generation")
    parser.add_argument("--skip-analysis", action="store_true", help="Skip analysis pipelines")
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
    run_summary = not args.skip_summary and 'generate_summary' in steps
    run_analysis = not args.skip_analysis and 'analyze' in steps

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

    # --- 2. Generate summary ---
    if run_summary:
        generate_summary()

    # --- 3. Run analysis ---
    if run_analysis:
        analysis_cfg = process_cfg.get('analyze', {})
        filter_str = analysis_cfg.get('filter')
        pipelines = analysis_cfg.get('standard_pipelines', [])
        if pipelines:
            run_analysis_pipelines(pipelines, filter_str)
        else:
            logger.info("No analysis pipelines defined.")


if __name__ == "__main__":
    main()