#!/usr/bin/env bash
# Deploy the Discord bridge (com.unitares.discord-bridge) from a DEDICATED
# worktree pinned to origin/main — never the shared dev checkout.
#
# Why this exists: the bridge had a deploy-status.sh row but no deploy script,
# so deploy-apply.sh could only REPORT it ("SKIP ... no deploy script") and
# every sweep left it behind. It had been running 2 commits behind its own
# checkout.
#
# NOT "the last service outside the sweep" — an earlier version of this comment
# claimed that and it was false. dispatch-beam and dispatch-beam-codex also
# have COMPONENTS rows with no deploy_script_for() case, and they read CURRENT
# today, which is exactly why nobody notices. Absence of a dispatch entry is
# invisible until the service happens to drift.
#
# It matters more here than elsewhere: the bridge IS the alert delivery path
# (#alerts, the lease-plane and liveness criticals), so a silently stale bridge
# is a silently stale alarm.
#
# Three things make this different from the other per-service scripts, and each
# is the reason a generic script could not have covered it:
#
#   1. DIFFERENT REPO, DIFFERENT TRUNK. The bridge lives in
#      cirwel/unitares-discord-bridge on `main`, not in unitares on `master`.
#      Hence deploy_lib_ff_worktree's --branch flag, added with this script.
#   2. SECRETS. config.py calls load_dotenv() with no argument, so the bot reads
#      .env from its WorkingDirectory — and .env is gitignored, so a fresh
#      worktree has none. Handled below.
#   3. NO HTTP HEALTH ENDPOINT. It is a Discord gateway client, not a server;
#      its /health command reports the GOVERNANCE server's health, not its own.
#      The honest liveness signal is the heartbeat file its poll loop rewrites
#      every iteration — the same signal bridge_liveness_watchdog.sh trusts, so
#      deploy and watchdog cannot disagree about what "up" means.
#
# Deliberately NOT here: deploy_lib_nudge_lease_plane. Every unitares-repo
# deploy script calls it because it moves the SHARED unitares-deploy worktree
# under a running BEAM. This script touches a different repo entirely and cannot
# disturb the lease plane, so nudging it would be noise.
#
# Idempotent: creates the worktree if missing, fast-forwards (never resets
# forward), restarts the LaunchAgent, verifies the heartbeat actually advances,
# and rolls back to the previous commit if it does not.
set -euo pipefail

REPO="${BRIDGE_REPO:-$HOME/projects/unitares-discord-bridge}"
DEPLOY="${BRIDGE_DEPLOY:-$HOME/projects/unitares-discord-bridge-deploy}"
LABEL="com.unitares.discord-bridge"
PLIST="${BRIDGE_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
# Same default and same env-var name as bridge_liveness_watchdog.sh — one knob,
# not two that can be set to different paths.
HEARTBEAT="${BRIDGE_HEARTBEAT_PATH:-$HOME/.unitares/discord-bridge.heartbeat}"
BRIDGE_LOG="${BRIDGE_LOG:-$HOME/Library/Logs/unitares-discord-bridge.log}"
UID_NUM="$(id -u)"
TAG="deploy-bridge"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

# Keyed on the bridge worktree, so a bridge deploy and a governance-MCP deploy
# do not block each other — they share no worktree and no restart surface.
deploy_lib_acquire_lock "$TAG" "$DEPLOY"

# Match the exact interpreter path: kickstart does not re-read the plist, so a
# plist still pointing at the dev checkout would restart the OLD code and the
# heartbeat probe below would pass against it — a false success.
deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY/.venv/bin/python3" \
  --allow-env BRIDGE_ALLOW_DEV \
  --recipe "[$TAG]   cp \"$PLIST\" \"$PLIST.bak\"
[$TAG]   sed -i '' 's|$REPO|$DEPLOY|g' \"$PLIST\""

# --detach is mandatory, not stylistic: git allows one checkout of a branch per
# repo, and the dev checkout at $REPO is itself sitting ON `main`. A branch-mode
# `worktree add` would die with "already used by worktree at ..." the first time
# this ever ran. Same reason unitares-orchestrator is detached.
deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY" --detach --branch main
PREV="$DEPLOY_LIB_PREV"

