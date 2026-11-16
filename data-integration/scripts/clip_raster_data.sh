#!/usr/bin/env bash
# ----------------------------------------------------------------------
# clip_raster_data.sh
# Clip GeoTIFF rasters to the actual borders of a shapefile.
#
# Modes
#   Single file :
#     clip_raster_data.sh --source=in.tif --bounds=study.shp --target=out.tif
#     clip_raster_data.sh --source=in.tif --bounds=study.shp            (in-place)
#
#   Folder      :
#     clip_raster_data.sh --source=/data/rasters --bounds=study.shp --all
#         → recursively clips every *.tif/*.tiff under /data/rasters,
#           OVERWRITING each file in place (atomic: safe on crash).
#
#     clip_raster_data.sh --source=/data/rasters --bounds=study.shp --all --shallow
#         → same, but only the top-level folder (old behaviour).
#
#     clip_raster_data.sh --source=/data/rasters --bounds=study.shp --all --keep-originals
#         → writes <name>_clipped.tif next to each input; re-runs skip
#           files that already have their _clipped.tif neighbour.
#
#     clip_raster_data.sh --source=/data/rasters --bounds=study.shp --all --target-dir=/out
#         → writes to /out preserving the source subdirectory structure.
#
#   Batch behaviour:
#     --fail-fast        Abort on the first failed raster (default: continue).
#     --dry-run          List what would be clipped; write nothing.
#     --verbose          Show the underlying gdalwarp command lines.
# ----------------------------------------------------------------------

set -euo pipefail

# ---------------- Defaults --------------------------------------------
buffer=0.0                # buffer around the polygon; 0 = exact boundary
buffer_crs=""             # optional EPSG code to reproject to before buffering
resampling="near"         # gdalwarp -r: near|bilinear|cubic|cubicspline|lanczos|average|mode|max|min|med|q1|q3|sum|rms
nodata=""                 # optional -dstnodata value
dst_crs=""                # optional -t_srs EPSG:xxxx to reproject output
compress="DEFLATE"        # DEFLATE|LZW|ZSTD|NONE
alpha=0                   # add an alpha band instead of relying on nodata
suffix="_clipped"         # suffix used when --keep-originals is set
target_dir=""             # optional output folder for folder mode
all_mode=0
inplace=1                 # folder mode overwrites inputs by default
keep_originals=0          # --keep-originals flips this off
recursive=1               # NEW: --all walks subdirectories by default
fail_fast=0               # NEW: keep going after a failed raster
dry_run=0
verbose=0

source_file=""
bounds_file=""
target_file=""

usage() {
    cat <<EOF
Usage:
  $(basename "$0") --source=<file.tif>  --bounds=<study.shp> --target=<out.tif> [options]
  $(basename "$0") --source=<folder>    --bounds=<study.shp> --all          [options]

Required:
  --source=PATH        Input raster file, or folder when --all is used.
  --bounds=FILE.shp    Study-area shapefile.
  --target=FILE.tif    Output raster (single-file mode).
                       For folder mode use --target-dir=DIR or leave unset.

Folder mode:
  --all                Clip every *.tif / *.tiff under --source.
                       RECURSIVE by default. Output is written IN PLACE
                       (each input file is atomically overwritten).
  --shallow            With --all: do not descend into subdirectories.
                       (alias: --no-recursive)
  --keep-originals     Do not overwrite. Write <name>_clipped.tif next to
                       each input instead. Re-runs skip files that already
                       have their _clipped.tif neighbour.
  --target-dir=DIR     Write outputs to DIR, preserving the source
                       subdirectory structure. Mutually exclusive with
                       --keep-originals.
  --suffix=STR         Suffix for --keep-originals mode (default: _clipped).
  --inplace            Accepted for backward compatibility (no-op; in-place
                       is already the default in folder mode).

Geometry:
  --buffer=DEGREES     Buffer around the polygon. Units are those of the
                       shapefile CRS unless --buffer-crs is given.
  --buffer-crs=EPSG:n  Reproject to this CRS before buffering (use a
                       projected CRS for metric buffers, e.g. EPSG:32637).

Raster options:
  --resampling=METHOD  Resampling method (default: near).
  --nodata=VALUE       Set -dstnodata on the output.
  --dst-crs=EPSG:n     Reproject output to this CRS.
  --compress=CODEC     DEFLATE (default), LZW, ZSTD or NONE.
  --alpha              Add an alpha band to the output.

Batch behaviour:
  --fail-fast          Abort on the first failed raster (default: continue).
  --dry-run            Print what would be done; write nothing.
  --verbose            Show the underlying gdalwarp command lines.
  --help               Show this message.

Examples:
  # Clip every TIFF under /data/rasters, recursively, in place:
  $(basename "$0") --source=/data/rasters --bounds=kenya.shp --all

  # Same but write to a separate tree:
  $(basename "$0") --source=/data/rasters --bounds=kenya.shp --all --target-dir=/data/clipped

  # Keep originals, only top-level folder:
  $(basename "$0") --source=/data/rasters --bounds=kenya.shp --all --shallow --keep-originals
EOF
}

