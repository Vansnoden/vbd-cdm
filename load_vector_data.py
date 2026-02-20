#!/usr/bin/env python
# load_vector_data.py

import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from database.session import SessionLocal
from database import models, schemas
from etl.loaders import DatabaseLoader
from database.utils import ensure_dates_exist


def load_vector_files(data_dir, pattern="*.csv", column_mapping=None, date_format=None, **pandas_kwargs):
    """
    Load all vector observation files from data_dir matching pattern.
    
    :param data_dir: Directory containing the files.
    :param pattern: Glob pattern (e.g., "*.csv", "*.xlsx").
    :param column_mapping: Dict mapping file columns to model field names.
    :param date_format: Optional strptime format for date columns (if not already YYYY-MM-DD).
    :param pandas_kwargs: Additional arguments passed to pandas read_*.
    """
    data_dir = Path(data_dir)
    files = list(data_dir.glob(pattern))
    if not files:
        print(f"No files matching {pattern} found in {data_dir}")
        return

    db = SessionLocal()
    try:
        loader = DatabaseLoader(db)

        for file_path in files:
            print(f"Processing {file_path.name}...")
            # Read file based on extension
            if file_path.suffix.lower() == '.csv':
                df = pd.read_csv(file_path, **pandas_kwargs)
            elif file_path.suffix.lower() in ['.xls', '.xlsx']:
                df = pd.read_excel(file_path, **pandas_kwargs)
            else:
                print(f"Skipping unsupported file type: {file_path.suffix}")
                continue

            # Apply column mapping if provided
            if column_mapping:
                df = df.rename(columns=column_mapping)

            # Ensure required columns exist
            required = ['location_code', 'date_id', 'vector_species']
            missing = [col for col in required if col not in df.columns]
            if missing:
                print(f"  Missing required columns: {missing}. Skipping file.")
                continue

            # Convert date_id to YYYY-MM-DD if needed
            if date_format and 'date_id' in df.columns:
                # Assuming date_id column contains dates in a custom format
                df['date_id'] = pd.to_datetime(df['date_id'], format=date_format).dt.strftime('%Y-%m-%d')
            elif 'date_id' in df.columns and not pd.api.types.is_string_dtype(df['date_id']):
                # If it's datetime, convert to string
                df['date_id'] = pd.to_datetime(df['date_id']).dt.strftime('%Y-%m-%d')

            # Optional: ensure dim_time records exist for all dates in this file
            if 'date_id' in df.columns:
                min_date = df['date_id'].min()
                max_date = df['date_id'].max()
                ensure_dates_exist(min_date, max_date)

            # Convert to list of dicts and validate
            records = df.to_dict('records')
            validated = []
            for rec in records:
                try:
                    # Filter to only fields present in schema
                    schema_fields = schemas.VectorObservationCreate.model_fields.keys()
                    filtered = {k: v for k, v in rec.items() if k in schema_fields and pd.notna(v)}
                    validated_rec = schemas.VectorObservationCreate(**filtered)
                    validated.append(validated_rec.model_dump())
                except Exception as e:
                    print(f"  Validation error for record: {rec}")
                    print(f"  Error: {e}")
                    # Optionally skip or raise

            if validated:
                loader.load_from_dataframe(pd.DataFrame(validated), models.VectorObservation)
                print(f"  Loaded {len(validated)} vector observations from {file_path.name}")
            else:
                print(f"  No valid records in {file_path.name}")

    finally:
        db.close()


if __name__ == "__main__":
    # Example usage (adjust to your actual file layout)
    mapping = {
        'Trap ID': 'trap_id',
        'Location': 'location_code',
        'Date': 'date_id',
        'Species': 'vector_species',
        'Subspecies': 'vector_subspecies',
        'Life Stage': 'life_stage',
        'Sex': 'sex',
        'Collection Method': 'collection_method',
        'Count': 'count',
        'Density': 'density_per_trap',
        'Tested For': 'tested_for_pathogen',
        'Infection Status': 'infection_status',
        'Infection Rate': 'infection_rate'
    }
    load_vector_files(
        data_dir="/path/to/your/vector_data",
        pattern="*.xlsx",
        column_mapping=mapping,
        date_format="%d/%m/%Y"   # adjust to your date format
    )