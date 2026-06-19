#!/usr/bin/env bash
# Deploy the governance MCP from the master-pinned deploy worktree
# (~/projects/unitares-deploy), the Python-side analogue of
# deploy-lease-plane.sh (#569). Both services share that one worktree.
#
# DESIGN — why this is split into a one-time setup vs. a recurring deploy:
#
# The live MCP is a LaunchAgent. `launchctl kickstart` restarts the *process*
# but does NOT re-read the plist file — only a RELOAD (`launchctl unload` +
# `load`) picks up plist changes, and that reload needs a login/GUI context
# (it fails from a sandboxed/automated shell). So changing WHERE the MCP runs
# from is a one-time, operator-interactive step. After that the plist is
# STATIC (always points at the deploy worktree) and recurring deploys are just
# ff + kickstart — exactly like the lease plane, whose plist never changes.
#
# This script does ONLY the recurring deploy and REFUSES if the plist does not
# already point at the deploy worktree, so it can never (a) silently no-op a
# plist change kickstart won't apply, or (b) report a false success — the
# failure mode of the first cut of this script, where kickstart restarted the
# OLD code and the /health/ready check passed against it. It verifies the
# RUNNING process is actually executing the deploy-worktree code, because the
# old process answers /health/ready too.
#
# ONE-TIME SETUP (run interactively from a normal login shell, NOT automated):
#   PLIST=~/Library/LaunchAgents/com.unitares.governance-mcp.plist
#   cp "$PLIST" "$PLIST.bak"
#   sed -i '' 's|/Users/cirwel/projects/unitares|/Users/cirwel/projects/unitares-deploy|g' "$PLIST"
#   launchctl unload "$PLIST" && launchctl load "$PLIST"   # RELOAD (kickstart won't)
#   curl -s http://127.0.0.1:8767/health/ready             # expect {"status":"ready"}
#   # rollback: cp "$PLIST.bak" "$PLIST"; launchctl unload "$PLIST"; launchctl load "$PLIST"
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_MCP_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.governance-mcp"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
PORT="${UNITARES_MCP_PORT:-8767}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ── Serialize deploys (shared worktree) ──────────────────────────────────────
# deploy-mcp.sh and deploy-lease-plane.sh both fast-forward the SAME deploy
# worktree, and this script's failure path runs `git reset --hard $PREV`. Two
# deploys at once race the git index and — worse — the rollback can revert a
# parallel deploy to a stale commit (silent regression + a false "OK"). macOS
# has no flock(1), so guard with an atomic mkdir lock keyed to the worktree,
# reclaiming it only if the holder process is dead. Override via
# UNITARES_DEPLOY_LOCK; the lock is shared with deploy-lease-plane.sh because the
# name derives from the (shared) DEPLOY path.
LOCK_DIR="${UNITARES_DEPLOY_LOCK:-${TMPDIR:-/tmp}/unitares-deploy$(printf '%s' "$DEPLOY" | tr -c 'A-Za-z0-9' '_').lock}"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo '?')"
  if [[ "$holder" != '?' ]] && ! kill -0 "$holder" 2>/dev/null; then
    echo "[deploy-mcp] reclaiming stale deploy lock (holder PID $holder is dead): $LOCK_DIR" >&2
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || { echo "[deploy-mcp] lost a lock race — another deploy just started; refusing" >&2; exit 1; }
  else
    echo "[deploy-mcp] another deploy is in progress (lock: $LOCK_DIR, holder PID $holder) — refusing to run concurrently" >&2
    exit 1
  fi
fi
printf '%s' "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

if [[ ! -f "$PLIST" ]]; then
  echo "[deploy-mcp] $PLIST not installed — install the LaunchAgent first (see CLAUDE.md setup)" >&2
  exit 1
fi

# Precondition: the plist must already point at the deploy worktree. We do NOT
# change the plist here (that needs an operator-interactive reload). Refuse
# loudly with the one-time setup recipe rather than silently no-op.
if ! grep -q "$DEPLOY/src/mcp_server.py" "$PLIST"; then
  cat >&2 <<EOF
[deploy-mcp] REFUSING: the LaunchAgent plist does not point at the deploy
worktree, so kickstart would restart the OLD code and this script would lie
about success. Do the one-time setup interactively (login shell) first:

  cp "$PLIST" "$PLIST.bak"
  sed -i '' 's|$REPO|$DEPLOY|g' "$PLIST"
  launchctl unload "$PLIST" && launchctl load "$PLIST"   # RELOAD — kickstart won't
  curl -s http://127.0.0.1:$PORT/health/ready            # expect {"status":"ready"}

Then re-run this script for ongoing deploys.
EOF
  exit 1
fi

echo "[deploy-mcp] fetching origin/master"
git -C "$REPO" fetch origin master --quiet

PREV="$(git -C "$DEPLOY" rev-parse HEAD)"
echo "[deploy-mcp] fast-forwarding $DEPLOY to origin/master (ff-only; was ${PREV:0:8})"
git -C "$DEPLOY" merge --ff-only origin/master
mkdir -p "$DEPLOY/data/logs"

echo "[deploy-mcp] restarting $LABEL (plist is static + already points at the worktree, so kickstart suffices)"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "[deploy-mcp] verifying the RUNNING process is the deploy-worktree code (bge-m3 load can take ~30-90s)"
ok=""
for _ in $(seq 1 40); do
  sleep 3
  pid="$(launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid =/{print $2; exit}')"
  if curl -fsS -m4 "http://127.0.0.1:${PORT}/health/ready" 2>/dev/null | grep -q '"status":"ready"' \
     && [[ -n "$pid" ]] && ps -o command= -p "$pid" 2>/dev/null | grep -q "$DEPLOY/src/mcp_server.py"; then
    ok=yes
    break
  fi
done

if [[ "$ok" == yes ]]; then
  echo "[deploy-mcp] OK — governance MCP healthy on deploy-worktree code @ $(git -C "$DEPLOY" rev-parse --short HEAD)"
else
  echo "[deploy-mcp] FAILED — new code did not come up healthy. Rolling the worktree back to ${PREV:0:8} and restarting." >&2
  git -C "$DEPLOY" reset --hard "$PREV"
  launchctl kickstart -k "gui/$UID_NUM/$LABEL"
  echo "[deploy-mcp] rolled back. Investigate ${DEPLOY}/data/logs/mcp_server_error.log" >&2
  exit 1
fi
