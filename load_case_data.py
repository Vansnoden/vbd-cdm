# load_case_data.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from database.session import SessionLocal
from database import models, schemas
from etl.loaders import DatabaseLoader

def load_case_files(data_dir, pattern="*.csv", column_mapping=None):
    """
    Load all CSV/Excel files in data_dir matching pattern into host_case table.
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
            print(f"Loading {file_path.name}...")
            loader.load_from_file(
                file_path=file_path,
                model_class=models.HostCase,
                schema_class=schemas.HostCaseCreate,
                column_mapping=column_mapping,
                # Additional pandas args (e.g., sep=';' for CSV, sheet_name for Excel)
            )
        print("All case files loaded.")
    finally:
        db.close()

if __name__ == "__main__":
    # Example column mapping (adjust to your actual CSV headers)
    mapping = {
        'Case ID': 'case_identifier',
        'District': 'location_code',
        'Onset Date': 'date_id_onset',
        'Report Date': 'date_id_reported',
        'Disease': 'disease',
        'Status': 'case_status',
        'Age': 'age_years',
        'Sex': 'sex',
        'Outcome': 'outcome'
    }
    load_case_files(
        data_dir="/path/to/your/case_data",
        pattern="*.csv",
        column_mapping=mapping
    )