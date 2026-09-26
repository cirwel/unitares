#!/usr/bin/env bash
# open-plugin-skill-sync-pr.sh — keep the plugin's skill mirror current.
#
# unitares/skills/ is canonical; unitares-governance-plugin/skills/ is a byte
# mirror written by unitares' own scripts/dev/sync-plugin-skills.sh. Nothing ran
# that sync automatically, and the plugin's CI only checks the mirror against
# its OWN manifest, so a stale mirror passed CI. On 2026-09-25/26 the mirror fell
# behind twice in one night and was caught only by comparing by hand.
#
# This script is the plumbing around sync-plugin-skills.sh, not a second copy
# of it, and it is the ONE implementation both callers share:
#   * .github/workflows/plugin-skill-sync.yml, on every push to master that
#     touches skills/ (needs the PLUGIN_SYNC_TOKEN secret to push);
#   * an operator's local deploy wrapper, after it deploys.
# Both push the same automation branch, so they can never open two PRs.
#
#   1. fetch both repos; check out unitares origin/master and the plugin's
#      default branch in two temporary worktrees;
#   2. run unitares' sync script into the plugin worktree;
#   3. no change    -> report "in sync", done;
#      change       -> commit on ONE automation branch, replace it on the remote
#                      (compare-and-swap), and open a PR, or let the push update
#                      the PR that is already open.
# The branch always holds "the mirror at the latest unitares master", so
# replacing it on every run is correct and there is never more than one PR.
#
# It never merges anything.
#
# usage: open-plugin-skill-sync-pr.sh [--dry-run]
#   --dry-run   report what would change; no commit, no push, no PR
#
# env:  UNITARES_REPO_DIR       unitares checkout to fetch from (default: this repo)
#       UNITARES_PLUGIN_REPO   plugin checkout (default: ../unitares-governance-plugin)
#       SKILL_SYNC_WT_ROOT     where the temporary worktrees go
#                              (default: $RUNNER_TEMP, else $TMPDIR, else /tmp)
#       SKILL_SYNC_BRANCH      automation branch (default auto/plugin-skill-sync)
#       SKILL_SYNC_GH          gh binary (tests substitute a fake)
#
# exit: 0 in sync / PR opened or updated / dry-run
#       1 could not complete (a deploy wrapper warns and carries on; the
#         workflow run fails, which is how a broken sync gets noticed)
#       2 skipped: another open PR is already syncing the mirror
set -uo pipefail

SELF_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNITARES_REPO="${UNITARES_REPO_DIR:-$SELF_ROOT}"
PLUGIN_REPO="${UNITARES_PLUGIN_REPO:-$SELF_ROOT/../unitares-governance-plugin}"
# An operator with a worktree convention passes their own root.
WT_ROOT="${SKILL_SYNC_WT_ROOT:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}}"
WT_ROOT="${WT_ROOT%/}"
BRANCH="${SKILL_SYNC_BRANCH:-auto/plugin-skill-sync}"
GH="${SKILL_SYNC_GH:-gh}"
SYNC_TITLE_PREFIX="chore(skills): sync mirror"

DRY_RUN=""
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) printf 'open-plugin-skill-sync-pr: unknown option %s\n' "$1" >&2; exit 1 ;;
esac

say() { printf '  skills: %s\n' "$*"; }
fail() { printf '  skills: %s\n' "$*" >&2; exit 1; }

for repo in "$UNITARES_REPO" "$PLUGIN_REPO"; do
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || fail "no git checkout at $repo — skipping"
done
mkdir -p "$WT_ROOT" || fail "cannot create $WT_ROOT"

# Explicit refspecs: a CI checkout is shallow and may lack remote-tracking refs.
git -C "$UNITARES_REPO" fetch -q origin "+refs/heads/master:refs/remotes/origin/master" || fail "fetch unitares failed"
# The plugin's default branch, asked of the remote itself: a CI checkout has no
# refs/remotes/origin/HEAD, so reading that alone would silently assume master.
BASE="$(git -C "$PLUGIN_REPO" ls-remote --symref origin HEAD 2>/dev/null \
  | sed -n 's#^ref: refs/heads/\([^[:space:]]*\)[[:space:]].*#\1#p' | head -1)"
