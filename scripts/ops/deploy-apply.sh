#!/usr/bin/env bash
# deploy-apply.sh — one command to "deploy what needs it": read deploy-status.sh
# verdicts and run each service's dedicated deploy script for everything that is
# STALE (running older code than its checkout) or BEHIND (checkout behind
# origin). The composable answer to "reboot the things that need a restart to
# pick up deploy changes."
#
# It never pulls or restarts a service itself. It invokes only the deploy
# script explicitly declared by the validated inventory. Each script owns its
# process, migration, revision and rollback checks. Missing automation is
# reported as incomplete, never guessed from a service name.
#
# Detection is delegated to deploy-status.sh. The inventory must come from
# reviewed operator configuration; no fleet is selected by default.
#
# ONE TREE, MANY SERVICES: services may deploy from the SAME worktree
# (as declared by the operator inventory). Each per-service script fast-forwards that
# shared tree before restarting its own process. So when one of them REFUSES and
# rolls the tree back -- deploy-mcp.sh does exactly this on a migration gap, to
# keep disk from sitting ahead of a running process -- the next service in this
# loop immediately fast-forwards the same tree again and the rollback is undone.
# Measured 2026-08-20 in the deploy tree's reflog: rollback at 02:38:03, undone
# by the following deploy in the SAME second, and the same pair at 02:09:24/25.
# The guarantee deploy-mcp.sh prints is therefore not held once the sweep moves
# on. So: a failure HOLDS that service's checkout for the rest of the run, and
# any later service sharing it is reported, not deployed. This stays true to
# "never touches a tree itself" -- holding is a decision NOT to dispatch.
#
# Flags:
#   --dry-run   show what would be deployed; run nothing
#   --no-fetch  use cached remotes (default refreshes them for accurate verdicts)
#   --request-service=SERVICE  also deploy this CURRENT service (repeatable)
# Requests use the same safety checks as drift-driven deployments. They do not
# restart DOWN services or bypass a held checkout or reviewed-revision pin.
# Exit 1: deployment/inspection failed. Exit 3: incomplete (skipped or DOWN).
set -uo pipefail

OPS_DIR="$(cd "$(dirname "$0")" && pwd)"
STATUS="$OPS_DIR/deploy-status.sh"

# Read-only handshake for wrappers: older tooling must not silently ignore a
# caller's reviewed-revision pin. Kept in this script so no status/deploy runs.
if [[ "$#" = 1 && "$1" = --capabilities ]]; then
  echo 'expected-revision-v1 incomplete-exit-v1 requested-service-v1 inventory-v1'
  exit 0
fi

DRY_RUN=0
FETCH=1
requested_services=""
for a in "$@"; do
  case "$a" in
    --dry-run)  DRY_RUN=1 ;;
    --no-fetch) FETCH=0 ;;
    --request-service=*)
      service="${a#--request-service=}"
      if [[ ! "$service" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
        echo "[apply] invalid requested service: $service" >&2
        exit 1
      fi
      requested_services="$(printf '%s\n%s' "$requested_services" "$service")"
      ;;
    -h|--help)
      sed -n '2,21p' "$0"
      sed -n '/^# Flags:/,/^# Exit /p' "$0"
      exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# Dispatch metadata travels with the status snapshot. Inventory validation
# happens before probes and no service-name dispatch table is built in.
deploy_script_for() {
  printf '%s' "$status_json" | python3 -c '
import json, sys
for row in json.load(sys.stdin):
    if row.get("name") == sys.argv[1]:
        print(row.get("deploy_script", ""))
        break
' "$1"
}

status_args="--json"
[ "$FETCH" = 1 ] && status_args="$status_args --fetch"

# ${FETCH:+...} expands whenever FETCH is SET, and FETCH=0 is set — so this
# line claimed "fetch verdicts" even under --no-fetch. Test the value.
echo "[apply] reading deploy-status.sh ($([ "$FETCH" = 1 ] && echo fetched || echo cached) verdicts) ..."
# shellcheck disable=SC2086
status_json="$("$STATUS" $status_args)" || { echo "[apply] deploy-status.sh failed" >&2; exit 1; }

# Group by physical targets, even if another status producer supplies aliases.
# Resolve the entire snapshot before any dispatch: both checkout holds and
# DOWN sibling protection must use the same stable identities throughout a run.
status_json="$(printf '%s' "$status_json" | python3 -c '
import json, os, subprocess, sys
services = json.load(sys.stdin)
for svc in services:
    for key in ("checkout", "deploy_script"):
        path = svc.get(key, "")
        if not path:
            continue
        if not isinstance(path, str) or not os.path.isabs(path) or any(c in path for c in "|\t\r\n\0"):
            raise SystemExit("[apply] invalid status path: " + key)
        path = os.path.realpath(path)
        if key == "checkout" and os.path.isdir(path):
            probe = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
            if probe.returncode == 0:
                path = os.path.realpath(probe.stdout.rstrip("\n"))
        if any(c in path for c in "|\t\r\n\0"):
            raise SystemExit("[apply] invalid resolved status path: " + key)
        svc[key] = path
print(json.dumps(services))
')" || { echo "[apply] could not resolve deployment identities" >&2; exit 1; }

# Emit one TAB-separated "name<TAB>verdict<TAB>checkout<TAB>pickup" line per
# STALE/BEHIND/DOWN or requested service, in status-table order. A request only
# selects an existing row, so repeated flags never dispatch a service twice.
# Validate every request before dispatching anything. deploy-status.sh --json
# is valid JSON; parse it with python3
# (tolerant of the verdict's optional " [DEV]" suffix). `checkout` is the git
# worktree the service's deploy script fast-forwards -- the field the shared-tree
# hold below is keyed on.
needs="$(
  printf '%s' "$status_json" | python3 -c '
import json, sys
services = json.load(sys.stdin)
requested = set(sys.argv[1].splitlines()) - {""}
for name in sorted(requested):
    matches = sum(svc.get("name") == name for svc in services)
    if matches != 1:
        reason = "unknown" if matches == 0 else "ambiguous"
        print("[apply] %s requested service: %s" % (reason, name), file=sys.stderr)
        sys.exit(1)
for svc in services:
    v = svc.get("verdict", "")
    if (v.startswith("STALE") or v.startswith("BEHIND") or v.startswith("DOWN")
            or svc.get("name") in requested):
        print("%s\t%s\t%s\t%s" % (
            svc.get("name", ""), v, svc.get("checkout", ""),
            svc.get("pickup", "unknown"),
        ))
' "$requested_services"
)" || { echo "[apply] could not validate deploy-status --json and service requests" >&2; exit 1; }

