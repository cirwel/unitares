#!/usr/bin/env bash
# Check skill freshness against source file modification times.
# Exit 0 if all fresh, exit 1 if any stale. Suitable as pre-commit hook.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# In this repo skills/ lives at the repo root (scripts/client/../..), not at
# scripts/client/.. — that layout belongs to the plugin repo this script was
# adapted from. _check_freshness.py reads "<root>/skills".
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECTS_ROOT="${UNITARES_PROJECTS_ROOT:-$(cd "${REPO_ROOT}/.." && pwd)}"

exec python3 "${SCRIPT_DIR}/_check_freshness.py" "${REPO_ROOT}" "${PROJECTS_ROOT}"
