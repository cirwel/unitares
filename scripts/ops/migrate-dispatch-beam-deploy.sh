#!/usr/bin/env bash
# ONE-TIME migration: move both dispatch_beam LaunchAgents off the dev checkout
# and onto a pinned deploy worktree, with their persistent state relocated
# outside every checkout.
#
# Run this once. After it, deploy-dispatch-beam.sh works and `cirwel update`
# stops skipping dispatch-beam / dispatch-beam-codex.
#
# ── What makes this delicate ────────────────────────────────────────────────
#
# dispatch_beam keeps four pieces of state per instance, and by default resolves
# every one of them against `File.cwd!()`:
#
#   data/dispatch_snapshot.bin   thread_id -> claude --resume id   (19 threads)
#   data/governance_identity.json  harness identity lineage
#   data/boot_witness.bin        boot counter / last-boot clock
#   data/presence.bin            last-connected clock
#
# Change the working directory without repointing those and the bot starts
# against an EMPTY snapshot. That failure is silent by construction:
# `Dispatch.Discord.Consumer.handle_event/1` tests `known_thread?`, which is
# `Snapshot.get(cid) != nil`, and falls through to `:noop` logging NOTHING. Both
# bots would look perfectly healthy — process up, gateway connected, heartbeats
# fine — while every existing thread ignored every message. That exact shape
# already cost a day on 2026-08-02.
#
# So state moves OUT of the checkouts entirely, to ~/.local/state/dispatch-beam,
# and both plists pin all four paths absolutely. Same reasoning as the bridge's
# secrets: state that lives in the dev checkout makes the DEPLOY worktree depend
# on the dev tree still existing, and deleting a dev checkout should never be
# able to kill a live service.
#
# The dev checkout keeps working: data/ and .env* become symlinks into the
# shared state dir, so a manual scripts/start.sh still reads the same files it
# reads today. One real copy, never two that can drift.
#
# Idempotent and reversible. Every plist is backed up to <plist>.pre-deploy-bak
# before it is touched. Re-running after a partial failure resumes cleanly.
set -euo pipefail

REPO="${DISPATCH_REPO:-$HOME/projects/dispatch_beam}"
DEPLOY="${DISPATCH_DEPLOY:-$HOME/projects/dispatch_beam-deploy}"
STATE_DIR="${DISPATCH_STATE_DIR:-$HOME/.local/state/dispatch-beam}"
UID_NUM="$(id -u)"
TAG="migrate-dispatch-beam"
PB=/usr/libexec/PlistBuddy

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1
[ "${1:-}" = "-n" ] && DRY_RUN=1

# label | env-var prefix suffix ("" for claude, "_codex" for codex)
INSTANCES=("com.cirwel.dispatch-beam|" "com.cirwel.dispatch-beam-codex|_codex")

say() { echo "[$TAG] $*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "[$TAG] DRY  $*"
  else
    "$@"
  fi
}
# PlistBuddy needs a single -c string, so it cannot go through run() verbatim.
pb() {
  local plist="$1" cmd="$2"
  if [ "$DRY_RUN" = 1 ]; then
    echo "[$TAG] DRY  PlistBuddy -c '$cmd' $(basename "$plist")"
  else
    "$PB" -c "$cmd" "$plist" >/dev/null
  fi
}
# Set if the key exists, Add if it does not — PlistBuddy has no upsert.
pb_set() {
  local plist="$1" key="$2" value="$3"
  if "$PB" -c "Print $key" "$plist" >/dev/null 2>&1; then
    pb "$plist" "Set $key $value"
  else
    pb "$plist" "Add $key string $value"
  fi
}

[ "$DRY_RUN" = 1 ] && say "DRY RUN — nothing will be changed."

# --- preflight --------------------------------------------------------------
[ -d "$REPO/.git" ] || { say "REFUSING: no git checkout at $REPO"; exit 1; }
[ -x "$PB" ] || { say "REFUSING: PlistBuddy not found at $PB"; exit 1; }

for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label _sfx <<<"$entry"
  [ -f "$HOME/Library/LaunchAgents/$label.plist" ] || {
    say "REFUSING: $label.plist not installed"; exit 1; }
done

# The snapshot is the piece whose loss is silent, so its presence is the
# precondition worth naming explicitly.
if [ ! -f "$REPO/data/dispatch_snapshot.bin" ] && [ ! -f "$STATE_DIR/dispatch_snapshot.bin" ]; then
  say "REFUSING: no dispatch_snapshot.bin in $REPO/data or $STATE_DIR."
  say "Migrating now would deploy both bots against an empty thread table."
  exit 1
fi

say "repo:   $REPO"
say "deploy: $DEPLOY"
say "state:  $STATE_DIR"

# --- 1. stop both services --------------------------------------------------
# Before ANY state moves. A running bot writes presence.bin every 60s and the
# snapshot on every turn; moving files out from under it would lose whichever
# write lands mid-move.
say "step 1/6: stopping both instances"
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label _sfx <<<"$entry"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  run launchctl unload "$plist"
done

# --- 2. relocate state ------------------------------------------------------
say "step 2/6: relocating state to $STATE_DIR"
run mkdir -p "$STATE_DIR"

