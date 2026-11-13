#!/usr/bin/env bash

# ----------------------------------------------------------------------
# clip_timeseries_data
# Clip a GRIB/NetCDF file to the actual borders of a shapefile.
#
# Output format is inferred from the target extension:
#   *.nc  *.nc4              -> NetCDF  (-f nc)
#   *.grib  *.grb            -> GRIB1   (-f grb)
#   *.grib2 *.grb2           -> GRIB2   (-f grb2)
# Use --format to override the inference.
#
# Approach:
#   1. Union all polygons in the shapefile into a single geometry.
#   2. Optionally buffer that geometry by --buffer degrees.
#   3. Crop the source grid to the polygon's bounding box  (sellonlatbox).
#   4. Convert the polygon to the ASCII format `cdo maskregion` expects.
#   5. Mask the cropped file to the region.
#   6. Optionally aggregate to daily (mean for 2t, sum for tp).
#   7. Optionally rename 2t -> t2m.
#   8. Convert the final NetCDF intermediate to the requested output format.
#
# Step 3 is what makes the output smaller than the input. `maskregion`
# alone only sets out-of-region cells to missing; it does not shrink the
# grid, so the file size is unchanged. `sellonlatbox` reduces the grid to
# the polygon's bounding rectangle before masking.
#
# The intermediate processing is always performed in NetCDF. This is the
# format CDO handles most reliably across versions; the final write to
# GRIB (when requested) is a single copy operation, so any GRIB-specific
# limitation (parameter table lookup, missing-value encoding) surfaces
# at exactly one place, at the end.
# ----------------------------------------------------------------------

set -e

# Defaults
buffer=0.0                 # polygon buffer in degrees; 0 = exact boundary
daily=0
verbose=0
variables="2t,tp"          # default: temperature and precipitation
rename_t2m=0
output_format=""           # empty = infer from target extension

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
        --format=*)
            output_format="${arg#*=}"
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
    echo "Usage: $0 --source=<file> --bounds=<shapefile> --target=<out.[nc|grib|grib2]> [--daily] [--buffer=DEGREES] [--variables=2t,tp] [--format=nc|grb|grb2] [--rename-t2m] [--verbose]"
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
# Resolve the output format.
#
# If --format was provided, it wins. Otherwise we infer from the target
# extension. The three CDO format identifiers are the ones we care about:
#   nc    -> NetCDF
#   grb   -> GRIB1
#   grb2  -> GRIB2
# ----------------------------------------------------------------------
if [[ -z "$output_format" ]]; then
    case "${target_file,,}" in
        *.nc|*.nc4)      output_format="nc"   ;;
        *.grib2|*.grb2)  output_format="grb2" ;;
        *.grib|*.grb)    output_format="grb"  ;;
        *)
            echo "ERROR: cannot infer output format from '$target_file'."
            echo "       Use --target=<file>.nc, .grib, or .grib2,"
            echo "       or pass --format=nc|grb|grb2 explicitly."
            exit 1
            ;;
    esac
fi

# Validate the format if --format was supplied explicitly.
case "$output_format" in
    nc|grb|grb2) : ;;
    *)
        echo "ERROR: --format must be one of: nc, grb, grb2 (got: '$output_format')"
        exit 1
        ;;
esac

# Warn about the combination that most often surprises users: --rename-t2m
# followed by GRIB1 output. In GRIB1, '2t' is the canonical ECMWF shortName;
# renaming to 't2m' and then asking CDO to look up the GRIB1 parameter table
# can produce a generic parameter code or a warning, depending on the CDO
# and eccodes version. The rename is safe for NetCDF and generally safe for
# GRIB2, where 't2m' is a standard discipline/category/number triple.
if [[ $rename_t2m -eq 1 && "$output_format" == "grb" ]]; then
    echo "WARNING: --rename-t2m with GRIB1 output may not round-trip cleanly."
    echo "         '2t' is the canonical ECMWF GRIB1 shortName for 2 m temperature;"
    echo "         't2m' is the CF-style name and is best paired with NetCDF or GRIB2."
    echo "         Proceeding; verify the output parameter name with 'cdo showname $target_file'."
fi

# ----------------------------------------------------------------------
# Temporary workspace
#
# mktemp -d creates an empty directory; individual files inside are
# created by the tools that write them.
# ----------------------------------------------------------------------
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

temp_geojson="$work_dir/polygon.geojson"
temp_ascii="$work_dir/polygon.txt"
temp_nc="$work_dir/masked.nc"
temp_processed="$work_dir/processed.nc"
temp_renamed="$work_dir/renamed.nc"

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
    echo "Output format:   $output_format"
fi

# ----------------------------------------------------------------------
# Step 2: Union the polygons, optionally buffer, and write to GeoJSON
# ----------------------------------------------------------------------
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
# Step 2b: Compute the bounding box of the unioned polygon.
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
# Step 4: Select variables, crop to bbox, then mask to the polygon.
#
# CDO applies operators right-to-left:
#   1. selname      picks the requested variables.
#   2. sellonlatbox crops the grid to the polygon's bounding rectangle.
#   3. maskregion   sets cells outside the polygon to missing.
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
# (sum). We process each separately, then merge. All operations are in
# NetCDF; the final output format is applied in Step 7.
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

    cdo merge "${mean_files[@]}" "${sum_files[@]}" "$temp_processed"
else
    cp "$temp_nc" "$temp_processed"
fi

# ----------------------------------------------------------------------
# Step 6: Optionally rename 2t -> t2m.
#
# The rename is applied to the NetCDF intermediate. This means the final
# write to GRIB sees the renamed variable and looks up its GRIB parameter
# accordingly. See the warning issued at the top of the script for the
# GRIB1 caveat.
# ----------------------------------------------------------------------
if [[ $rename_t2m -eq 1 ]]; then
    cdo chname,2t,t2m "$temp_processed" "$temp_renamed"
    if [[ $verbose -eq 1 ]]; then
        echo "Renamed '2t' to 't2m' in NetCDF intermediate."
    fi
else
    cp "$temp_processed" "$temp_renamed"
fi

# ----------------------------------------------------------------------
# Step 7: Write the final output in the requested format.
#
# For NetCDF, a copy is enough. For GRIB, CDO's -f grb / -f grb2 flag
# drives the writer, which looks up the GRIB parameter table from the
# NetCDF variable names and attributes. The 'copy' operator is a no-op
# on the data; only the container changes.
# ----------------------------------------------------------------------
case "$output_format" in
    nc)
        cp "$temp_renamed" "$target_file"
        ;;
    grb)
        cdo -f grb copy "$temp_renamed" "$target_file"
        ;;
    grb2)
        cdo -f grb2 copy "$temp_renamed" "$target_file"
        ;;
esac

if [[ $verbose -eq 1 ]]; then
    echo "Wrote final output as ${output_format}: $target_file"
    cdo showname "$target_file"
fi

# ----------------------------------------------------------------------
# Step 8: Final report
# ----------------------------------------------------------------------
echo "Clipped data saved to $target_file (format: $output_format)"
echo "Final check:"
cdo info "$target_file" | head -5