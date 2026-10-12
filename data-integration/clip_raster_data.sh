#!/usr/bin/env bash
# ----------------------------------------------------------------------
# clip_raster_data
# Clip GeoTIFF rasters to the actual borders of a shapefile.
#
# Approach:
#   1. Optionally union + buffer the shapefile into a temp GeoJSON that
#      is used as the cutline.  With no buffer, the shapefile itself is
#      the cutline (gdalwarp reprojects it to the raster CRS on the fly).
#   2. gdalwarp -cutline ... -crop_to_cutline, which keeps pixels whose
#      CENTRE falls inside the polygon, sets everything else to nodata,
#      and trims the raster extent to the polygon's bounding box.
#   3. Output is written as a compressed, tiled GeoTIFF, then atomically
#      moved into place.  The atomic replace is what makes in-place
#      overwriting safe: a failed warp never leaves a half-written file.
#
# Modes
#   Single file :  clip_raster_data --source=in.tif --bounds=study.shp --target=out.tif
#
#   Folder      :  clip_raster_data --source=/data/rasters --bounds=study.shp --all
#                  (overwrites each input file in place; this is the default)
#
#   Folder, keep
#   originals   :  clip_raster_data --source=/data/rasters --bounds=study.shp --all --keep-originals
#                  (writes <name>_clipped.tif next to each input)
#
#   Folder to
#   a directory :  clip_raster_data --source=/data/rasters --bounds=study.shp --all --target-dir=/out
#                  (writes each clipped file into /out, preserving names)
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
  --all                Process every *.tif / *.tiff in --source.
                       DEFAULT BEHAVIOUR: overwrite each input file in place.
  --keep-originals     Do not overwrite. Write <name>_clipped.tif next to
                       each input instead. Mutually exclusive with --target-dir.
  --target-dir=DIR     Write outputs to DIR (preserves input basenames).
  --suffix=STR         Suffix for --keep-originals mode (default: _clipped).
  --inplace            Accepted for backward compatibility. In-place
                       overwrite is now the default; this flag is a no-op.

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

Misc:
  --dry-run            Print what would be done, write nothing.
  --verbose            Show the underlying gdalwarp command lines.
  --help               Show this message.
EOF
}

# ---------------- Argument parsing ------------------------------------
for arg in "$@"; do
    case $arg in
        --source=*)         source_file="${arg#*=}" ;;
        --bounds=*)         bounds_file="${arg#*=}" ;;
        --target=*)         target_file="${arg#*=}" ;;
        --target-dir=*)     target_dir="${arg#*=}" ;;
        --suffix=*)         suffix="${arg#*=}" ;;
        --buffer=*)         buffer="${arg#*=}" ;;
        --buffer-crs=*)     buffer_crs="${arg#*=}" ;;
        --resampling=*)     resampling="${arg#*=}" ;;
        --nodata=*)         nodata="${arg#*=}" ;;
        --dst-crs=*)        dst_crs="${arg#*=}" ;;
        --compress=*)       compress="${arg#*=}" ;;
        --all)              all_mode=1 ;;
        --inplace)          inplace=1 ;;      # no-op; kept for compat
        --keep-originals)   keep_originals=1; inplace=0 ;;
        --alpha)            alpha=1 ;;
        --dry-run)          dry_run=1 ;;
        --verbose)          verbose=1 ;;
        --help|-h)          usage; exit 0 ;;
        *) echo "Unknown option: $arg"; usage; exit 1 ;;
    esac
done

# ---------------- Validation ------------------------------------------
if [[ -z "$source_file" || -z "$bounds_file" ]]; then
    echo "ERROR: --source and --bounds are required."
    usage; exit 1
fi

if [[ ! -f "$bounds_file" ]]; then
    echo "ERROR: Bounds file not found: $bounds_file"; exit 1
fi

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
# No buffer  -> use the shapefile directly and pass its layer name.
# Buffer     -> build a temp GeoJSON with a single unioned + buffered
#               geometry and use that.  gdalwarp automatically reprojects
#               the cutline to the raster CRS either way.
cutline="$bounds_file"
cutline_layer="$layer_name"

if awk -v b="$buffer" 'BEGIN{exit !(b>0)}'; then
    cutline="$work_dir/cutline.geojson"

    if [[ -n "$buffer_crs" ]]; then
        # Two-step: warp first, then buffer, so the buffer distance is in
        # the units of the target CRS rather than the shapefile's.
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

    # Predictor helps DEFLATE/LZW on continuous data; harmless elsewhere.
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

    # Atomic replace: works whether $dst equals $src (in-place) or not.
    # Same-directory rename is a single filesystem operation, so a crash
    # between gdalwarp finishing and this mv cannot corrupt the original.
    mv -f "$tmp_out" "$dst"

    if [[ "$src" == "$dst" ]]; then
        echo "  -> $dst (overwritten)"
    else
        echo "  -> $dst"
    fi
}

# ---------------- Dispatch --------------------------------------------
if [[ $all_mode -eq 1 ]]; then
    shopt -s nullglob
    files=("$source_file"/*.tif "$source_file"/*.tiff \
           "$source_file"/*.TIF "$source_file"/*.TIFF)
    shopt -u nullglob

    if (( ${#files[@]} == 0 )); then
        echo "ERROR: no .tif/.tiff files found in $source_file"; exit 1
    fi

    # Summarise the mode up-front so the user knows what will happen.
    if [[ -n "$target_dir" ]]; then
        mode_label="write to $target_dir"
    elif [[ $keep_originals -eq 1 ]]; then
        mode_label="keep originals, write <name>${suffix}.tif"
    else
        mode_label="OVERWRITE inputs in place"
    fi

    if [[ $dry_run -eq 1 ]]; then
        echo "DRY-RUN: ${#files[@]} raster(s) in $source_file; mode: $mode_label"
    else
        echo "Clipping ${#files[@]} raster(s) in $source_file  [$mode_label]"
    fi

    for f in "${files[@]}"; do
        base=$(basename "$f")
        name="${base%.*}"
        ext="${base##*.}"

        if [[ -n "$target_dir" ]]; then
            mkdir -p "$target_dir"
            dst="$target_dir/$base"
        elif [[ $keep_originals -eq 1 ]]; then
            dst="$(dirname "$f")/${name}${suffix}.${ext}"
        else
            # Default: overwrite in place.
            dst="$f"
        fi

        clip_one "$f" "$dst"
    done
else
    clip_one "$source_file" "$target_file"
fi

# ---------------- Final report ----------------------------------------
if [[ $dry_run -eq 0 ]]; then
    echo "Done."
fi