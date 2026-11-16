#!/usr/bin/env bash
#
# bundle.sh - Concatenate all Python files under data-integration/ into one
# text file for review, sharing, or pasting into a chat.
#
# Scoped to the directory containing this script, so it works no matter where
# it is invoked from. Honours .gitignore by using `git ls-files` when this
# directory lives inside a Git repository; otherwise falls back to `find`.
#
# Usage:
#   ./bundle.sh                       # writes ./python_bundle.txt
#   ./bundle.sh -o out.txt
#   ./bundle.sh -h
#
set -euo pipefail

# ---------- defaults ---------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
ROOT="$SCRIPT_DIR"
OUTPUT="data_integration_bundle.txt"

usage() {
  cat <<EOF
bundle.sh - concatenate all Python files in data-integration/ into one file

Options:
  -o, --output FILE     Output file (default: python_bundle.txt)
  -h, --help            Show this help
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output) OUTPUT="$2"; shift 2 ;;
    -h|--help)   usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ "$OUTPUT" = /* ]] || OUTPUT="$ROOT/$OUTPUT"

# ---------- directories to prune ---------------------------------------------
# Each entry is matched as a path component relative to ROOT.
PRUNE_DIRS=(
  .git .svn .hg
  .idea .vscode
  .pytest_cache .mypy_cache .ruff_cache
  __pycache__
  .venv venv env ENV
  node_modules
  .ipynb_checkpoints
  build dist
  docs/_build
  outputs
  logs
  .cache
  .conda
  clip_timeseries_data
  data/raw data/processed data/interim data/external data/tmp
)

prune_re=""
for d in "${PRUNE_DIRS[@]}"; do
  prune_re+="(^|/)${d//./\\.}(/|\$)|"
done
prune_re="${prune_re%|}"

is_pruned() {
  local rel="$1"
  [[ "$rel" =~ $prune_re ]]
}

# ---------- collect the file list --------------------------------------------
# Run git from inside $ROOT so paths are scoped to data-integration/.
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  mapfile -t ALL_FILES < <(
    cd "$ROOT" && git ls-files --cached --others --exclude-standard | sort
  )
else
  echo "Not inside a Git repository - falling back to find." >&2
  mapfile -t ALL_FILES < <(
    cd "$ROOT" && find . -type f -name '*.py' | sed 's|^\./||' | sort
  )
fi

FILES=()
for rel in "${ALL_FILES[@]}"; do
  f="$ROOT/$rel"

  # Only Python files.
  [[ "$rel" == *.py ]] || continue

  # Skip the output file (defensive).
  [[ "$f" == "$OUTPUT" ]] && continue

  # Skip pruned directories.
  is_pruned "$rel" && continue

  # Skip empty files.
  [[ -s "$f" ]] || continue

  # Skip large files (> 500 KB).
  size=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
  if (( size > 512000 )); then
    printf '  [skip large: %4d KB] %s\n' $((size/1024)) "$rel" >&2
    continue
  fi

  # Skip binary files.
  enc="$(file --mime-encoding -b "$f" 2>/dev/null || echo text)"
  if [[ "$enc" == "binary" ]]; then
    printf '  [skip binary] %s\n' "$rel" >&2
    continue
  fi

  FILES+=("$rel")
done

# ---------- write the bundle -------------------------------------------------
{
  echo "==============================================================="
  echo "Python integration bundle"
  echo "Root:       $ROOT"
  echo "Generated:  $(date -Iseconds)"
  echo "Host:       $(hostname)"
  echo "Files:      ${#FILES[@]}"
  echo "==============================================================="

  for rel in "${FILES[@]}"; do
    echo
    echo "# =============================================================="
    echo "# File: $rel"
    echo "# =============================================================="
    cat -- "$ROOT/$rel"
    [[ -n "$(tail -c1 "$ROOT/$rel")" ]] && echo
  done

  echo
  echo "==============================================================="
  echo "End of bundle"
  echo "==============================================================="
} > "$OUTPUT"

printf '\nBundle written to: %s\n' "$OUTPUT"
printf 'Included: %d Python files\n' "${#FILES[@]}"
printf 'Size:     %s\n' "$(du -h "$OUTPUT" | cut -f1)"