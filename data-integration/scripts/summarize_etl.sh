#!/usr/bin/env bash
# summarize_etl.sh - read logs/etl.log and report what the pipeline actually did.
#
# Usage:
#   ./run.sh etl-summary                        # last run in logs/etl.log
#   ./run.sh etl-summary path/to/etl.log        # last run in another file
#   ./run.sh etl-summary --all                  # aggregate every run
#   ./run.sh etl-summary --run 2                # second-most-recent run
#   ./run.sh etl-summary --top 40 --log-level INFO
#   ./run.sh etl-summary --fail-on-error        # exit 1 if any ERROR/CRITICAL
#
# Why this script is tied to specific strings
# -------------------------------------------
# Every section below greps for a message that exists in the current code, and
# each pattern carries a comment naming the function that emits it. When the
# pipeline changes, a pattern stops matching and the section prints "(none)" —
# which looks exactly like "no problems found". Section 15 (PATTERN COVERAGE)
# exists to make that difference visible: it reports, per pattern, whether the
# run block contains it at all.
#
# The log is opened in append mode by main.py, so it accumulates every run.
# By default this script reports the LAST run only, sliced from its
# "Loaded configuration from" line. --all aggregates the whole file, which mixes
# runs and is only useful for trend questions.
#
# Output is plain text, designed to be pasted into a chat/debugging thread.

set -uo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
LOG="logs/etl.log"
MODE="last"          # last | all
RUN_INDEX=1          # 1 = most recent run, 2 = the one before it, ...
TOP=20
FAIL_ON_ERROR=0
WANT_LEVEL=""        # only show WARNING/ERROR rows of this exact level in §10/§11

usage() {
    cat <<EOF
$PROG - summarise the VBD CDM ETL log

Usage: $PROG [LOG] [options]

Arguments:
  LOG                 log file (default: logs/etl.log)

Options:
  --all               aggregate the whole file instead of the last run
  --run N             report the Nth-most-recent run (default: 1)
  --top N             rows per section (default: 20)
  --level LEVEL       restrict sections 10-11 to WARNING, ERROR or CRITICAL
  --fail-on-error     exit 1 when the run contains an ERROR or CRITICAL
  -h, --help          this message

Sections:
   0 run identity      7 FK alignment
   1 log overview      8 locations
   2 ingest sources    9 vocabulary + reconciliation
   3 table loads      10 resolution ledger
   4 load-time repairs 11 burden expansion
   5 lookups/failures 12 resistance
   6 phase timeline   13 duplicates, validation, orphans
  14 final summary    15 pattern coverage (drift check)
  16 consistency checks
EOF
}

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --all)           MODE="all"; shift ;;
        --run)           MODE="last"; RUN_INDEX="${2:?--run needs a number}"; shift 2 ;;
        --top)           TOP="${2:?--top needs a number}"; shift 2 ;;
        --level)         WANT_LEVEL="${2:?--level needs a name}"; shift 2 ;;
        --fail-on-error) FAIL_ON_ERROR=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        -*)              echo "$PROG: unknown option $1" >&2; usage >&2; exit 2 ;;
        *)               LOG="$1"; shift ;;
    esac
done

if [ ! -f "$LOG" ]; then
    echo "ERROR: log file not found: $LOG" >&2
    exit 1
fi

TMPDIR_SUM="$(mktemp -d "${TMPDIR:-/tmp}/etlsum.XXXXXX")" || exit 1
trap 'rm -rf "$TMPDIR_SUM"' EXIT
RUN="$TMPDIR_SUM/run.log"

# ---------------------------------------------------------------- run scoping
extract_run() {
    local src="$1" total start
    total=$(grep -c 'Loaded configuration from' "$src" 2>/dev/null || true)
    if [ "$MODE" = "all" ] || [ "${total:-0}" -eq 0 ]; then
        cat "$src"
        return
    fi
    start=$(grep -n 'Loaded configuration from' "$src" \
            | tail -n "$RUN_INDEX" | head -n 1 | cut -d: -f1)
    if [ -z "$start" ]; then
        cat "$src"
    else
        tail -n +"$start" "$src"
    fi
}
extract_run "$LOG" > "$RUN"

TOTAL_RUNS=$(grep -c 'Loaded configuration from' "$LOG" 2>/dev/null || true)
TOTAL_RUNS=${TOTAL_RUNS:-0}