# ---------------- Argument parsing ------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source=*)         source_file="${1#*=}" ;;
        --bounds=*)         bounds_file="${1#*=}" ;;
        --target=*)         target_file="${1#*=}" ;;
        --target-dir=*)     target_dir="${1#*=}" ;;
        --suffix=*)         suffix="${1#*=}" ;;
        --buffer=*)         buffer="${1#*=}" ;;
        --buffer-crs=*)     buffer_crs="${1#*=}" ;;
        --resampling=*)     resampling="${1#*=}" ;;
        --nodata=*)         nodata="${1#*=}" ;;
        --dst-crs=*)        dst_crs="${1#*=}" ;;
        --compress=*)       compress="${1#*=}" ;;
        --all)              all_mode=1 ;;
        --shallow|--no-recursive)
                            recursive=0 ;;
        --inplace)          inplace=1 ;;      # no-op; kept for compat
        --keep-originals)   keep_originals=1; inplace=0 ;;
        --alpha)            alpha=1 ;;
        --fail-fast)        fail_fast=1 ;;
        --dry-run)          dry_run=1 ;;
        --verbose)          verbose=1 ;;
        --help|-h)          usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
    shift
done

# ---------------- Validation ------------------------------------------
if [[ -z "$source_file" || -z "$bounds_file" ]]; then
    echo "ERROR: --source and --bounds are required."
    usage; exit 1
fi

if [[ ! -f "$bounds_file" ]]; then
    echo "ERROR: Bounds file not found: $bounds_file"; exit 1
fi

# Normalise the source folder path so ${f#$source_file/} works reliably.
source_file="${source_file%/}"

if [[ $all_mode -eq 1 ]]; then
    [[ -d "$source_file" ]] || { echo "ERROR: --all requires --source to be a directory."; exit 1; }
    if [[ $keep_originals -eq 1 && -n "$target_dir" ]]; then
        echo "ERROR: --keep-originals and --target-dir are mutually exclusive."
        exit 1
    fi
    if [[ -n "$target_file" ]]; then
        echo "ERROR: --target is not valid with --all. Use --target-dir or --keep-originals."
        exit 1
    fi
else
    [[ -f "$source_file" ]] || { echo "ERROR: Source file not found: $source_file"; exit 1; }
    if [[ -z "$target_file" ]]; then
        if [[ $inplace -eq 1 ]]; then
            target_file="$source_file"
        else
            echo "ERROR: single-file mode needs --target=<out.tif> (or --inplace)."; exit 1
        fi
    fi
fi

