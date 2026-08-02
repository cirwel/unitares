#!/usr/bin/env bash
# pr-babysitter.sh — un-strand armed PRs under strict branch protection.
#
# master requires 7 status checks with "require branches up to date", so every
# merge flips every other open PR to BEHIND — including PRs whose sessions
# armed auto-merge and parked (the fleet's working convention). Auto-merge
# does NOT update the branch itself, so without intervention an armed, green,
# conflict-free PR strands forever. GitHub's merge queue would remove this at
# the root, but it is unavailable on user-owned repos (verified 2026-08-02:
# the rulesets API 422s on the merge_queue rule type for this repo).
#
# This script is the smallest possible substitute: for each open PR that is
#   - not a draft,
#   - armed (auto-merge enabled),
#   - MERGEABLE (no conflicts), and
#   - BEHIND (base moved),
# run `gh pr update-branch`. That triggers a fresh CI cycle; auto-merge
# completes on green. One merge per cycle; the loop converges.
#
# Deliberately out of scope — these stay session judgment, never automated:
#   - readying drafts (ship.sh convention: validate first),
#   - resolving conflicts,
#   - arming auto-merge on anything.
set -uo pipefail

REPO="${PR_BABYSITTER_REPO:-cirwel/unitares}"

gh pr list -R "$REPO" \
  --json number,isDraft,mergeable,mergeStateStatus,autoMergeRequest \
  --jq '.[]
        | select(.isDraft == false
                 and .autoMergeRequest != null
                 and .mergeable == "MERGEABLE"
                 and .mergeStateStatus == "BEHIND")
        | .number' |
while read -r n; do
  echo "$(date -u +%FT%TZ) update-branch #$n"
  gh pr update-branch "$n" -R "$REPO" || true
done