# ---------------------------------------------------------------- helpers
section() {
    printf '\n%s\n%s\n%s\n' \
        "============================================================" \
        "$1" \
        "============================================================"
}

note() { printf '  %s\n' "$*"; }

# Lines of the log, normalised to "TIMESTAMP LEVEL message" (logger name dropped).
# The format is main.py's: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
normalise() {
    sed -nE 's/^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}),[0-9]{3} - [^ ]+ - ([A-Z]+) - +(.*)$/\1 \2 \3/p' "$RUN"
}

# Masked, counted, first-example-preserving dedup of WARNING/ERROR/CRITICAL.
dedupe() {
    local levels="$1"
    local filter=""
    [ -n "$WANT_LEVEL" ] && filter="$WANT_LEVEL"
    sed -nE "s/^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}),[0-9]{3} - [^ ]+ - (${levels}) - +(.*)\$/\\2@@\\1@@\\3/p" "$RUN" \
    | awk -F'@@' -v keep="$filter" '
        keep != "" && $1 != keep { next }
        { lvl=$1; ts=$2; msg=$3;
          key=msg
          gsub(/0x[0-9a-fA-F]+/, "HEX", key)
          gsub(/[0-9]+/, "N", key)
          gsub(/[[:space:]]+/, " ", key)
          if (!(key in cnt)) { ord[++n]=key; first[key]=ts; sample[key]=msg; level[key]=lvl }
          cnt[key]++ }
        END { for (i=1;i<=n;i++) { k=ord[i]
                printf "%07d\t%s\t%s\t%s\n", cnt[k], first[k], level[k], sample[k] } }' \
    | sort -rn \
    | head -n "$TOP" \
    | awk -F'\t' '{ printf "  %5d  %s  %-7s  %s\n", $1, $2, $3, $4 }'
}

count_of() { grep -cE "$1" "$RUN" 2>/dev/null || true; }

show_block() {
    # print "$1" lines starting at the first match, at most "$2" lines
    awk -v pat="$1" -v limit="${2:-14}" '
        $0 ~ pat { printing = 1 }
        printing { print; if (++n >= limit) exit }'
}

# ============================================================================
section "0. RUN IDENTITY"
if [ "$MODE" = "all" ]; then
    note "scope:        ALL RUNS ($TOTAL_RUNS invocation(s) in this file)"
else
    note "scope:        run $RUN_INDEX of $TOTAL_RUNS (most recent first)"
fi
note "file:         $LOG"

grep -E "Loaded configuration from" "$RUN" | sed -E 's/.* - INFO - +/  config:       /' | tail -3
grep -E "run id .*\| registry" "$RUN" | sed -E 's/.* - INFO - +/  identity:     /' | tail -3
grep -E "database .*\(file:" "$RUN" | sed -E 's/.* - INFO - +/  database:     /' | tail -3
grep -E "registry .*: [0-9]+ concept pack" "$RUN" | sed -E 's/.* - INFO - +/  registry:     /' | tail -3
grep -E "^.* - INFO - +(  run id:    |  registry:  |  countries: |  boundary:  )" "$RUN" \
    | sed -E 's/.* - INFO - +/  consolidator: /' | tail -4
grep -E "Run .* complete\." "$RUN" | sed -E 's/.* - INFO - +/  finished:     /' | tail -3

