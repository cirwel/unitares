#!/usr/bin/env bash
# Restart the lease plane after a shared-worktree disturbance (#1277 fix 1).
#
# The lease plane runs from the SAME deploy worktree as the governance MCP,
# gateway, sentinel, and wave3a services. Any script that re-creates that
# worktree (`git worktree add` → fresh checkout, gitignored deps/ + _build/
# GONE) or moves its sources (`merge --ff-only`, `reset --hard` touching
# elixir/lease_plane) shifts the disk out from under the running BEAM. The
# 2026-06-27 incident (#1277) was exactly this: the worktree was re-created
# at 10:41, the 4-day-old BEAM kept serving in-RAM modules, and the first
# error-render needed Plug.Exception from a deps/ that no longer existed —
# file-lease coordination failed OPEN for ~5.4h. The acquire-healthcheck's
# auto-restart (#1284, armed 2026-07-02) is the safety net; THIS is the root
# fix: the disturbing script restarts the plane at disturbance time, not at
# first failure. start.sh self-heals deps (mix deps.get) and compiles at
# boot, so a kickstart is a full remediation.
#
# Usage:
#   nudge-lease-plane.sh --reason "worktree re-created"
#   nudge-lease-plane.sh --reason "mcp deploy ff" --if-changed <PREV> <NEW>
#
# --if-changed PREV NEW: no-op unless elixir/lease_plane differs between the
#   two commits (use for ff/rollback moves; omit for worktree re-creation,
#   where gitignored build state is gone regardless of source diffs).
#
# Exit: 0 on nudge-or-skip; 1 if the plane failed to answer after the nudge
# (callers should treat that as a loud warning, not a deploy abort — the
# acquire-healthcheck auto-restart remains the backstop).
set -euo pipefail

DEPLOY="${UNITARES_MCP_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.lease-plane"
UID_NUM="$(id -u)"
PORT="${UNITARES_LEASE_PLANE_PORT:-8788}"

REASON=""
CHECK_RANGE=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason) REASON="$2"; shift 2 ;;
    --if-changed) CHECK_RANGE=("$2" "$3"); shift 3 ;;
    *) echo "[nudge-lease-plane] unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ${#CHECK_RANGE[@]} -eq 2 ]]; then
  if git -C "$DEPLOY" diff --quiet "${CHECK_RANGE[0]}" "${CHECK_RANGE[1]}" -- elixir/lease_plane 2>/dev/null; then
    echo "[nudge-lease-plane] elixir/lease_plane unchanged ${CHECK_RANGE[0]:0:8}..${CHECK_RANGE[1]:0:8} — no restart needed"
    exit 0
  fi
fi

if ! launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  echo "[nudge-lease-plane] $LABEL not loaded on this machine — skipping"
  exit 0
fi

echo "[nudge-lease-plane] restarting $LABEL (${REASON:-shared-worktree disturbance}); start.sh re-runs deps.get + compiles at boot"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

# Bounded wait for the listener: any HTTP status (200/401/...) means the BEAM
# is up and serving fresh-compiled code; 000 means not yet accepting.
for _ in $(seq 1 30); do
  sleep 2
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:${PORT}/v1/health" 2>/dev/null || echo 000)"
  if [[ "$code" != "000" ]]; then
    echo "[nudge-lease-plane] plane answering (HTTP $code) on :$PORT"
    exit 0
  fi
done

echo "[nudge-lease-plane] WARNING: plane not answering on :$PORT within 60s of restart — check ~/Library/Logs/unitares-lease-plane.log (acquire-healthcheck auto-restart remains the backstop)" >&2
exit 1
