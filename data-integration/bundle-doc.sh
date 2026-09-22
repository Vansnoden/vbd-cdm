#!/usr/bin/env bash
#
# bundle-doc.sh — concatenate the Sphinx documentation sources in ./docs
# into a single text file for review, sharing, or pasting into a chat.
#
# Usage:
#   ./bundle-doc.sh
#
# Writes: ./data_integration_doc_bundle.txt
#
# Includes:
#   - Markdown sources  (*.md)
#   - reStructuredText sources (*.rst)
#   - Sphinx configuration (conf.py)
#
# Excludes:
#   - _build/    (rendered output)
#   - generated/ (autosummary stubs; regenerated on every build)
#   - _static/   (binary assets)
#   - _templates/
#
set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DOCS_DIR="$SCRIPT_DIR/docs"
OUTPUT="$SCRIPT_DIR/data_integration_doc_bundle.txt"

[[ -d "$DOCS_DIR" ]] || { echo "Error: docs directory not found at $DOCS_DIR" >&2; exit 1; }

# Collect doc sources, pruning build artefacts and asset directories.
mapfile -t FILES < <(
  find "$DOCS_DIR" \
    -type d \( -name '_build' -o -name 'generated' -o -name '_static' -o -name '_templates' \) -prune -o \
    -type f \( -name '*.md' -o -name '*.rst' -o -name 'conf.py' \) -print \
  | sort
)

{
  echo "==============================================================="
  echo "Documentation bundle — data-integration"
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