if [ -z "$needs" ]; then
  echo "[apply] nothing to deploy — no service is STALE, BEHIND or DOWN."
  exit 0
fi

# Record every DOWN service before dispatch: an earlier service can indirectly
# restart one later in the table (shared lease-plane nudges, paired bots).
UNITARES_DEPLOY_DOWN_SERVICES=""
down_scripts=""
while IFS=$'\t' read -r name verdict checkout pickup; do
  case "$verdict" in
    DOWN*)
      UNITARES_DEPLOY_DOWN_SERVICES="$(printf '%s\n%s' "$UNITARES_DEPLOY_DOWN_SERVICES" "$name")"
      script="$(deploy_script_for "$name")"
      [ -z "$script" ] || down_scripts="$(printf '%s\n%s' "$down_scripts" "$script")"
      ;;
  esac
done <<EOF
$needs
EOF
export UNITARES_DEPLOY_DOWN_SERVICES

deployed=""; skipped=""; failed=""; held=""; down=""
# Checkouts a failed deploy has pinned. Newline-delimited and matched whole-line:
# a substring test would let /a/b hold /a/b-two, which is a different tree.
held_trees=""
tree_is_held() {
  [ -n "$1" ] || return 1
  printf '%s\n' "$held_trees" | grep -qxF "$1"
}

while IFS=$'\t' read -r name verdict checkout pickup; do
  [ -z "$name" ] && continue
  case "$verdict" in
    DOWN*)
      echo "[apply] DOWN  $name — not restarted automatically; inspect why the service stopped" >&2
      down="$down $name"
      continue
      ;;
    CURRENT*|STALE*|BEHIND*) ;;
    *)
      echo "[apply] SKIP  $name ($verdict) — requested service is not confirmed CURRENT, STALE or BEHIND" >&2
      skipped="$skipped $name"
      continue
      ;;
  esac
  script="$(deploy_script_for "$name")"

  if [ -n "$script" ] && printf '%s\n' "$down_scripts" | grep -qxF "$script"; then
    echo "[apply] SKIP  $name ($verdict) — its deploy script also controls a DOWN service" >&2
    skipped="$skipped $name"
    continue
  fi

  # Ordering is not a fix here. Whichever service comes second inherits the
  # problem, so the hold is on the TREE, not on a position in the list.
  if tree_is_held "$checkout"; then
    echo "[apply] HOLD  $name ($verdict) — an earlier deploy from $checkout refused and rolled it back; not advancing that tree again this run" >&2
    held="$held $name"
    continue
  fi

  if [ -z "$script" ]; then
    echo "[apply] SKIP  $name ($verdict) — no deploy automation registered (pickup=$pickup; checkout=${checkout:-unknown})" >&2
    echo "[apply]       live process and checkout were left unchanged" >&2
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
    # A refusal may have left the tree deliberately rolled back. Whether it did
    # is not observable from here, and guessing the wrong way re-creates the
    # exact defect -- so hold unconditionally. The cost of a needless hold is a
    # deploy deferred to the next run; the cost of a missed one is a live
    # process running against a schema that is not there.
    if [ -n "$checkout" ]; then
      held_trees="$(printf '%s\n%s' "$held_trees" "$checkout")"
    fi
  fi
done <<EOF
$needs
EOF

echo
echo "[apply] summary:"
echo "  deployed:${deployed:-  none}"
echo "  skipped: ${skipped:-  none}"
echo "  held:    ${held:-  none}"
echo "  failed:  ${failed:-  none}"
echo "  down:    ${down:-  none}"
[ -n "$held" ] && echo "  (held = not attempted: shares a checkout with a failed deploy. Fix that failure, then re-run.)"
if [ -n "$skipped" ]; then
  if [ -n "$requested_services" ]; then
    echo "  (skipped = drift or requested work remains; no deploy automation ran for those services.)"
  else
    echo "  (skipped = drift remains; no deploy automation ran for those services.)"
  fi
fi

# Preserve an actual deploy failure; otherwise distinguish an incomplete
# update from success, including dry runs that cannot cover every service.
[ -z "$failed" ] || exit 1
[ -z "$skipped$held$down" ] || exit 3
exit 0