# ============================================================================
section "1. LOG OVERVIEW"
lines=$(wc -l < "$RUN" 2>/dev/null || echo 0)
bytes=$(wc -c < "$RUN" 2>/dev/null || echo 0)
first_ts=$(grep -m1 -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$RUN" || true)
last_ts=$(grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$RUN" | tail -1 || true)
note "lines:        $lines"
note "bytes:        $bytes"
note "first ts:     ${first_ts:-(none)}"
note "last ts:      ${last_ts:-(none)}"
note "DEBUG:        $(count_of ' - DEBUG - ')"
note "INFO:         $(count_of ' - INFO - ')"
note "WARNING:      $(count_of ' - WARNING - ')"
note "ERROR:        $(count_of ' - ERROR - ')"
note "CRITICAL:     $(count_of ' - CRITICAL - ')"
note "tracebacks:   $(count_of '^Traceback \(most recent call last\)')"
if [ "$MODE" != "all" ] && [ "${TOTAL_RUNS:-0}" -gt 1 ]; then
    note ""
    note "This file holds $TOTAL_RUNS runs; only the selected one is reported."
    note "Use --all to aggregate, --run N to pick a different one."
fi

# ============================================================================
section "2. INGEST - PER-SOURCE OUTCOME"
# main.run_ingestion_for_source: "Source %r completed: %d dimension row(s), %d fact row(s)"
# %r quotes the name with single quotes.
if grep -qE "Source .* completed:" "$RUN"; then
    printf '  %-42s %12s %12s\n' "source" "dim rows" "fact rows"
    printf '  %-42s %12s %12s\n' "------" "--------" "---------"
    grep -E "Source .* completed:" "$RUN" \
    | sed -E "s/.*Source ['\"]?([^'\"]+)['\"]? completed: ([0-9]+) dimension row\(s\), ([0-9]+) fact row\(s\).*/\1@@\2@@\3/" \
    | awk -F'@@' '{ printf "  %-42s %12d %12d\n", $1, $2, $3 }'
    printf '  %-42s %12s %12s\n' "" "----------" "----------"
    grep -E "Source .* completed:" "$RUN" \
    | sed -E "s/.* completed: ([0-9]+) dimension row\(s\), ([0-9]+) fact row\(s\).*/\1 \2/" \
    | awk '{ d+=$1; f+=$2; n++ } END { printf "  %-42s %12d %12d  (%d source(s))\n", "TOTAL", d, f, n }'
else
    note "(no completed source: either none ran, or the run is still in progress)"
fi
echo ""
note "sources started:  $(count_of '===== Processing source:')"
note "files found:      $(count_of 'Found [0-9]+ \.[a-z0-9]+ file\(s\)')"
note "files processed:  $(count_of 'Processing file:')"
note "chunks reported:  $(count_of 'processed [0-9]+ chunk\(s\) so far')"

# ============================================================================
section "3. INGEST - PER-TABLE LOADS"
# etl.loaders.db_loader.DBLoader.load_table:
#   "Loaded %d rows into %s (input had %d)"       <- note: no "prepared had"
#   "Upserted %d rows into %s via COPY and staging" (PostgreSQL COPY path)
if grep -qE "Loaded [0-9]+ rows into" "$RUN"; then
    grep -oE "Loaded [0-9]+ rows into [a-z_]+ \(input had [0-9]+\)" "$RUN" \
    | sed -E 's/Loaded ([0-9]+) rows into ([a-z_]+) \(input had ([0-9]+)\)/\2 \1 \3/' \
    | awk '{ loaded[$1]+=$2; input[$1]+=$3; n[$1]++ }
           END { for (t in loaded) printf "%s %d %d %d\n", t, loaded[t], input[t], n[t] }' \
    | sort -k1,1 > "$TMPDIR_SUM/loads.txt"

    printf '  %-32s %11s %11s %9s %8s\n' "table" "loaded" "input" "not written" "batches"
    printf '  %-32s %11s %11s %9s %8s\n' "-----" "------" "-----" "-----------" "-------"
    awk '{ printf "  %-32s %11s %11s %9s %8s\n", $1, $2, $3, $3-$2, $4 }' "$TMPDIR_SUM/loads.txt"
    awk '{ l+=$2; i+=$3; n+=$4 } END {
        printf "  %-32s %11d %11s %9s %8d\n", "TOTAL", l, i, i-l, n }' "$TMPDIR_SUM/loads.txt"
    echo ""
    note "COPY-path loads: $(count_of 'Upserted [0-9]+ rows into .* via COPY')"
else
    note "(no table loads recorded in this run)"
fi
echo ""
note "rows built by the transformer (main.load_dimensions/load_facts):"
grep -oE "Built [0-9]+ rows for table '[a-z_]+'" "$RUN" \
| sed -E "s/Built ([0-9]+) rows for table '([a-z_]+)'/\2 \1/" \
| awk '{ b[$1]+=$2; n[$1]++ }
       END { for (t in b) printf "%s %d %d\n", t, b[t], n[t] }' \
| sort -k1,1 \
| awk '{ printf "  %-32s %11s %11s\n", $1, $2, $3 }'
[ "$(count_of "Built [0-9]+ rows for table")" = "0" ] && note "  (none - the transformer logged no build counts)"

# ============================================================================
section "4. INGEST - LOAD-TIME REPAIRS"
note "NaN filled from a model default (load_table):"
grep -oE "[a-z_]+\.[a-z_]+: filled [0-9]+ NaN value" "$RUN" | sed -E 's/^/  /' | sort | uniq -c | sort -rn | head -"$TOP"
note ""
note "rows dropped for a NOT NULL column with no default:"
grep -oE "[a-z_]+\.[a-z_]+: dropped [0-9]+ row" "$RUN" | sed -E 's/^/  /' | sort | uniq -c | sort -rn | head -"$TOP"
note ""
note "rows dropped because only a SERVER default exists (an INSERT would fail):"
grep -oE "[a-z_]+\.[a-z_]+: [0-9]+ row\(s\) still NaN after default fill" "$RUN" | sed -E 's/^/  /' | head -"$TOP"
[ "$(count_of 'still NaN after default fill')" = "0" ] && note "  (none)"
note ""
note "main.load_facts / load_dimensions drops:"
grep -E "dropped [0-9]+ row\(s\) with null|removed [0-9]+ row\(s\) with NULL" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
[ "$(count_of 'dropped [0-9]+ row\(s\) with null|removed [0-9]+ row\(s\) with NULL')" = "0" ] && note "  (none)"
note ""
note "in-batch duplicate collapse (DBLoader._prepare_for_upsert):"
grep -oE "Collapsed [0-9]+ duplicate row\(s\) within the batch for [a-z_]+" "$RUN" | sed -E 's/^/  /' | head -"$TOP"
[ "$(count_of 'Collapsed [0-9]+ duplicate row')" = "0" ] && note "  (none)"
note ""
note "natural-key NULL warnings (the constraint cannot bite):"
grep -E "collapsing in-batch duplicates on|conflict target .* contains NULL values" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
[ "$(count_of 'collapsing in-batch duplicates on|conflict target .* contains NULL')" = "0" ] && note "  (none)"

# ============================================================================
section "5. MAPPING - LOOKUPS AND TRANSFORM FAILURES"
note "dimension lookups that found nothing (DimensionKeyIndex):"
# MappingTransformer._apply_map_transform: "No match found for %s.%s = %r"
# %r quotes strings, leaves numbers bare - both are counted here.
grep -oE "No match found for [a-z_]+\.[a-z_]+ = .*" "$RUN" \
| sed -E 's/No match found for ([a-z_]+)\.([a-z_]+) = (.*)/\1.\2 = \3/' \
| sed -E 's/[0-9]+/N/g' \
| sort | uniq -c | sort -rn | head -"$TOP" | sed -E 's/^/  /'
[ "$(count_of 'No match found for')" = "0" ] && note "  (none)"
note ""
note "refs no index could be built for (logged once per pass):"
grep -E "Cannot build a dimension index|No dimension index for" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
[ "$(count_of 'Cannot build a dimension index|No dimension index for')" = "0" ] && note "  (none)"
note ""
note "mapping-shape problems:"
grep -E "Invalid ref format|is not '<table>.<column>'|non-authoritative|with a .map. ref but no" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
[ "$(count_of 'Invalid ref format|is not .<table>.<column>.|non-authoritative')" = "0" ] && note "  (none)"
note ""
note "transform failures (masked, counted):"
grep -oE "(Cast to [a-z]+ failed for value .*|Date parse failed for .*|Split index [0-9]+ out of range.*|unit_convert: could not parse .*|unit_convert: divide by zero.*|Concat: column .*|Unknown transform type: .*)" "$RUN" \
| sed -E 's/[0-9]+/N/g; s/'"'"'[^'"'"']*'"'"'/Q/g' \
| sort | uniq -c | sort -rn | head -"$TOP" | sed -E 's/^/  /'
[ "$(count_of 'Cast to .* failed|Date parse failed|Split index .* out of range|unit_convert: could not parse|unit_convert: divide by zero|Concat: column|Unknown transform type')" = "0" ] && note "  (none)"

# ============================================================================
section "6. CONSOLIDATION - PHASE TIMELINE"
# utilities.consolidation.consolidate_all emits these banners in this order.
TIMELINE='CONSOLIDATING CDM|FK: ALIGN FOREIGN KEYS|A6a: STANDARDISE COUNTRY CODES|A6: CONSOLIDATE LOCATIONS|VOCAB: LOAD SHIPPED VOCABULARIES|A8: VOCABULARY RECONCILIATION|A8b: ALIGN DIMENSION VALUES|A8c: ALIGN FACT VALUES|A9: ATTACH ADMIN CONCEPTS|A9b: NAME LOCATIONS|B5: MERGE DIMENSION ROWS|FACTS: REASSIGN BY CONCEPT|CASCADES: AUDIT|BURDEN: EXPAND TO DAILY|RESISTANCE: CLASSIFY BIOASSAYS|FACT DUPLICATES \+ VALIDATION|RECONCILIATION COMPLETE|BURDEN EXPANSION COMPLETE|CONSOLIDATION COMPLETE'
if grep -qE " - INFO - +($TIMELINE)" "$RUN"; then
    grep -E " - INFO - +($TIMELINE)" "$RUN" \
    | sed -E 's/^([0-9-]+ [0-9:]+),[0-9]+ - [^ ]+ - INFO - +/  \1  /' \
    | head -60
else
    note "(the consolidation phase banners are absent: consolidation did not run,"
    note " or the run was killed before it started)"
fi

# ============================================================================
section "7. FK ALIGNMENT"
grep -E "Repaired: [0-9]+ \| Orphans:|ALIGNING ALL FOREIGN KEYS" "$RUN" \
    | sed -E 's/.* - INFO - +/  /' | head -5
note ""
note "per column: repair outcomes"
grep -E "repair complete \([0-9]+ fixed|still carry an unresolvable FK|NULLed [0-9]+ unresolvable FK" "$RUN" \
    | sed -E 's/^([0-9-]+ [0-9:]+),[0-9]+ - [^ ]+ - (INFO|WARNING|ERROR) - +/\1  \2  /' | head -"$TOP"
[ "$(count_of 'repair complete|could not be classified|still carry an unresolvable FK|NULLed .* unresolvable')" = "0" ] && note "  (none)"
note ""
note "quarantine moves (NOT NULL FK, row REMOVED from the fact table):"
grep -E "MOVED [0-9]+ row\(s\) into _quarantine" "$RUN" \
    | sed -E 's/^([0-9-]+ [0-9:]+),[0-9]+ - [^ ]+ - WARNING - +/\1  /' | head -"$TOP"
[ "$(count_of 'MOVED [0-9]+ row\(s\) into _quarantine')" = "0" ] && note "  (none)"
note ""
note "dimensions the aligner could not read:"
grep -E "Could not get valid values from|Could not query orphans for" "$RUN" \
    | sed -E 's/.* - WARNING - +/  /' | head -"$TOP"
[ "$(count_of 'Could not get valid values from|Could not query orphans for')" = "0" ] && note "  (none)"
note ""
note "NOTE: rows skipped because a fact FK is NOT NULL are counted in"
note "      _align_dimension_foreign_keys' returned 'skipped_notnull' and are"
note "      never logged. A silent skip there is invisible to this script."

# ============================================================================
section "8. LOCATIONS"
note "A6a country standardisation:"
grep -E "country: [0-9]+ row\(s\) examined|country: [0-9]+ distinct code\(s\)|country: [0-9]+ centroid" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -6
[ "$(count_of 'country: [0-9]+ row\(s\) examined')" = "0" ] && note "  (none)"
note ""
note "A6 merge, hierarchy and grid parenting:"
grep -E "locations: [0-9]+ -> [0-9]+ row\(s\)|hierarchy: placed|grid parents:|no grid cells found|but no administrative row carries coordinates|detached" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -8
[ "$(count_of 'locations: [0-9]+ -> |grid parents:')" = "0" ] && note "  (none)"
note ""
note "A9b naming from coordinates:"
grep -E "location names: [0-9]+ of [0-9]+ row\(s\) renamed|location naming: [0-9]+ polygon|name/identity similarity" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -6
[ "$(count_of 'location names: .* renamed')" = "0" ] && note "  (none)"
note ""
note "dim_location row-level summary:"
grep -E "LOCATION SUMMARY|dim_location: [0-9]+ row\(s\) merged|dim_location: no duplicate standard_code|same-name group" "$RUN" \
    | sed -E 's/.* - INFO - +/  /' | head -6
note ""
note "identity merges:"
grep -E "dim_location: .* merged across|dim_location: .* still has .* referencing" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"

# ============================================================================
section "9. VOCABULARIES"
note "VOCAB - shipped CSV load (utilities.vocabulary.alignment):"
grep -E "VOCAB: LOAD SHIPPED VOCABULARIES|Vocabularies:  [0-9]+ loaded|concept\(s\) from |stays unresolved" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
[ "$(count_of 'Vocabularies:  [0-9]+ loaded')" = "0" ] && note "  (no vocabulary-load summary line)"
note ""
note "A8 reconciliation:"
grep -E "registry digest .* recorded in dim_vocabulary|RECONCILIATION COMPLETE|rows stamped with a concept|: [0-9]+ value\(s\), [0-9]+ applied|: [0-9]+ value\(s\) vs |pack .* has no concepts in dim_concept|cover this field" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
[ "$(count_of 'rows stamped with a concept')" = "0" ] && note "  (reconciliation did not report a stamped total)"
note ""
note "fields with NO governing pack (values kept as written, merged on a key only):"
grep -E "field\(s\) have NO vocabulary pack|have NO governing vocabulary" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -3
grep -E "^.* - WARNING - +    [a-z_]+\.[a-z_]+ +[0-9]+ distinct value" "$RUN" \
    | sed -E 's/.* - WARNING - +/    /' | head -"$TOP"
[ "$(count_of 'NO vocabulary pack|NO governing vocabulary')" = "0" ] && note "  (none: every catalogued text field is bound)"
note ""
note "A8b / A8c value alignment + B5 merge:"
grep -E "Value align:|Standardised [a-z_]+:|A8b:|A8c:|B5:|merged on the pack's normaliser|merged on the generic key|surrogate-keyed; A6 merges it|table\(s\) left unmerged" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"

# ============================================================================
section "10. RESOLUTION LEDGER (fact_resolution_log / fact_concept_map)"
grep -E "Appended [0-9]+ ledger row|Projected [0-9]+ current-state row|close: flushed|close: could not flush" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING|ERROR) - +/  /' | head -"$TOP"
[ "$(count_of 'Appended [0-9]+ ledger row|Projected [0-9]+ current-state row|close: flushed')" = "0" ] && note "  (no ledger writes recorded)"
note ""
note "human-review protection and contract violations:"
grep -E "carry a human approval|are .* but carry no concept_code|contract says that combination" "$RUN" \
    | sed -E 's/.* - (WARNING|ERROR) - +/  /' | head -"$TOP"
