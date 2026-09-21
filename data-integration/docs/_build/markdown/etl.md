# ETL pipeline

The ETL is a five-stage pipeline. Each stage is a Python module with a single
public function, so stages can be exercised independently.

```text
┌──────────┐   ┌─────────────┐   ┌───────────────┐   ┌────────┐   ┌──────────────┐
│  Config  │──▶│  Extractor  │──▶│  Transformer  │──▶│ Loader │──▶│ Consolidator │
│  (YAML)  │   │ (File*)     │   │ (Mapping*)    │   │ (DB*)  │   │ (Python)     │
└──────────┘   └─────────────┘   └───────────────┘   └────────┘   └──────────────┘
     │              │                    │                 │              │
  source_         pandas              dict of            upsert        FK repair,
  config          DataFrame           {table: df}        into CDM      dedup,
                                                                       vocab match
```

## Stage 1 - Config

`utilities.data_transform_library.load_yaml_config` reads `base_config.yaml`.
Each `data_sources.<name>` entry declares:

```yaml
sources:
  anopheles_kenya:
    source_name: "anopheles_kenya"
    type: "folder"
    path: "data/raw/vectors"
    extension: "csv"
    matching: "mappings/vectors_to_cdm.csv"
    extract_mode: "single"     # or "chunked" for raster
    extension_options:
      sep: ";"
      encoding: "utf-8"
```

## Stage 2 - Extract

`etl.extractors.file_extractor.FileExtractor` dispatches on file extension
via `utilities.data_transform_library`. Supported formats:

| Extension                | Backend                                                 |
|--------------------------|---------------------------------------------------------|
| `csv`, `tsv`             | `pandas.read_csv` (chunked if `chunksize` provided)     |
| `xlsx`, `xls`            | `calamine` (fast) with `openpyxl` fallback              |
| `json`                   | `pandas.read_json` (line-delimited by default)          |
| `xml`                    | `xml.etree.ElementTree.iterparse` (streaming)           |
| `nc`, `nc4`              | `xarray`                                                |
| `grib`, `grib2`          | `cfgrib` via `xarray`                                   |
| `cog`, `tif`, `tiff`     | `rioxarray`; `tif_to_df_chunked` yields windowed chunks |
| `dta`, `sas7bdat`, `sav` | `pyreadstat` chunked                                    |
| `geojson`, `gpkg`, `shp` | `pyogrio` (geometry dropped by default)                 |

### Chunked extraction

For very large rasters, set `extract_mode: "chunked"`. The extractor yields
DataFrames per window; `main.run_ingestion_for_source` upserts each chunk
before reading the next. This keeps memory bounded and makes the pipeline
resumable.

## Stage 3 - Transform

`etl.transformers.mapping_transformer.MappingTransformer` reads a mapping
CSV. See [Mapping CSV format](mapping.md) for the format.

Supported transforms:

| Name           | Behaviour                                                                                                                             |
|----------------|---------------------------------------------------------------------------------------------------------------------------------------|
| `none`         | Pass through.                                                                                                                         |
| `map`          | `{"value_a": "A", "value_b": "B"}` - dict lookup; a `"ref"` key<br/>triggers a dimension FK lookup (see mapping).                     |
| `cast`         | `{"type": "int"}` - int, float, str.                                                                                                  |
| `date_parse`   | `{"format": "%d/%m/%Y"}` - returns `YYYY-MM-DD`.                                                                                      |
| `format_date`  | `{"columns": ["year","month","day"], "default": null}` - builds a<br/>date string from three integer columns. Handles missing values. |
| `split`        | `{"delimiter": ",", "index": 0}` - take one field from a delimited<br/>string.                                                        |
| `unit_convert` | `{"factor": 0.1, "offset": 0}` - linear transform.                                                                                    |
| `concat`       | `{"columns": ["a","b"], "separator": " "}` - join string columns.                                                                     |

## Stage 4 - Load

`etl.loaders.db_loader.DBLoader` upserts DataFrames using natural keys.
The natural-key configuration is per-table (`NATURAL_KEY_CONFIG`).

Two backends:

* **PostgreSQL** - `COPY` into a temp staging table, then `INSERT ... ON
  CONFLICT DO UPDATE`. Only triggered when `use_copy=True` and the batch
  is larger than twice `batch_size`.
* **SQLite** - batched `INSERT ... ON CONFLICT DO UPDATE`.
  The loaders **never** use `INSERT OR REPLACE` (which would delete and
  reinsert the PK, breaking autoincrement IDs and cascade references).

### Null sentinels

Nullable natural-key columns are replaced with sentinel values before insert
(empty string for text, `-1.0` for concentration). This is required because
SQL treats NULLs as distinct in unique indexes. See
[The VBD CDM contract](cdm_contract.md) §4.

## Stage 5 - Consolidate

`utilities.consolidation.DataConsolidator` runs eleven steps, in order:

1. Align all foreign keys - null or repair orphaned FK values.
2. Standardise country codes to ISO alpha-2.
3. Bind climate-grid coordinates to the study region.
4. Standardise vocabularies against GADM / IDOMAL / NCBI.
5. Fix orphaned burden records specifically.
6. Expand yearly burden to daily case rows.
7. Compute resistance status from mortality counts.
8. Consolidate locations (name-based, then DBSCAN-proximity-based).
9. Consolidate vocabulary duplicates (disease, vector, insecticide, source).
10. Detect and resolve fact-table duplicates.
11. Final verification.

The FK-alignment pass is the most important - it can be run standalone:

```python
from utilities.consolidation import DataConsolidator
with DataConsolidator() as c:
    c.align_all_foreign_keys(dry_run=True)
```
