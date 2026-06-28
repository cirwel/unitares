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
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.gateway-mcp"
PLIST="${UNITARES_GATEWAY_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_GATEWAY_PORT:-8768}"
UID_NUM="$(id -u)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ── Serialize deploys (shared worktree) ──────────────────────────────────────
LOCK_DIR="${UNITARES_DEPLOY_LOCK:-${TMPDIR:-/tmp}/unitares-deploy$(printf '%s' "$DEPLOY" | tr -c 'A-Za-z0-9' '_').lock}"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo '?')"
  if [[ "$holder" != '?' ]] && ! kill -0 "$holder" 2>/dev/null; then
    echo "[deploy] reclaiming stale deploy lock (holder PID $holder is dead): $LOCK_DIR" >&2
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || { echo "[deploy] lost a lock race — another deploy just started; refusing" >&2; exit 1; }
  else
    echo "[deploy] another deploy is in progress (lock: $LOCK_DIR, holder PID $holder) — refusing to run concurrently" >&2
    exit 1
  fi
fi
printf '%s' "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# ── Pre-flight: the LaunchAgent must load from the deploy worktree ────────────
# Match the exact program path so a kickstart can't silently restart the dev
# checkout and then have /health pass against the OLD process.
if [[ -f "$PLIST" ]] && ! grep -q "$DEPLOY/src/gateway_server.py" "$PLIST"; then
  echo "[deploy] WARNING: $LABEL does not run $DEPLOY/src/gateway_server.py (still restart-DEV)." >&2
  echo "[deploy] kickstart would restart the OLD location and /health would pass against it." >&2
  echo "[deploy] One-time migration (interactive login shell — a RELOAD, kickstart won't re-read the plist):" >&2
  echo "[deploy]   cp \"$PLIST\" \"$PLIST.bak\"" >&2
  echo "[deploy]   sed -i '' 's|$REPO|$DEPLOY|g' \"$PLIST\"" >&2
  echo "[deploy]   launchctl unload \"$PLIST\" && launchctl load \"$PLIST\"" >&2
  echo "[deploy] Refusing (set UNITARES_GATEWAY_ALLOW_DEV=1 to restart the dev checkout anyway)." >&2
  [[ "${UNITARES_GATEWAY_ALLOW_DEV:-0}" == "1" ]] || exit 2
fi

echo "[deploy] fetching origin/master"
git -C "$REPO" fetch origin master --quiet

if ! git -C "$REPO" worktree list --porcelain | grep -qx "worktree $DEPLOY"; then
  echo "[deploy] creating dedicated deploy worktree at $DEPLOY (on master)"
  git -C "$REPO" worktree add "$DEPLOY" master
fi

echo "[deploy] fast-forwarding $DEPLOY to origin/master (ff-only; refuses if it would lose work)"
git -C "$DEPLOY" merge --ff-only origin/master
mkdir -p "$DEPLOY/data/logs"

echo "[deploy] restarting $LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "[deploy] verifying /health on :$PORT"
ok=""
for _ in $(seq 1 12); do
  sleep 3
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    ok=yes
    break
  fi
done

if [[ "$ok" == yes ]]; then
  echo "[deploy] OK — gateway-mcp healthy on :$PORT (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
else
  echo "[deploy] FAILED — /health on :$PORT did not respond within timeout." >&2
  echo "[deploy] Check: launchctl list | grep $LABEL ; tail -80 $DEPLOY/data/logs/gateway_server_error.log" >&2
  exit 1
fi
