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

REPO="${UNITARES_REPO:?set the source repository path}"
DEPLOY="${UNITARES_DEPLOY:?set the dedicated deployment worktree path}"
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

deploy_lib_require_loaded_secrets_env "$TAG" "$PLIST" "gui/$UID_NUM/$LABEL"

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"
PREV="$DEPLOY_LIB_PREV"
deploy_lib_nudge_lease_plane "$TAG" "deploy-dialectic-live.sh" "$DEPLOY"

# Roll the shared worktree back and restart the previous build. The app shares
# the deploy worktree with five other services, so leaving the tree parked at
# a known-bad commit hands the next deploy a broken baseline.
roll_back() {
  echo "[deploy] FAILED — $1" >&2
  echo "[deploy] Rolling the worktree back to ${PREV:0:8} and restarting." >&2
  git -C "$DEPLOY" reset --hard "$PREV"
  # PREV may predate build-assets.sh; fall back to the plain alias there.
  ( cd "$DEPLOY/elixir/dialectic_live" && export MIX_ENV=prod && mix compile \
      && if [ -x scripts/build-assets.sh ]; then scripts/build-assets.sh --strict; else mix assets.deploy; fi ) || \
    echo "[deploy] WARNING: rollback rebuild failed — the node may not come back cleanly." >&2
  deploy_lib_require_loaded_secrets_env "$TAG" "$PLIST" "gui/$UID_NUM/$LABEL"
  deploy_lib_restart_service "$TAG" "gui/$UID_NUM" "$LABEL" "$PLIST" || \
    echo "[deploy] WARNING: the rollback restart failed too." >&2
  echo "[deploy] rolled back. Investigate:" >&2
  echo "[deploy]   launchctl list | grep $LABEL" >&2
  echo "[deploy]   tail -80 \$HOME/Library/Logs/unitares-dialectic-live.log" >&2
  exit 1
}

# scripts/start.sh runs deps.get on every boot, and builds assets when the
# build on disk is not this checkout's. A failure there surfaces only in the
# service log. Doing both HERE turns that into a deploy-time error with the
# output in front of you, before anything is restarted, and writes the stamp
# that lets the restart below skip the asset build. It runs in a conditional,
# so a failure rolls the tree back instead of `set -e` exiting with the shared
# worktree at the failed revision; a condition switches `set -e` off, even in
# the subshell, so the steps are chained with &&.
echo "[deploy] compiling dialectic_live + assets (MIX_ENV=prod; surfaces errors before restart)"
if ! (
  cd "$DEPLOY/elixir/dialectic_live" &&
    export MIX_ENV=prod &&
    mix deps.get --only prod &&
    mix compile &&
    scripts/build-assets.sh --strict
); then
  roll_back "the dependency fetch, compile or asset build failed (see above)."
fi

# deploy_lib_restart_service reloads the job (bootout + bootstrap) when its
# plist changed since the last deploy-driven restart: a bare kickstart reuses
# launchd's cached definition, so changed EnvironmentVariables never loaded.
echo "[deploy] restarting $LABEL"
deploy_lib_require_loaded_secrets_env "$TAG" "$PLIST" "gui/$UID_NUM/$LABEL"
if ! deploy_lib_restart_service "$TAG" "gui/$UID_NUM" "$LABEL" "$PLIST"; then
  roll_back "the restart did not complete (see above)."
fi

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

# The LiveView socket is what makes the Timeline pane live, and /health says
# nothing about it: prod pins the socket to the endpoint host (`localhost`), so
# an upgrade with Origin http://127.0.0.1:$PORT is refused with 403 and the
# page silently never goes live (portal-live-timeline-v0 §3.1). Check the
# documented trial origin gets 101. On a 101 curl holds the socket open until
# -m expires, so read the status code and ignore curl's exit code.
check_dialectic_live_socket() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    -H "Origin: http://localhost:$PORT" \
    "http://127.0.0.1:$PORT/live/websocket?vsn=2.0.0" 2>/dev/null || true)"
  [ "$code" = "101" ]
}

echo "[deploy] verifying /health on :$PORT"
if deploy_lib_poll 15 3 check_dialectic_live_health; then
  echo "[deploy] OK — dialectic-live healthy on :$PORT (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
  # A warning, not a rollback: the node is serving, and a socket refusal is a
  # config fault (check_origin, PHX_HOST) a code rollback would not fix.
  if check_dialectic_live_socket; then
    echo "[deploy] OK — LiveView socket upgrades for Origin http://localhost:$PORT"
  else
    echo "[deploy] WARNING — /live/websocket did not return 101 for Origin http://localhost:$PORT;" >&2
    echo "[deploy] the Timeline pane at http://localhost:$PORT/timeline will not go live. Check check_origin / PHX_HOST." >&2
  fi
else
  roll_back "/health on :$PORT did not respond within timeout. Phoenix prod needs SECRET_KEY_BASE (DIALECTIC_LIVE_SECRET_KEY_BASE in the selected file); without it start.sh stops (exit 0)."
fi
