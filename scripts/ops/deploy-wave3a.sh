#!/usr/bin/env bash
# Deploy the Wave 3a BEAM handler app (com.unitares.wave3a-handlers) from the
# DEDICATED clean worktree pinned to origin/master — never the shared dev tree.
#
# Why: like the Sentinel, wave3a-handlers starts via `mix run` against a checkout
# on disk and has run from ~/projects/unitares (restart-DEV/⚠DEV in
# deploy-status.sh), so a merged fix wasn't live until a manual pull + kickstart
# — the running-process-vs-master-commit drift class. Mirrors deploy-sentinel.sh
# / deploy-lease-plane.sh: a dedicated worktree makes running-code ==
# origin/master by construction.
#
# Verify uses the open /health endpoint on :8770 (the bearer-gated handler
# routes are not probed). Idempotent: creates the worktree if missing,
# fast-forwards (never resets), recompiles (MIX_ENV=prod, matching the plist),
# restarts the LaunchAgent, and confirms /health.
#
# NOTE: the wave3a plist ships WITHOUT RunAtLoad (operator-gated cutover per RFC
# beam-wave-3a §5). This script only deploys a service that is already loaded +
# running; if it is intentionally unloaded, deploy-status reports DOWN (not
# STALE) and the sweep skips it.
#
# Shared blocks (lock, plist preflight, ff + lease-plane nudge) live in
# deploy-lib.sh.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.wave3a-handlers"
PLIST="${UNITARES_WAVE3A_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_WAVE3A_PORT:-8770}"
UID_NUM="$(id -u)"
TAG="deploy"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY" \
  --allow-env UNITARES_WAVE3A_ALLOW_DEV \
  --recipe "[deploy]   re-render the plist against the deploy worktree
[deploy]   (sed __UNITARES_ROOT__ -> $DEPLOY ; see the template header)"

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"
deploy_lib_nudge_lease_plane "$TAG" "deploy-wave3a.sh" "$DEPLOY"

echo "[deploy] compiling wave3a_handlers (MIX_ENV=prod; surfaces compile errors before restart)"
( cd "$DEPLOY/elixir/wave3a_handlers" && mix deps.get && MIX_ENV=prod mix compile )

echo "[deploy] restarting $LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

check_wave3a_health() {
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
}

echo "[deploy] verifying /health on :$PORT"
if deploy_lib_poll 12 3 check_wave3a_health; then
  echo "[deploy] OK — wave3a-handlers healthy on :$PORT (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
else
  echo "[deploy] FAILED — /health on :$PORT did not respond within timeout." >&2
  echo "[deploy] Check: launchctl list | grep $LABEL ; tail -80 $HOME/Library/Logs/unitares-wave3a-handlers.log" >&2
  exit 1
fi
