#!/usr/bin/env bash
# Deploy dialectic_live (com.unitares.dialectic-live) — the Phoenix app on
# :8790 — from the shared master-pinned worktree.
#
# Why this exists: dialectic_live was running and serving, but appeared ZERO
# times in deploy-status.sh. It was invisible to the whole sweep — no verdict
# row, no dispatch entry, no lease-plane-style nudge — while loading from the
# SHARED deploy worktree that every other deploy fast-forwards. So each
# `deploy-apply.sh` run moved this app's source on disk underneath a BEAM that
# nobody restarted, and nothing anywhere reported the drift.
#
# Found 2026-08-13 while chasing the plug 1.20.1 retirement notice: the running
# node had been up 4 days across several worktree fast-forwards. It happened to
# be safe — zero commits had touched elixir/dialectic_live in that window — but
# that was luck, not a guarantee, and no tool would have said otherwise.
#
# Scoped to elixir/dialectic_live in the status table for the same reason
# sentinel and wave3a are: the shared tree accumulates hundreds of unrelated
# commits, so raw distance is not evidence THIS app is stale.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.dialectic-live"
PLIST="${UNITARES_DIALECTIC_LIVE_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_DIALECTIC_LIVE_PORT:-8790}"
UID_NUM="$(id -u)"
TAG="deploy"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY" \
  --allow-env UNITARES_DIALECTIC_LIVE_ALLOW_DEV \
  --recipe "[deploy]   re-render the plist against the deploy worktree
[deploy]   (sed __UNITARES_ROOT__ -> $DEPLOY ; see the template header)"

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"
PREV="$DEPLOY_LIB_PREV"
deploy_lib_nudge_lease_plane "$TAG" "deploy-dialectic-live.sh" "$DEPLOY"

# scripts/start.sh already runs deps.get + assets.deploy on every boot, so a
# failure there would surface only as a launchd crash-loop with KeepAlive
# restarting it forever. Doing both HERE turns that into a deploy-time error
# with the output in front of you, before anything is restarted.
echo "[deploy] compiling dialectic_live + assets (MIX_ENV=prod; surfaces errors before restart)"
(
  cd "$DEPLOY/elixir/dialectic_live"
  export MIX_ENV=prod
  mix deps.get --only prod
  mix compile
  mix assets.deploy
)

echo "[deploy] restarting $LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

# Probes /health — the route added alongside this script — NOT the LiveView
# root. `/` mounts the live view, which synchronously calls the governance
# backend; a slow or degraded governance server would then make THIS deploy's
# verify slow or failing for a reason that has nothing to do with whether the
# deploy worked. /health is deliberately decoupled from that.
#
# Deliberately not a process-liveness check either: KeepAlive keeps a
# crash-looping node "present" in launchctl, so a PID proves nothing here.
#
# -m 4 matches house style (deploy-mcp.sh -m4, deploy-status.sh -m 2). Without
# it a single stalled connect can eat far more than its ~3s share of the poll
# budget, silently turning a 45s verify window into one long hang.
check_dialectic_live_health() {
  curl -fsS -m 4 -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null
}

echo "[deploy] verifying /health on :$PORT"
if deploy_lib_poll 15 3 check_dialectic_live_health; then
  echo "[deploy] OK — dialectic-live healthy on :$PORT (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
else
  echo "[deploy] FAILED — /health on :$PORT did not respond within timeout." >&2
  echo "[deploy] Phoenix prod needs SECRET_KEY_BASE (DIALECTIC_LIVE_SECRET_KEY_BASE in secrets.env);" >&2
  echo "[deploy] a missing one makes start.sh exit 1 and launchd restart it forever." >&2
  # Rolling back matters MORE here than for the single-service scripts: this app
  # shares the unitares-deploy worktree with five other services, so leaving the
  # tree parked at a known-bad commit hands the next deploy a broken baseline.
  # Without this the app just crash-loops under KeepAlive with no recovery.
  echo "[deploy] Rolling the worktree back to ${PREV:0:8} and restarting." >&2
  git -C "$DEPLOY" reset --hard "$PREV"
  ( cd "$DEPLOY/elixir/dialectic_live" && MIX_ENV=prod mix compile && MIX_ENV=prod mix assets.deploy ) || \
    echo "[deploy] WARNING: rollback rebuild failed — the node may not come back cleanly." >&2
  launchctl kickstart -k "gui/$UID_NUM/$LABEL"
  echo "[deploy] rolled back. Investigate:" >&2
  echo "[deploy]   launchctl list | grep $LABEL" >&2
  echo "[deploy]   tail -80 \$HOME/Library/Logs/unitares-dialectic-live.log" >&2
  exit 1
fi
