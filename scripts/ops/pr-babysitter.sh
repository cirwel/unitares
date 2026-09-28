#!/usr/bin/env bash
# pr-babysitter.sh — a label-driven merge queue for strict branch protection.
#
# master requires its checks to pass on a branch that is up to date with it
# (`strict`), so every merge re-dirties every other open PR. GitHub's own merge
# queue would handle this at the root, but it is unavailable on a user-owned
# repo (verified 2026-08-02: the rulesets API 422s on the merge_queue rule
# type), and conventions §4 records the CodeQL work it would need besides.
#
# With auto-merge set, GitHub updates an armed PR's branch itself when the base
# moves, but not reliably and not for every armed PR at once: on 2026-09-27 it
# updated one of two armed PRs 43 s and 101 s after master moved, and left the
# other for this script's previous version to update minutes later. Arming
# many PRs at once and updating them all (what this script used to do) re-runs
# CI on every armed PR after each merge, roughly N²/2 runs to land N.
#
# This script keeps exactly one PR armed at a time. Each tick:
#   1. Tidy the slot. An armed PR carrying the approval label that has become
#      CONFLICTING, whose checks failed on its current head, or that has a check
#      parked for approval, is disarmed so it stops holding the slot; its label
#      stays, so it returns to the queue.
#   2. Pin the head each newly labelled PR is at (see below), every tick,
#      whether or not the slot is free.
#   3. If a PR is still armed (including one the maintainer armed by hand, which
#      the script never disarms, even while it conflicts), it holds the slot.
#      If it is BEHIND and neither the base nor its arming has moved for
#      PR_QUEUE_BASE_GRACE_MIN minutes, GitHub's updater has not acted: a PR
#      this script armed is disarmed and updated (step 4 re-arms it), one
#      armed by hand is only updated. Then stop.
#   4. Otherwise walk the queue in the order the label was applied and arm the
#      first PR that can go: its head still the one the approval covers, no
#      open "merge after #N" dependency, MERGEABLE, no check needing approval.
#      A BEHIND head of queue is updated first, unarmed, and armed on a later
#      tick once the updated head re-validates. It is armed with
#      --match-head-commit on that head.
#      A PR whose checks failed on an up-to-date head gets its failed Actions
#      jobs re-run once (marked by the retried label); after that it is skipped
#      until someone removes that label.
#
# The approval label is the maintainer's merge decision made ahead of time,
# exactly like arming, so an agent never applies it (AGENTS.md / CLAUDE.md
# shared contract). It approves the PR as it stood when the label went on. The
# script pins the head and diff fingerprint it first sees under a label
# (STATE_FILE); a later head stays covered only while the diff is unchanged
# (a clean base update), and a commit dated after the label is refused
# outright. Anything else is stale
# until the label is re-applied, which pins afresh. A label is pinned only if
# the script sees it within PR_QUEUE_PIN_WINDOW_MIN of going on; an older one
# with no pin (the script was down, or its state lost) must be re-applied. The
# residual gap: a commit made before the label but pushed before the script
# first sees it (normally the next tick, never beyond that window) is pinned
# as approved.
#
# Threat model. The pin catches honest mistakes: an agent pushing a follow-up
# after the maintainer approved, or a stale branch changing under the label.
# It is not a security boundary against a deliberately hostile agent, and does
# not try to be: every actor here authenticates as the same GitHub account, so
# such an agent could apply the label itself, or run `gh pr merge --auto`,
# with nothing technical to stop it. Hardening the fingerprint against forged
# history (an edit moved to another spot with identical context, backdated or
# GitHub-imitating commit metadata) would not close that door, so it is out of
# scope; the guard against a hostile agent is who holds the credentials.
#
# Deliberately out of scope — these stay human or session judgment:
#   - readying drafts (the draft→ready mark is the owning agent's gate),
#   - resolving conflicts,
#   - arming anything that does not carry the label, or that targets a branch
#     other than PR_QUEUE_BASE (a stacked PR runs no CI; arming it merges it).
set -uo pipefail