[ "$(count_of 'carry a human approval|but carry no concept_code')" = "0" ] && note "  (none)"

# ============================================================================
section "11. BURDEN EXPANSION"
grep -E "BURDEN EXPANSION COMPLETE|Burden expansion:|^.* - INFO - +  measures:|^.* - INFO - +  denominators:|written at .* target|round to 0 cases|shared a case key|allocation: burden_id|national allocation is ON" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
if [ "$(count_of 'BURDEN EXPANSION COMPLETE')" = "0" ]; then
    note "(no BURDEN EXPANSION COMPLETE line: the phase did not run, or it raised)"
fi

# ============================================================================
section "12. RESISTANCE CLASSIFICATION"
grep -E "COMPUTING RESISTANCE STATUS|  pack: .*cut-offs|Resistance:    [0-9]+ classified|could not be classified|still have no resistance_status" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
[ "$(count_of 'COMPUTING RESISTANCE STATUS')" = "0" ] && note "  (classification did not run)"

# ============================================================================
section "13. DUPLICATES, VALIDATION AND REMAINING ORPHANS"
note "fact-table duplicates:"
grep -E "Resolving fact table duplicates|no duplicates; untouched|collapsed [0-9]+ duplicate group|fact duplicate group" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
note ""
note "dimension duplicate merges (B5):"
grep -E "duplicate group\(s\), [0-9]+ removed|no duplicate group" "$RUN" \
    | sed -E 's/.* - INFO - +/  /' | head -"$TOP"
