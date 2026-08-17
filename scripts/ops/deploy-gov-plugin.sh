#!/usr/bin/env bash
# deploy-gov-plugin.sh — advance the unitares-governance plugin checkout to
# origin/master. For this service, that IS the deploy.
#
# Why this exists: gov-plugin had a deploy-status.sh row but no deploy script,
# so deploy-apply.sh could only REPORT it — as "SKIP ... no deploy script", on
# stderr, in the middle of a sweep that prints a reassuring summary afterwards.
# The practical consequence was that `cirwel update` deployed everything except
# this, and the plugin only moved when someone remembered to move it by hand.
# Measured 2026-08-17: the checkout sat 3 commits behind on a stale `codex/*`
# branch (#118/#119/#120 unmerged into the live hooks) while the Codex plugin
# cache had already refreshed to the newer build — the two principals had
# drifted apart, with Claude the stale one.
#
# THREE THINGS MAKE THIS UNLIKE EVERY OTHER PER-SERVICE DEPLOY SCRIPT, and each
# rules out a piece of the usual machinery:
#
#   1. NO DEPLOY WORKTREE. The plugin is installed with source=directory (see
#      known_marketplaces.json), so the checkout's working tree IS the live hook
#      code — there is no copy to fast-forward into. deploy_lib_ff_worktree
#      therefore does not apply and the pull happens in place. This is also why
#      deploy-status.sh calls the pickup `live-from-checkout` and why `behind`
#      is the ONLY drift signal that exists here: there is no process to be
#      STALE against.
#   2. NO LaunchAgent. Nothing to kickstart, so no plist preflight and no health
#      probe — a passing probe would mean nothing anyway. The consumer is the
#      operator's agent process, which reads hooks at SESSION START, so a deploy
#      lands in the NEXT session and running sessions keep the code they started
#      with. "Deployed" here means the tree is current, not that anything
#      restarted.
#   3. IT IS SOMEONE'S WORKING CHECKOUT. Every other deploy target is a
#      dedicated tree nobody edits. This one is where plugin work actually
#      happens, and a branch or dirty file left there silently becomes the live
#      hook code for every session on the machine. So the guards below are the
#      substance of this script, not preamble: an unattended sweep must never
#      discard in-flight work in order to make a hook current. Every guard
#      refuses and exits non-zero rather than guessing.
#
# Per-principal caveat this script CANNOT fix: a second agent principal may run
# the plugin from its own version-pinned cache copy rather than this checkout.
# Advancing the checkout deploys the change for the directory-source principal
# only. This script reports what the cache holds so the difference is visible,
# and deliberately does not touch it — that cache is refreshed by its own host.
#
# Flags:
#   --dry-run   report what would move; change nothing
set -euo pipefail

REPO="${GOV_PLUGIN_REPO:-$HOME/projects/unitares-governance-plugin}"
TRUNK="${GOV_PLUGIN_TRUNK:-master}"
# Reported, never written. Empty or missing is normal on a machine that runs
# only the directory-source principal.
CODEX_CACHE="${GOV_PLUGIN_CODEX_CACHE:-$HOME/.codex/plugins/cache/unitares-governance/unitares-governance}"
TAG="deploy-gov-plugin"

DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

# Keyed on the plugin checkout, so this never contends with a governance-MCP or
# lease-plane deploy — different repo, no shared worktree. It exists because two
# concurrent sweeps would otherwise race `git checkout` on the same index.
deploy_lib_acquire_lock "$TAG" "$REPO"

# rev-parse, not `[[ -d $REPO/.git ]]`: in a git worktree .git is a FILE, and
# the directory test would refuse a perfectly valid checkout.
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || { echo "[$TAG] not a git checkout: $REPO" >&2; exit 1; }

# Capture status output and its EXIT CODE separately. `2>/dev/null` alone would
# turn a FAILED `git status` (index.lock held by a concurrent git process —
# exactly the "another session is mid-operation" case these guards exist for)
# into empty output, indistinguishable from "clean", so the guard would wave
# through the one situation it was written to stop.
if ! dirty="$(git -C "$REPO" status --porcelain 2>&1)"; then
  echo "[$TAG] cannot read git status in $REPO — refusing to move it." >&2
  echo "[$TAG] $dirty" >&2
  exit 1
fi
if [[ -n "$dirty" ]]; then
  echo "[$TAG] $REPO has uncommitted changes — refusing to move it." >&2
  echo "[$TAG] those edits ARE the live hooks right now; another session may be" >&2
  echo "[$TAG] working there. Check: git -C $REPO status" >&2
  exit 1
fi