REPO="${PR_BABYSITTER_REPO:-cirwel/unitares}"
BASE="${PR_QUEUE_BASE:-master}"
LABEL="${PR_QUEUE_LABEL:-approved-to-merge}"
RETRIED_LABEL="${PR_QUEUE_RETRIED_LABEL:-merge-retried}"
BASE_GRACE_MIN="${PR_QUEUE_BASE_GRACE_MIN:-3}"
STALL_WARN_MIN="${PR_QUEUE_STALL_WARN_MIN:-90}"
PIN_WINDOW_MIN="${PR_QUEUE_PIN_WINDOW_MIN:-15}"
# Checks that must have passed on the head before it is armed, beyond the ones
# branch protection requires. `review` is not a required check on master, and
# its NEUTRAL conclusion means "unreviewed", so without this an agent's label
# on a PR whose review never ran would merge it.
REQUIRED_CHECKS="${PR_QUEUE_REQUIRED_CHECKS-review}"
# PRs carrying any of these labels are never armed: the operator merges them
# by hand. `governance-sensitive` is what CI applies to a PR touching an
# enforcement constant, and docs/SCOPE_AND_THREAT_MODEL.md names the human
# merge gate as the control for exactly those diffs.
OPERATOR_ONLY_LABELS="${PR_QUEUE_OPERATOR_ONLY_LABELS-governance-sensitive}"
operator_only() {  # <pr-json> -> why only the operator may merge it, if so
  local l
  # A fork PR: the fleet's own PRs never come from forks, and CI cannot label
  # one (its token is read-only there), so nothing else would flag it.
  jq -e '.isCrossRepository == true' <<<"$1" >/dev/null && { echo "a fork"; return 0; }
  for l in $OPERATOR_ONLY_LABELS; do
    jq -e --arg l "$l" 'any(.labels[]?; .name == $l)' <<<"$1" >/dev/null && { echo "$l"; return 0; }
  done
  return 1
}  # set it empty to require none
DRY_RUN="${PR_QUEUE_DRY_RUN:-0}"
# Which head each approval covers: "<pr> <head-sha> <labelled-at>" per line.
STATE_FILE="${PR_QUEUE_STATE_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/unitares/pr-queue-approvals}"

log() { echo "$(date -u +%FT%TZ) $*"; }

# Mutating gh calls go through here so a dry run prints them instead.
act() {
  if [ "$DRY_RUN" = "1" ]; then
    log "dry-run: $*"
  else
    "$@"
  fi
}

# One PR comment per lasting skip reason and head, so the reason is visible on
# the PR and not only in this machine's log: whoever looks next (its owner, or
# an agent adopting it) sees what to do. A hidden marker dedupes; a new push
# (a new head) gets a fresh notice. PR_QUEUE_NOTIFY=0 turns it off.
NOTIFY="${PR_QUEUE_NOTIFY:-1}"
notify() {  # <pr> <reason-key> <head-sha> <message>
  [ "$NOTIFY" = "1" ] || return 0
  local marker="<!-- pr-queue-notice $2 ${3:0:12} -->" bodies
  bodies=$(gh api --paginate "repos/$REPO/issues/$1/comments" --jq '.[].body' 2>/dev/null) || return 0
  grep -qF -- "$marker" <<<"$bodies" && return 0
  act gh pr comment "$1" -R "$REPO" --body "$marker
**Merge queue:** $4" >/dev/null || log "#$1 notice could not be posted"
}

minutes_since() { jq -rn --arg d "$1" '((now - ($d | fromdateiso8601)) / 60) | floor'; }

command -v jq >/dev/null || { log "jq not found; nothing done"; exit 1; }

# gh pr list defaults to 30 results; the queue must see every open PR.
prs=$(gh pr list -R "$REPO" --state open --limit 500 \
  --json number,isDraft,mergeable,mergeStateStatus,autoMergeRequest,baseRefName,labels,statusCheckRollup,body,headRefOid,isCrossRepository) \
  || { log "gh pr list failed; nothing done"; exit 1; }