if [ -z "$BASE" ]; then
  BASE="$(git -C "$PLUGIN_REPO" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null)"
  BASE="${BASE#origin/}"
fi
[ -n "$BASE" ] || fail "cannot tell the plugin's default branch"
git -C "$PLUGIN_REPO" fetch -q origin "+refs/heads/$BASE:refs/remotes/origin/$BASE" || fail "fetch plugin $BASE failed"
SRC_SHA="$(git -C "$UNITARES_REPO" rev-parse --short=8 origin/master)" || fail "no unitares origin/master"

# Where the automation branch stands now. The push below is a compare-and-swap
# against this value, so a concurrent run that pushed in between makes this one
# fail instead of silently replacing its work. Empty means "must not exist".
REMOTE_OID="$(git -C "$PLUGIN_REPO" ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1)"

# Unique per run, so two runs (or another session) can never share a path. A
# failed `worktree add` stops everything: on 2026-09-25 two sessions picked the
# same worktree path in the same second and one synced into the other's tree.
SRC_WT="$WT_ROOT/unitares-skillsync-src-$$"
DST_WT="$WT_ROOT/unitares-governance-plugin-skillsync-$$"
for p in "$SRC_WT" "$DST_WT"; do
  [ ! -e "$p" ] || fail "$p already exists — refusing to touch it"
done

cleanup() {
  # Only paths this run created. --force because a dry-run leaves them dirty.
  [ -n "${DST_MADE:-}" ] && git -C "$PLUGIN_REPO" worktree remove --force "$DST_WT" >/dev/null 2>&1
  [ -n "${SRC_MADE:-}" ] && git -C "$UNITARES_REPO" worktree remove --force "$SRC_WT" >/dev/null 2>&1
  return 0
}
trap cleanup EXIT

git -C "$UNITARES_REPO" worktree add -q --detach "$SRC_WT" origin/master >/dev/null 2>&1 \
  || fail "could not create $SRC_WT"
SRC_MADE=1
SYNC="$SRC_WT/scripts/dev/sync-plugin-skills.sh"
[ -f "$SYNC" ] || fail "unitares $SRC_SHA has no scripts/dev/sync-plugin-skills.sh"

# Detached for the sync; the branch is only created when there is something to
# commit, so a dry-run or an in-sync run never moves it.
git -C "$PLUGIN_REPO" worktree add -q --detach "$DST_WT" "origin/$BASE" >/dev/null 2>&1 \
  || fail "could not create $DST_WT"
DST_MADE=1

SYNC_LOG="$(cd "$SRC_WT" && UNITARES_PLUGIN_REPO="$DST_WT" bash "$SYNC" 2>&1)"
SYNC_RC=$?
RULE_NOTE=""
case "$SYNC_RC" in
  0) ;;
  # sync-plugin-skills.sh exit 5: the mirror WAS written, but the plugin's
  # port of the attestation rule disagrees with canonical's. A re-sync cannot
  # fix that (ship.sh treats it as a warning too), so carry on and say so.
  5) RULE_NOTE="the plugin's attestation-rule port disagrees with canonical (sync-plugin-skills.sh exit 5); port src/skill_attestations.py into the plugin's _check_freshness.py"
     printf '  skills: warning: %s\n' "$RULE_NOTE" >&2 ;;
  *) printf '%s\n' "$SYNC_LOG" | tail -5 >&2
     fail "sync-plugin-skills.sh failed (exit $SYNC_RC)" ;;
esac

CHANGED="$(git -C "$DST_WT" status --porcelain -- skills)"
if [ -z "$CHANGED" ]; then
  say "plugin mirror in sync with unitares master ($SRC_SHA)"
  exit 0
fi

# Skill names whose SKILL.md changed, plus a count of everything else.
SKILLS="$(printf '%s\n' "$CHANGED" | sed -n 's#^.. skills/\([^/.][^/]*\)/SKILL\.md$#\1#p' | sort -u | paste -sd, - | sed 's/,/, /g')"
# The manifest is derived from the rest, so it is not counted (matching the
# skills-drift summary an operator's status view shows).
N_FILES="$(printf '%s\n' "$CHANGED" | grep -v '/SKILLS_MANIFEST\.sha256$' | grep -c .)"
SUMMARY="$N_FILES file(s)${SKILLS:+; skills: $SKILLS}"