note ""
note "validation and residuals:"
grep -E "Country codes: [0-9]+ valid|Hierarchy:     [0-9]+/|Remaining orphans:|still references [0-9]+ deleted key|C7 conflicts|canonical label for their code" "$RUN" \
    | sed -E 's/.* - (INFO|WARNING) - +/  /' | head -"$TOP"
note ""
note "per-FK orphan totals:"
grep -E "^.* - WARNING - +    [a-z_]+\.[a-z_]+: [0-9]+$" "$RUN" \
    | sed -E 's/.* - WARNING - +/    /' | head -"$TOP"
[ "$(count_of '^.* - WARNING - +    [a-z_]+\.[a-z_]+: [0-9]+$')" = "0" ] && note "    (none)"

# ============================================================================
section "14. FINAL RUN SUMMARY (main._log_consolidation_summary)"
if grep -q "CONSOLIDATION SUMMARY" "$RUN"; then
    awk '/CONSOLIDATION SUMMARY/ { on=1 }
         on { print "  " $0 }
         on && /^-{5,}$/ && ++seen >= 1 && NR > 1 && $0 ~ /^-+$/ && length($0) > 20 { print "  " $0 }' "$RUN" \
        | sed -E 's/^  ([0-9-]+ [0-9:]+),[0-9]+ - [^ ]+ - INFO - /  /' \
        | head -40
