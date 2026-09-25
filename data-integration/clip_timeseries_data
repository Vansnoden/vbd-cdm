#!/usr/bin/env bash

# ----------------------------------------------------------------------
# clip_timeseries_data
# Clip a GRIB/NetCDF file to the actual borders of a shapefile.
#
# Approach:
#   1. Union all polygons in the shapefile into a single geometry.
#   2. Optionally buffer that geometry by --buffer degrees.
#   3. Crop the source grid to the polygon's bounding box  (sellonlatbox).
#   4. Convert the polygon to the ASCII format `cdo maskregion` expects.
#   5. Mask the cropped file to the region.
#
# Step 3 is what makes the output smaller than the input. `maskregion`
# alone only sets out-of-region cells to missing; it does not shrink the
# grid, so the file size is unchanged. `sellonlatbox` reduces the grid to
# the polygon's bounding rectangle before masking. The masked cells inside
# that rectangle are still present (as missing values), which is the
# behaviour most downstream tools expect. To physically discard them as
# well, see the note at the end of Step 5.
#
# Optional daily aggregation (mean for 2t, sum for tp) and variable
# rename (2t -> t2m) are handled the same way as before.
# ----------------------------------------------------------------------

set -e

# Defaults
buffer=0.0                 # polygon buffer in degrees; 0 = exact boundary
daily=0
verbose=0
variables="2t,tp"          # default: temperature and precipitation
rename_t2m=0

# Parse arguments
for arg in "$@"; do
    case $arg in
        --source=*)
            source_file="${arg#*=}"
            ;;
        --bounds=*)
            bounds_file="${arg#*=}"
            ;;
        --target=*)
            target_file="${arg#*=}"
            ;;
        --daily)
            daily=1
            ;;
        --buffer=*)
            buffer="${arg#*=}"
            ;;
        --variables=*)
            variables="${arg#*=}"
            ;;
        --rename-t2m)
            rename_t2m=1
            ;;
        --verbose)
            verbose=1
            ;;
        *)
            echo "Unknown option: $arg"
            exit 1
            ;;
    esac
done

# Check required arguments
if [[ -z "$source_file" || -z "$bounds_file" || -z "$target_file" ]]; then
    echo "ERROR: Missing required arguments."
    echo "Usage: $0 --source=<file> --bounds=<shapefile> --target=<output.nc> [--daily] [--buffer=DEGREES] [--variables=2t,tp] [--rename-t2m] [--verbose]"
    exit 1
fi

# Check input file
if [[ ! -f "$source_file" ]]; then
    echo "ERROR: Source file not found: $source_file"
    exit 1
fi

# Check shapefile
if [[ ! -f "$bounds_file" ]]; then
    echo "ERROR: Bounds file not found: $bounds_file"
    exit 1
fi

# Check required tools
command -v cdo      >/dev/null 2>&1 || { echo "ERROR: cdo not found. Install with: conda install -c conda-forge cdo"; exit 1; }
command -v ogrinfo  >/dev/null 2>&1 || { echo "ERROR: ogrinfo not found. Install GDAL."; exit 1; }
command -v ogr2ogr  >/dev/null 2>&1 || { echo "ERROR: ogr2ogr not found. Install GDAL."; exit 1; }
command -v python3  >/dev/null 2>&1 || { echo "ERROR: python3 not found. Install Python 3."; exit 1; }

# ----------------------------------------------------------------------
# Temporary workspace
#
# mktemp -d creates an empty directory; individual files inside are
# created by the tools that write them. Using --suffix on mktemp would
# pre-create an empty file, which ogr2ogr (and others) refuse to
# overwrite.
# ----------------------------------------------------------------------
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

temp_geojson="$work_dir/polygon.geojson"
temp_ascii="$work_dir/polygon.txt"
temp_nc="$work_dir/masked.nc"

# ----------------------------------------------------------------------
# Step 1: Discover the layer name inside the shapefile
# ----------------------------------------------------------------------
layer_name=$(ogrinfo -so -al "$bounds_file" 2>/dev/null \
             | grep -iE "^\s*Layer name:" \
             | head -1 \
             | sed 's/.*Layer name: *//' \
             | tr -d '\r')

if [[ -z "$layer_name" ]]; then
    # Fall back to basename without extension, which is what GDAL uses by default.
    layer_name=$(basename "$bounds_file" .shp)
