#!/usr/bin/env python
# main.py

import sys
import argparse
import logging
from pathlib import Path

import yaml

# Ensure project root is in path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine, Base, SessionLocal
from etl.loaders import DatabaseLoader
from etl.mappings import GBIF_VECTOR_OBSERVATION_MAPPING

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def reset_database():
    confirm = input("⚠️  This will DELETE ALL DATA. Type 'yes' to continue: ")
    if confirm.lower() != "yes":
        print("Aborted.")
        sys.exit(0)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("✅ Database reset and tables recreated.")


def run_step(step: str, loader: DatabaseLoader, config: dict, study_area: str) -> bool:
    """Execute a single ETL step. Returns True on success, False on failure."""
    logger.info(f"Starting step: {step}")

    try:
        if step == "vocabulary":
            vocab_path = Path(config["data_paths"]["vocabulary"])
            if not vocab_path.exists():
                logger.error(f"Vocabulary file not found: {vocab_path}")
                return False
            stats = loader.load_dimensions_from_vocabulary(vocab_path)
            logger.info(f"Vocabulary loaded: {stats['inserted']} inserted, {stats['updated']} updated")
            if stats["errors"]:
                logger.warning(f"Errors: {stats['errors'][:3]}")
            return True

        elif step == "location":
            gpkg_path = Path(config["data_paths"]["study_areas"][study_area])
            if not gpkg_path.exists():
                logger.error(f"Study area GeoPackage not found: {gpkg_path}")
                return False
            stats = loader.load_location_from_gpkg(
                gpkg_path=gpkg_path,
                admin_level="region",
                auto_populate_country=True
            )
            logger.info(f"Location: {stats['inserted']} inserted, {stats['updated']} updated")
            return True

        elif step == "environmental":
            env_config = config["data_paths"]["environmental"]
            data_root = Path(env_config["root"])
            source_name = env_config["source_name"]
            if not data_root.exists():
                logger.error(f"Environmental data root not found: {data_root}")
                return False
            stats = loader.load_environmental_from_netcdf(data_root, source_name=source_name)
            logger.info(f"Environmental: {stats['processed_files']} files processed")
            if stats["errors"]:
                logger.warning(f"First 3 errors: {stats['errors'][:3]}")
            return True

        elif step == "vectors":
            gbif_config = config["data_paths"]["gbif"]
            gbif_dir = Path(gbif_config["dir"])
            if not gbif_dir.exists():
                logger.warning(f"GBIF directory not found: {gbif_dir}")
                return True  # not fatal
            stats = loader.load_vector_from_csv(
                data_dir=gbif_dir,
                pattern=gbif_config["pattern"],
                column_mapping=GBIF_VECTOR_OBSERVATION_MAPPING,
                source_name=gbif_config["source_name"],
                sep=gbif_config.get("sep", ",")
            )
            logger.info(f"GBIF vectors: {stats['loaded_records']} records loaded")
            if stats["errors"]:
                logger.warning(f"First 3 errors: {stats['errors'][:3]}")
            return True

        else:
            logger.error(f"Unknown step: {step}")
            return False

    except Exception as e:
        logger.error(f"Exception in step '{step}': {e}")
        return False


def backfill_countries(loader: DatabaseLoader):
    logger.info("Backfilling country information...")
    updated = loader._populate_location_countries()
    logger.info(f"Updated {updated} locations with country info.")


def main():
    parser = argparse.ArgumentParser(description="CDM ETL Pipeline")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML configuration")
    parser.add_argument("--steps", help="Comma-separated list of steps (vocabulary,location,environmental,vectors)")
    parser.add_argument("--study-area", help="Study area name (must match config)")
    parser.add_argument("--reset-db", action="store_true", help="Drop and recreate all tables")
    parser.add_argument("--backfill-countries", action="store_true", help="Only backfill country names and exit")
    args = parser.parse_args()

    config = load_config(args.config)
    study_area = args.study_area or config.get("default_study_area", "greater_accra")

    if args.reset_db:
        reset_database()

    if args.backfill_countries:
        db = SessionLocal()
        loader = DatabaseLoader(db)
        backfill_countries(loader)
        db.close()
        return

    steps = args.steps.split(",") if args.steps else config.get("default_steps", ["vocabulary", "location", "environmental", "vectors"])

    db = SessionLocal()
    loader = DatabaseLoader(db)
    results = {}

    for step in steps:
        # Ensure a clean transaction state before each step
        db.rollback()

        success = run_step(step, loader, config, study_area)
        if success:
            db.commit()
            results[step] = "SUCCESS"
        else:
            db.rollback()
            results[step] = "FAILED"
            logger.warning(f"Step '{step}' failed. Continuing to next step.")

    # Optional backfill after location step
    if "location" in steps and results.get("location") == "SUCCESS":
        backfill_countries(loader)
        db.commit()

    print("\n" + "=" * 50)
    print("ETL PIPELINE SUMMARY")
    print("=" * 50)
    for step, status in results.items():
        print(f"{step:20} : {status}")

    db.close()


if __name__ == "__main__":
    main()