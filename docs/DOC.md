
# Vector‑Borne Disease Common Data Model (CDM) – User Guide

This document explains how to set up, configure, and use the **Vector‑Borne Disease Common Data Model (CDM)** to integrate your own data (climatic, vector, case, intervention, socioeconomic, etc.) and perform lagged correlation analyses.

## 1. Overview

The CDM is a **star‑schema database** (SQLite or PostgreSQL) that harmonises diverse data sources related to vector‑borne diseases. It includes:

- **Dimension tables** – `dim_time`, `dim_location`, `dim_disease`, `dim_vector`, `dim_insecticide`, `dim_identification_method`, `dim_resistance_test_type`, `dim_socioeconomic_indicator`, `dim_source`
- **Fact tables** – `fact_vector_observation`, `fact_disease_case`, `fact_insecticide_resistance`, `fact_bionomics`, `fact_intervention`, `fact_socioeconomic_condition`, `fact_health_facility`, `fact_disease_burden`, `fact_environmental`

All fact tables reference `dim_time` and `dim_location`, enabling cross‑domain temporal and spatial analysis.

## 2. Installation & Setup

### 2.1 Requirements

- Python 3.10+
- SQLite (default) or PostgreSQL + PostGIS
- Dependencies (install via `pip install -r requirements.txt`)

Main libraries:  
`sqlalchemy`, `pandas`, `geopandas`, `xarray`, `rasterio`, `shapely`, `pyyaml`, `alembic`

### 2.2 Clone the repository

```bash
git clone https://github.com/your-org/vbd-cdm.git
cd vbd-cdm
python -m venv venv
source venv/bin/activate   # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt


### 2.3 Configuration

Create a `.env` file (or set environment variables) for database connection:

```ini
# For SQLite (default)
DATABASE_URL=sqlite:///./vbd_cdm.sqlite
SQL_LITE_DB_NAME=vbd_cdm.sqlite

# For PostgreSQL (optional)
# DB_USER=your_user
# DB_PASSWORD=your_password
# DB_HOST=localhost
# DB_PORT=5432
# DB_NAME=vbd_cdm
```

Create a `config.yaml` file in the project root. See the example below and adapt paths to your own data.

```yaml
data_paths:
  vocabulary: "data/vocabulary.csv"                # master vocabulary file
  study_areas:
    greater_accra: "study_areas/ghana/greater_accra.gpkg"
    siaya: "study_areas/kenya/siaya_county/siaya_county.gpkg"
  environmental:
    root: "/path/to/clipped/netcdf/files"
    source_name: "ERA5-Land"
  gbif:
    dir: "/path/to/gbif/csv"
    pattern: "*.csv"
    sep: "\t"
    source_name: "GBIF"
  who_cases:
    dir: "/path/to/case/data"
    pattern: "*.csv"
    source_name: "WHO"
  # ... other data sources

default_study_area: "greater_accra"
default_steps:
  - vocabulary
  - location
  - environmental
  - vectors