else
    note "(no CONSOLIDATION SUMMARY block: main skips it when consolidation"
    note " reported an error, or consolidation was skipped by config)"
fi
note ""
grep -E "CONSOLIDATION COMPLETE" "$RUN" | sed -E 's/.* - INFO - +/  /' | tail -2
grep -E "Run .* complete\." "$RUN" | sed -E 's/.* - INFO - +/  /' | tail -1
grep -E "Generating database summary|Summary generated in" "$RUN" | sed -E 's/.* - INFO - +/  /' | tail -2

# ============================================================================
section "15. PATTERN COVERAGE (drift check)"
note "Whether each pattern this script depends on appears at all in the run."
note "A section printing (none) is only trustworthy when its pattern is here."
note ""
COVERAGE='run header::Loaded configuration from
run identity::run id .* \| registry
dim_time seeding::created [0-9]+ new time record
source start::===== Processing source:
source completion::Source .* completed:
source failure::Failed to process source
transformer build::Built [0-9]+ rows for table
table load::Loaded [0-9]+ rows into
COPY load::Upserted [0-9]+ rows into .* via COPY
NaN fill::filled [0-9]+ NaN value
rows dropped (NOT NULL):::[a-z_]+\.[a-z_]+: dropped [0-9]+ row
in-batch collapse::Collapsed [0-9]+ duplicate row
FK alignment banner::ALIGNING ALL FOREIGN KEYS
FK alignment totals::Repaired: [0-9]+ \| Orphans:
country standardisation::A6a: STANDARDISE COUNTRY CODES
location consolidation::A6: CONSOLIDATE LOCATIONS
location naming::location names: .* renamed
vocabulary load banner::VOCAB: LOAD SHIPPED VOCABULARIES
reconciliation banner::A8: VOCABULARY RECONCILIATION
reconciliation totals::rows stamped with a concept
value alignment::A8b: ALIGN DIMENSION VALUES
spatial concepts::A9: ATTACH ADMIN CONCEPTS
concept merge::B5: MERGE DIMENSION ROWS
resolution ledger::Appended [0-9]+ ledger row
burden expansion::BURDEN EXPANSION COMPLETE
resistance::COMPUTING RESISTANCE STATUS
fact duplicates::Resolving fact table duplicates
final summary::CONSOLIDATION SUMMARY
close / drain::close: flushed'
missing=0
printf '%s\n' "$COVERAGE" | while IFS= read -r row; do
    label="${row%%::*}"
    pattern="${row#*::}"
    if grep -qE "$pattern" "$RUN"; then
        printf '  %-34s present\n' "$label"
    else
        printf '  %-34s NOT FOUND\n' "$label"
    fi