# Shared jq vocabulary. A check has FAILED when a finished run concluded badly
# (CheckRun) or a status context reports failure (StatusContext); it is PENDING
# while queued or running; ACTION_REQUIRED is a run parked for approval, which
# a re-run cannot clear.
JQ_DEFS='
def labelled($l): any(.labels[]?; .name == $l);
def failed: [.statusCheckRollup[]?
  | select(((.conclusion // "") | test("^(FAILURE|TIMED_OUT|CANCELLED|STARTUP_FAILURE)$"))
           or ((.state // "") | test("^(FAILURE|ERROR)$")))];
def pending: [.statusCheckRollup[]?
  | select(((.status // "") | test("^(QUEUED|IN_PROGRESS|PENDING|WAITING|REQUESTED)$"))
           or ((.state // "") | test("^(PENDING|EXPECTED)$")))];
def parked: [.statusCheckRollup[]? | select((.conclusion // "") == "ACTION_REQUIRED")];
'
# q [jq options...] EXPR — jq with the vocabulary above; the filter comes last.
q() { local expr="${!#}"; jq "${@:1:$#-1}" "$JQ_DEFS $expr"; }

# --- approval helpers -----------------------------------------------------------
# When did the approval label last go on, and when was the last commit that
# was not a base-update merge (GitHub's updater, `gh pr update-branch`)?
# Prints "<labelled-at> <last-pushed-commit-at>"; either may be "-".
approval_times() {
  # --paginate without --jq prints one JSON array per page; jq reads the stream.
  gh api --paginate "repos/$REPO/issues/$1/timeline" 2>/dev/null | jq -r --arg l "$LABEL" '
    .[] | if .event == "labeled" and .label.name == $l then "L \(.created_at)"
          elif .event == "committed"
               and ((.committer.name == "GitHub" and (.message | startswith("Merge branch ")))
                    | not)
            then "C \(.committer.date)"
          else empty end'
}

# What an approval covers. First sight of a label records the PR's head and a
# fingerprint of what it changes against the base, read from GitHub's compare
# of base...<that exact head SHA> (never the PR's current ref, which can move
# mid-tick). Per file: name, status, previous name, and only the patch's added
# and removed lines (no hunk headers, no context, both of which a clean base
# update can shift), so a clean base update leaves the fingerprint unchanged; a file with no patch (binary, or too large) contributes its blob
# SHA instead. A later head stays covered only while the fingerprint matches:
# commit metadata (committer name, message, even a web-flow signature) is
# author-controlled or API-mintable, so it cannot prove a commit was only a
# base update, but the content can. A re-applied label pins afresh.
# A missing state file reads as no pins; an unreadable one is an error, never
# "no pins", since that would re-approve whatever head is there now.
pinned() {  # <pr> <labelled-at> -> "<sha> <fingerprint>"
  [ -e "$STATE_FILE" ] || return 0
  [ -f "$STATE_FILE" ] && [ -r "$STATE_FILE" ] || return 1
  awk -v n="$1" -v l="$2" '$1 == n && $3 == l { h = $2 " " $4 } END { if (h) print h }' "$STATE_FILE"
}
pin() {  # <pr> <sha> <labelled-at> <fingerprint>
  [ "$DRY_RUN" = "1" ] && return 0
  mkdir -p "$(dirname "$STATE_FILE")" && echo "$1 $2 $3 $4" >>"$STATE_FILE"
}
# <head-sha>. GitHub's compare lists at most 300 files; a list that long may
# be cut short, so it fails, and so does the approval that depends on it.
fingerprint() {
  gh api "repos/$REPO/compare/$BASE...$1" 2>/dev/null | jq -er '
    if (.files | length) >= 300 then error("too many files") else . end
    | [.files[] | {f: .filename, s: .status, prev: .previous_filename,
                   p: (if .patch then (.patch | split("\n") | map(select(startswith("+") or startswith("-"))) | join("\n"))
                       else .sha end)}]
    | tojson' 2>/dev/null | shasum -a 256 | cut -c1-64
}

# An armed, labelled PR whose head has moved since its pin stays armed only if
# its content still matches (GitHub's base updates move the head too).
# --match-head-commit binds only the arming; a push after it would otherwise
# merge. With no pin (armed by hand, or state lost) it is left alone.
still_approved() {  # <pr> <head>
  local at line sha fp now
  at=$(latest_label_time "$1") || return 1
  [ -n "$at" ] || return 0
  line=$(pinned "$1" "$at") || return 1
  [ -n "$line" ] || return 0
  read -r sha fp <<<"$line"
  [ "$sha" = "$2" ] && return 0
  now=$(fingerprint "$2") && [ "$now" = "$fp" ] || return 1
  pin "$1" "$2" "$at" "$fp" || true
}

# Prints the latest label time, or nothing when there is no label event;
# fails (2) when the timeline cannot be read, which is not the same thing.
# <pr-json> -> space-separated "<check>=<state>" for each required check that
# is not SUCCESS; empty when all passed. MISSING and PENDING are normal for
# about a minute after a base update, while the review gate re-evaluates.
unmet_required_checks() {
  local check state unmet=""
  for check in $REQUIRED_CHECKS; do
    state=$(jq -r --arg c "$check" '[.statusCheckRollup[]? | select((.name // .context) == $c)
             | (.conclusion // .state // "") | if . == "" then "PENDING" else . end]
             | if length == 0 then "MISSING" elif all(. == "SUCCESS") then "SUCCESS"
               else map(select(. != "SUCCESS")) | .[0] end' <<<"$1")
    [ "$state" = "SUCCESS" ] || unmet+=" $check=$state"
  done
  echo "${unmet# }"
}

# The label is best-effort (CI tolerates failing to apply it), so before
# arming, the queue checks the diff itself against the same manifest CI uses
# (scripts/dev/check_governance_sensitivity.sh's --diff rules), read from
# GitHub's compare of base...<head>. It fails closed: no manifest, an
# unreadable or possibly truncated compare, or a matched file with no patch to
# inspect all count as sensitive.
# The manifest is read from the base branch through GitHub, not from this
# script's checkout: the deploy tree can lag master, and CI judges against
# master's manifest. PR_QUEUE_SENSITIVITY_MANIFEST names a local file instead
# (tests; "" disables the check). Read once per tick.
MANIFEST_PATH_IN_REPO="scripts/dev/governance_sensitivity_manifest.tsv"
manifest_rows=""
manifest_state=""  # "", "ok" or "unreadable"
base_sha=""        # the base's commit this tick judges against, for rules and diff alike
load_manifest() {
  [ -n "$manifest_state" ] && return 0
  if [ -n "${PR_QUEUE_SENSITIVITY_MANIFEST+set}" ]; then
    if [ -z "$PR_QUEUE_SENSITIVITY_MANIFEST" ]; then manifest_state="off"; return 0; fi
    manifest_rows=$(cat "$PR_QUEUE_SENSITIVITY_MANIFEST" 2>/dev/null) && manifest_state="ok" || manifest_state="unreadable"
  else
    # Pin the base once, so a merge mid-tick cannot pair new rules with an
    # old diff or the reverse.
    base_sha=$(gh api "repos/$REPO/commits/$BASE" --jq .sha 2>/dev/null) || base_sha=""
    [ -n "$base_sha" ] \
      && manifest_rows=$(gh api "repos/$REPO/contents/$MANIFEST_PATH_IN_REPO?ref=$base_sha" --jq .content 2>/dev/null | base64 -d 2>/dev/null) \
      && [ -n "$manifest_rows" ] && manifest_state="ok" || manifest_state="unreadable"
  fi
}
sensitive_path() {  # <head-sha> -> prints what makes it sensitive; fails when it is not
  load_manifest
  [ "$manifest_state" = "off" ] && return 1
  [ "$manifest_state" = "ok" ] || { echo "the sensitivity manifest could not be read"; return 0; }
  local cmp path symbol why file patch
  cmp=$(gh api "repos/$REPO/compare/${base_sha:-$BASE}...$1" 2>/dev/null) \
    && jq -e '(.files | length) < 300' <<<"$cmp" >/dev/null 2>&1 \
    || { echo "its diff could not be checked"; return 0; }
  while IFS=$'\t' read -r path symbol why; do
    [ -n "$path" ] || continue
    file=$(jq -c --arg p "$path" 'first(.files[] | select(.filename == $p or .previous_filename == $p)) // empty' <<<"$cmp")
    [ -n "$file" ] || continue
    [ "$symbol" = "-" ] && { echo "$path"; return 0; }
    patch=$(jq -r '.patch // empty' <<<"$file")
    [ -n "$patch" ] || { echo "$path (no patch to check)"; return 0; }
    grep -E '^[+-][^+-]' <<<"$patch" | grep -Eq -- "$symbol" && { echo "$path"; return 0; }
  done < <(grep -v '^#' <<<"$manifest_rows" | grep -v '^[[:space:]]*$')
  return 1
}

latest_label_time() {
  local t
  t=$(approval_times "$1") || return 2
  { grep '^L ' <<<"$t" || true; } | cut -d' ' -f2 | sort | tail -1
}

load_manifest  # once per tick, in this shell (sensitive_path runs in subshells)

# --- 1. tidy the slot -----------------------------------------------------------
# Arms this script made: "<pr> <armed-at>" per line. An armed PR is the
# script's when GitHub's arming time matches a recorded one. Only those are
# ever disarmed; a PR the maintainer armed by hand (labelled or not, or
# re-armed later, which carries a later time) is left alone.
ARMS_FILE="$STATE_FILE.arms"
record_arm() {
  [ "$DRY_RUN" = "1" ] && return 0
  mkdir -p "$(dirname "$ARMS_FILE")" && echo "$1 $(date -u +%FT%TZ)" >>"$ARMS_FILE"
}
armed_by_script() {  # <pr> <enabledAt>
  local at
  [ -f "$ARMS_FILE" ] && [ -n "$2" ] || return 1
  at=$(awk -v n="$1" '$1 == n { t = $2 } END { if (t) print t }' "$ARMS_FILE") && [ -n "$at" ] || return 1
  jq -en --arg a "$at" --arg b "$2" '(($a | fromdateiso8601) - ($b | fromdateiso8601)) | (if . < 0 then -. else . end) <= 120' >/dev/null
}

armed=$(q -c --arg b "$BASE" \
  'map(select(.isDraft == false and .baseRefName == $b and .autoMergeRequest != null)) | .[]' <<<"$prs") \
  || { log "could not read the open PRs; nothing done"; exit 1; }

disarmed=" "
hold_order=0
while read -r pr; do
  [ -n "$pr" ] || continue
  n=$(jq -r .number <<<"$pr")
  reason=""
  # Only arms this script made are ever disarmed; the maintainer's own arm,
  # labelled or not, is theirs to manage.
  armed_by_script "$n" "$(jq -r '.autoMergeRequest.enabledAt // empty' <<<"$pr")" || continue
  if held_by=$(operator_only "$pr"); then
    reason="it is labelled $held_by, which only the operator merges"
  elif ! q -e --arg l "$LABEL" 'labelled($l)' <<<"$pr" >/dev/null; then
    reason="its $LABEL label was removed"  # removing the label withdraws the approval
  elif [ "$(jq -r .mergeable <<<"$pr")" = "CONFLICTING" ]; then
    reason="CONFLICTING"
  elif ! still_approved "$n" "$(jq -r .headRefOid <<<"$pr")"; then
    reason="its head changed since the approval"
  elif unmet=$(unmet_required_checks "$pr") && [ -n "$unmet" ]; then
    # Anything but SUCCESS disarms: review is not a branch-protection check,
    # so an armed PR would otherwise merge without it (NEUTRAL means
    # unreviewed). MISSING/PENDING is usual for about a minute after a base
    # update; the PR keeps its place, since the tick stops here and the next
    # one re-arms it once the check passes.
    reason="$unmet is not passing"
    case " $unmet" in *=MISSING*|*=PENDING*) hold_order=1 ;; esac
  elif [ "$(q 'parked | length' <<<"$pr")" -gt 0 ]; then
    reason="a check is waiting for approval (ACTION_REQUIRED)"
  elif [ "$(jq -r .mergeStateStatus <<<"$pr")" != "BEHIND" ] \
       && [ "$(q 'failed | length' <<<"$pr")" -gt 0 ] \
       && [ "$(q 'pending | length' <<<"$pr")" -eq 0 ]; then
    reason="checks failed on its current head"
  fi
  if [ -n "$reason" ]; then
    log "#$n armed but $reason; disarming so it stops holding the queue"
    if act gh pr merge "$n" -R "$REPO" --disable-auto; then
      disarmed="$disarmed$n "
    else
      log "#$n disarm failed; nothing else done this tick"
      exit 0
    fi
  fi
done <<<"$armed"
if [ "$hold_order" = "1" ]; then
  log "a disarmed PR is waiting on a required check; nothing else armed this tick"
  exit 0
fi

# --- 2. approvals ---------------------------------------------------------------
# Runs every tick, before the slot check, so a PR labelled while another holds
# the slot is pinned as soon as the script sees the label, not when it reaches
# the head of the queue.
queued=$(q -c --arg b "$BASE" --arg l "$LABEL" \
  'map(select(.isDraft == false and .baseRefName == $b and .autoMergeRequest == null
              and labelled($l))) | .[]' <<<"$prs") \
  || { log "could not read the queue; nothing done"; exit 1; }

# Order by when the label went on, which is the order the maintainer approved.
ordered=""
while read -r pr; do
  [ -n "$pr" ] || continue
  n=$(jq -r .number <<<"$pr")
  times=$(approval_times "$n") || { log "#$n timeline unreadable; skipped"; continue; }
  labelled_at=$(grep '^L ' <<<"$times" | cut -d' ' -f2 | sort | tail -1)
  pushed_at=$(grep '^C ' <<<"$times" | cut -d' ' -f2 | sort | tail -1)
  [ -n "$labelled_at" ] || { log "#$n has no readable label event; skipped"; continue; }
  if [ -n "$pushed_at" ] && [[ "$pushed_at" > "$labelled_at" ]]; then
    log "#$n has a commit from $pushed_at, after its approval at $labelled_at; re-apply $LABEL to approve it"
    notify "$n" stale-push "$(jq -r .headRefOid <<<"$pr")" "skipped: a commit from $pushed_at came after the \`$LABEL\` label ($labelled_at), so the approval no longer covers this head. Once validation passes again, the approval needs renewing: remove the label, then add it (AGENTS.md says who may)."
    continue
  fi
  head=$(jq -r .headRefOid <<<"$pr")
  pinned_line=$(pinned "$n" "$labelled_at") || { log "#$n approval state unreadable ($STATE_FILE); skipped"; continue; }
  if [ -z "$pinned_line" ]; then
    # Pin only a label the script sees soon after it went on. An older label
    # with no pin (the script was down, or its state was lost) no longer says
    # what was approved.
    age=$(minutes_since "$labelled_at")
    if [ "$age" -gt "$PIN_WINDOW_MIN" ]; then
      log "#$n approval at $labelled_at was never pinned and is ${age}m old; re-apply $LABEL to approve its head"
      notify "$n" unpinned "$head" "skipped: the \`$LABEL\` label went on at $labelled_at, more than ${PIN_WINDOW_MIN} minutes before the queue saw it, so it no longer says which head was approved. It needs renewing: remove the label, then add it (AGENTS.md says who may)."
      continue
    fi
    fp=$(fingerprint "$head") || { log "#$n diff unreadable; cannot record what was approved; skipped"; continue; }
    pin "$n" "$head" "$labelled_at" "$fp" || { log "#$n could not record its approval; skipped"; continue; }
  else
    read -r pinned_sha pinned_fp <<<"$pinned_line"
    if [ "$pinned_sha" != "$head" ]; then
      fp=$(fingerprint "$head") || { log "#$n diff unreadable; skipped"; continue; }
      if [ "$fp" != "$pinned_fp" ]; then
        log "#$n changed since its approval at ${pinned_sha:0:8}; re-apply $LABEL to approve ${head:0:8}"
        notify "$n" changed "$head" "skipped: the change differs from what was approved at \`${pinned_sha:0:8}\` (more than a base update). Once validation passes on \`${head:0:8}\`, the approval needs renewing: remove the \`$LABEL\` label, then add it (AGENTS.md says who may)."
        continue
      fi
      pin "$n" "$head" "$labelled_at" "$fp" || true
    fi
  fi
  ordered+="$labelled_at $n $head"$'\n'
done <<<"$queued"

# --- 3. a PR holds the slot ---------------------------------------------------
# Any armed PR on the base holds it, except one this tick just disarmed. A
# hand-armed PR counts even when it conflicts: the script never disarms it, so
# arming another would leave two armed the moment its conflict is resolved,
# which is exactly the parallel CI re-run the queue exists to prevent. A long
# hold is logged below.
holder=$(q -c --arg b "$BASE" --arg skip "$disarmed" \
  'map(select(.isDraft == false and .baseRefName == $b and .autoMergeRequest != null
              and (.number as $n | $skip | contains(" \($n) ") | not)))
   | sort_by(.number) | .[0] // empty' <<<"$prs") \
  || { log "could not read the open PRs; nothing done"; exit 1; }

if [ -n "$holder" ]; then
  n=$(jq -r .number <<<"$holder")
  # gh marshals a missing time as year 0001; treat that as unknown.
  armed_at=$(jq -r '.autoMergeRequest.enabledAt // empty | select(startswith("0001-") | not)' <<<"$holder")
  if [ -n "$armed_at" ]; then
    held=$(minutes_since "$armed_at")
    [ "$held" -ge "$STALL_WARN_MIN" ] \
      && log "#$n has held the queue for ${held}m ($(jq -r .mergeStateStatus <<<"$holder")); needs a look"
  fi
  if [ "$(jq -r .mergeStateStatus <<<"$holder")" = "BEHIND" ]; then
    if armed_by_script "$n" "$armed_at"; then
      # No grace for the script's own arm: GitHub's updater can move the head
      # within a minute, and the arm must not outlive the head it validated.
      # Disarm now and update; the queue re-arms it once the new head passes.
      log "#$n armed and BEHIND; disarming to update"
      act gh pr merge "$n" -R "$REPO" --disable-auto || { log "#$n disarm failed; not updating"; exit 0; }
      act gh pr update-branch "$n" -R "$REPO" || true
    else
      # A hand-armed PR is the operator's: only update it, after the grace.
      moved=$(gh api "repos/$REPO/commits/$BASE" --jq .commit.committer.date) || moved=""
      if [ -n "$moved" ]; then
        # Measure from whichever came later: the base moving, or the arming.
        since=$(jq -rn --arg a "$moved" --arg b "${armed_at:-$moved}" '[$a, $b] | max')
        idle=$(minutes_since "$since")
        if [ "$idle" -ge "$BASE_GRACE_MIN" ]; then
          log "#$n armed and BEHIND, ${idle}m without GitHub updating it; updating"
          act gh pr update-branch "$n" -R "$REPO" || true
        fi
      fi
    fi
  fi
  exit 0
fi

# --- 4. the queue ---------------------------------------------------------------
# "merge after #N" / "merge after owner/repo#N" in the body: wait until N merges.
dependency_open() {
  local pr="$1" dep repo num state
  for dep in $(jq -r '.body // ""' <<<"$pr" \
      | grep -oiE 'merge after ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+' \
      | sed -E 's/^[Mm][Ee][Rr][Gg][Ee] [Aa][Ff][Tt][Ee][Rr] //'); do
    repo="${dep%#*}"; num="${dep##*#}"
    [ -n "$repo" ] || repo="$REPO"
    state=$(gh pr view "$num" -R "$repo" --json state --jq .state 2>/dev/null) || state="UNREADABLE"
    if [ "$state" != "MERGED" ] && [ "$state" != "CLOSED" ]; then
      echo "$repo#$num ($state)"
      return 0
    fi
  done
  return 1
}

while read -r _ n head; do
  [ -n "${n:-}" ] || continue
  pr=$(jq -c --argjson n "$n" '.[] | select(.number == $n)' <<<"$prs")

  if held_by=$(operator_only "$pr"); then
    log "#$n is labelled $held_by; the operator merges it by hand; skipped"
    notify "$n" operator-only "$head" "not armed: this PR is labelled \`$held_by\`, so the operator merges it by hand (docs/SCOPE_AND_THREAT_MODEL.md). The queue has moved on to the next PR."
    continue
  fi

  if dep=$(dependency_open "$pr"); then
    log "#$n waits on $dep (merge after); skipped"
    continue
  fi

  case "$(jq -r .mergeable <<<"$pr")" in
    MERGEABLE) ;;
    CONFLICTING)
      log "#$n queued but CONFLICTING; skipped"
      notify "$n" conflicting "$head" "skipped: this PR conflicts with master. Once master is merged in and validation passes again, the approval needs renewing (remove the \`$LABEL\` label, then add it), by whoever the delivery contract (AGENTS.md) says may apply it."
      continue ;;
    # GitHub is still computing mergeability, typically right after a merge.
    # Wait for it rather than letting a later PR jump the order.
    *) exit 0 ;;
  esac

  if [ "$(q 'parked | length' <<<"$pr")" -gt 0 ]; then
    log "#$n has a check waiting for approval (ACTION_REQUIRED); skipped"
    notify "$n" parked "$head" "skipped: $(q -r 'parked | map(.name // .context) | unique | join(", ")' <<<"$pr") is waiting for approval (ACTION_REQUIRED). A re-run does not clear it; see the check's details."
    continue
  fi

  failed=$(q 'failed | length' <<<"$pr")
  if [ "$failed" -gt 0 ] && [ "$(jq -r .mergeStateStatus <<<"$pr")" != "BEHIND" ]; then
    # A BEHIND PR is armed anyway: GitHub re-runs everything on the fresh base.
    if [ "$(q 'pending | length' <<<"$pr")" -gt 0 ]; then
      continue  # its run is still going; failed jobs can be re-run once it ends
    fi
    if jq -e --arg l "$RETRIED_LABEL" 'any(.labels[]?; .name == $l)' <<<"$pr" >/dev/null; then
      log "#$n: $failed check(s) still failing after one retry; skipped until $RETRIED_LABEL is removed"
      notify "$n" failing "$head" "skipped: $(q -r 'failed | map(.name // .context) | unique | join(", ")' <<<"$pr") still failing after one automatic re-run. Fix it, then remove the \`$RETRIED_LABEL\` label."
      continue
    fi
    runs=$(q -r 'failed | [.[] | (.detailsUrl // .targetUrl // "")
                           | capture("/actions/runs/(?<id>[0-9]+)") | .id]
                 | unique | .[]' <<<"$pr") \
      || { log "#$n could not read its failing checks; skipped"; continue; }
    if [ -z "$runs" ]; then
      log "#$n: $failed failing check(s) are not Actions runs; skipped"
      continue
    fi
    # Mark first: if the marker cannot be applied, re-running would repeat forever.
    act gh pr edit "$n" -R "$REPO" --add-label "$RETRIED_LABEL" \
      || { log "#$n could not apply $RETRIED_LABEL; not re-running"; continue; }
    started=0
    for run in $runs; do
      act gh run rerun "$run" --failed -R "$REPO" && started=$((started + 1))
    done
    if [ "$started" -eq 0 ]; then
      if act gh pr edit "$n" -R "$REPO" --remove-label "$RETRIED_LABEL"; then
        log "#$n: no re-run started; retry not spent"
      else
        log "#$n: no re-run started, and $RETRIED_LABEL could not be removed; remove it by hand to retry"
      fi
    else
      log "#$n: $failed failing check(s); re-ran $started run(s) once"
    fi
    continue
  fi

  unmet=$(unmet_required_checks "$pr")
  if [ -n "$unmet" ]; then
    case " $unmet" in
      # Still being evaluated (the review gate posts NEUTRAL, not nothing, for
      # an unreviewed PR): wait rather than let a later PR jump the order.
      *=MISSING*|*=PENDING*) log "#$n waiting on $unmet (must pass before arming)"; exit 0 ;;
      *)
        log "#$n: $unmet (must pass before arming); skipped"
        notify "$n" required "$head" "skipped: \`$unmet\`. The queue arms a PR only after these pass; for \`review\`, run \`scripts/dev/review.sh\` and fix or dispose its findings (NEUTRAL means unreviewed)."
        continue ;;
    esac
  fi

  if why=$(sensitive_path "$head"); then
    log "#$n touches a governance-sensitive surface ($why); the operator merges it by hand; skipped"
    notify "$n" sensitive "$head" "not armed: this diff touches a governance-sensitive surface (\`$why\`, per scripts/dev/governance_sensitivity_manifest.tsv), so the operator merges it by hand (docs/SCOPE_AND_THREAT_MODEL.md). The queue has moved on to the next PR."
    continue
  fi

  if [ "$(jq -r .mergeStateStatus <<<"$pr")" = "BEHIND" ]; then
    # Update first, unarmed, and arm the updated head only once it has been
    # re-validated (approval fingerprint, required checks) on a later tick. The
    # PR keeps its place meanwhile: the tick stops here. Arming first and
    # updating after would leave auto-merge on across a head nothing has
    # checked, and `review` is not branch-protected. GitHub's own updater was
    # no help anyway: it acted for 1 of 16 queue arms on 2026-09-27.
    log "#$n updating before arming (head of queue)"
    act gh pr update-branch "$n" -R "$REPO" || log "#$n update failed; retried next tick"
    exit 0
  fi

  log "#$n arming (head of queue)"
  # The repo deletes merged branches itself, so no --delete-branch: gh would
  # also try to delete a local branch in whatever directory this runs from.
  # --match-head-commit: arm only the head the approval covers, so a push that
  # lands between this tick's read and the call is refused, not merged.
  if act gh pr merge "$n" -R "$REPO" --auto --squash --match-head-commit "$head"; then
    # Unrecorded, the arm could not be withdrawn by removing the label later.
    if ! record_arm "$n"; then
      log "#$n armed, but the arm could not be recorded ($ARMS_FILE); disarming"
      act gh pr merge "$n" -R "$REPO" --disable-auto || log "#$n disarm failed too; disarm it by hand"
    fi
  else
    log "#$n arm failed; nothing else armed this tick"
  fi
  # Stop either way. After a failed call we cannot tell whether GitHub armed
  # it, and the next tick reads the real state.
  exit 0
done < <(sort <<<"$ordered")

exit 0
