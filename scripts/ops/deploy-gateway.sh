#!/usr/bin/env bash
# Deploy the Gateway MCP (com.unitares.gateway-mcp) from the DEDICATED clean
# worktree pinned to origin/master — never the shared dev tree.
#
# The gateway is a Python reduced-surface proxy on :8768 (src/gateway_server.py)
# that fronts the governance MCP on :8767. Like that MCP it has run from
# ~/projects/unitares (restart-DEV/⚠DEV in deploy-status.sh), so a merged fix
# wasn't live until a manual pull + kickstart — the running-process-vs-master-
# commit drift class. The Python-side analogue of deploy-sentinel.sh; the same
# shape as deploy-mcp.sh but for the gateway process. No compile step (Python);
# deps come from the worktree's environment exactly as the governance MCP's do.
#
# Idempotent: creates the worktree if missing, fast-forwards (never resets),
# restarts the LaunchAgent, and verifies /health on :8768.
#
# Shared blocks (lock, plist preflight, ff + lease-plane nudge) live in
# deploy-lib.sh.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.gateway-mcp"
PLIST="${UNITARES_GATEWAY_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_GATEWAY_PORT:-8768}"
UID_NUM="$(id -u)"
TAG="deploy"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

# Match the exact program path so a kickstart can't silently restart the dev
# checkout and then have /health pass against the OLD process.
deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY/src/gateway_server.py" \
  --allow-env UNITARES_GATEWAY_ALLOW_DEV \
  --recipe "[deploy]   cp \"$PLIST\" \"$PLIST.bak\"
[deploy]   sed -i '' 's|$REPO|$DEPLOY|g' \"$PLIST\""

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"
deploy_lib_nudge_lease_plane "$TAG" "deploy-gateway.sh" "$DEPLOY"
mkdir -p "$DEPLOY/data/logs"

echo "[deploy] restarting $LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

check_gateway_health() {
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
}

echo "[deploy] verifying /health on :$PORT"
if deploy_lib_poll 12 3 check_gateway_health; then
  echo "[deploy] OK — gateway-mcp healthy on :$PORT (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
else
  echo "[deploy] FAILED — /health on :$PORT did not respond within timeout." >&2
  echo "[deploy] Check: launchctl list | grep $LABEL ; tail -80 $DEPLOY/data/logs/gateway_server_error.log" >&2
  exit 1
fi