```

### 2.4 Initialise the database

Run Alembic migrations to create the schema:

```bash
alembic upgrade head
```

Or use the built‑in table creation (if you prefer a clean slate):

```bash
python main.py --reset-db
```

## 3. Preparing Your Data

### 3.1 Vocabulary (dimensions)

All dimension tables (`dim_disease`, `dim_vector`, `dim_insecticide`, etc.) are populated from a **single CSV file** – `data/vocabulary.csv`.  

Edit this file to add your own disease names, vector species, insecticides, etc. The expected columns are:

| domain | code | name | description | source_standard | additional_attributes (JSON) |
|--------|------|------|-------------|----------------|------------------------------|
| disease | MAL | Malaria | ... | ICD-10:B54 | `{"pathogen":"Plasmodium falciparum","icd_code":"B54"}` |
| vector | ANGAM | Anopheles gambiae | ... | Darwin Core | `{"genus":"Anopheles","family":"Culicidae"}` |
| ... | ... | ... | ... | ... | ... |

You can add rows for any domain (`disease`, `vector`, `insecticide`, `identification_method`, `resistance_test_type`, `socioeconomic_indicator`, `source`). The loader will map the `code` and `name` to the appropriate model fields (e.g., `disease_code` / `disease_name`).

### 3.2 Location data

The CDM uses a **hierarchical location model**. You need at least one region (e.g., district, county) with a geometry (Polygon) to aggregate point data.  
Provide a GeoPackage (`.gpkg`) or shapefile with administrative boundaries. The column `NAME` or `name` is used as the location name.  

Example loading (via `main.py` or script):

```python
loader.load_location_from_gpkg(
    gpkg_path="study_areas/kenya/siaya_county.gpkg",
    admin_level="region"
)
```

Vector observation points will be automatically inserted as `admin_level='site'` and linked to the parent region via `parent_location_code` (if you set it during ETL).

### 3.3 Environmental data (NetCDF / GRIB)

The ETL expects **NetCDF files** (daily aggregated) that have been clipped to your study region.  
If you have ERA5‑Land GRIB files, use the provided `clip_era5_by_shapefile` function (in `database/utils.py`) to clip and convert to NetCDF.

Once you have NetCDF files named like `2024_data_greater_accra.nc`, place them in the directory configured in `config.yaml` (`data_paths.environmental.root`). The loader will automatically:

- Parse the location code from the filename (e.g., `greater_accra`)
- Extract daily values for each variable defined in `etl/mappings.py` (`NC_VARIABLE_MAP`)
- Insert into `fact_environmental`

### 3.4 Vector observation data (GBIF, Vector Atlas, etc.)

Your vector data must be in CSV/Excel format with at least:  
- Latitude, longitude  
- Year, month (or full date)  
- Species name  
- Count (optional, default = 1)

Define a **column mapping dictionary** in `etl/mappings.py` (or pass it directly).  
Example for GBIF:

```python
GBIF_VECTOR_OBSERVATION_MAPPING = {
    '__latitude__': 'decimalLatitude',
    '__longitude__': 'decimalLongitude',
    '__year__': 'year',
    '__month__': 'month',
    '__count__': 'individualCount',
    '__event_date_full__': 'eventDate',
    'vector_species': 'scientificName',
    # additional optional fields:
    'life_stage': 'lifeStage',
    'sex': 'sex',
}
```

Then load:

```python
loader.load_vector_from_csv(
    data_dir="/path/to/gbif",
    pattern="*.csv",
    column_mapping=GBIF_VECTOR_OBSERVATION_MAPPING,
    source_name="GBIF"
)
```

The loader will:

- Clean coordinates and dates
- Create point locations (if not existing)
- Map species names to `dim_vector` (auto‑generating codes if needed)
- Insert into `fact_vector_observation`

### 3.5 Case data (disease incidence)

For case reports, provide a CSV with columns such as:

| case_id | district | onset_date | disease | age | sex | outcome |
|---------|----------|------------|---------|-----|-----|---------|

Create a mapping:

```python
CASE_MAPPING = {
    'case_identifier': 'case_id',
    'location_code': 'district',
    'date_id_onset': 'onset_date',
    'disease': 'disease',
    'age_years': 'age',
    'sex': 'sex',
    'outcome': 'outcome',
    '__constants__': {
        'disease': 'malaria',          # if disease column is missing
        'host_type': 'human',
    }
}
```

Then load:

```python
loader.load_cases_from_file(
    file_path="cases.csv",
    column_mapping=CASE_MAPPING,
    source_name="WHO"
)
```

The loader will convert disease names to `disease_code` using `dim_disease` (auto‑creating missing codes).

### 3.6 Intervention data

Similarly, provide a CSV with columns like `location`, `start_date`, `end_date`, `type`, `coverage`. Map and load.

### 3.7 Raster data (disease burden)

For GeoTIFF rasters (e.g., Malaria Atlas Project incidence maps), you can extract values for each location polygon:

```python
loader.load_map_raster_to_disease_burden(
    raster_path="incidence_2024.tif",
    year=2024,
    disease="malaria",
    metric_type="incidence_rate"
)
```

## 4. Running the ETL Pipeline

The recommended way is to use the command‑line interface `main.py`.

### 4.1 Basic usage

```bash
# Run all default steps (vocabulary, location, environmental, vectors)
python main.py

# Run only specific steps
python main.py --steps vocabulary,location,vectors

# Use a different study area
python main.py --study-area siaya

# Reset database (deletes all data) and run pipeline
python main.py --reset-db

