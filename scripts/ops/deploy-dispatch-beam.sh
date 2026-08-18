#!/usr/bin/env bash
# Deploy BOTH dispatch_beam instances (com.cirwel.dispatch-beam and
# com.cirwel.dispatch-beam-codex) from a DEDICATED worktree pinned to
# origin/master — never the shared dev checkout.
#
# Why this exists: deploy-bridge.sh's own header already named this gap —
# "dispatch-beam and dispatch-beam-codex also have COMPONENTS rows with no
# deploy_script_for() case, and they read CURRENT today, which is exactly why
# nobody notices." On 2026-08-18 they stopped reading CURRENT: both went
# BEHIND(1), `cirwel update` reported the drift and then skipped it, exactly as
# the bridge used to be skipped.
#
# Four things make this different from every other per-service script:
#
#   1. TWO SERVICES, ONE WORKTREE. The claude and codex bots are the same code
#      with different tokens, snames and state files. deploy-apply.sh calls this
#      once per BEHIND component, so a naive script restarts both bots twice.
#      Handled by the per-label marker files below.
#   2. STATE LIVES OUTSIDE THE CHECKOUT — and MUST. dispatch_beam persists its
#      resume ids (data/dispatch_snapshot.bin), its boot counter and its
#      last-connected clock. Those paths default to `File.cwd!()/data`, so
#      moving the working directory to a deploy worktree without repointing them
#      hands the bot an EMPTY snapshot. That is not a degraded start: an empty
#      snapshot table makes `known_thread?` false for every pre-existing thread,
#      so all of them go silently unresponsive — no error, no log line. The
#      migration (migrate-dispatch-beam-deploy.sh) moves state to
#      ~/.local/state/dispatch-beam and pins all four paths in both plists; this
#      script REFUSES to run until that has happened.
#   3. NO HTTP HEALTH ENDPOINT, and the honest signal is two-stage. A Discord
#      gateway client has no port to probe. `data/boot_witness.bin` advances
#      when the supervision tree starts, which proves the new code booted;
#      `presence.bin` advances only while the gateway is actually CONNECTED,
#      which is the stronger claim. Both are checked, but they fail differently
#      — see the reachability gate below.
#   4. THE NETWORK IS A LEGITIMATE REASON TO NOT BE CONNECTED. Since
#      dispatch_beam#81, Dispatch.Host distinguishes "we are broken" from "the
#      house is dark". This script makes the same distinction: it will not roll
#      back a perfectly good deploy because the ISP is down. It checks
#      reachability BEFORE deciding what a stalled presence clock means.
#
# Deliberately NOT here: deploy_lib_nudge_lease_plane. That exists because
# unitares-repo deploys move the SHARED unitares-deploy worktree under a running
# BEAM. This is a different repo entirely and cannot disturb the lease plane.
#
# Idempotent: creates the worktree if missing, fast-forwards (never resets
# forward), restarts only the instances that actually need it, verifies each,
# and rolls the worktree back if a restart does not come up.
set -euo pipefail

REPO="${DISPATCH_REPO:-$HOME/projects/dispatch_beam}"
DEPLOY="${DISPATCH_DEPLOY:-$HOME/projects/dispatch_beam-deploy}"
STATE_DIR="${DISPATCH_STATE_DIR:-$HOME/.local/state/dispatch-beam}"
MARKER_DIR="$STATE_DIR/.deploy"
LOG_DIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"
TAG="deploy-dispatch-beam"

# label | plist basename | boot-witness file | presence file | log basename
INSTANCES=(
  "com.cirwel.dispatch-beam|boot_witness.bin|presence.bin|dispatch-beam.log"
  "com.cirwel.dispatch-beam-codex|boot_witness_codex.bin|presence_codex.bin|dispatch-beam-codex.log"
)

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

# Keyed on the dispatch worktree: a dispatch deploy and a governance-MCP deploy
# share no worktree and no restart surface, so they must not block each other.
deploy_lib_acquire_lock "$TAG" "$DEPLOY"

# --- preconditions ----------------------------------------------------------
# State must already live outside the checkouts. Checked BEFORE the worktree is
# touched, because the failure this prevents is silent and expensive: a bot that
# boots cleanly against an empty snapshot looks healthy from every angle this
# script can see, and only the humans in those threads find out.
if [ ! -d "$STATE_DIR" ]; then
  echo "[$TAG] REFUSING: no shared state directory at $STATE_DIR." >&2
  echo "[$TAG] Deploying from a worktree without it would start both bots against an" >&2
  echo "[$TAG] EMPTY snapshot — every pre-existing thread goes silently unresponsive." >&2
  echo "[$TAG] Run the one-time migration first:" >&2
  echo "[$TAG]   $(dirname "$0")/migrate-dispatch-beam-deploy.sh --dry-run" >&2
  exit 1