fi

if [[ $verbose -eq 1 ]]; then
    echo "Shapefile layer: $layer_name"
fi

# ----------------------------------------------------------------------
# Step 2: Union the polygons, optionally buffer, and write to GeoJSON
#
# ST_Union collapses the Kenya counties (47 polygons) into a single
# geometry. ST_Buffer grows that geometry by $buffer degrees; on an
# EPSG:4326 layer the units are degrees. For a 0.25° climate grid,
# 0.0 (exact) or 0.15 (half a cell) are sensible; 1.0 includes roughly
# 111 km beyond the border on every side.
# ----------------------------------------------------------------------
# CHANGED: `bc -l` replaced with `awk` so the comparison does not depend
# on `bc` being installed and tolerates empty/non-numeric `$buffer`.
if awk "BEGIN{exit !($buffer > 0)}"; then
    sql="SELECT ST_Buffer(ST_Union(geometry), $buffer) AS geometry FROM \"$layer_name\""
    if [[ $verbose -eq 1 ]]; then
        echo "Unioning and buffering by $buffer degrees ..."
    fi
else
    sql="SELECT ST_Union(geometry) AS geometry FROM \"$layer_name\""
    if [[ $verbose -eq 1 ]]; then
        echo "Unioning polygons (no buffer) ..."
    fi
fi

ogr2ogr -f GeoJSON \
        -t_srs EPSG:4326 \
        -dialect SQLite \
        -sql "$sql" \
        "$temp_geojson" \
        "$bounds_file"

# ----------------------------------------------------------------------
# Step 2b (NEW): Compute the bounding box of the unioned polygon.
#
# ogrinfo -so -al on the single-feature GeoJSON prints one line of the
# form:   Extent: (minX, minY) - (maxX, maxY)
# We extract the four values and reorder to the argument order that
# `cdo sellonlatbox` expects: lon_min lon_max lat_min lat_max.
# ----------------------------------------------------------------------
extent_line=$(ogrinfo -so -al "$temp_geojson" 2>/dev/null \
              | grep -E "Extent:" \
              | head -1)

if [[ -z "$extent_line" ]]; then
    echo "ERROR: could not read extent from $temp_geojson"
    exit 1
fi

bbox=$(echo "$extent_line" \
       | sed -E 's/.*Extent: \(([-0-9.eE+]+), ([-0-9.eE+]+)\) - \(([-0-9.eE+]+), ([-0-9.eE+]+)\).*/\1 \3 \2 \4/')

read -r lon_min lon_max lat_min lat_max <<< "$bbox"

if [[ $verbose -eq 1 ]]; then
    echo "Bounding box: lon=[$lon_min, $lon_max]  lat=[$lat_min, $lat_max]"
fi

# ----------------------------------------------------------------------
# Step 3: Convert the GeoJSON geometry to CDO's maskregion ASCII format
#
# maskregion expects lines of "lon lat" with a single "&" separating
# multiple polygons. Only exterior rings are written. Interior rings
# (holes) are rare on country and county boundaries and are not needed
# for masking a regular climate grid. If you ever clip to a study area
# whose polygon genuinely excludes a hole (a lake, a park), this file
# will over-include that hole; extending the writer to emit interior
# rings with an inverted mask is left as an exercise.
# ----------------------------------------------------------------------
python3 - "$temp_geojson" "$temp_ascii" <<'PYEOF'
import json, sys

geojson_path, out_path = sys.argv[1], sys.argv[2]

with open(geojson_path) as f:
    data = json.load(f)

feature_count = 0
vertex_count  = 0

with open(out_path, "w") as out:
    for feature in data.get("features", []):
        geom = feature.get("geometry")
        if not geom:
            continue
        gtype = geom["type"]
        coords = geom["coordinates"]

        if gtype == "Polygon":
            polygons = [coords]
        elif gtype == "MultiPolygon":
            polygons = coords
        else:
            # GeometryCollection or unsupported type — skip.
            continue

        for poly in polygons:
            # poly[0] is the exterior ring; poly[1:] are holes (ignored).
            ring = poly[0]
            for pt in ring:
                x, y = pt[0], pt[1]
                out.write(f"{x:.6f} {y:.6f}\n")
                vertex_count += 1
            out.write("&\n")
            feature_count += 1

