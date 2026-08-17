#!/usr/bin/env bash
# Compatibility entrypoint for the resident merge conductor.
#
# Keep this filename so the installed LaunchAgent upgrades in place. GitHub's
# repository-level native updater already refreshes armed PRs; the historical
# polling updater is therefore retired when this entrypoint lands. Existing
# plists have no conductor flags and safely select report-only mode.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

repo="${UNITARES_MERGE_REPO:-${PR_BABYSITTER_REPO:-cirwel/unitares}}"
args=(--repo "$repo")

case "${UNITARES_MERGE_CONDUCTOR_EXECUTE:-0}" in
    1|true|TRUE|yes|YES|on|ON) args+=(--execute) ;;
esac
case "${UNITARES_MERGE_CONDUCTOR_REVIEW:-0}" in
    1|true|TRUE|yes|YES|on|ON) args+=(--review) ;;
esac

exec python3 "$PROJECT_ROOT/scripts/ops/merge_conductor.py" "${args[@]}"
