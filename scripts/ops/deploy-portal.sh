#!/usr/bin/env bash
# Deploy the operator hub (com.unitares.portal), the Phoenix app on
# 127.0.0.1:8791 (elixir/portal; portal-hub-v0 H2d-3), from the shared
# master-pinned worktree. Modelled on deploy-dialectic-live.sh: compile and
# build the stylesheet before restarting, verify /health and the LiveView
# socket, and roll the shared tree back if the hub does not come up.
#
# The hub answers only for its one Host and port and its one Origin
# (Portal.Boundary), so every check below uses exactly 127.0.0.1:$PORT.
set -euo pipefail

REPO="${UNITARES_REPO:?set the source repository path}"
DEPLOY="${UNITARES_DEPLOY:?set the dedicated deployment worktree path}"
LABEL="com.unitares.portal"
PLIST="${UNITARES_PORTAL_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_PORTAL_PORT:-8791}"
UID_NUM="$(id -u)"
TAG="deploy"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY" \
  --allow-env UNITARES_PORTAL_ALLOW_DEV \
  --recipe "[deploy]   re-render the plist against the deploy worktree
[deploy]   (sed __UNITARES_ROOT__ -> $DEPLOY ; see the template header)"

deploy_lib_require_loaded_secrets_env "$TAG" "$PLIST" "gui/$UID_NUM/$LABEL"

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"
PREV="$DEPLOY_LIB_PREV"
deploy_lib_nudge_lease_plane "$TAG" "deploy-portal.sh" "$DEPLOY"

# scripts/start.sh runs deps.get and the stylesheet build on every boot. Doing
# both HERE turns a failure into a deploy-time error in front of you, before
# anything restarts, and writes the stamp that lets the restart skip the build.
echo "[deploy] compiling the hub + stylesheet (MIX_ENV=prod; surfaces errors before restart)"
(
  cd "$DEPLOY/elixir/portal"
  export MIX_ENV=prod
  mix deps.get --only prod
  mix compile
  scripts/build-assets.sh --strict
)

echo "[deploy] restarting $LABEL"
deploy_lib_require_loaded_secrets_env "$TAG" "$PLIST" "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

# /health answers {"ok": true} on the hub's own Host and needs no sign-in.
# KeepAlive keeps a crash-looping node "present" in launchctl, so a PID proves
# nothing; -m 4 keeps one stalled connect from eating the poll budget.
check_portal_health() {
  curl -fsS -m 4 -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null
}

# The LiveView socket must accept the hub's own Origin and refuse any other.
# The upgrade answers 101 for the right Origin (a join without a session is
# refused later, inside the socket) and 403 for a foreign one. On a 101 curl
# holds the socket open until -m expires, so read only the status code.
socket_status() {
  curl -s -o /dev/null -w '%{http_code}' -m 2 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    -H "Origin: $1" \
    "http://127.0.0.1:$PORT/live/websocket?vsn=2.0.0" 2>/dev/null || true
}

echo "[deploy] verifying /health on :$PORT"
if deploy_lib_poll 15 3 check_portal_health; then
  echo "[deploy] OK — the hub is healthy on 127.0.0.1:$PORT (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
  # Warnings, not a rollback: the node is serving, and a socket fault is a
  # configuration problem (PORTAL_HOST, PORTAL_PORT) a rollback would not fix.
  own="$(socket_status "http://127.0.0.1:$PORT")"
  foreign="$(socket_status "http://evil.example")"
  if [ "$own" = "101" ] && [ "$foreign" = "403" ]; then
    echo "[deploy] OK — the LiveView socket accepts only the hub's Origin (101 own, 403 foreign)"
  else
    echo "[deploy] WARNING — the LiveView socket answered $own for the hub's Origin and $foreign for a foreign one" >&2
    echo "[deploy] (expected 101 and 403). Check PORTAL_HOST / PORTAL_PORT." >&2
  fi
else
  echo "[deploy] FAILED — /health on :$PORT did not respond within timeout." >&2
  echo "[deploy] The prod boot needs PORTAL_SECRET_KEY_BASE in the selected secrets file;" >&2
  echo "[deploy] without it start.sh exits 1 and launchd restarts it forever." >&2
  # The hub shares the deploy worktree with the other services, so leaving the
  # tree at a known-bad commit hands the next deploy a broken baseline.
  echo "[deploy] Rolling the worktree back to ${PREV:0:8} and restarting." >&2
  git -C "$DEPLOY" reset --hard "$PREV"
  # PREV may predate the hub; then there is nothing of it to rebuild.
  if [ -x "$DEPLOY/elixir/portal/scripts/build-assets.sh" ]; then
    ( cd "$DEPLOY/elixir/portal" && export MIX_ENV=prod && mix compile && scripts/build-assets.sh --strict ) || \
      echo "[deploy] WARNING: rollback rebuild failed — the node may not come back cleanly." >&2
  fi
  deploy_lib_require_loaded_secrets_env "$TAG" "$PLIST" "gui/$UID_NUM/$LABEL"
  launchctl kickstart -k "gui/$UID_NUM/$LABEL"
  echo "[deploy] rolled back. Investigate:" >&2
  echo "[deploy]   launchctl list | grep $LABEL" >&2
  echo "[deploy]   tail -80 \$HOME/Library/Logs/unitares-portal.log" >&2
  exit 1
fi
