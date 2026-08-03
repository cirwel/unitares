#!/usr/bin/env bash
# Deploy the Surface Lease Plane from a DEDICATED clean worktree pinned to
# origin/master — never from a developer working tree.
#
# Why: the running service starts via `mix run` against a checkout on disk. If
# that checkout is the dev tree, it drifts — stale feature branches, uncommitted
# edits to the very files being served. On 2026-06-02 the running lease plane
# was serving un-reviewed local edits to http_router.ex because it ran from
# ~/projects/unitares while that checkout sat on a feature branch with WIP, and
# a merged fix (#568) was NOT actually live. This is the
# running-process-vs-master-commit drift class (feedback_running-process-vs-
# master-commit.md). BEAM hot-code-reload is the eventual answer (see the
# operator runbook "Hot code reload"); until that's automated, this script makes
# full-restart deploys reproducible from a clean tree.
#
# Idempotent: creates the deploy worktree if missing, fast-forwards it to
# origin/master (never a destructive reset), recompiles, restarts the
# LaunchAgent, and verifies health.
#
# Shared blocks (lock, plist preflight, ff) live in deploy-lib.sh.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_LEASE_PLANE_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.lease-plane"
PLIST="${UNITARES_LEASE_PLANE_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
UID_NUM="$(id -u)"
TAG="deploy"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

# Preflight parity with the sibling scripts: if the plist still loads from the
# dev checkout, the kickstart below restarts the OLD location and the health
# probe passes against it. (Historically this script had no preflight — the
# exact false-success gap the preflight exists to close.)
deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY" \
  --allow-env UNITARES_LEASE_PLANE_ALLOW_DEV \
  --recipe "[deploy]   sed -i '' 's|$REPO|$DEPLOY|g' \"$PLIST\"   # or re-render the plist template against $DEPLOY"

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"

echo "[deploy] compiling lease_plane (surfaces compile errors before the restart)"
( cd "$DEPLOY/elixir/lease_plane" && mix deps.get && mix compile )

echo "[deploy] restarting $LABEL (gui domain — it is a LaunchAgent, not a system daemon)"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "[deploy] verifying health"
TOKEN="$(
  python3 - <<'PY'
import os
for line in open(os.environ.get("UNITARES_SECRETS_ENV", f"{os.environ['HOME']}/.config/cirwel/secrets.env")):
    line = line.strip()
    if line.startswith("export "):
        line = line[7:]
    if line.startswith("LEASE_PLANE_BEARER_TOKEN="):
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)"

check_lease_plane_health() {
  curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/v1/health 2>/dev/null | grep -q '"ok":true'
}

if deploy_lib_poll 8 3 check_lease_plane_health; then
  echo "[deploy] OK — lease plane healthy, serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD)"
else
  echo "[deploy] FAILED — lease plane did not return healthy. Check ~/Library/Logs/unitares-lease-plane.log" >&2
  exit 1
fi