if [ -n "$DRY_RUN" ]; then
  say "plugin mirror is BEHIND unitares master ($SRC_SHA): $SUMMARY — update would open a sync PR"
  exit 0
fi

# Someone may already be syncing by hand. Two sync PRs race each other and
# conflict on SKILLS_MANIFEST.sha256, so step aside rather than open a second.
OTHER="$(cd "$DST_WT" && "$GH" pr list --state open --search "\"$SYNC_TITLE_PREFIX\" in:title" \
  --json number,headRefName -q ".[] | select(.headRefName != \"$BRANCH\") | .number" 2>/dev/null | head -1)"
if [ -n "$OTHER" ]; then
  say "plugin mirror behind ($SUMMARY), but #$OTHER is already syncing it — not opening another"
  exit 2
fi

FRESH="unknown"
if [ -x "$DST_WT/scripts/check-skill-freshness.sh" ]; then
  if (cd "$DST_WT" && ./scripts/check-skill-freshness.sh >/dev/null 2>&1); then
    FRESH="all skills FRESH"
  else
    FRESH="STALE skills present (canonical needs a re-verify; CI will flag it)"
  fi
fi

TITLE="$SYNC_TITLE_PREFIX with unitares master ($SRC_SHA)"
git -C "$DST_WT" add skills || fail "git add failed"
git -C "$DST_WT" commit -q -m "$TITLE" -m "Byte mirror of unitares/skills at $SRC_SHA via sync-plugin-skills.sh ($SUMMARY). Opened by open-plugin-skill-sync-pr.sh." \
  || fail "commit failed"
# The branch belongs to this automation and always means "mirror at latest
# master", so replacing its previous content is the intent -- but only the
# content this run saw (REMOTE_OID); anything newer wins.
# The lease names REMOTE_OID explicitly, so it is an exact compare-and-swap and
# not the implicit form a background fetch can silently refresh. The repo's
# usual --force-if-includes would reject every run here: each sync commit is
# built fresh on the plugin's base, so it never contains the previous tip.
git -C "$DST_WT" push -q --force-with-lease="refs/heads/$BRANCH:$REMOTE_OID" origin "HEAD:refs/heads/$BRANCH" >/dev/null 2>&1 \
  || fail "push of $BRANCH refused or failed (moved since this run looked?)"

# printf, not a heredoc inside $( ): bash 3.2 misparses quotes in that form.
BODY="$(printf '%s\n' \
  "Byte mirror of \`unitares/skills\` at \`$SRC_SHA\`, written by \`scripts/dev/sync-plugin-skills.sh\`." \
  "" \
  "- Changed: $SUMMARY" \
  "- Freshness: $FRESH" \
  ${RULE_NOTE:+"- Attestation rule: $RULE_NOTE"} \
  "" \
  "Opened automatically by \`scripts/dev/open-plugin-skill-sync-pr.sh\` in unitares. Later runs replace this branch with the newest unitares master, so this stays the only sync PR. Nothing outside \`skills/\` changes. Merging is left to the maintainer.")"

# `.[].number`, not `.[0].number`: iterating yields nothing on an empty list,
# so "no open PR" can never read as a PR called "null".
OPEN="$(cd "$DST_WT" && "$GH" pr list --state open --head "$BRANCH" --json number -q '.[].number' 2>/dev/null | head -1)"
if [ -n "$OPEN" ]; then
  # Title AND body: the body names the source commit and what changed, so a
  # title-only update would leave the PR describing an older sync.
  EDIT_ERR="$(cd "$DST_WT" && "$GH" pr edit "$OPEN" --title "$TITLE" --body "$BODY" 2>&1 >/dev/null)" \
    || fail "pushed $BRANCH but could not update #$OPEN: $EDIT_ERR"
  say "plugin mirror behind ($SUMMARY) — updated #$OPEN"
  exit 0
fi


URL="$(cd "$DST_WT" && "$GH" pr create --base "$BASE" --head "$BRANCH" --title "$TITLE" --body "$BODY" 2>&1)" \
  || fail "gh pr create failed: $URL"
say "plugin mirror behind ($SUMMARY) — opened $URL"
exit 0
