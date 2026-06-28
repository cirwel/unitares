#!/usr/bin/env bash
# deploy-apply.sh — one command to "deploy what needs it": read deploy-status.sh
# verdicts and run each service's dedicated deploy script for everything that is
# STALE (running older code than its checkout) or BEHIND (checkout behind
# origin). The composable answer to "reboot the things that need a restart to
# pick up deploy changes."
#
# SAFE BY CONSTRUCTION: this never pulls or restarts a service itself. It only
# dispatches to per-service deploy scripts (deploy-mcp.sh / deploy-lease-plane.sh
# / deploy-sentinel.sh), each of which deploys from a master-pinned worktree and
# REFUSES if its LaunchAgent still loads from the shared dev checkout. Services
# still on restart-DEV with no deploy script (e.g. gateway-mcp, wave3a-handlers)
# are REPORTED, never touched — give them a deploy worktree + a deploy script to
# bring them into the sweep.
#
# Detection is delegated to deploy-status.sh (single source of truth for "what's
# live vs on disk"), so this stays a thin, safe orchestrator.
#
# Flags:
#   --dry-run   show what would be deployed; run nothing
#   --no-fetch  use cached remotes (default refreshes them for accurate verdicts)
set -uo pipefail

OPS_DIR="$(cd "$(dirname "$0")" && pwd)"
STATUS="$OPS_DIR/deploy-status.sh"

DRY_RUN=0
FETCH=1
for a in "$@"; do
  case "$a" in
    --dry-run)  DRY_RUN=1 ;;
    --no-fetch) FETCH=0 ;;
    -h|--help)  sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# name -> deploy script. Extend this as services move off restart-DEV onto
# dedicated deploy worktrees with their own deploy-*.sh. (case, not an
# associative array, so this runs on macOS's stock bash 3.2 like the siblings.)
deploy_script_for() {
  case "$1" in
    governance-mcp)  echo "$OPS_DIR/deploy-mcp.sh" ;;
    gateway-mcp)     echo "$OPS_DIR/deploy-gateway.sh" ;;
    lease-plane)     echo "$OPS_DIR/deploy-lease-plane.sh" ;;
    sentinel-beam)   echo "$OPS_DIR/deploy-sentinel.sh" ;;
    wave3a-handlers) echo "$OPS_DIR/deploy-wave3a.sh" ;;
    *)               echo "" ;;
  esac
}

status_args="--json"
[ "$FETCH" = 1 ] && status_args="$status_args --fetch"

echo "[apply] reading deploy-status.sh (${FETCH:+fetch }verdicts) ..."
# shellcheck disable=SC2086
status_json="$("$STATUS" $status_args)" || { echo "[apply] deploy-status.sh failed" >&2; exit 1; }

# Emit one TAB-separated "name<TAB>verdict" line per STALE/BEHIND service.
# deploy-status.sh --json is valid JSON; parse it with python3 (tolerant of the
# verdict's optional " [DEV]" suffix).
needs="$(
  printf '%s' "$status_json" | python3 -c '
import json, sys
for svc in json.load(sys.stdin):
    v = svc.get("verdict", "")
    if v.startswith("STALE") or v.startswith("BEHIND"):
        print("%s\t%s" % (svc.get("name", ""), v))
'
)" || { echo "[apply] could not parse deploy-status --json" >&2; exit 1; }

if [ -z "$needs" ]; then
  echo "[apply] nothing to deploy — no service is STALE or BEHIND."
  exit 0
fi

deployed=""; skipped=""; failed=""
while IFS=$'\t' read -r name verdict; do
  [ -z "$name" ] && continue
  script="$(deploy_script_for "$name")"

  if [ -z "$script" ]; then
    echo "[apply] SKIP  $name ($verdict) — no deploy script (still restart-DEV?); migrate it to a worktree first" >&2
    skipped="$skipped $name"
    continue
  fi
  if [ ! -x "$script" ]; then
    echo "[apply] SKIP  $name ($verdict) — $(basename "$script") missing or not executable" >&2
    skipped="$skipped $name"
    continue
  fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "[apply] DRY   would deploy $name ($verdict) via $(basename "$script")"
    deployed="$deployed $name(dry)"
    continue
  fi

  echo "[apply] ===> deploying $name ($verdict) via $(basename "$script")"
  if "$script"; then
    deployed="$deployed $name"
  else
    echo "[apply] FAILED $name — see output above" >&2
    failed="$failed $name"
  fi
done <<EOF
$needs
EOF

echo
echo "[apply] summary:"
echo "  deployed:${deployed:-  none}"
echo "  skipped: ${skipped:-  none}"
echo "  failed:  ${failed:-  none}"

# Non-zero if anything failed, so callers/CI can gate on it.
[ -z "$failed" ]