# --- secrets ----------------------------------------------------------------
# config.py calls load_dotenv() with no path, so the bot reads <cwd>/.env — and
# .env is gitignored, so a worktree never carries one. The real file lives in
# the fleet secrets dir (0600, outside every checkout) and both checkouts hold a
# symlink to it. Symlink, never copy: two real files would drift and nobody
# could tell which one the live bot actually read.
#
# Outside the checkouts ON PURPOSE. It first lived in the dev checkout, which
# made the DEPLOY worktree depend on the DEV checkout still existing — delete or
# move the dev tree and the live bridge dies at its next restart, for a reason
# nothing in the deploy path would have explained.
ENV_FILE="${BRIDGE_ENV_FILE:-$HOME/.config/cirwel/discord-bridge.env}"
if [ ! -f "$ENV_FILE" ]; then
  echo "[$TAG] REFUSING: no secrets file at $ENV_FILE." >&2
  echo "[$TAG] The bot would boot with an empty DISCORD_BOT_TOKEN and never connect." >&2
  if [ -f "$REPO/.env" ] && [ ! -L "$REPO/.env" ]; then
    echo "[$TAG] This machine still has the pre-migration layout. One-time move:" >&2
    echo "[$TAG]   mv \"$REPO/.env\" \"$ENV_FILE\" && chmod 600 \"$ENV_FILE\"" >&2
    echo "[$TAG]   ln -sfn \"$ENV_FILE\" \"$REPO/.env\"" >&2
  fi
  exit 1
fi
# -L, not -e: -e is false for a DANGLING symlink, so a link left pointing at an
# old location would fall through to `ln -s` on an existing path and fail.
if [ ! -L "$DEPLOY/.env" ] || [ ! -e "$DEPLOY/.env" ]; then
  echo "[$TAG] linking .env -> $ENV_FILE"
  ln -sfn "$ENV_FILE" "$DEPLOY/.env"
fi

# --- dependencies -----------------------------------------------------------
# .venv is gitignored, so a fresh worktree has none. Reinstall only when the
# venv is missing or pyproject.toml actually moved — an unconditional pip run
# on every deploy would add ~20s to a no-op.
NEED_DEPS=0
[ -x "$DEPLOY/.venv/bin/python3" ] || NEED_DEPS=1
if [ "${DEPLOY_LIB_FRESH:-0}" = 1 ]; then
  NEED_DEPS=1
elif ! git -C "$DEPLOY" diff --quiet "$PREV" HEAD -- pyproject.toml 2>/dev/null; then
  echo "[$TAG] pyproject.toml changed in this range — refreshing dependencies"
  NEED_DEPS=1
fi

if [ "$NEED_DEPS" = 1 ]; then
  if [ ! -x "$DEPLOY/.venv/bin/python3" ]; then
    # Mirror the DEV venv's base interpreter rather than trusting whatever
    # `python3` resolves to on PATH. On this machine those disagree — the dev
    # venv is uv-managed CPython 3.12.12 while /opt/homebrew/bin/python3 is
    # 3.14.4 — so a bare `python3 -m venv` would silently deploy the bridge onto
    # a different interpreter than the one the checkout is developed and tested
    # against. Falls back to PATH python3 only when the dev venv is unreadable.
    VENV_BASE="$(sed -n 's/^home = //p' "$REPO/.venv/pyvenv.cfg" 2>/dev/null)/python3"
    [ -x "$VENV_BASE" ] || VENV_BASE="$(command -v python3)"
    echo "[$TAG] creating venv at $DEPLOY/.venv (base: $VENV_BASE)"
    "$VENV_BASE" -m venv "$DEPLOY/.venv"
  fi
  echo "[$TAG] installing dependencies (pip install -e .)"
  if ! "$DEPLOY/.venv/bin/python3" -m pip install -e "$DEPLOY" --quiet; then
    echo "[$TAG] FAILED — dependency install failed; rolling worktree back to ${PREV:0:8} and NOT restarting." >&2
    git -C "$DEPLOY" reset --hard "$PREV"
    exit 1
  fi
fi

# --- restart ----------------------------------------------------------------
# Record the heartbeat's mtime BEFORE the restart. "File exists" proves nothing
# here — the old process wrote it seconds ago, and a bot that crashes on boot
# leaves that stale file sitting there looking healthy. Only an mtime that moves
# past this mark proves the NEW process reached its poll loop.
hb_mtime() { stat -f %m "$HEARTBEAT" 2>/dev/null || echo 0; }
HB_BEFORE="$(hb_mtime)"

echo "[$TAG] restarting $LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

heartbeat_advanced() { [ "$(hb_mtime)" -gt "$HB_BEFORE" ]; }

# Generous window: Discord gateway connect + first poll. Default poll interval
# is 10s (EVENT_POLL_INTERVAL), so 90s is ~9 missed iterations, not a hair
# trigger.
echo "[$TAG] verifying the heartbeat advances past $HB_BEFORE (up to 90s)"
if deploy_lib_poll 18 5 heartbeat_advanced; then
  echo "[$TAG] OK — bridge healthy, poll loop live (serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD))"
else
  echo "[$TAG] FAILED — heartbeat never advanced; the new code did not reach its poll loop." >&2
  echo "[$TAG] Rolling the worktree back to ${PREV:0:8} and restarting." >&2
  git -C "$DEPLOY" reset --hard "$PREV"
  launchctl kickstart -k "gui/$UID_NUM/$LABEL"
  echo "[$TAG] rolled back. Investigate: tail -80 $BRIDGE_LOG" >&2
  exit 1
fi
