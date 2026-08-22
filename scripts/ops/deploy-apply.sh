#!/usr/bin/env bash
# deploy-apply.sh — one command to "deploy what needs it": read deploy-status.sh
# verdicts and run each service's dedicated deploy script for everything that is
# STALE (running older code than its checkout) or BEHIND (checkout behind
# origin). The composable answer to "reboot the things that need a restart to
# pick up deploy changes."
#
# SAFE BY CONSTRUCTION: this never pulls or restarts a service itself. It only
# dispatches to per-service deploy scripts (deploy-mcp.sh / deploy-gateway.sh /
# deploy-lease-plane.sh / deploy-sentinel.sh / deploy-wave3a.sh /
# deploy-orchestrator.sh / deploy-bridge.sh / deploy-openai-gov-proxy.sh /
# deploy-gov-plugin.sh). Each of the process-backed ones deploys from a
# trunk-pinned worktree and REFUSES if its LaunchAgent still loads from a shared
# dev checkout. deploy-gov-plugin.sh is the exception and says so at length: its
# target has no worktree and no LaunchAgent, so it pulls the checkout in place
# behind its own refusal guards.
# Any service NOT in the script_for() table below is REPORTED, never touched —
# give it a deploy script to bring it into the sweep. Keep this list in sync
# with that table.
#
# Detection is delegated to deploy-status.sh (single source of truth for "what's
# live vs on disk"), so this stays a thin, safe orchestrator.
#
# ONE TREE, MANY SERVICES: seven services deploy from the SAME worktree
# ($HOME/projects/unitares-deploy). Each per-service script fast-forwards that
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
    # Deploys from its own worktree, not unitares-deploy, and self-skips the
    # restart when elixir/agent_orchestrator is unchanged — so sweeping it is
    # safe even though its shared-history commit distance is always large.
    agent-orchestrator) echo "$OPS_DIR/deploy-orchestrator.sh" ;;
    # Different repo (cirwel/unitares-discord-bridge) and different trunk
    # (`main`). It is the alert delivery path, so leaving it out of the sweep
    # meant the alarm itself could go stale unnoticed.
    discord-bridge)  echo "$OPS_DIR/deploy-bridge.sh" ;;
    dialectic-live)  echo "$OPS_DIR/deploy-dialectic-live.sh" ;;
    # Different repo (cirwel/unitares-host-adapter). Its LaunchAgent used to
    # import directly from the development checkout, so the deploy script
    # refuses until migrate-openai-gov-proxy.sh moves it to its own worktree.
    openai-gov-proxy) echo "$OPS_DIR/deploy-openai-gov-proxy.sh" ;;
    # The one entry with no worktree and no LaunchAgent: the plugin checkout IS
    # the deployed artifact, so its script pulls in place and there is nothing
    # to restart. It was reachable only by hand until 2026-08-17, which meant
    # `cirwel update` reported it BEHIND and then left it that way.
    gov-plugin)      echo "$OPS_DIR/deploy-gov-plugin.sh" ;;
    # Two components, ONE script: the claude and codex bots are the same code in
    # one worktree under two tokens. This loop calls the script once per BEHIND
    # component, so deploy-dispatch-beam.sh is idempotent per label — the second
    # invocation finds that instance already running the target SHA and skips
    # the restart rather than bouncing both bots twice.
    dispatch-beam|dispatch-beam-codex)
                     echo "$OPS_DIR/deploy-dispatch-beam.sh" ;;
    *)               echo "" ;;
  esac
}

status_args="--json"
[ "$FETCH" = 1 ] && status_args="$status_args --fetch"

# ${FETCH:+...} expands whenever FETCH is SET, and FETCH=0 is set — so this
# line claimed "fetch verdicts" even under --no-fetch. Test the value.
echo "[apply] reading deploy-status.sh ($([ "$FETCH" = 1 ] && echo fetched || echo cached) verdicts) ..."
# shellcheck disable=SC2086
status_json="$("$STATUS" $status_args)" || { echo "[apply] deploy-status.sh failed" >&2; exit 1; }

# Emit one TAB-separated "name<TAB>verdict<TAB>checkout<TAB>pickup" line per STALE/BEHIND
# service. deploy-status.sh --json is valid JSON; parse it with python3
# (tolerant of the verdict's optional " [DEV]" suffix). `checkout` is the git
# worktree the service's deploy script fast-forwards -- the field the shared-tree
# hold below is keyed on.
needs="$(
  printf '%s' "$status_json" | python3 -c '
import json, sys
for svc in json.load(sys.stdin):
    v = svc.get("verdict", "")
    if v.startswith("STALE") or v.startswith("BEHIND"):
        print("%s\t%s\t%s\t%s" % (
            svc.get("name", ""), v, svc.get("checkout", ""),
            svc.get("pickup", "unknown"),
        ))
'
)" || { echo "[apply] could not parse deploy-status --json" >&2; exit 1; }

if [ -z "$needs" ]; then
  echo "[apply] nothing to deploy — no service is STALE or BEHIND."
  exit 0
fi

deployed=""; skipped=""; failed=""; held=""
# Checkouts a failed deploy has pinned. Newline-delimited and matched whole-line:
# a substring test would let /a/b hold /a/b-two, which is a different tree.
held_trees=""
tree_is_held() {
  [ -n "$1" ] || return 1
  printf '%s\n' "$held_trees" | grep -qxF "$1"
}

while IFS=$'\t' read -r name verdict checkout pickup; do
  [ -z "$name" ] && continue
  script="$(deploy_script_for "$name")"

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
[ -n "$held" ] && echo "  (held = not attempted: shares a checkout with a failed deploy. Fix that failure, then re-run.)"
[ -n "$skipped" ] && echo "  (skipped = drift remains; no deploy automation ran for those services.)"

# Non-zero if anything failed, so callers/CI can gate on it.
[ -z "$failed" ]
