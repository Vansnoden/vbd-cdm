#!/usr/bin/env bash
#
# tif2cog.sh - convert a GeoTIFF (.tif/.tiff) to Cloud Optimized GeoTIFF (.cog)
#              using GDAL's COG driver.
#
# Requires: gdal_translate (GDAL >= 3.1).
#
# Usage:
#   ./tif2cog.sh input.tif
#   ./tif2cog.sh input.tif -o output.cog
#   ./tif2cog.sh -f input.tif                 # overwrite existing output
#   ./tif2cog.sh -c ZSTD -b 256 input.tif     # custom compression, tile size
#   ./tif2cog.sh -h
#
set -euo pipefail

PROG="$(basename -- "${BASH_SOURCE[0]}")"

usage() {
    cat <<EOF
${PROG} - convert a GeoTIFF to Cloud Optimized GeoTIFF (COG)

Usage:
  ${PROG} [OPTIONS] INPUT.tif|INPUT.tiff

Options:
  -o, --output FILE     Output path (default: INPUT with .cog extension)
  -c, --compress ALG    COG compression: DEFLATE (default), LZW, ZSTD, JPEG, NONE
  -b, --blocksize N     Tile size in pixels, multiple of 16 (default: 512)
  -f, --force           Overwrite the output if it already exists
  -n, --dry-run         Print the gdal_translate command and exit
  -h, --help            Show this help and exit

Requires:
  gdal_translate from GDAL >= 3.1 (the COG driver was introduced in 3.1).

Notes:
  CDO (Climate Data Operators) does NOT convert TIFF to COG; it handles
  NetCDF and GRIB. This script uses GDAL, which is the canonical tool for
  the COG format. Verify with:  gdalinfo --format COG
EOF
}

# ---- defaults --------------------------------------------------------------
OUTPUT=""
COMPRESS="DEFLATE"
BLOCKSIZE="512"
FORCE=0
DRY_RUN=0
INPUT=""

# ---- parse arguments -------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)    OUTPUT="$2";    shift 2 ;;
        -c|--compress)  COMPRESS="$2";  shift 2 ;;
        -b|--blocksize) BLOCKSIZE="$2"; shift 2 ;;
        -f|--force)     FORCE=1;        shift ;;
        -n|--dry-run)   DRY_RUN=1;      shift ;;
        -h|--help)      usage; exit 0 ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [[ -n "$INPUT" ]]; then
                echo "Error: more than one input file ('$INPUT' and '$1')" >&2
                exit 2
            fi
            INPUT="$1"
            shift
            ;;
    esac
done

# ---- validate input --------------------------------------------------------
if [[ -z "$INPUT" ]]; then
    echo "Error: no input file given." >&2
    usage >&2
    exit 2
fi

if [[ ! -f "$INPUT" ]]; then
    echo "Error: input file not found: $INPUT" >&2
    exit 1
fi

case "${INPUT,,}" in
    *.tif|*.tiff) : ;;
    *)
        echo "Error: input must be .tif or .tiff (got: $INPUT)" >&2
        exit 2
        ;;
esac

# ---- validate GDAL and the COG driver -------------------------------------
if ! command -v gdal_translate >/dev/null 2>&1; then
    echo "Error: gdal_translate not found in PATH." >&2
    echo "       Install GDAL >= 3.1 (Void Linux: sudo xbps-install -S gdal)." >&2
    exit 1
fi

if ! gdalinfo --format COG >/dev/null 2>&1; then
    echo "Error: GDAL's COG driver is not available." >&2
    echo "       Check with: gdalinfo --format COG" >&2
    echo "       Upgrade GDAL to >= 3.1." >&2
    exit 1
fi

if ! gdalinfo "$INPUT" >/dev/null 2>&1; then
    echo "Error: gdalinfo cannot read the input file: $INPUT" >&2
    echo "       The file may be corrupt or not a recognised GeoTIFF." >&2
    exit 1
fi

# ---- validate COG parameters ----------------------------------------------
if ! [[ "$BLOCKSIZE" =~ ^[0-9]+$ ]] || (( BLOCKSIZE <= 0 || BLOCKSIZE % 16 != 0 )); then
    echo "Error: --blocksize must be a positive multiple of 16 (got: $BLOCKSIZE)" >&2
    exit 2
fi

case "${COMPRESS^^}" in
    DEFLATE|LZW|ZSTD|JPEG|NONE) : ;;
    *)
        echo "Error: --compress must be one of: DEFLATE, LZW, ZSTD, JPEG, NONE (got: $COMPRESS)" >&2
        exit 2
        ;;
esac

# ---- determine output path ------------------------------------------------
if [[ -z "$OUTPUT" ]]; then
    OUTPUT="${INPUT%.*}.cog"
fi

if [[ -e "$OUTPUT" && $FORCE -eq 0 ]]; then
    echo "Error: output already exists: $OUTPUT" >&2
    echo "       Use -f to overwrite." >&2
    exit 1
fi

# ---- pick PREDICTOR based on the source data type -------------------------
# PREDICTOR=2 (horizontal differencing) is right for integer rasters;
# PREDICTOR=3 (floating point) is right for float rasters. Using 2 on a
# float raster does not fail loudly - it just compresses badly. Infer the
# type from gdalinfo instead of guessing.
PREDICTOR=2
DTYPE="$(gdalinfo "$INPUT" 2>/dev/null \
    | sed -n 's/.*Type=\([A-Za-z0-9]*\).*/\1/p' \
    | head -n 1)"
case "${DTYPE^^}" in
    FLOAT32|FLOAT64|CFLOAT32|CFLOAT64) PREDICTOR=3 ;;
esac

# ---- assemble the gdal_translate command ----------------------------------
# OVERVIEWS=IGNORE_EXISTING makes GDAL regenerate the overview pyramid
# rather than copying any pre-existing overviews from the source; COG
# requires the overviews to be present and internal-tiled.
# BIGTIFF=IF_SAFER emits BigTIFF only when the output would otherwise
# exceed 4 GB.
COG_OPTS="COMPRESS=${COMPRESS^^}"
COG_OPTS+=",PREDICTOR=${PREDICTOR}"
COG_OPTS+=",BLOCKSIZE=${BLOCKSIZE}"
COG_OPTS+=",OVERVIEWS=IGNORE_EXISTING"
COG_OPTS+=",BIGTIFF=IF_SAFER"

CMD=(gdal_translate -of COG -co "$COG_OPTS" "$INPUT" "$OUTPUT")

# ---- run, or print for review ---------------------------------------------
if (( DRY_RUN )); then
    printf 'Dry run:\n  '
    printf '%q ' "${CMD[@]}"
    printf '\n'
    exit 0
fi

if [[ -e "$OUTPUT" && $FORCE -eq 1 ]]; then
    rm -f -- "$OUTPUT"
fi

printf 'Converting: %s\n' "$INPUT"
printf '        to: %s\n' "$OUTPUT"
printf '   options: %s\n' "$COG_OPTS"

if ! "${CMD[@]}"; then
    echo "Error: gdal_translate failed." >&2
    # Do not leave a partially written output behind; the caller would
    # otherwise mistake it for a completed conversion.
    [[ -e "$OUTPUT" ]] && rm -f -- "$OUTPUT"
    exit 1
fi

# ---- sanity check ---------------------------------------------------------
if [[ ! -s "$OUTPUT" ]]; then
    echo "Error: output file is empty: $OUTPUT" >&2
    exit 1
fi

printf 'Done: %s (%s)\n' "$OUTPUT" "$(du -h "$OUTPUT" | cut -f1)"