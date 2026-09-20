#!/usr/bin/env bash
#
# bundle.sh — concatenate all source files in this project into a single
# text file for review, sharing, or pasting into a chat.
#
# Usage:
#   ./bundle.sh                       # writes ./project_bundle.txt
#   ./bundle.sh -o out.txt
#   ./bundle.sh -r /path/to/project
#   ./bundle.sh -e env,log            # comma-separated extra extensions
#   ./bundle.sh -h
#
set -euo pipefail

# ---------- defaults ---------------------------------------------------------
SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
ROOT="$SCRIPT_DIR"
OUTPUT="project_bundle.txt"
EXTRA_EXTS=""

usage() {
  cat <<EOF
bundle.sh — concatenate project source files into one text file

Options:
  -o, --output FILE     Output file (default: project_bundle.txt)
  -r, --root DIR        Project root (default: script's own directory)
  -e, --extras LIST     Extra extensions to include, comma-separated
  -h, --help            Show this help
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output) OUTPUT="$2"; shift 2 ;;
    -r|--root)   ROOT="$2";   shift 2 ;;
    -e|--extras) EXTRA_EXTS="$2"; shift 2 ;;
    -h|--help)   usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -d "$ROOT" ]] || { echo "Error: root '$ROOT' not found." >&2; exit 1; }
[[ "$OUTPUT" = /* ]] || OUTPUT="$ROOT/$OUTPUT"

# ---------- inclusion rules --------------------------------------------------
EXTS=(
  py
  R Rmd qmd rmd
  sh bash zsh
  sql
  yaml yml toml ini cfg conf
  json
  md markdown
  csv tsv
  txt
)

NAMED=(
  DESCRIPTION
  NAMESPACE
  Makefile
  Dockerfile
  .gitignore
  .Rbuildignore
  .lintr
  .env.example
  LICENSE
  LICENSE.md
  README
  README.md
  renv.lock
)

if [[ -n "$EXTRA_EXTS" ]]; then
  IFS=',' read -ra _extras <<< "$EXTRA_EXTS"
  for e in "${_extras[@]}"; do
    e="${e// /}"
    [[ -n "$e" ]] && EXTS+=("$e")
  done
fi

# Directories to prune. Each entry is matched as a path component.
PRUNE_DIRS=(
  .git .svn .hg
  .Rproj.user
  renv
  __pycache__ .pytest_cache .mypy_cache .ruff_cache
  .venv venv env ENV
  node_modules
  .ipynb_checkpoints
  .idea .vscode
  build dist
  _targets _freeze .quarto
  html latex site
  logs
  .cache
  .conda
)

# Build a single extended regex: (^|/)foo(/|$)|(^|/)bar(/|$)|...
prune_re=""
for d in "${PRUNE_DIRS[@]}"; do
  prune_re+="(^|/)${d//./\\.}(/|\$)|"
done
prune_re="${prune_re%|}"

is_pruned() {
  local rel="$1"
  [[ "$rel" =~ $prune_re ]]
}

has_ext() {
  local ext="$1"
  for e in "${EXTS[@]}"; do
    [[ "$ext" == "$e" ]] && return 0
  done
  return 1
}

is_named() {
  local base="$1"
  for n in "${NAMED[@]}"; do
    [[ "$base" == "$n" ]] && return 0
  done
  return 1
}

# ---------- walk the tree ----------------------------------------------------
mapfile -t ALL_FILES < <(find "$ROOT" -type f | sort)

FILES=()
for f in "${ALL_FILES[@]}"; do
  rel="${f#$ROOT/}"
  base="$(basename "$f")"

  # Skip the output file and previous bundles.
  [[ "$f" == "$OUTPUT" ]] && continue
  case "$base" in
    project_bundle.txt|*_bundle.txt|*_bundle.md) continue ;;
  esac

  # Skip pruned directories.
  is_pruned "$rel" && continue

  # Include if extension is whitelisted OR basename is whitelisted.
  include=0
  if [[ "$base" == *.* && "$base" != .* ]]; then
    ext="${base##*.}"
    has_ext "$ext" && include=1
  fi
  [[ $include -eq 0 ]] && is_named "$base" && include=1
  [[ $include -eq 1 ]] || continue

  # Skip empty files.
  [[ -s "$f" ]] || continue

  # Skip large files (> 500 KB).
  size=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
  if (( size > 512000 )); then
    printf '  [skip large: %4d KB] %s\n' $((size/1024)) "$rel" >&2
    continue
  fi

  # Skip binary files. `file --mime-encoding -b` prints "binary" for binaries,
  # or something like "us-ascii" / "utf-8" for text.
  enc="$(file --mime-encoding -b "$f" 2>/dev/null || echo text)"
  if [[ "$enc" == "binary" ]]; then
    printf '  [skip binary] %s\n' "$rel" >&2
    continue
  fi

  FILES+=("$f")
done

# ---------- write the bundle -------------------------------------------------
{
  echo "==============================================================="
  echo "Project bundle"
  echo "Root:       $ROOT"
  echo "Generated:  $(date -Iseconds)"
  echo "Host:       $(hostname)"
  echo "Files:      ${#FILES[@]}"
  echo "==============================================================="

  for f in "${FILES[@]}"; do
    rel="${f#$ROOT/}"
    echo
    echo "# =============================================================="
    echo "# File: $rel"
    echo "# =============================================================="
    cat -- "$f"
    # Ensure trailing newline before the next header.
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
