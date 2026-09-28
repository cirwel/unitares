#!/usr/bin/env bash
# Check skill freshness against the content of the sources each skill cites.
# Exit 0 if all fresh, exit 1 if any stale. Suitable as pre-commit hook.
# `--stamp NAME...` re-records a skill after its claims were re-checked. Set
# SKILL_ATTESTATION_VERIFIER to who re-checked them; without it, only a person
# at a terminal falls back to git user.name (if set), and any other stamp is
# refused. A shell an agent harness or CI runner marks as its own (CLAUDECODE,
# CODEX_THREAD_ID, CODEX_CI, AI_AGENT, CI; AGENT_ENV_MARKERS in
# _check_freshness.py) is not a person's, even with a terminal on stdin.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# In this repo skills/ lives at the repo root (scripts/client/../..), not at
# scripts/client/.. — that layout belongs to the plugin repo this script was
# adapted from. _check_freshness.py reads "<root>/skills".
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECTS_ROOT="${UNITARES_PROJECTS_ROOT:-$(cd "${REPO_ROOT}/.." && pwd)}"

exec python3 "${SCRIPT_DIR}/_check_freshness.py" "${REPO_ROOT}" "${PROJECTS_ROOT}" "$@"
