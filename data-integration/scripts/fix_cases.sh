#!/usr/bin/env bash
# fix_cases.sh - launcher for scripts/fix_cases.py.
#
# This wrapper holds no arguments, no defaults and no expansion logic. It used to
# embed its own argparse block with its own help text and its own call to
# expand_burden_to_daily(), which meant the shell path and the Python path could
# disagree about defaults and about which flags existed. One CLI, one file.
#
# Usage:
#   ./scripts/fix_cases.sh --check
#   ./scripts/fix_cases.sh
#   ./scripts/fix_cases.sh --force
#   ./scripts/fix_cases.sh --population 54000000
#
# Exit status is the Python script's: 0 ok, 1 failure, 3 nothing to expand,
# 4 no population denominator, 5 record-level errors.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# Tolerate being moved to the repository root: look for the script beside this
# wrapper first, then in scripts/.
if [[ -f "${SCRIPT_DIR}/fix_cases.py" ]]; then
    PY_SCRIPT="${SCRIPT_DIR}/fix_cases.py"
elif [[ -f "${SCRIPT_DIR}/scripts/fix_cases.py" ]]; then
    PY_SCRIPT="${SCRIPT_DIR}/scripts/fix_cases.py"
else
    echo "fix_cases.sh: cannot find fix_cases.py beside ${SCRIPT_DIR}" >&2
    exit 1
fi

# The repository root is the parent of scripts/. fix_cases.py resolves its own
# imports, so the working directory only matters for locating .env — which
# database.session reads at import time.
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
cd "${REPO_ROOT}"

exec "${PYTHON:-python3}" "${PY_SCRIPT}" "$@"