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

# ── Flags ────────────────────────────────────────────────────────────────────
# --apply-migrations: apply any pending DB migrations as part of the deploy
# (opt-in, since DDL is a deliberate/approved action). Also settable via
# UNITARES_DEPLOY_APPLY_MIGRATIONS=1. Default is detect-and-refuse on a gap.
APPLY_MIGRATIONS="${UNITARES_DEPLOY_APPLY_MIGRATIONS:-0}"
for arg in "$@"; do
  case "$arg" in
    --apply-migrations) APPLY_MIGRATIONS=1 ;;
    *) echo "[deploy-mcp] unknown argument: $arg" >&2; exit 2 ;;
  esac
done

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

# ── Migration preflight (gate, not silent) ───────────────────────────────────
# This script historically restarted the MCP without touching DB migrations,
# and migrations are applied by hand — twice the code went live expecting a
# schema that wasn't there (the Phase-A half-deploy; migration 042). This gate
# runs the just-fast-forwarded worktree's own migration checker against the
# live DB and REFUSES to restart on a gap, rather than silently bringing up
# code that expects an unapplied schema. DDL stays a deliberate, opt-in action:
#   - default: detect a gap and refuse (with the apply recipe), rolling the
#     worktree back to $PREV so on-disk code never gets left ahead of the
#     still-running process (that mismatch is its own reboot-time foot-gun).
#   - --apply-migrations: apply pending migrations FIRST (schema before code),
#     re-verify, then proceed to the restart.
# Note: a later health-failure rollback resets the worktree but NOT the DB —
# migrations are forward-only and additive (IF NOT EXISTS), so new-schema +
# old-code is the safe direction (the inverse is the failure mode above).
MIGRATE="$DEPLOY/scripts/dev/apply_migrations.py"
MIGRATE_DBURL=()
[[ -n "${UNITARES_DEPLOY_DB_URL:-}" ]] && MIGRATE_DBURL=(--db-url "$UNITARES_DEPLOY_DB_URL")
# Expand with the ${arr[@]+"${arr[@]}"} guard everywhere below: on macOS bash 3.2
# (the default /bin/bash) "${empty_array[@]}" trips `set -u` ("unbound variable")
# and aborts the deploy when no UNITARES_DEPLOY_DB_URL is set (the common case).
if [[ -f "$MIGRATE" ]]; then
  echo "[deploy-mcp] migration preflight: is the live DB in sync with the deploy-worktree manifest?"
  if ! python3 "$MIGRATE" --check "${MIGRATE_DBURL[@]+"${MIGRATE_DBURL[@]}"}"; then
    if [[ "$APPLY_MIGRATIONS" == 1 ]]; then
      echo "[deploy-mcp] applying pending migrations (operator opt-in) BEFORE restart"
      if ! python3 "$MIGRATE" --apply "${MIGRATE_DBURL[@]+"${MIGRATE_DBURL[@]}"}" \
         || ! python3 "$MIGRATE" --check "${MIGRATE_DBURL[@]+"${MIGRATE_DBURL[@]}"}"; then
        echo "[deploy-mcp] FAILED — migrations did not reach sync; rolling worktree back to ${PREV:0:8} and NOT restarting." >&2
        git -C "$DEPLOY" reset --hard "$PREV"
        exit 1
      fi
    else
      echo "[deploy-mcp] REFUSING: live DB is not in sync with the migration manifest in the code about to deploy." >&2
      echo "[deploy-mcp] Restarting now would bring up code expecting an unapplied schema (the Phase-A / #042 half-deploy failure mode)." >&2
      echo "[deploy-mcp] Rolling the worktree back to ${PREV:0:8} (so disk does not sit ahead of the running process)." >&2
      git -C "$DEPLOY" reset --hard "$PREV"
      echo "[deploy-mcp] Then either apply the gap and re-deploy:" >&2
      echo "[deploy-mcp]     python3 $MIGRATE --apply" >&2
      echo "[deploy-mcp]   or re-run this deploy with migrations applied automatically:" >&2
      echo "[deploy-mcp]     $0 --apply-migrations" >&2
      exit 1
    fi
  fi
fi

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
