#!/usr/bin/env bash
#
# bundle-doc.sh - concatenate the Markdown documentation sources in ./docs
# into a single text file for review, sharing, or pasting into a chat.
#
# Usage:
#   ./bundle-doc.sh
#
# Writes: ./data_analysis_doc_bundle.txt
#
set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DOCS_DIR="$SCRIPT_DIR/docs"
OUTPUT="$SCRIPT_DIR/data_analysis_doc_bundle.txt"

[[ -d "$DOCS_DIR" ]] || { echo "Error: docs directory not found at $DOCS_DIR" >&2; exit 1; }

# Collect Markdown sources, sorted for deterministic output.
mapfile -t FILES < <(find "$DOCS_DIR" -type f -name '*.md' | sort)

{
  echo "==============================================================="
  echo "Documentation bundle - data-analysis"
  echo "Root:       $SCRIPT_DIR"
  echo "Generated:  $(date -Iseconds)"
  echo "Host:       $(hostname)"
  echo "Files:      ${#FILES[@]}"
  echo "==============================================================="

  for f in "${FILES[@]}"; do
    rel="${f#$SCRIPT_DIR/}"
    echo
    echo "# =============================================================="
    echo "# File: $rel"
    echo "# =============================================================="
    cat -- "$f"
    # Ensure a trailing newline before the next header.
    [[ -n "$(tail -c1 "$f")" ]] && echo
  done

  echo
  echo "==============================================================="
  echo "End of bundle"
  echo "==============================================================="
} > "$OUTPUT"

printf '\nBundle written to: %s\n' "$OUTPUT"
printf 'Included: %d files\n' "${#FILES[@]}"
printf 'Size:     %s\n' "$(du -h "$OUTPUT" | cut -f1)"