fi

for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label _bw _pres _log <<<"$entry"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  # kickstart does not re-read the plist, so a plist still pointing at the dev
  # checkout would restart the OLD code and every probe below would pass against
  # it — a false success, which is worse than a failure.
  deploy_lib_require_plist_target "$TAG" "$plist" "$DEPLOY" \
    --allow-env DISPATCH_ALLOW_DEV \
    --recipe "[$TAG]   $(dirname "$0")/migrate-dispatch-beam-deploy.sh"
done

# --- fast-forward -----------------------------------------------------------
# --detach is mandatory: git allows one checkout of a branch per repo and the
# dev checkout at $REPO is itself sitting ON master, so branch-mode `worktree
# add` would die with "already used by worktree at ...". Same reason
# unitares-orchestrator and the bridge are detached.
deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY" --detach
PREV="$DEPLOY_LIB_PREV"
HEAD_SHA="$(git -C "$DEPLOY" rev-parse HEAD)"

# --- secrets ----------------------------------------------------------------
# scripts/run.sh sources $DISPATCH_ENV_FILE relative to its working directory,
# and .env* is gitignored, so a fresh worktree carries none. Symlink, never
# copy: two real token files would drift and nobody could tell which one the
# live bot actually read. Same reasoning as deploy-bridge.sh.
for env_name in .env .env.codex; do
  src="$STATE_DIR/$env_name"
  if [ ! -f "$src" ]; then
    echo "[$TAG] REFUSING: no $env_name at $src — the bot would boot with an empty" >&2
    echo "[$TAG] DISCORD_BOT_TOKEN and never connect. Run migrate-dispatch-beam-deploy.sh." >&2
    exit 1
  fi
  # -L not -e: -e is false for a DANGLING symlink, so a link left pointing at an
  # old location would fall through to `ln -s` on an existing path and fail.
  if [ ! -L "$DEPLOY/$env_name" ] || [ ! -e "$DEPLOY/$env_name" ]; then
    echo "[$TAG] linking $env_name -> $src"
    ln -sfn "$src" "$DEPLOY/$env_name"
  fi
done

# --- dependencies -----------------------------------------------------------
# deps/ and _build/ are gitignored, so a fresh worktree has neither. An
# unconditional `mix deps.get` costs seconds on a no-op, but compiling is the
# expensive part and is also the thing that must not be skipped when it matters.
NEED_DEPS=0
[ -d "$DEPLOY/_build" ] || NEED_DEPS=1
if [ "${DEPLOY_LIB_FRESH:-0}" = 1 ]; then
  NEED_DEPS=1
elif ! git -C "$DEPLOY" diff --quiet "$PREV" HEAD -- mix.exs mix.lock 2>/dev/null; then
  echo "[$TAG] mix.exs/mix.lock changed in this range — refreshing dependencies"
  NEED_DEPS=1
fi

if [ "$NEED_DEPS" = 1 ]; then
  echo "[$TAG] fetching dependencies (mix deps.get)"
  if ! (cd "$DEPLOY" && MIX_ENV=prod mix deps.get >/dev/null); then
    echo "[$TAG] FAILED — dependency fetch failed; rolling back to ${PREV:0:8}, NOT restarting." >&2
    git -C "$DEPLOY" reset --hard "$PREV"
    exit 1
  fi
fi

# Compile BEFORE any restart. A compile error caught here costs nothing; the
# same error caught by launchd is a KeepAlive respawn loop against a bot that
# never connects. --warnings-as-errors matches the repo's own CI gate.
echo "[$TAG] compiling (mix compile --warnings-as-errors)"
if ! (cd "$DEPLOY" && MIX_ENV=prod mix compile --warnings-as-errors); then
  echo "[$TAG] FAILED — compile failed; rolling back to ${PREV:0:8}, NOT restarting." >&2
  git -C "$DEPLOY" reset --hard "$PREV"
  exit 1
fi