# Only backfill country names for existing locations
python main.py --backfill-countries
```

### 4.2 Understanding the steps

- `vocabulary` – loads `data/vocabulary.csv` into all dimension tables.
- `location` – loads the GeoPackage for the selected study area into `dim_location`.
- `environmental` – reads NetCDF files and populates `fact_environmental`.
- `vectors` – reads GBIF/Vector Atlas CSV files and populates `fact_vector_observation`.
- `cases` – (if configured) loads case data into `fact_disease_case`.
- `burden` – (if configured) loads raster burden data.

Each step commits on success; if a step fails, the pipeline **continues** with the next step (no hard stop).

## 5. Analysing Data – Lagged Correlation Example

Once the database is populated, you can use SQL or pandas to compute lagged correlations.  
Example (Python + pandas + SQLAlchemy):

```python
import pandas as pd
from sqlalchemy import text
from database.session import engine

# 1. Get monthly environmental data (pivoted)
env_query = """
SELECT 
    location_code,
    strftime('%Y-%m-01', date_id) as month,
    AVG(CASE WHEN variable = 'temperature_mean' THEN value END) as temp,
    AVG(CASE WHEN variable = 'rainfall_total' THEN value END) as rain
FROM fact_environmental
WHERE location_code = 'greater_accra'
GROUP BY location_code, month
ORDER BY month
"""
df_env = pd.read_sql(env_query, engine)

# 2. Get monthly vector abundance
vec_query = """
SELECT 
    location_code,
    strftime('%Y-%m-01', date_id) as month,
    SUM(count) as abundance
FROM fact_vector_observation
WHERE location_code = 'greater_accra'
GROUP BY location_code, month
ORDER BY month
"""
df_vec = pd.read_sql(vec_query, engine)

# 3. Merge
df = pd.merge(df_vec, df_env, on=['location_code','month'])

# 4. Create lags
for lag in range(1, 5):
    df[f'temp_lag_{lag}'] = df['temp'].shift(lag)
    df[f'rain_lag_{lag}'] = df['rain'].shift(lag)

# 5. Correlations
print(df[['abundance'] + [f'temp_lag_{l}' for l in range(1,5)]].corr())
print(df[['abundance'] + [f'rain_lag_{l}' for l in range(1,5)]].corr())
```

## 6. Tips for Mapping Your Own Data

- **Always start with a small sample file** to test the mapping.
- **Use the `__constants__` key** in your mapping to supply default values (e.g., disease name, host type).
- **Date handling**: The loader accepts `__year__` and `__month__`, or a full `__event_date_full__` column (parsed with pandas).
- **Coordinates**: The loader expects decimal degrees (WGS84). Negative longitudes are handled automatically.
- **Location codes** for points are generated as `point_{lat}_{lon}`. You can later assign `parent_location_code` to link them to a region.
- **If a dimension value (e.g., vector species) is not in the vocabulary**, the loader will create it on the fly (using default codes). For full control, pre‑populate the vocabulary.

## 7. Troubleshooting

| Problem | Likely cause | Solution |
|---------|--------------|----------|
| `UNIQUE constraint failed: dim_location.location_code` | Duplicate points in the same batch | The loader now de‑duplicates before insert. If persists, check your CSV for identical coordinates. |
| `Not a boolean value: 'no'` | `is_holiday` column expects `True`/`False` | Ensure `ensure_dates_exist` uses boolean `False` (not string `'no'`). Fixed in `database/utils.py`. |
| `PendingRollbackError` | A previous step failed and left the session in a rollback state | The pipeline now rolls back before each step. If you run scripts manually, call `session.rollback()` before re‑using. |
| No data loaded from NetCDF | Filename pattern not recognised | Filenames must contain `YYYY_data_LOCATION.nc` (e.g., `2024_data_greater_accra.nc`). |
| Vector observations have `vector_code = 'UNKNOWN'` | Species column missing in mapping | Add `'vector_species': 'your_species_column'` to the mapping. |

## 8. Extending the CDM

- **Add a new variable to `fact_environmental`** – simply include it in `NC_VARIABLE_MAP` (no schema change needed, because `fact_environmental` uses a `variable`/`value` pair).
- **Add a new dimension table** – create a new model in `models.py`, add an Alembic migration, and extend the vocabulary loader.
- **Add a new fact table** – similar process; ensure it references `dim_time` and `dim_location`.

## 9. Getting Help

- Check the example notebooks in the `notebooks/` folder.
- Look at the `tests/` folder (if available) for unit tests.
- Open an issue on the GitHub repository.

---

**Happy modelling!**  
Use the CDM to break down silos and answer critical questions about vector‑borne disease dynamics.
```