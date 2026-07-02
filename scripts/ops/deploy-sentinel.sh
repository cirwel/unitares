#!/usr/bin/env bash
# Deploy BEAM Sentinel (com.unitares.sentinel-beam) from the DEDICATED clean
# worktree pinned to origin/master — never the shared developer working tree.
#
# Why: the Sentinel starts via `mix run` against a checkout on disk. It has been
# running from ~/projects/unitares (the SHARED dev tree, marked restart-DEV/⚠DEV
# in deploy-status.sh), so a merged fix was NOT live until someone manually
# pulled + kickstarted. That is the running-process-vs-master-commit drift class
# (feedback_running-process-vs-master-commit.md) — it caused the 2026-06-28
# "forced-release fix merged but Sentinel still alerting" incident. Mirrors
# deploy-lease-plane.sh: a dedicated worktree makes running-code == origin/master
# by construction.
#
# Verify step uses the boot BUILD-STAMP (PR #1126): on startup the Sentinel logs
#   "BEAM Sentinel booted: unitares_sentinel <vsn> @<sha>"
# and emits a sentinel_build_finding. This script confirms the booted <sha>
# matches the deployed worktree HEAD, so "is the fix live?" is checked, not
# assumed — no HTTP health port exists for the Sentinel.
#
# Idempotent: creates the worktree if missing, fast-forwards to origin/master
# (never a destructive reset), recompiles, restarts the LaunchAgent, and confirms
# the booted sha.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.sentinel-beam"
LOG="${UNITARES_SENTINEL_LOG:-$HOME/Library/Logs/unitares-sentinel-beam.log}"
PLIST="${UNITARES_SENTINEL_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
UID_NUM="$(id -u)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ── Serialize deploys (shared worktree) ──────────────────────────────────────
# deploy-lease-plane.sh / deploy-mcp.sh / this script all fast-forward the SAME
# deploy worktree, so concurrent runs race the git index. macOS has no flock(1);
# guard with an atomic mkdir lock keyed to the worktree path (shared key on
# purpose), reclaiming it only if the holder is dead. Override via
# UNITARES_DEPLOY_LOCK.
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
# If the rendered plist still points at the dev checkout, a kickstart restarts
# the OLD location and this deploy is a no-op. This is the one-time migration off
# restart-DEV; warn loudly with the exact fix rather than silently doing nothing.
if [[ -f "$PLIST" ]] && ! grep -q "$DEPLOY" "$PLIST"; then
  echo "[deploy] WARNING: $LABEL does not appear to load from $DEPLOY." >&2
  echo "[deploy] It is still on the shared dev checkout (restart-DEV). Until you migrate it," >&2
  echo "[deploy] kickstart restarts the OLD location and this deploy will not take effect." >&2
  echo "[deploy] One-time migration (render the plist against the deploy worktree, then reload):" >&2
  echo "[deploy]   sed -e \"s|__UNITARES_ROOT__|$DEPLOY|g\" -e \"s|__HOME__|\$HOME|g\" \\\\" >&2
  echo "[deploy]       (… plus the other placeholders in the template header …) \\\\" >&2
  echo "[deploy]       \"$DEPLOY/scripts/ops/com.unitares.sentinel-beam.plist.template\" > \"$PLIST\"" >&2
  echo "[deploy]   launchctl unload \"$PLIST\" && launchctl load \"$PLIST\"" >&2
  echo "[deploy] Refusing to continue (set UNITARES_SENTINEL_ALLOW_DEV=1 to restart the dev checkout anyway)." >&2
  [[ "${UNITARES_SENTINEL_ALLOW_DEV:-0}" == "1" ]] || exit 2
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
  "$(dirname "$0")/nudge-lease-plane.sh" --reason "deploy-sentinel.sh: deploy worktree re-created (deps/_build gone)" || true
else
  "$(dirname "$0")/nudge-lease-plane.sh" --reason "deploy-sentinel.sh: shared-worktree ff" \
    --if-changed "$LEASE_PREV" "$(git -C "$DEPLOY" rev-parse HEAD)" || true
fi

echo "[deploy] compiling sentinel (surfaces compile errors before the restart)"
( cd "$DEPLOY/elixir/sentinel" && mix deps.get && mix compile )

EXPECT_SHA="$(git -C "$DEPLOY" rev-parse --short=12 HEAD)"

# Only match boot stamps written AFTER this restart, so a prior boot on the same
# sha (idempotent re-run) or a stale line can't false-positive a crash-looping
# node. Capture the current log length, then scan only the lines appended after.
prev_lines=0
[[ -f "$LOG" ]] && prev_lines="$(wc -l < "$LOG" | tr -d ' ')"

echo "[deploy] restarting $LABEL (gui domain — it is a LaunchAgent, not a system daemon)"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "[deploy] verifying booted sha == $EXPECT_SHA via build-stamp (PR #1126) in $LOG"
ok=""
for _ in $(seq 1 12); do
  sleep 3
  if [[ -f "$LOG" ]] && \
     tail -n "+$((prev_lines + 1))" "$LOG" 2>/dev/null | grep -q "BEAM Sentinel booted:.*@$EXPECT_SHA"; then
    ok=yes
    break
  fi
done

if [[ "$ok" == yes ]]; then
  echo "[deploy] OK — sentinel-beam booted on $EXPECT_SHA (serving from $DEPLOY)"
else
  echo "[deploy] FAILED — did not observe a fresh boot stamp @$EXPECT_SHA in $LOG within timeout." >&2
  echo "[deploy] The node may be crash-looping on the new code. Check:" >&2
  echo "[deploy]   launchctl list | grep $LABEL" >&2
  echo "[deploy]   tail -80 $LOG" >&2
  exit 1
fi