if [ -d "$REPO/data" ] && [ ! -L "$REPO/data" ]; then
  # mv the CONTENTS, not the directory: a partially-completed earlier run may
  # have left $STATE_DIR already populated, and this must resume rather than
  # nest data/ inside itself.
  if [ "$DRY_RUN" = 1 ]; then
    say "DRY  would move $(find "$REPO/data" -maxdepth 1 -type f | wc -l | tr -d ' ') file(s) from $REPO/data to $STATE_DIR"
  else
    find "$REPO/data" -maxdepth 1 -type f -exec mv -n {} "$STATE_DIR/" \;
    rmdir "$REPO/data" 2>/dev/null || say "note: $REPO/data not empty, leaving it"
  fi
  # The dev checkout keeps working against the same files it always read.
  [ -e "$REPO/data" ] || run ln -sfn "$STATE_DIR" "$REPO/data"
else
  say "  state already relocated (or data/ is already a symlink)"
fi

# .env* are gitignored, so the deploy worktree will never carry them. 0600 and
# outside every checkout, exactly like the bridge's secrets file.
for env_name in .env .env.codex; do
  if [ -f "$REPO/$env_name" ] && [ ! -L "$REPO/$env_name" ]; then
    say "  moving $env_name to $STATE_DIR"
    run mv -n "$REPO/$env_name" "$STATE_DIR/$env_name"
    run chmod 600 "$STATE_DIR/$env_name"
    run ln -sfn "$STATE_DIR/$env_name" "$REPO/$env_name"
  fi
done

# --- 3. create the deploy worktree -----------------------------------------
say "step 3/6: creating the deploy worktree"
if [ -d "$DEPLOY/.git" ] || [ -f "$DEPLOY/.git" ]; then
  say "  worktree already exists at $DEPLOY"
else
  # --detach: git allows one checkout of a branch per repo and $REPO is itself
  # on master, so branch mode would refuse.
  run git -C "$REPO" fetch origin --quiet
  run git -C "$REPO" worktree add --detach "$DEPLOY" origin/master
fi
for env_name in .env .env.codex; do
  run ln -sfn "$STATE_DIR/$env_name" "$DEPLOY/$env_name"
done

# --- 4. rewrite both plists -------------------------------------------------
say "step 4/6: repointing both LaunchAgents at the deploy worktree"
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label sfx <<<"$entry"
  plist="$HOME/Library/LaunchAgents/$label.plist"

  [ -f "$plist.pre-deploy-bak" ] || run cp "$plist" "$plist.pre-deploy-bak"

  pb_set "$plist" ":WorkingDirectory" "$DEPLOY"
  pb_set "$plist" ":ProgramArguments:0" "$DEPLOY/scripts/run.sh"

  # Pin all four state paths ABSOLUTELY, for BOTH instances. The codex plist
  # already pinned them (at the dev checkout); the claude plist relied on the
  # cwd fallback, which is precisely what breaks when cwd moves. After this,
  # neither instance depends on its working directory for state.
  pb_set "$plist" ":EnvironmentVariables:DISPATCH_SNAPSHOT_PATH" "$STATE_DIR/dispatch_snapshot${sfx}.bin"
  pb_set "$plist" ":EnvironmentVariables:DISPATCH_GOVERNANCE_ID_PATH" "$STATE_DIR/governance_identity${sfx}.json"
  pb_set "$plist" ":EnvironmentVariables:DISPATCH_BOOT_WITNESS_PATH" "$STATE_DIR/boot_witness${sfx}.bin"
  pb_set "$plist" ":EnvironmentVariables:DISPATCH_PRESENCE_PATH" "$STATE_DIR/presence${sfx}.bin"
done

# --- 5. reload --------------------------------------------------------------
# A RELOAD, not a kickstart: kickstart does not re-read the plist, so the edits
# above would not take effect and the bots would restart on the old paths.
say "step 5/6: reloading both LaunchAgents"
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label _sfx <<<"$entry"
  run launchctl load "$HOME/Library/LaunchAgents/$label.plist"
done

# --- 6. verify --------------------------------------------------------------
say "step 6/6: verifying"
if [ "$DRY_RUN" = 1 ]; then
  say "DRY  would wait for both instances to write a boot witness and connect"
  say "DRY RUN complete — nothing changed. Re-run without --dry-run to apply."
  exit 0
fi

ok=1
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label sfx <<<"$entry"
  for _ in $(seq 1 20); do
    pid="$(launchctl list 2>/dev/null | awk -v l="$label" '$3 == l { print $1 }')"
    [ -n "$pid" ] && [ "$pid" != "-" ] && break
    sleep 3
  done
  if [ -n "${pid:-}" ] && [ "$pid" != "-" ]; then
    say "  $label up (pid $pid)"
  else
    say "  $label did NOT come up" >&2
    ok=0
  fi
done

# The snapshot is the thing whose silent loss this migration exists to prevent,
# so confirm the live process actually found it rather than trusting the move.
if [ -f "$STATE_DIR/dispatch_snapshot.bin" ]; then
  say "  snapshot present at $STATE_DIR/dispatch_snapshot.bin"
else
  say "  WARNING: no snapshot at $STATE_DIR — threads will not rehydrate" >&2
  ok=0
fi

if [ "$ok" = 1 ]; then
  say "OK — both instances now serve from $DEPLOY with state in $STATE_DIR."
  say "Next: cirwel status should show dispatch-beam deployable, and"
  say "      deploy-dispatch-beam.sh now handles it."
else
  say "INCOMPLETE — restore with:" >&2
  for entry in "${INSTANCES[@]}"; do
    IFS='|' read -r label _sfx <<<"$entry"
    plist="$HOME/Library/LaunchAgents/$label.plist"
    echo "[$TAG]   launchctl unload \"$plist\" && cp \"$plist.pre-deploy-bak\" \"$plist\" && launchctl load \"$plist\"" >&2
  done
  exit 1
fi