done
echo ""
if printf '%s\n' "$COVERAGE" | while IFS= read -r row; do
        label="${row%%::*}"; pattern="${row#*::}"
        grep -qE "$pattern" "$RUN" || echo "$label"
    done | grep -q .; then
    note "Patterns marked NOT FOUND mean this script cannot describe that part of"
    note "the run. Either the phase did not execute, or the message changed and"
    note "the grep above needs updating - check the emitting function named in the"
    note "section header before concluding 'no problems found'."
fi

# ============================================================================
section "16. CONSISTENCY CHECKS"
problems=0
flag() { printf '  [!!] %s\n' "$*"; problems=$((problems + 1)); }
ok()   { printf '  [ok] %s\n' "$*"; }

started=$(count_of '===== Processing source:')
finished=$(count_of "Source .* completed:")
failed=$(count_of 'Failed to process source')
if [ "${started:-0}" -gt 0 ]; then
    if [ $((finished + failed)) -eq "$started" ]; then
        ok "sources accounted for: $started started, $finished completed, $failed failed"
    else
        flag "sources unaccounted for: $started started, $finished completed, $failed failed"
        flag "  -> a source raised without logging, or the run was killed mid-source"
    fi
fi

cons_started=$(count_of 'CONSOLIDATING CDM')
cons_done=$(count_of 'CONSOLIDATION COMPLETE')
summary_seen=$(count_of 'CONSOLIDATION SUMMARY')
if [ "$cons_started" -gt 0 ] && [ "$cons_done" -eq 0 ]; then
    flag "consolidation started but never completed (killed, or it raised)"