# ---------------- External tools --------------------------------------
command -v gdalwarp >/dev/null 2>&1 || { echo "ERROR: gdalwarp not found. Install GDAL (conda install -c conda-forge gdal)."; exit 1; }
command -v ogrinfo  >/dev/null 2>&1 || { echo "ERROR: ogrinfo not found. Install GDAL."; exit 1; }
command -v ogr2ogr  >/dev/null 2>&1 || { echo "ERROR: ogr2ogr not found. Install GDAL."; exit 1; }

# ---------------- Temporary workspace ---------------------------------
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

# ---------------- Discover the layer name -----------------------------
layer_name=$(ogrinfo -so -al "$bounds_file" 2>/dev/null \
             | grep -iE "^\s*Layer name:" \
             | head -1 \
             | sed 's/.*Layer name: *//' \
             | tr -d '\r')
if [[ -z "$layer_name" ]]; then
    layer_name=$(basename "$bounds_file" .shp)
fi

# ---------------- Build the cutline -----------------------------------
cutline="$bounds_file"
cutline_layer="$layer_name"

if awk -v b="$buffer" 'BEGIN{exit !(b>0)}'; then
    cutline="$work_dir/cutline.geojson"

    if [[ -n "$buffer_crs" ]]; then
        if [[ $verbose -eq 1 ]]; then
            echo "Projecting shapefile to $buffer_crs before buffering ..."
        fi
        projected="$work_dir/projected.geojson"
        ogr2ogr -f GeoJSON -t_srs "$buffer_crs" \
                -nln projected \
                "$projected" "$bounds_file" "$layer_name"
        sql="SELECT ST_Buffer(ST_Union(geometry), $buffer) AS geometry FROM \"projected\""
        ogr2ogr -f GeoJSON -dialect SQLite -sql "$sql" "$cutline" "$projected"
    else
        if [[ $verbose -eq 1 ]]; then
            echo "Unioning and buffering by $buffer (shapefile CRS units) ..."
        fi
        sql="SELECT ST_Buffer(ST_Union(geometry), $buffer) AS geometry FROM \"$layer_name\""
        ogr2ogr -f GeoJSON -dialect SQLite -sql "$sql" "$cutline" "$bounds_file"
    fi

    cutline_layer=""     # GeoJSON has a single layer, no -cl needed
elif [[ $verbose -eq 1 ]]; then
    echo "Using shapefile directly as cutline (no buffer) ..."
fi

# ---------------- Core clipping routine -------------------------------
clip_one() {
    local src="$1" dst="$2"
    local dst_dir tmp_out
    dst_dir=$(dirname "$dst")
    tmp_out="$dst_dir/.clip_tmp_$$_${RANDOM}.tif"

    if [[ $dry_run -eq 1 ]]; then
        if [[ "$src" == "$dst" ]]; then
            echo "DRY-RUN: $src  ->  (overwrite in place)"
        else
            echo "DRY-RUN: $src  ->  $dst"
        fi
        return 0
    fi

    local -a cmd=(gdalwarp
        -overwrite
        -of GTiff
        -cutline "$cutline"
        -crop_to_cutline
        -r  "$resampling"
        -multi
        -wo NUM_THREADS=ALL_CPUS
        -co "COMPRESS=$compress"
        -co TILED=YES
        -co BIGTIFF=IF_SAFER
        -co NUM_THREADS=ALL_CPUS
    )

    [[ -n "$cutline_layer" ]] && cmd+=(-cl "$cutline_layer")
    [[ -n "$nodata" ]]        && cmd+=(-dstnodata "$nodata")
    [[ -n "$dst_crs" ]]       && cmd+=(-t_srs "$dst_crs")
    [[ $alpha -eq 1 ]]        && cmd+=(-dstalpha)

    case "${compress^^}" in
        DEFLATE|LZW) cmd+=(-co PREDICTOR=2) ;;
    esac

    cmd+=("$src" "$tmp_out")

    if [[ $verbose -eq 1 ]]; then
        printf '  cmd:'; printf ' %q' "${cmd[@]}"; echo
    fi

    if ! "${cmd[@]}"; then
        rm -f "$tmp_out"
        echo "ERROR: gdalwarp failed for $src" >&2
        return 1
    fi

    mv -f "$tmp_out" "$dst"

    if [[ "$src" == "$dst" ]]; then
        echo "  -> $dst (overwritten)"
    else
        echo "  -> $dst"
    fi
}

