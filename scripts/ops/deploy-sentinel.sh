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
#
# Shared blocks (lock, plist preflight, ff + lease-plane nudge) live in
# deploy-lib.sh.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.sentinel-beam"
LOG="${UNITARES_SENTINEL_LOG:-$HOME/Library/Logs/unitares-sentinel-beam.log}"
PLIST="${UNITARES_SENTINEL_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
UID_NUM="$(id -u)"
TAG="deploy"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY" \
  --allow-env UNITARES_SENTINEL_ALLOW_DEV \
  --recipe "[deploy]   sed -e \"s|__UNITARES_ROOT__|$DEPLOY|g\" -e \"s|__HOME__|\$HOME|g\" \\
[deploy]       (… plus the other placeholders in the template header …) \\
[deploy]       \"$DEPLOY/scripts/ops/com.unitares.sentinel-beam.plist.template\" > \"$PLIST\""

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY"
deploy_lib_nudge_lease_plane "$TAG" "deploy-sentinel.sh" "$DEPLOY"

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

check_sentinel_boot_stamp() {
  [[ -f "$LOG" ]] && \
    tail -n "+$((prev_lines + 1))" "$LOG" 2>/dev/null | grep -q "BEAM Sentinel booted:.*@$EXPECT_SHA"
}

echo "[deploy] verifying booted sha == $EXPECT_SHA via build-stamp (PR #1126) in $LOG"
if deploy_lib_poll 12 3 check_sentinel_boot_stamp; then
  echo "[deploy] OK — sentinel-beam booted on $EXPECT_SHA (serving from $DEPLOY)"
else
  echo "[deploy] FAILED — did not observe a fresh boot stamp @$EXPECT_SHA in $LOG within timeout." >&2
  echo "[deploy] The node may be crash-looping on the new code. Check:" >&2
  echo "[deploy]   launchctl list | grep $LABEL" >&2
  echo "[deploy]   tail -80 $LOG" >&2
  exit 1
fi
