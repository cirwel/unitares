#!/usr/bin/env bash
# Installed by install-review-sweep.py. Only execute the merged review driver;
# the separate PR checkout supplies read-only context to the reviewer.
set -euo pipefail

REPO="${UNITARES_REVIEW_REPO:-$HOME/projects/unitares}"
LANE="${UNITARES_REVIEW_WORKTREES:-$HOME/projects/wt}"
BASE_WT="$LANE/unitares-review-bot"
PR_WT="$LANE/unitares-review-bot-pr"

git -C "$REPO" fetch --quiet origin master
mkdir -p "$LANE"
if [[ ! -d "$BASE_WT" ]]; then
    git -C "$REPO" worktree add --quiet --detach "$BASE_WT" origin/master
fi
if [[ -n "$(git -C "$BASE_WT" status --porcelain --untracked-files=normal)" ]]; then
    echo "[review-sweep] trusted checkout is dirty; refusing to run it" >&2
    exit 1
fi
git -C "$BASE_WT" worktree lock --reason \
    "review-sweep trusted checkout (com.unitares.review-sweep)" "$BASE_WT" 2>/dev/null || true
git -C "$BASE_WT" checkout --quiet --detach origin/master

cd "$BASE_WT"
rc=0
python3 scripts/dev/review_gate.py sweep --worktree "$PR_WT" "$@" || rc=$?
if git -C "$REPO" worktree list --porcelain | grep -Fxq "worktree $PR_WT"; then
    git -C "$REPO" worktree lock --reason \
        "review-sweep PR checkout (com.unitares.review-sweep)" "$PR_WT" 2>/dev/null || true
fi
exit "$rc"
