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
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_LEASE_PLANE_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.lease-plane"
UID_NUM="$(id -u)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

echo "[deploy] fetching origin/master"
git -C "$REPO" fetch origin master --quiet

if ! git -C "$REPO" worktree list --porcelain | grep -qx "worktree $DEPLOY"; then
  echo "[deploy] creating dedicated deploy worktree at $DEPLOY (on master)"
  git -C "$REPO" worktree add "$DEPLOY" master
fi

echo "[deploy] fast-forwarding $DEPLOY to origin/master (ff-only; refuses if it would lose work)"
git -C "$DEPLOY" merge --ff-only origin/master

echo "[deploy] compiling lease_plane (surfaces compile errors before the restart)"
( cd "$DEPLOY/elixir/lease_plane" && mix deps.get && mix compile )

echo "[deploy] restarting $LABEL (gui domain — it is a LaunchAgent, not a system daemon)"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "[deploy] verifying health"
TOKEN="$(
  python3 - <<'PY'
for line in open(f"{__import__('os').environ['HOME']}/.config/cirwel/secrets.env"):
    line = line.strip()
    if line.startswith("export "):
        line = line[7:]
    if line.startswith("LEASE_PLANE_BEARER_TOKEN="):
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)"
ok=""
for _ in 1 2 3 4 5 6 7 8; do
  sleep 3
  if curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/v1/health 2>/dev/null | grep -q '"ok":true'; then
    ok=yes
    break
  fi
done

if [[ "$ok" == yes ]]; then
  echo "[deploy] OK — lease plane healthy, serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD)"
else
  echo "[deploy] FAILED — lease plane did not return healthy. Check ~/Library/Logs/unitares-lease-plane.log" >&2
  exit 1
fi
