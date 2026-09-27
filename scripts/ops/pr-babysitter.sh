#!/usr/bin/env bash
# pr-babysitter.sh — a label-driven merge queue for strict branch protection.
#
# master requires its checks to pass on a branch that is up to date with it
# (`strict`), so every merge re-dirties every other open PR. With auto-merge
# set, GitHub updates the branch itself and merges on green
# (docs/operations/github-workflow-conventions.md §4) — but it does that for
# EVERY armed PR at once. Arm fourteen and each merge re-runs CI on the other
# thirteen, one of which wins: roughly N²/2 CI runs to land N PRs, all
# competing for the same Actions concurrency, so each merge gets slower.
#
# This script keeps exactly one PR armed at a time:
#   1. If any open, non-draft, conflict-free PR is already armed, it is in
#      flight: do nothing (except the stale-base fallback below).
#   2. Otherwise take the queue — open, non-draft, unarmed PRs carrying the
#      `queue` label — lowest number first, and arm the first one that is
#      MERGEABLE with no failing check. GitHub updates it, runs CI, merges it;
#      the next tick arms the next.
#
# The `queue` label is the approval. Arming says "this one is approved, land
# it when green", which is the maintainer's merge decision; the label is that
# same decision made ahead of time, so it is applied by the maintainer and
# never by an agent (AGENTS.md / CLAUDE.md shared contract).
#
# Two gaps it also covers:
#   - GitHub DISARMS auto-merge when a required check fails, even transiently,
#     and the PR then strands silently. A queued PR with a failing check gets
#     its failed Actions jobs re-run ONCE, marked with `queue-retried`; after
#     that it is skipped until someone removes that label.
#   - If the in-flight PR is still BEHIND after its base has sat still for
#     PR_QUEUE_BASE_GRACE_MIN minutes, GitHub's own updater has not acted and
#     the queue would stall; update that one branch. The grace period is what
#     keeps this from racing the native updater (§4).
#
# Deliberately out of scope — these stay human or session judgment:
#   - readying drafts (the draft→ready mark is the owning agent's gate),
#   - resolving conflicts,
#   - arming anything that does not carry the label.
set -uo pipefail

REPO="${PR_BABYSITTER_REPO:-cirwel/unitares}"
LABEL="${PR_QUEUE_LABEL:-queue}"
RETRIED_LABEL="${PR_QUEUE_RETRIED_LABEL:-queue-retried}"
BASE_GRACE_MIN="${PR_QUEUE_BASE_GRACE_MIN:-15}"
DRY_RUN="${PR_QUEUE_DRY_RUN:-0}"

log() { echo "$(date -u +%FT%TZ) $*"; }

# Mutating gh calls go through here so a dry run prints them instead.
act() {
  if [ "$DRY_RUN" = "1" ]; then
    log "dry-run: $*"
  else
    "$@"
  fi
}

command -v jq >/dev/null || { log "jq not found; nothing done"; exit 1; }

# gh pr list defaults to 30 results; the queue must see every open PR.
prs=$(gh pr list -R "$REPO" --state open --limit 500 \
  --json number,isDraft,mergeable,mergeStateStatus,autoMergeRequest,baseRefName,labels,statusCheckRollup) \
  || { log "gh pr list failed; nothing done"; exit 1; }

# A check has failed when a finished run concluded badly (CheckRun) or a
# status context reports failure/error (StatusContext).
FAILED_CHECKS='[.statusCheckRollup[]?
  | select((.conclusion // "") as $c | (.state // "") as $s
           | ($c | test("^(FAILURE|TIMED_OUT|CANCELLED|ACTION_REQUIRED|STARTUP_FAILURE)$"))
             or ($s | test("^(FAILURE|ERROR)$")))]'

# --- 1. in flight --------------------------------------------------------------
inflight=$(jq -c 'map(select(.isDraft == false
                             and .autoMergeRequest != null
                             and .mergeable != "CONFLICTING"))
                  | sort_by(.number) | .[0] // empty' <<<"$prs") \
  || { log "could not read the open PRs; nothing done"; exit 1; }

if [ -n "$inflight" ]; then
  n=$(jq -r .number <<<"$inflight")
  if [ "$(jq -r .mergeStateStatus <<<"$inflight")" = "BEHIND" ]; then
    base=$(jq -r .baseRefName <<<"$inflight")
    moved=$(gh api "repos/$REPO/commits/$base" --jq .commit.committer.date) || moved=""
    if [ -n "$moved" ]; then
      idle=$(jq -rn --arg d "$moved" '((now - ($d | fromdateiso8601)) / 60) | floor')
      if [ "$idle" -ge "$BASE_GRACE_MIN" ]; then
        log "#$n armed and BEHIND with $base idle ${idle}m; native updater has not acted, updating"
        act gh pr update-branch "$n" -R "$REPO" || true
      fi
    fi
  fi
  exit 0
fi

# --- 2. the queue --------------------------------------------------------------
queue=$(jq -c --arg label "$LABEL" 'map(select(.isDraft == false
                                               and .autoMergeRequest == null
                                               and any(.labels[]?; .name == $label)))
                                    | sort_by(.number) | .[]' <<<"$prs") \
  || { log "could not read the queue; nothing done"; exit 1; }

while read -r pr; do
  [ -n "$pr" ] || continue
  n=$(jq -r .number <<<"$pr")
  mergeable=$(jq -r .mergeable <<<"$pr")

  if [ "$mergeable" != "MERGEABLE" ]; then
    # UNKNOWN is GitHub still computing; CONFLICTING needs a person.
    [ "$mergeable" = "CONFLICTING" ] && log "#$n queued but CONFLICTING; skipped"
    continue
  fi

  failed=$(jq "$FAILED_CHECKS | length" <<<"$pr") \
    || { log "#$n could not read its checks; skipped"; continue; }
  if [ "$failed" -gt 0 ]; then
    if jq -e --arg l "$RETRIED_LABEL" 'any(.labels[]?; .name == $l)' <<<"$pr" >/dev/null; then
      log "#$n queued but $failed check(s) still failing after one retry; skipped"
      continue
    fi
    runs=$(jq -r "$FAILED_CHECKS"' | [.[] | (.detailsUrl // .targetUrl // "")
                                         | capture("/actions/runs/(?<id>[0-9]+)") | .id]
                                   | unique | .[]' <<<"$pr") \
      || { log "#$n could not read its failing checks; skipped"; continue; }
    if [ -z "$runs" ]; then
      log "#$n queued but $failed failing check(s) are not Actions runs; skipped"
      continue
    fi
    log "#$n queued with $failed failing check(s); re-running once: $(tr '\n' ' ' <<<"$runs")"
    for run in $runs; do
      act gh run rerun "$run" --failed -R "$REPO" || true
    done
    act gh pr edit "$n" -R "$REPO" --add-label "$RETRIED_LABEL" || true
    continue
  fi

  log "#$n arming (head of queue)"
  if act gh pr merge "$n" -R "$REPO" --auto --squash --delete-branch; then
    exit 0
  fi
  log "#$n arm failed; trying the next queued PR"
done <<<"$queue"

exit 0
