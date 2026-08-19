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
# Two more things live in a checkout that must not:
#
#   * deps/ and _build/ are gitignored, so a fresh worktree has neither, and
#     scripts/run.sh is a bare `mix run --no-halt`. The worktree is therefore
#     BUILT before either bot is stopped — otherwise the reload starts a
#     KeepAlive respawn loop, not a bot, and the compile runs during the outage
#     instead of before it.
#   * an installed plist can itself be a SYMLINK into a checkout (the claude one
#     was, at a TRACKED file). Editing through it puts the live launchd config
#     under version control, where an ordinary `git checkout` reverts the
#     running service. Such a link is replaced with a real file first.
#
# Idempotent and reversible. Every plist is backed up to <plist>.pre-deploy-bak
# before it is touched. Re-running after a partial failure resumes cleanly.
set -euo pipefail

REPO="${DISPATCH_REPO:-$HOME/projects/dispatch_beam}"
DEPLOY="${DISPATCH_DEPLOY:-$HOME/projects/dispatch_beam-deploy}"
STATE_DIR="${DISPATCH_STATE_DIR:-$HOME/.local/state/dispatch-beam}"
LOG_DIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"
TAG="migrate-dispatch-beam"
PB=/usr/libexec/PlistBuddy
# mix/elixir must resolve even when this is invoked from a bare shell.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

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