print(f"  wrote {feature_count} polygon(s), {vertex_count} vertices",
      file=sys.stderr)
PYEOF

if [[ $verbose -eq 1 ]]; then
    echo "Polygon ASCII written to: $temp_ascii"
    head -5 "$temp_ascii"
    echo "..."
fi

# ----------------------------------------------------------------------
# Step 4: Select variables, crop to bbox, then mask to the polygon
#
# CDO applies operators right-to-left:
#   1. selname      picks the requested variables.
#   2. sellonlatbox crops the grid to the polygon's bounding rectangle.
#      This is what actually reduces the file size.
#   3. maskregion   sets cells outside the polygon to missing.
#
# maskregion keeps grid cells whose CENTERS fall inside the polygon and
# sets every other cell to missing. The grid itself is not changed by
# maskregion; it is already the cropped rectangle from step 2.
# ----------------------------------------------------------------------
if [[ $verbose -eq 1 ]]; then
    echo "Cropping to bbox then masking $source_file to polygon ..."
fi

cdo -f nc \
    maskregion,"$temp_ascii" \
    -sellonlatbox,"$lon_min","$lon_max","$lat_min","$lat_max" \
    -selname,"$variables" \
    "$source_file" "$temp_nc"

if [[ $verbose -eq 1 ]]; then
    echo "After mask:"
    cdo -sinfo "$temp_nc" | head -20
fi

# ----------------------------------------------------------------------
# Step 5: Daily resampling (if requested)
#
# Different aggregation for temperature (mean) versus precipitation
# (sum). We process each separately, then merge.
#
# NOTE: If the masked file is still larger than you want, add
#   cdo reducegrid,"$temp_ascii" "$temp_nc" "$target_file"
# after this block. reducegrid converts the rectangular grid to an
# unstructured 1-D list of only the in-region cells. Many downstream
# tools (xarray, GIS software) handle unstructured grids poorly, so
# only do this if you need the size reduction and can adapt the reader.
# ----------------------------------------------------------------------
if [[ $daily -eq 1 ]]; then
    present_vars=$(cdo showname "$temp_nc" 2>/dev/null)

    mean_inputs=""
    sum_inputs=""

    for v in $present_vars; do
        case "$v" in
            2t|t2m|t2|2d|d2m|sp|ssr|rh|u10|v10|10u|10v)
                mean_inputs="$mean_inputs,$v"
                ;;
            tp|precipitation|rainfall_total)
                sum_inputs="$sum_inputs,$v"
                ;;
            *)
                mean_inputs="$mean_inputs,$v"
                ;;
        esac
    done

    mean_inputs="${mean_inputs#,}"
    sum_inputs="${sum_inputs#,}"

    mean_files=()
    sum_files=()

    if [[ -n "$mean_inputs" ]]; then
        IFS=',' read -ra _mvars <<< "$mean_inputs"
        i=0
        for v in "${_mvars[@]}"; do
            f="$work_dir/mean_${i}.nc"
            cdo daymean -selname,"$v" "$temp_nc" "$f"
            mean_files+=("$f")
            i=$((i+1))
        done
    fi

    if [[ -n "$sum_inputs" ]]; then
        IFS=',' read -ra _svars <<< "$sum_inputs"
        i=0
        for v in "${_svars[@]}"; do
            f="$work_dir/sum_${i}.nc"
            cdo daysum -selname,"$v" "$temp_nc" "$f"
            sum_files+=("$f")
            i=$((i+1))
        done
    fi

    cdo merge "${mean_files[@]}" "${sum_files[@]}" "$target_file"
else
    cp "$temp_nc" "$target_file"
fi

# ----------------------------------------------------------------------
# Step 6: Optionally rename 2t to t2m
# ----------------------------------------------------------------------
if [[ $rename_t2m -eq 1 ]]; then
    tmp_renamed="$work_dir/renamed.nc"
    cdo chname,2t,t2m "$target_file" "$tmp_renamed"
    mv "$tmp_renamed" "$target_file"
    echo "Renamed variable '2t' to 't2m'."
fi

# ----------------------------------------------------------------------
# Step 7: Final report
# ----------------------------------------------------------------------
echo "Clipped data saved to $target_file"
echo "Final check:"
cdo info "$target_file" | head -5