#!/usr/bin/env bash
# Installed by install-review-sweep.py. Only execute the merged review driver;
# the separate PR checkout supplies read-only context to the reviewer.
set -euo pipefail

REPO="${UNITARES_REVIEW_REPO:-$HOME/projects/unitares}"
LANE="${UNITARES_REVIEW_WORKTREES:-$HOME/projects/wt}"
BASE_WT="$LANE/unitares-review-bot"
PR_WT="$LANE/unitares-review-bot-pr"

# The installed runner scopes and revokes the token, including nested review
# reads/writes. Never fall back to the human login for this App's repository.
APP_RUNNER=()
case "$(git -C "$REPO" remote get-url origin | tr '[:upper:]' '[:lower:]')" in
    https://github.com/cirwel/*)
        command -v unitares-github >/dev/null || {
            echo "[review-sweep] unitares-github is required; no human-login fallback" >&2
            exit 2
        }
        APP_RUNNER=(unitares-github)
        (cd "$REPO" && "${APP_RUNNER[@]}" --read-only -- git fetch --quiet origin master)
        ;;
    *github.com*cirwel/*)
        echo "[review-sweep] cirwel repositories require their canonical HTTPS origin" >&2
        exit 2
        ;;
    *) git -C "$REPO" fetch --quiet origin master ;;
esac
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
if [[ ${#APP_RUNNER[@]} -gt 0 ]]; then
    "${APP_RUNNER[@]}" -- review sweep --worktree "$PR_WT" "$@" || rc=$?
else
    python3 scripts/dev/review_gate.py sweep --worktree "$PR_WT" "$@" || rc=$?
fi
if git -C "$REPO" worktree list --porcelain | grep -Fxq "worktree $PR_WT"; then
    git -C "$REPO" worktree lock --reason \
        "review-sweep PR checkout (com.unitares.review-sweep)" "$PR_WT" 2>/dev/null || true
fi
exit "$rc"