elif [ "$cons_started" -gt 0 ] && [ "$summary_seen" -eq 0 ]; then
    flag "consolidation completed but the summary was skipped"
    flag "  -> main.run_consolidation_step logs an error and exits 2"
elif [ "$cons_started" -gt 0 ]; then
    ok "consolidation started, completed and summarised"
elif [ "$summary_seen" -eq 0 ]; then
    ok "consolidation not part of this run (--skip-consolidation or no step)"
fi

if [ "$(count_of 'BURDEN EXPANSION COMPLETE')" -gt 0 ]; then
    burden_line=$(grep -E "BURDEN EXPANSION COMPLETE" "$RUN" | tail -1)
    ins=$(printf '%s' "$burden_line" | sed -E 's/.*COMPLETE: ([0-9]+) inserted.*/\1/')
    nop=$(printf '%s' "$burden_line" | sed -E 's/.* ([0-9]+) no-population.*/\1/')
    zero=$(printf '%s' "$burden_line" | sed -E 's/.* ([0-9]+) zero-total.*/\1/')
    if [ "${ins:-0}" -eq 0 ] && [ "${nop:-0}" -gt 0 ]; then
        flag "burden expansion inserted 0 rows and skipped $nop for lack of a denominator"
        flag "  -> the population series is not on the burden locations; --national-allocation"
    elif [ "${ins:-0}" -eq 0 ] && [ "${zero:-0}" -gt 0 ]; then
        flag "burden expansion inserted 0 rows: every group rounded to 0 cases"
        flag "  -> for rate records this is the rate SCALE, not the denominator"
    else
        ok "burden expansion: $ins row(s) inserted, $nop no-denominator, $zero zero-total"
    fi
fi

errors=$(count_of ' - ERROR - ')
crit=$(count_of ' - CRITICAL - ')
if [ $((errors + crit)) -eq 0 ]; then
    ok "no ERROR or CRITICAL lines in this run"
else
    flag "$errors ERROR and $crit CRITICAL line(s) - see section 10"
fi

if [ "$problems" -eq 0 ]; then
    echo ""
    note "STATUS: no inconsistencies found in this run."
else
    echo ""
    note "STATUS: $problems potential inconsistency/inconsistencies (see [!!] above)."
fi

# ============================================================================
section "17. WARNINGS (masked, counted)"
dedupe "WARNING" || true
[ "$(count_of ' - WARNING - ')" = "0" ] && note "(none)"

section "18. ERRORS AND CRITICAL (masked, counted)"
dedupe "ERROR|CRITICAL" || true
[ "$(count_of ' - (ERROR|CRITICAL) - ')" = "0" ] && note "(none)"

section "19. WHERE THEY COME FROM (logger name)"
normalise | awk '{ print $3 }' > /dev/null 2>&1 || true
sed -nE 's/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} - ([^ ]+) - (WARNING|ERROR|CRITICAL) - .*$/\1 \2/p' "$RUN" \
| sort | uniq -c | sort -rn | head -"$TOP" | sed -E 's/^/  /'
[ "$(count_of ' - (WARNING|ERROR|CRITICAL) - ')" = "0" ] && note "(none)"

echo ""
echo "=== END OF SUMMARY ==="

if [ "$FAIL_ON_ERROR" -eq 1 ] && [ $((errors + crit)) -gt 0 ]; then
    exit 1
fi
exit 0