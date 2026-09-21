#!/usr/bin/env bash
# summarize_etl.sh - extract key diagnostic info from logs/etl.log
#
# Usage:
#   bash summarize_etl.sh                    # uses logs/etl.log
#   bash summarize_etl.sh path/to/some.log   # any log file
#
# Output is plain text, designed to be pasted into a chat/debugging thread.

set -u

LOG="${1:-logs/etl.log}"

if [ ! -f "$LOG" ]; then
    echo "ERROR: log file not found: $LOG" >&2
    exit 1
fi

section() {
    printf '\n'
    printf '%s\n' "============================================================"
    printf '%s\n' "$1"
    printf '%s\n' "============================================================"
}

# ---------------------------------------------------------------
# 0. Pipeline invocations (one line per run)
# ---------------------------------------------------------------
section "0. PIPELINE INVOCATIONS (one per run)"
grep "Loaded configuration from" "$LOG" | tail -20

# ---------------------------------------------------------------
# 1. Log overview
# ---------------------------------------------------------------
section "1. LOG OVERVIEW"
echo "File:       $LOG"
echo "Size:       $(du -h "$LOG" | cut -f1)"
echo "Lines:      $(wc -l < "$LOG")"
first_ts=$(grep -m1 -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$LOG" || true)
last_ts=$(tail -2000 "$LOG" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' | tail -1 || true)
echo "First ts:   ${first_ts:-(none)}"
echo "Last ts:    ${last_ts:-(none)}"
echo "INFO:       $(grep -c ' - INFO - '     "$LOG" || true)"
echo "WARNING:    $(grep -c ' - WARNING - '  "$LOG" || true)"
echo "ERROR:      $(grep -c ' - ERROR - '    "$LOG" || true)"
echo "CRITICAL:   $(grep -c ' - CRITICAL - ' "$LOG" || true)"

# ---------------------------------------------------------------
# 2. Per-source completion summary
# ---------------------------------------------------------------
section "2. PER-SOURCE COMPLETION SUMMARY"
grep -E "Source '[^']+' completed" "$LOG" | \
    sed -E "s/.*Source '([^']+)' completed\. Total dim rows loaded: ([0-9]+)\. Total facts rows loaded: ([0-9]+).*/\1|\2|\3/" | \
    awk -F'|' '{ printf "%-45s  dim=%-8s  facts=%s\n", $1, $2, $3 }'

# ---------------------------------------------------------------
# 3. Source-level failures
# ---------------------------------------------------------------
section "3. SOURCE-LEVEL FAILURES"
fails=$(grep -E "Failed to process source" "$LOG" || true)
if [ -z "$fails" ]; then
    echo "(none)"
else
    echo "$fails" | sed -E 's/^([0-9-]+ [0-9:,]+).* - (ERROR|CRITICAL) - /\1  /'
fi

# ---------------------------------------------------------------
# 4. Per-table load activity (aggregated)
# ---------------------------------------------------------------
section "4. PER-TABLE LOAD ACTIVITY (aggregated)"
grep -oE "Loaded [0-9]+ rows into [a-z_]+ \(input had [0-9]+, prepared had [0-9]+\)" "$LOG" | \
    sed -E 's/Loaded ([0-9]+) rows into ([a-z_]+) \(input had ([0-9]+), prepared had ([0-9]+)\)/\2|\1|\3|\4/' | \
    awk -F'|' '{
        t=$1; loaded[t]+=$2; input[t]+=$3; prepared[t]+=$4; n[t]++
    }
    END {
        printf "%-32s %12s %12s %12s %6s\n", "table", "loaded", "input", "prepared", "batches"
        printf "%-32s %12s %12s %12s %6s\n", "-----", "------", "-----", "--------", "-------"
        for (t in loaded)
            printf "%-32s %12d %12d %12d %6d\n", t, loaded[t], input[t], prepared[t], n[t]
    }' | sort

# ---------------------------------------------------------------
# 5. Consolidation step markers
# ---------------------------------------------------------------
section "5. CONSOLIDATION STEP MARKERS"
grep -E "utilities\.consolidation - INFO - (Step [0-9]+:|STARTING FULL DATA CONSOLIDATION|FOREIGN KEY ALIGNMENT COMPLETE|BURDEN EXPANSION COMPLETE|VOCABULARY STANDARDIZATION COMPLETE|CONSOLIDATION COMPLETE)" "$LOG" | \
    sed -E 's/.* - INFO - +//' | tail -40

# ---------------------------------------------------------------
# 6. Foreign-key alignment outcomes
# ---------------------------------------------------------------
section "6. FK ALIGNMENT - ORPHAN FINDINGS"
grep -E "Found [0-9]+ orphaned records|All orphans fixed in|orphans remain in" "$LOG" | \
    sed -E 's/.* - (INFO|WARNING) - +//' | tail -80

section "6b. FK ALIGNMENT - NO VALID VALUES / SKIPPED"
grep -E "No valid values found in|Skipping .*: NOT NULL" "$LOG" | \
    sed -E 's/.* - (INFO|WARNING) - +//' | sort | uniq -c | sort -rn | head -20

# ---------------------------------------------------------------
# 7. Quarantine events (facts deleted by FK aligner)
# ---------------------------------------------------------------
section "7. QUARANTINE EVENTS (fact rows deleted)"
q=$(grep -E "Quarantined and deleted [0-9]+ orphan rows from" "$LOG" || true)
if [ -z "$q" ]; then
    echo "(none)"
else
    echo "$q" | sed -E 's/.* - WARNING - +//'
fi

# ---------------------------------------------------------------
# 8. Hard deletes during location merge
# ---------------------------------------------------------------
section "8. HARD DELETES DURING LOCATION MERGE"
d=$(grep -E "DELETED [0-9]+ rows from [a-z_]+" "$LOG" || true)
if [ -z "$d" ]; then
    echo "(none)"
else
    echo "$d" | sed -E 's/.* - WARNING - +//'
fi

# ---------------------------------------------------------------
# 9. Location consolidation split
# ---------------------------------------------------------------
section "9. LOCATION CONSOLIDATION SPLIT"
grep -E "Grid cells preserved as-is|Location consolidation: [0-9]+ original" "$LOG" | \
    sed -E 's/.* - INFO - +//' | tail -10

# ---------------------------------------------------------------
# 10. Mapping lookup failures (deduplicated)
# ---------------------------------------------------------------
section "10. MAPPING LOOKUP FAILURES (top 25 by count)"
grep -oE "No match found for [a-z_]+\.[a-z_]+ = '[^']*'" "$LOG" | \
    sed -E "s/No match found for ([a-z_]+)\.([a-z_]+) = '(.*)'/\1.\2 <- '\3'/" | \
    sort | uniq -c | sort -rn | head -25

# ---------------------------------------------------------------
# 11. Value transform failures (deduplicated)
# ---------------------------------------------------------------
section "11. VALUE TRANSFORM FAILURES (top 25 by count)"
grep -oE "(Cast to [a-z]+ failed for value '[^']*'|Date parse failed for '[^']*'|Unit conversion failed for value '[^']*'|Split index [0-9]+ out of range for value '[^']*'|Concat: column '[^']*' missing or null in row [0-9]+)" "$LOG" | \
    sed -E "s/[0-9]+/N/g" | \
    sort | uniq -c | sort -rn | head -25

# ---------------------------------------------------------------
# 12. Unique ERROR messages (deduplicated, numbers masked)
# ---------------------------------------------------------------
section "12. UNIQUE ERROR MESSAGES (top 30 by count)"
grep -E " - ERROR - | - CRITICAL - " "$LOG" | \
    sed -E 's/^[0-9-]+ [0-9:,]+ - [a-zA-Z_.]+ - (ERROR|CRITICAL) - //' | \
    sed -E 's/[0-9]+/N/g' | \
    sed -E 's/(0x[0-9a-fA-F]+)/HEX/g' | \
    sort | uniq -c | sort -rn | head -30

# ---------------------------------------------------------------
# 13. Unique WARNING messages (deduplicated, numbers masked)
# ---------------------------------------------------------------
section "13. UNIQUE WARNING MESSAGES (top 30 by count)"
grep -E " - WARNING - " "$LOG" | \
    sed -E 's/^[0-9-]+ [0-9:,]+ - [a-zA-Z_.]+ - WARNING - //' | \
    sed -E 's/[0-9]+/N/g' | \
    sort | uniq -c | sort -rn | head -30

# ---------------------------------------------------------------
# 14. Final consolidation result block
# ---------------------------------------------------------------
section "14. FINAL CONSOLIDATION RESULT"
grep -A12 "CONSOLIDATION COMPLETE" "$LOG" | tail -15

# ---------------------------------------------------------------
# 15. Per-table 'Loading dimension/fact' counts (from ingest)
# ---------------------------------------------------------------
section "15. INGEST-SIDE TABLE COUNTS (from 'Loading ... with N rows')"
grep -oE "Loading (dimension|fact) [a-z_]+ with [0-9]+ rows" "$LOG" | \
    sed -E 's/Loading (dimension|fact) ([a-z_]+) with ([0-9]+) rows/\2|\3/' | \
    awk -F'|' '{ s[$1]+=$2; n[$1]++ }
        END {
            printf "%-32s %12s %6s\n", "table", "sum", "batches"
            printf "%-32s %12s %6s\n", "-----", "---", "-------"
            for (t in s) printf "%-32s %12d %6d\n", t, s[t], n[t]
        }' | sort

echo ""
echo "=== END OF SUMMARY ==="