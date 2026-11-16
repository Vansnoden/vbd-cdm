#!/usr/bin/env bash
# run.sh — unified entry point for VBD CDM utilities.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
SCRIPTS="$ROOT/scripts"

usage() {
    cat <<'EOF'
run.sh — unified entry point for VBD CDM utilities

Usage:
  ./run.sh <command> [args...]

Commands:
  reformat-csv [args]            Normalise CSV/TSV/TXT to clean UTF-8, in place
  fix-resistance                 Recompute mortality + resistance_status
  generate-er                    ER diagram via ERAlchemy (auto layout)
  generate-er-neo                ER diagram via DOT grid (curated layout)
  explore [-- args...]           Exploratory plots (Python, matplotlib)
  export-csv [--out P] [--limit N]
                                 Export every fact table to CSV (zip)
  fix-cases [--force] [--seed N] Expand burden data into daily cases
  bundle [args]                  Bundle all Python files into one text
  clip-raster [args]             Clip GeoTIFFs to a shapefile (recursive)
  clip-timeseries [args]         Clip NetCDF/GRIB to a shapefile
  tif2cog [args]                 Convert GeoTIFF → Cloud-Optimized GeoTIFF
  quarantine [DB]                Summarise _quarantine_* tables
  etl-summary [LOG]              Summarise logs/etl.log
  help                           Show this message

Examples:
  ./run.sh reformat-csv data.csv
  ./run.sh reformat-csv --folder data --recursive --backup
  ./run.sh reformat-csv --folder data --dry-run -v
  ./run.sh fix-resistance
  ./run.sh generate-er-neo
  ./run.sh export-csv --out outputs/cdm_export --limit 5000
  ./run.sh clip-raster --source in.tif --bounds study.shp --target out.tif
  ./run.sh clip-raster --source /data/rasters --bounds kenya.shp --all
  ./run.sh etl-summary logs/etl.log
EOF
}

cmd="${1:-help}"
shift || true

case "$cmd" in
    reformat-csv)     exec bash "$SCRIPTS/reformat_csv.sh"          "$@" ;;
    fix-resistance)   exec bash "$SCRIPTS/fix_resistance_status.sh" "$@" ;;
    generate-er)      exec bash "$SCRIPTS/generate_er.sh"           "$@" ;;
    generate-er-neo)  exec bash "$SCRIPTS/generate_er_neo.sh"       "$@" ;;
    explore)          exec bash "$SCRIPTS/explore_data.sh"          "$@" ;;
    export-csv)       exec bash "$SCRIPTS/export_to_csv.sh"         "$@" ;;
    fix-cases)        exec bash "$SCRIPTS/fix_cases.sh"             "$@" ;;
    bundle)           exec bash "$SCRIPTS/bundle.sh"                "$@" ;;
    clip-raster)      exec bash "$SCRIPTS/clip_raster_data.sh"      "$@" ;;
    clip-timeseries)  exec bash "$SCRIPTS/clip_timeseries_data.sh"  "$@" ;;
    tif2cog)          exec bash "$SCRIPTS/tif2cog.sh"               "$@" ;;
    quarantine)       exec bash "$SCRIPTS/inspect_quarantine.sh"    "$@" ;;
    etl-summary)      exec bash "$SCRIPTS/summarize_etl.sh"         "$@" ;;
    help|-h|--help)   usage ;;
    *) echo "Unknown command: $cmd" >&2; usage; exit 1 ;;
esac
