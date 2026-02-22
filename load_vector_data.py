#!/usr/bin/env python
# load_vector_data.py – Generic loader using column mapping

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from shapely.geometry import Point

from database.session import SessionLocal
from database import models, schemas
from database.utils import ensure_dates_exist
from etl.loaders import DatabaseLoader


def generate_location_code(lat, lon):
    """Generate a unique location code from coordinates."""
    # Replace negative sign with 'n' and decimal point with '_' for readability
    return f"point_{lat:.4f}_{lon:.4f}".replace('-', 'n').replace('.', '_')


def load_vector_files(data_dir, pattern="*.csv", column_mapping=None, **pandas_kwargs):
    """
    Load vector observation files using a column mapping dictionary.

    The column_mapping must contain special keys:
        __latitude__  : name of column with latitude
        __longitude__ : name of column with longitude
        __year__      : name of column with year
        __month__     : name of column with month
        __count__     : either a single column name or a list of column names to sum
    Additional keys (not starting with '__') are mapped directly to VectorObservation fields.
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
            print(f"\nProcessing {file_path.name}...")

            # --- 1. Read file ---
            if file_path.suffix.lower() == '.csv':
                df = pd.read_csv(file_path, **pandas_kwargs)
            elif file_path.suffix.lower() in ['.xls', '.xlsx']:
                df = pd.read_excel(file_path, **pandas_kwargs)
            else:
                print(f"  Skipping unsupported file type: {file_path.suffix}")
                continue

            if df.empty:
                print("  File is empty.")
                continue

            # --- 2. Extract required columns from mapping ---
            lat_col = column_mapping.get('__latitude__')
            lon_col = column_mapping.get('__longitude__')
            year_col = column_mapping.get('__year__')
            month_col = column_mapping.get('__month__')
            count_spec = column_mapping.get('__count__')

            if not all([lat_col, lon_col, year_col, month_col, count_spec]):
                print("  Missing one or more required mapping keys (__latitude__, __longitude__, __year__, __month__, __count__). Skipping file.")
                continue

            # Check that required columns exist
            required_cols = [lat_col, lon_col, year_col, month_col]
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                print(f"  Missing columns: {missing}. Skipping file.")
                continue

            # --- 3. Handle count (single column or list of columns) ---
            if isinstance(count_spec, str):
                if count_spec not in df.columns:
                    print(f"  Count column '{count_spec}' not found. Using default count=1 for all rows.")
                    df['_count'] = 1
                else:
                    df['_count'] = pd.to_numeric(df[count_spec], errors='coerce').fillna(1)
            elif isinstance(count_spec, list):
                # For a list of columns, we sum them (but not typical for GBIF)
                missing_counts = [c for c in count_spec if c not in df.columns]
                if missing_counts:
                    print(f"  Missing count columns: {missing_counts}. Using default count=1.")
                    df['_count'] = 1
                else:
                    for c in count_spec:
                        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
                    df['_count'] = df[count_spec].sum(axis=1)
            else:
                print("  __count__ must be a string or a list. Skipping file.")
                continue

            # --- 4. Generate location codes and upsert locations ---
            df['_lat'] = df[lat_col].astype(float)
            df['_lon'] = df[lon_col].astype(float)
            df['location_code'] = df.apply(
                lambda r: generate_location_code(r['_lat'], r['_lon']), axis=1
            )

            # Drop duplicates on location_code (keep first occurrence)
            unique_locs = df[['location_code', '_lat', '_lon']].drop_duplicates(subset=['location_code'])

            # Upsert unique locations
            for _, row in unique_locs.iterrows():
                existing = db.query(models.Location).filter_by(location_code=row['location_code']).first()
                if not existing:
                    point = Point(row['_lon'], row['_lat'])
                    loc = models.Location(
                        location_code=row['location_code'],
                        name=f"Sampling site at {row['_lat']:.4f}, {row['_lon']:.4f}",
                        admin_level='site',
                        geometry_wkt=point.wkt,
                        centroid_lat=row['_lat'],
                        centroid_lon=row['_lon']
                    )
                    db.add(loc)
            db.commit()
            print(f"  Upserted {len(unique_locs)} location sites.")

            # --- 5. Construct date_id and ensure dim_time exists ---
            # Convert year and month to numeric, coercing errors to NaN
            df['year'] = pd.to_numeric(df[year_col], errors='coerce')
            df['month'] = pd.to_numeric(df[month_col], errors='coerce')
            
            # Drop rows where year or month is NaN
            df = df.dropna(subset=['year', 'month'])
            
            # Keep only realistic years (1900–2100) and months 1–12
            df = df[(df['year'] >= 1900) & (df['year'] <= 2100) & (df['month'].between(1, 12))]
            
            if df.empty:
                print("  No rows with valid year/month after cleaning. Skipping file.")
                continue
            
            # Convert to integer (now safe)
            df['year'] = df['year'].astype(int)
            df['month'] = df['month'].astype(int)
            
            # Create date_id (first day of month)
            df['date_id'] = df.apply(
                lambda r: f"{r['year']:04d}-{r['month']:02d}-01", axis=1
            )
            
            # Compute min and max date from valid rows
            min_date = df['date_id'].min()
            max_date = df['date_id'].max()
            
            # Ensure dim_time records exist for all dates in this range
            ensure_dates_exist(min_date, max_date)

            # --- 6. Build field mapping for direct attributes ---
            field_mapping = {}
            for key, value in column_mapping.items():
                if not key.startswith('__') and value in df.columns:
                    field_mapping[key] = value

            # --- 7. Create validated records ---
            obs_records = []
            for _, row in df.iterrows():
                record = {
                    'location_code': row['location_code'],
                    'date_id': row['date_id'],
                    'count': int(row['_count']),
                }
                # Add directly mapped fields
                for out_field, src_col in field_mapping.items():
                    val = row[src_col]
                    if pd.notna(val):
                        record[out_field] = val

                # Validate with Pydantic
                try:
                    validated = schemas.VectorObservationCreate(**record)
                    obs_records.append(validated.model_dump())
                except Exception as e:
                    print(f"  Validation error for record {record}: {e}")

            if obs_records:
                loader.load_from_dataframe(pd.DataFrame(obs_records), models.VectorObservation)
                print(f"  Loaded {len(obs_records)} vector observations.")
            else:
                print("  No valid observation records.")

    finally:
        db.close()