# --- reachability -----------------------------------------------------------
# Decides what a stalled presence clock is allowed to MEAN. presence.bin only
# advances while the gateway is connected, so with no network it never advances
# no matter how good the code is. Rolling back on that would revert a healthy
# deploy for a reason that has nothing to do with the deploy. Mirrors
# Dispatch.Host (dispatch_beam#81): IP literal first so a dead resolver does not
# read as a dead internet.
network_up() {
  nc -z -G 2 1.1.1.1 53 >/dev/null 2>&1 ||
    nc -z -G 2 8.8.8.8 53 >/dev/null 2>&1
}

if network_up; then
  NET_OK=1
  echo "[$TAG] network reachable — presence must advance for a deploy to pass"
else
  NET_OK=0
  echo "[$TAG] WARNING: no network. Boot will be verified, gateway connection CANNOT be." >&2
  echo "[$TAG] A pass here means 'the new code booted', not 'the bot is online'." >&2
fi

# --- restart ----------------------------------------------------------------
mkdir -p "$MARKER_DIR"
mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }
label_pid() { launchctl list 2>/dev/null | awk -v l="$1" '$3 == l { print $1 }'; }

restarted=""; skipped=""
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label bw pres log <<<"$entry"
  marker="$MARKER_DIR/$label"
  pid="$(label_pid "$label")"

  # deploy-apply.sh invokes this script once per BEHIND component, and both
  # dispatch rows share this script. Without this, the second invocation
  # restarts both bots again for nothing. The PID is part of the key so a
  # launchd respawn (crash loop) still redeploys rather than reading as done.
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$HEAD_SHA:$pid" ] && [ -n "$pid" ]; then
    echo "[$TAG] $label already running ${HEAD_SHA:0:8} (pid $pid) — no restart"
    skipped="$skipped $label"
    continue
  fi

  BW_BEFORE="$(mtime "$STATE_DIR/$bw")"
  PRES_BEFORE="$(mtime "$STATE_DIR/$pres")"

  echo "[$TAG] restarting $label"
  launchctl kickstart -k "gui/$UID_NUM/$label"

  # Stage 1 — did the new code boot? BootWitness writes early in the
  # supervision tree, so this fails fast on a boot crash.
  booted() { [ "$(mtime "$STATE_DIR/$bw")" -gt "$BW_BEFORE" ]; }
  echo "[$TAG]   waiting for boot witness (up to 60s)"
  if ! deploy_lib_poll 20 3 booted; then
    echo "[$TAG] FAILED — $label never wrote a boot witness; the new code did not start." >&2
    echo "[$TAG] Rolling the worktree back to ${PREV:0:8} and restarting both instances." >&2
    git -C "$DEPLOY" reset --hard "$PREV"
    for e2 in "${INSTANCES[@]}"; do
      IFS='|' read -r l2 _ _ _ <<<"$e2"
      launchctl kickstart -k "gui/$UID_NUM/$l2"
    done
    echo "[$TAG] rolled back. Investigate: tail -80 $LOG_DIR/$log" >&2
    exit 1
  fi

  # Stage 2 — did it actually reach Discord? Only meaningful with a network;
  # see the reachability gate above.
  if [ "$NET_OK" = 1 ]; then
    connected() { [ "$(mtime "$STATE_DIR/$pres")" -gt "$PRES_BEFORE" ]; }
    # Presence writes are throttled to 60s and the liveness check runs every
    # 30s, so the first post-boot write can legitimately be ~90s out. 150s is
    # ~1.5 write intervals past that, not a hair trigger.
    echo "[$TAG]   waiting for the gateway to connect (up to 150s)"
    if ! deploy_lib_poll 30 5 connected; then
      echo "[$TAG] FAILED — $label booted but never connected to Discord." >&2
      echo "[$TAG] Rolling the worktree back to ${PREV:0:8} and restarting both instances." >&2
      git -C "$DEPLOY" reset --hard "$PREV"
      for e2 in "${INSTANCES[@]}"; do
        IFS='|' read -r l2 _ _ _ <<<"$e2"
        launchctl kickstart -k "gui/$UID_NUM/$l2"
      done
      echo "[$TAG] rolled back. Investigate: tail -80 $LOG_DIR/$log" >&2
      exit 1
    fi
  fi

  printf '%s:%s' "$HEAD_SHA" "$(label_pid "$label")" >"$marker"
  restarted="$restarted $label"
done

echo "[$TAG] OK — serving from $DEPLOY @ ${HEAD_SHA:0:8}"
echo "[$TAG]   restarted:${restarted:-  none}"
echo "[$TAG]   unchanged:${skipped:-  none}"
[ "$NET_OK" = 1 ] || echo "[$TAG]   NOTE: gateway connection unverified (no network at deploy time)."