# Fetch must SUCCEED. Offline, a stale cached origin/$TRUNK does not error — it
# answers confidently and wrongly, and "already current" would then be a
# freshness claim this script never verified. Same reasoning as the deploy
# scripts' plist preflight: refuse rather than report a false success.
if ! git -C "$REPO" fetch origin "$TRUNK" --quiet; then
  echo "[$TAG] git fetch failed — cannot confirm what origin/$TRUNK is. NOT deploying." >&2
  exit 1
fi

branch="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
before="$(git -C "$REPO" rev-parse --short HEAD)"
target="$(git -C "$REPO" rev-parse --short "origin/$TRUNK")"

# Guard: does anything reachable from here NOT exist on origin/$TRUNK? If so
# this checkout is carrying work — unpushed commits, or a branch whose PR has
# not landed — and moving it would take that work out of the live hooks with no
# record of what changed.
#
# This FAILS CLOSED on squash-merged branches: a squash rewrites the commits, so
# an already-landed branch still reports unique commits and this refuses. That
# is the intended trade. `--is-ancestor` would call such a branch unmerged too,
# and the alternative — comparing trees to detect an equivalent squash — is
# exactly the kind of cleverness that decides on its own to discard someone's
# commits. An operator resolving it by hand costs a minute; the other error
# costs the work.
unique_head="$(git -C "$REPO" rev-list --count "origin/$TRUNK..HEAD")"
if [[ "$unique_head" != "0" ]]; then
  echo "[$TAG] $REPO is on '$branch' with $unique_head commit(s) not on origin/$TRUNK — refusing." >&2
  git -C "$REPO" log --oneline "origin/$TRUNK..HEAD" | sed "s/^/[$TAG]   /" >&2
  echo "[$TAG] push and land them, or move the checkout by hand once you know they are safe." >&2
  exit 1
fi

# Same guard for the LOCAL trunk branch, which is where we are about to land.
# `git pull --ff-only` does NOT catch this: if local $TRUNK is AHEAD of origin,
# the pull reports "Already up to date" and succeeds, quietly deploying unpushed
# commits nobody reviewed. Checked before the checkout so a refusal leaves the
# checkout exactly where it was.
if git -C "$REPO" show-ref --verify --quiet "refs/heads/$TRUNK"; then
  unique_trunk="$(git -C "$REPO" rev-list --count "origin/$TRUNK..$TRUNK")"
  if [[ "$unique_trunk" != "0" ]]; then
    echo "[$TAG] local $TRUNK has $unique_trunk unpushed commit(s) — refusing to deploy it." >&2
    git -C "$REPO" log --oneline "origin/$TRUNK..$TRUNK" | sed "s/^/[$TAG]   /" >&2
    exit 1
  fi
fi

report_cache() {
  [[ -d "$CODEX_CACHE" ]] || return 0
  local versions
  versions="$(ls -1 "$CODEX_CACHE" 2>/dev/null | tr '\n' ' ')"
  [[ -n "$versions" ]] || return 0
  echo "[$TAG] note: a cache-copy principal holds version(s): ${versions% }"
  echo "[$TAG]       that copy refreshes on its own — this deploy did not touch it."
}

if [[ "$branch" == "$TRUNK" && "$before" == "$target" ]]; then
  echo "[$TAG] already current ($TRUNK@$before)"
  report_cache
  exit 0
fi

behind="$(git -C "$REPO" rev-list --count "HEAD..origin/$TRUNK")"
if [[ "$DRY_RUN" == 1 ]]; then
  echo "[$TAG] DRY would move $REPO: $branch@$before -> $TRUNK@$target (behind=$behind)"
  git -C "$REPO" log --oneline "HEAD..origin/$TRUNK" | sed "s/^/[$TAG]   /"
  report_cache
  exit 0
fi

echo "[$TAG] $branch@$before -> $TRUNK@$target (behind=$behind)"
if [[ "$branch" != "$TRUNK" ]]; then
  if git -C "$REPO" show-ref --verify --quiet "refs/heads/$TRUNK"; then
    git -C "$REPO" checkout "$TRUNK" --quiet
  else
    git -C "$REPO" checkout -b "$TRUNK" --track "origin/$TRUNK" --quiet
  fi
fi
git -C "$REPO" merge --ff-only "origin/$TRUNK" --quiet

after="$(git -C "$REPO" rev-parse --short HEAD)"
if [[ "$after" != "$target" ]]; then
  echo "[$TAG] FAILED: expected $target, checkout is at $after" >&2
  exit 1
fi

git -C "$REPO" log --oneline "$before..$after" | sed "s/^/[$TAG]   /"
echo "[$TAG] OK — $REPO now at $TRUNK@$after"
echo "[$TAG] hooks load at SESSION START: this lands in the next session, not running ones."
report_cache