# ---------------- Dispatch --------------------------------------------
if [[ $all_mode -eq 1 ]]; then
    # ---- Discover inputs (recursive by default) ----
    find_args=( "$source_file" -type f \( -iname '*.tif' -o -iname '*.tiff' \) ! -name '.*' )
    [[ $recursive -eq 0 ]] && find_args=( "$source_file" -maxdepth 1 "${find_args[@]:1}" )

    mapfile -t files < <(find "${find_args[@]}" | sort)

    if (( ${#files[@]} == 0 )); then
        echo "ERROR: no .tif/.tiff files found under $source_file${recursive:+ (recursive)}"
        exit 1
    fi

    # ---- Describe mode up-front ----
    if [[ -n "$target_dir" ]]; then
        mode_label="write to $target_dir (preserving structure)"
    elif [[ $keep_originals -eq 1 ]]; then
        mode_label="keep originals, write <name>${suffix}.tif"
    else
        mode_label="OVERWRITE inputs in place"
    fi

    scope="recursive"
    [[ $recursive -eq 0 ]] && scope="shallow"

    if [[ $dry_run -eq 1 ]]; then
        echo "DRY-RUN: ${#files[@]} raster(s) under $source_file [$scope; $mode_label]"
    else
        echo "Clipping ${#files[@]} raster(s) under $source_file  [$scope; $mode_label]"
    fi

    # ---- Loop with counters ----
    succeeded=0
    failed=0
    skipped=0

    for f in "${files[@]}"; do
        base=$(basename "$f")
        name="${base%.*}"
        ext="${base##*.}"

        # Skip already-processed files in --keep-originals mode.
        # Only skip when the corresponding source file exists alongside,
        # so a real file literally named "foo_clipped.tif" is still processed.
        if [[ $keep_originals -eq 1 && "$name" == *"$suffix" ]]; then
            stem="${name%"$suffix"}"
            original="$(dirname "$f")/${stem}.${ext}"
            if [[ -f "$original" ]]; then
                skipped=$((skipped + 1))
                [[ $verbose -eq 1 ]] && echo "  [skip] $f (already has $suffix)"
                continue
            fi
        fi

        # ---- Determine output path ----
        if [[ -n "$target_dir" ]]; then
            rel="${f#"$source_file"/}"
            [[ "$rel" == "$f" ]] && rel="$base"     # safety fallback
            if [[ $dry_run -eq 0 ]]; then
                mkdir -p "$target_dir/$(dirname "$rel")"
            fi
            dst="$target_dir/$rel"
        elif [[ $keep_originals -eq 1 ]]; then
            dst="$(dirname "$f")/${name}${suffix}.${ext}"
        else
            dst="$f"                                 # in-place
        fi

        # ---- Clip; continue on error unless --fail-fast ----
        if clip_one "$f" "$dst"; then
            succeeded=$((succeeded + 1))
        else
            failed=$((failed + 1))
            if [[ $fail_fast -eq 1 ]]; then
                echo "ERROR: --fail-fast set; aborting after failure on $f" >&2
                exit 1
            fi
        fi
    done

    # ---- Summary ----
    echo
    echo "=== Summary ==="
    printf '  discovered : %d\n' "${#files[@]}"
    printf '  succeeded  : %d\n' "$succeeded"
    printf '  skipped    : %d\n' "$skipped"
    printf '  failed     : %d\n' "$failed"
    [[ $dry_run -eq 1 ]] && echo "  (dry-run — no files were written)"
    [[ $failed -gt 0 ]] && exit 1
else
    clip_one "$source_file" "$target_file"
    [[ $dry_run -eq 0 ]] && echo "Done."
fi