# Decode a persisted snapshot and report how many threads it holds. Plain
# binary_to_term, NOT :safe — Snapshot.restore/2 uses :safe and that is exactly
# what quarantined a perfectly good file on 2026-08-02 (the atoms it refuses to
# create are not yet interned on a cold VM). Here we only ever READ.
snapshot_entry_count() {
  local f="$1"
  [ -f "$f" ] || { echo ""; return; }
  erl -noshell -eval "
    case file:read_file(\"$f\") of
      {ok,B} -> try io:format(\"~p\",[length(binary_to_term(B))]) catch _:_ -> ok end;
      _ -> ok
    end, halt()." 2>/dev/null
}

# Ask the RUNNING node how many threads its live table actually holds. This is
# the only check that distinguishes "the file was moved" from "the bot can
# answer in those threads" — an empty table makes known_thread? false
# everywhere and handle_event/1 falls through to :noop, logging NOTHING while
# process, gateway and heartbeats all read perfectly healthy.
live_snapshot_size() {
  local sname="$1" host cookie
  host="$(hostname -s)"
  cookie="$HOME/.erlang.cookie"
  [ -f "$cookie" ] || { echo ""; return; }
  erl -sname "migrate_probe_$$" -setcookie "$(cat "$cookie")" -noshell -eval "
    N = list_to_atom(\"${sname}@${host}\"),
    case net_adm:ping(N) of
      pong -> io:format(\"~p\", [rpc:call(N, ets, info, [dispatch_snapshot, size])]);
      pang -> ok
    end, halt()." 2>/dev/null
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

# Recorded BEFORE anything moves, so step 7 can compare the live table against
# what was actually on disk rather than against nothing.
EXPECT_claude=""; EXPECT_codex=""
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r _label sfx <<<"$entry"
  for base in "$STATE_DIR" "$REPO/data"; do
    [ -f "$base/dispatch_snapshot${sfx}.bin" ] || continue
    n="$(snapshot_entry_count "$base/dispatch_snapshot${sfx}.bin")"
    [ -n "$n" ] && { [ -z "$sfx" ] && EXPECT_claude="$n" || EXPECT_codex="$n"; break; }
  done
done
say "threads on disk: claude=${EXPECT_claude:-?} codex=${EXPECT_codex:-?}"

# --- 1. create and PROVISION the deploy worktree -----------------------------
# First, and while both bots are still UP, because this is the slow part and
# none of it touches anything live.
#
# ⛔The provisioning is not optional. deps/ and _build/ are gitignored, so a
# fresh worktree has NEITHER, and scripts/run.sh is a bare
# `exec elixir -S mix run --no-halt` — no deps.get, no compile. Reloading onto
# an unprovisioned worktree does not start a bot: it starts a KeepAlive respawn
# loop against "Cannot find dependency", every 15s (ThrottleInterval), forever.
# That is the same failure that a plist-only repoint of wave3a-handlers caused
# on 2026-06-22. Doing it here rather than after the stop also means the compile
# happens on the operator's clock instead of on the outage's.
say "step 1/7: creating and provisioning the deploy worktree"
if [ -d "$DEPLOY/.git" ] || [ -f "$DEPLOY/.git" ]; then
  say "  worktree already exists at $DEPLOY"
else
  # --detach: git allows one checkout of a branch per repo and $REPO is itself
  # on master, so branch mode would refuse.
  run git -C "$REPO" fetch origin --quiet
  run git -C "$REPO" worktree add --detach "$DEPLOY" origin/master
fi

if [ "$DRY_RUN" = 1 ]; then
  say "DRY  would run mix deps.get && mix compile in $DEPLOY"
else
  say "  fetching dependencies (mix deps.get)"
  (cd "$DEPLOY" && mix deps.get >/dev/null) || {
    say "REFUSING: mix deps.get failed in $DEPLOY. Nothing has been changed."; exit 1; }
  # The env run.sh actually boots is the DEFAULT one (it sets no MIX_ENV), so
  # that is the env that must be built here. deploy-dispatch-beam.sh separately
  # gates MIX_ENV=prod on every later deploy; this only has to guarantee that
  # the very first launchd start finds a compiled tree.
  say "  compiling (mix compile --warnings-as-errors)"
  (cd "$DEPLOY" && mix compile --warnings-as-errors >/dev/null) || {
    say "REFUSING: compile failed in $DEPLOY. Nothing has been changed."; exit 1; }
fi

# --- 2. stop both services --------------------------------------------------
# Before ANY state moves. A running bot writes presence.bin every 60s and the
# snapshot on every turn; moving files out from under it would lose whichever
# write lands mid-move.
say "step 2/7: stopping both instances"
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label _sfx <<<"$entry"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  run launchctl unload "$plist"
done

# --- 3. relocate state ------------------------------------------------------
say "step 3/7: relocating state to $STATE_DIR"
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

# --- 4. link the secrets into the deploy worktree ---------------------------
# After the relocation above, never before: linking first would point the
# deploy tree at a path that does not exist yet, and a dangling .env makes
# run.sh source nothing and the bot boot with an empty DISCORD_BOT_TOKEN.
say "step 4/7: linking secrets into the deploy worktree"
for env_name in .env .env.codex; do
  run ln -sfn "$STATE_DIR/$env_name" "$DEPLOY/$env_name"
done

# --- 5. rewrite both plists -------------------------------------------------
say "step 5/7: repointing both LaunchAgents at the deploy worktree"
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label sfx <<<"$entry"
  plist="$HOME/Library/LaunchAgents/$label.plist"

  # ⛔An installed plist may be a SYMLINK into a checkout. com.cirwel.dispatch-
  # beam.plist was exactly that on 2026-08-18 —
  #   ~/Library/LaunchAgents/com.cirwel.dispatch-beam.plist
  #     -> ~/projects/dispatch_beam/com.cirwel.dispatch-beam.plist  (TRACKED)
  # — so PlistBuddy edited a git working tree, and a later `git checkout` or
  # branch switch in the dev checkout would silently revert the LIVE service to
  # the pre-migration paths, re-arming the empty-snapshot trap this whole script
  # exists to disarm. It is the same rule as the state dir and the secrets file:
  # nothing launchd reads may live inside a checkout. De-link BEFORE the backup,
  # so the backup captures the real pre-migration config.
  if [ -L "$plist" ]; then
    say "  $label.plist is a symlink -> $(readlink "$plist"); replacing with a real file"
    if [ "$DRY_RUN" = 1 ]; then
      say "DRY  would copy the symlink target's content to a real file at $plist"
    else
      tmp="$(mktemp)"
      cp "$plist" "$tmp"          # follows the link, captures current content
      rm "$plist"                 # drops the LINK only; the checkout file stays
      cp "$tmp" "$plist"
      rm "$tmp"
      chmod 644 "$plist"
    fi
  fi

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

# --- 6. reload --------------------------------------------------------------
# A RELOAD, not a kickstart: kickstart does not re-read the plist, so the edits
# above would not take effect and the bots would restart on the old paths.
say "step 6/7: reloading both LaunchAgents"
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label _sfx <<<"$entry"
  run launchctl load "$HOME/Library/LaunchAgents/$label.plist"
done

# --- 7. verify --------------------------------------------------------------
say "step 7/7: verifying"
if [ "$DRY_RUN" = 1 ]; then
  say "DRY  would wait for both instances to write a boot witness and connect"
  say "DRY RUN complete — nothing changed. Re-run without --dry-run to apply."
  exit 0
fi

ok=1
pid_of() { launchctl list 2>/dev/null | awk -v l="$1" '$3 == l { print $1 }'; }

for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label sfx <<<"$entry"
  for _ in $(seq 1 20); do
    pid="$(pid_of "$label")"
    [ -n "$pid" ] && [ "$pid" != "-" ] && break
    sleep 3
  done
  if [ -z "${pid:-}" ] || [ "$pid" = "-" ]; then
    say "  $label did NOT come up" >&2
    ok=0
    continue
  fi
  # ⛔A pid is not evidence of a healthy start. KeepAlive respawns on a 15s
  # ThrottleInterval, so a bot crash-looping against a missing dependency or a
  # bad token HAS a pid most of the time it is sampled — the first check here
  # used to pass on exactly that. Require the SAME pid twice, ~10s apart.
  sleep 10
  pid2="$(pid_of "$label")"
  if [ "$pid2" != "$pid" ]; then
    say "  $label is RESPAWNING (pid $pid -> ${pid2:-none}) — not a healthy start" >&2
    say "  check $LOG_DIR/${label#com.cirwel.}.log" >&2
    ok=0
    continue
  fi
  say "  $label up and stable (pid $pid)"
done

# The snapshot is the thing whose silent loss this migration exists to prevent.
# File-presence does NOT establish that: the process can boot, connect, and
# heartbeat perfectly while holding an EMPTY table. Ask the live node.
for entry in "${INSTANCES[@]}"; do
  IFS='|' read -r label sfx <<<"$entry"
  [ -z "$sfx" ] && expect="$EXPECT_claude" || expect="$EXPECT_codex"
  sname="dispatch_beam${sfx}"

  if [ ! -f "$STATE_DIR/dispatch_snapshot${sfx}.bin" ]; then
    say "  WARNING: no dispatch_snapshot${sfx}.bin at $STATE_DIR — threads will not rehydrate" >&2
    ok=0
    continue
  fi

  live="$(live_snapshot_size "$sname")"
  if [ -z "$live" ] || [ "$live" = "undefined" ]; then
    # Loud, not silent, and NOT counted as a pass: an unreachable node here
    # means the strongest check did not run, which is different from it passing.
    say "  UNVERIFIED: could not read $sname's live table (no erl, no cookie, or" >&2
    say "  node unreachable). Confirm by hand before trusting those threads:" >&2
    say "    tail -f $LOG_DIR/${label#com.cirwel.}.log   # then reply in a thread" >&2
    ok=0
  elif [ -n "$expect" ] && [ "$live" != "$expect" ]; then
    say "  MISMATCH: $sname live table holds $live thread(s), disk held $expect" >&2
    ok=0
  else
    say "  $sname rehydrated ${live} thread(s)${expect:+ (disk: $expect)}"
  fi
done

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
