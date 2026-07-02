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
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.wave3a-handlers"
PLIST="${UNITARES_WAVE3A_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_WAVE3A_PORT:-8770}"
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
if [[ -f "$PLIST" ]] && ! grep -q "$DEPLOY" "$PLIST"; then
  echo "[deploy] WARNING: $LABEL does not appear to load from $DEPLOY (still restart-DEV)." >&2
  echo "[deploy] kickstart would restart the OLD location and this deploy would not take effect." >&2
  echo "[deploy] One-time migration: re-render the plist against the deploy worktree" >&2
  echo "[deploy]   (sed __UNITARES_ROOT__ -> $DEPLOY ; see the template header), then reload:" >&2
  echo "[deploy]   launchctl unload \"$PLIST\" && launchctl load \"$PLIST\"" >&2
  echo "[deploy] Refusing (set UNITARES_WAVE3A_ALLOW_DEV=1 to restart the dev checkout anyway)." >&2
  [[ "${UNITARES_WAVE3A_ALLOW_DEV:-0}" == "1" ]] || exit 2
fi

echo "[deploy] fetching origin/master"
git -C "$REPO" fetch origin master --quiet

LEASE_FRESH=0
if ! git -C "$REPO" worktree list --porcelain | grep -qx "worktree $DEPLOY"; then
  echo "[deploy] creating dedicated deploy worktree at $DEPLOY (on master)"
  git -C "$REPO" worktree add "$DEPLOY" master
  # A fresh `worktree add` checks out TRACKED files only — the lease plane's
  # gitignored deps/ + _build/ are GONE, while the running BEAM keeps serving
  # in-RAM modules until an unloaded module needs disk (the 06-27 ~5.4h
  # fail-open, #1277). Nudge the plane after the ff below.
  LEASE_FRESH=1
fi

LEASE_PREV="$(git -C "$DEPLOY" rev-parse HEAD)"
echo "[deploy] fast-forwarding $DEPLOY to origin/master (ff-only; refuses if it would lose work)"
git -C "$DEPLOY" merge --ff-only origin/master
# The shared worktree just moved under every co-resident service (#1277 fix 1).
if [[ "$LEASE_FRESH" == 1 ]]; then
  "$(dirname "$0")/nudge-lease-plane.sh" --reason "deploy-wave3a.sh: deploy worktree re-created (deps/_build gone)" || true
else
  "$(dirname "$0")/nudge-lease-plane.sh" --reason "deploy-wave3a.sh: shared-worktree ff" \
    --if-changed "$LEASE_PREV" "$(git -C "$DEPLOY" rev-parse HEAD)" || true
fi

echo "[deploy] compiling wave3a_handlers (MIX_ENV=prod; surfaces compile errors before restart)"
( cd "$DEPLOY/elixir/wave3a_handlers" && mix deps.get && MIX_ENV=prod mix compile )

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
  echo "[deploy] OK — wave3a-handlers healthy on :$PORT (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
else
  echo "[deploy] FAILED — /health on :$PORT did not respond within timeout." >&2
  echo "[deploy] Check: launchctl list | grep $LABEL ; tail -80 $HOME/Library/Logs/unitares-wave3a-handlers.log" >&2
  exit 1
fi
