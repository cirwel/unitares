#!/usr/bin/env bash
# ship.sh — agent-friendly commit-and-deliver
#
# Default delivery is a draft PR for everything — Codex and Claude share one
# contract so concurrent sessions stay predictable and the operator is the
# merge gate (docs/operations/github-workflow-conventions.md). In `auto` mode:
#   - Runtime code (agents/, src/mcp_handlers/, src/mcp_server*, src/core.py,
#     src/background_tasks.py) and detached HEAD work → fresh agent-prefixed
#     branch + draft PR.
#   - Everything else on a named feature branch → draft PR on that branch.
#
# Multiple agents push to this repo concurrently, so every change wants a
# rollback artifact (the PR) and cross-agent visibility. Draft PRs make work
# visible without pretending it is ready to merge. Use --direct to opt out
# (docs/tests only), or --auto-merge when the operator explicitly wants
# auto-merge-on-green.
#
# Usage:
#   ./scripts/dev/ship.sh "commit message"
#   ./scripts/dev/ship.sh --stage-all "commit message"
#   ./scripts/dev/ship.sh --draft-pr "commit message"
#   ./scripts/dev/ship.sh --open-pr "commit message"
#   ./scripts/dev/ship.sh --auto-merge "commit message"
#   ./scripts/dev/ship.sh --direct "commit message"
#   ./scripts/dev/ship.sh --classify          # just print "runtime" or "other"
#   ./scripts/dev/ship.sh --plan "commit message"
#
# Requirements: staged changes (git add already done) unless --stage-all is
# used, gh CLI authed.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Source operator secrets (LEASE_PLANE_BEARER_TOKEN, etc.) so the lease-advisory
# helper below can authenticate. The lease plane fails closed without a token,
# which surfaces as "lease: service_unavailable" — harmless per RFC v0.5 §6.1
# (Phase A is non-fatal) but obscures real outages. Sourcing is best-effort:
# missing file is fine, agents on hosts without the secrets still ship.
SECRETS_FILE="${HOME}/.config/cirwel/secrets.env"
if [[ -f "$SECRETS_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    set +a
fi

RUNTIME_PATTERNS=(
    '^agents/'
    '^src/mcp_handlers/'
    '^src/mcp_server'
    '^src/core\.py$'
    '^src/background_tasks\.py$'
    # Governance decision pipeline — drives every auto_attest verdict.
    # Fell into "other" for PR #467 (CIRS reason-string + behavioral
    # components instrumentation) and shipped without a PR until manually
    # opened. Same rollback-artifact rationale as elixir/ below.
    '^src/governance_monitor\.py$'
    '^src/governance_state\.py$'
    '^src/monitor_[^/]+\.py$'
    '^src/cirs\.py$'
    '^src/behavioral_[^/]+\.py$'
    '^governance_core/'
    # BEAM substrate (Sentinel, lease plane, future apps under elixir/) is
    # runtime: every change wants a CI-gated PR, not direct push. Without
    # this, a fix to elixir/sentinel/ falls through to "other" and ships
    # without a rollback artifact (see PR #446 — opened manually).
    '^elixir/'
)

classify_paths() {
    local files="$1"
    if [[ -z "$files" ]]; then
        echo "empty"; return
    fi
    while IFS= read -r f; do
        for pat in "${RUNTIME_PATTERNS[@]}"; do
            if [[ "$f" =~ $pat ]]; then
                echo "runtime"; return
            fi
        done
    done <<< "$files"
    echo "other"
}

classify() {
    local files; files=$(git diff --cached --name-only)
    classify_paths "$files"
}

worktree_changed_files() {
    {
        git diff --name-only
        git diff --cached --name-only
        git ls-files --others --exclude-standard
    } | sort -u
}

classify_worktree() {
    local files; files=$(worktree_changed_files)
    classify_paths "$files"
}

# Emit unresolved Watcher fingerprints touching staged files, comma-separated.
# Empty if nothing staged, no findings file, or no matches. Closes the race
# where ship.sh would otherwise compose a commit message before the operator
# processed a Watcher chime — CLAUDE.md asks for fingerprints in commit
# messages, but post-edit-hook → next-turn-chime is a separate channel from
# commit-message authorship. This pulls them back together.
watcher_findings_file() {
    local primary legacy
    primary=$(python3 - <<'PY'
import os
from pathlib import Path

override = os.environ.get("UNITARES_WATCHER_DATA_DIR")
if override:
    state_dir = Path(override).expanduser()
else:
    state_dir = Path.home() / ".unitares" / "watcher"
print(state_dir / "findings.jsonl")
PY
)
    legacy="$PROJECT_ROOT/data/watcher/findings.jsonl"

    if [[ -s "$primary" ]]; then
        printf '%s\n' "$primary"
    elif [[ -s "$legacy" ]]; then
        printf '%s\n' "$legacy"
    elif [[ -f "$primary" ]]; then
        printf '%s\n' "$primary"
    else
        printf '%s\n' "$legacy"
    fi
}

collect_watcher_fingerprints() {
    local files; files=$(git diff --cached --name-only)
    [[ -n "$files" ]] || return 0
    local findings; findings=$(watcher_findings_file)
    [[ -f "$findings" ]] || return 0
    awk -v root="$PROJECT_ROOT" '{print root"/"$0}' <<< "$files" \
        | python3 "$PROJECT_ROOT/scripts/dev/_ship_watcher_fingerprints.py" "$findings"
}

usage() {
    cat >&2 <<'USAGE'
usage: ship.sh [--stage-all] [--draft-pr|--open-pr|--auto-merge|--direct] "commit message"
       ship.sh --classify
       ship.sh [--stage-all] --plan "commit message"

Modes:
  auto         draft PR for everything (the default convention); runtime/detached
               work mints a fresh agent-prefixed branch, other work uses the
               current branch. --direct opts out for docs/tests-only pushes.
  --draft-pr  commit, push current/new branch, and open a draft PR
  --open-pr   commit, push current/new branch, and open a ready PR
  --auto-merge
               commit, push current/new branch, open a ready PR, and enable auto-merge
  --direct    commit and push the current branch; refuses detached HEAD
  --stage-all stage the full current worktree before classifying/committing.
               With --plan, previews that route without mutating the index.
USAGE
}

MODE="${UNITARES_SHIP_MODE:-auto}"
PLAN_ONLY=0
STAGE_ALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --classify)
            classify
            exit 0
            ;;
        --plan|--dry-run)
            PLAN_ONLY=1
            shift
            ;;
        --stage-all|--all)
            STAGE_ALL=1
            shift
            ;;
        --draft-pr|--draft)
            MODE="draft_pr"
            shift
            ;;
        --open-pr|--pr)
            MODE="open_pr"
            shift
            ;;
        --auto-merge)
            MODE="auto_merge"
            shift
            ;;
        --direct)
            MODE="direct"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            echo "unknown option: $1" >&2
            usage
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

MESSAGE="${*:-}"
if [[ -z "$MESSAGE" ]]; then
    usage
    exit 2
fi

if [[ "$STAGE_ALL" == "1" && "$PLAN_ONLY" != "1" ]]; then
    echo "[ship] staging all worktree changes"
    git add -A
fi

if [[ "$STAGE_ALL" == "1" && "$PLAN_ONLY" == "1" ]]; then
    KIND=$(classify_worktree)
else
    KIND=$(classify)
fi
BRANCH=$(git branch --show-current)
HEAD_SHORT=$(git rev-parse --short HEAD)
DETACHED=0
if [[ -z "$BRANCH" ]]; then
    DETACHED=1
fi

normalize_mode() {
    case "$1" in
        auto|draft_pr|open_pr|auto_merge|direct)
            echo "$1" ;;
        draft-pr|draft)
            echo "draft_pr" ;;
        open-pr|pr)
            echo "open_pr" ;;
        auto-merge)
            echo "auto_merge" ;;
        *)
            echo "invalid" ;;
    esac
}

MODE=$(normalize_mode "$MODE")
if [[ "$MODE" == "invalid" ]]; then
    echo "invalid UNITARES_SHIP_MODE; expected auto, draft-pr, open-pr, auto-merge, or direct" >&2
    exit 2
fi

DELIVERY="$MODE"
FORCE_AUTO_BRANCH=0
if [[ "$MODE" == "auto" ]]; then
    # Draft PR for everything: every session lands work as a draft PR regardless
    # of agent or whether the change is runtime or docs/tests, so the operator
    # stays the merge gate. Runtime and detached work additionally mint a fresh
    # agent-prefixed branch; non-runtime work on a named feature branch opens the
    # draft PR on that branch. --direct opts out for docs/tests-only pushes.
    DELIVERY="draft_pr"
    if [[ "$KIND" == "runtime" || "$DETACHED" == "1" ]]; then
        FORCE_AUTO_BRANCH=1
    fi
elif [[ "$DETACHED" == "1" && "$MODE" != "direct" ]]; then
    FORCE_AUTO_BRANCH=1
fi

if [[ "$KIND" == "empty" ]]; then
    echo "nothing staged — stage files with 'git add' first" >&2
    exit 2
fi

if [[ "$DELIVERY" == "direct" && "$DETACHED" == "1" ]]; then
    echo "detached HEAD cannot use direct delivery; rerun with --draft-pr or create a branch" >&2
    exit 2
fi

if [[ "$BRANCH" == "main" || "$BRANCH" == "master" ]]; then
    case "$DELIVERY" in
        draft_pr|open_pr|auto_merge)
            FORCE_AUTO_BRANCH=1 ;;
    esac
fi

if [[ "$PLAN_ONLY" == "1" ]]; then
    branch_label="${BRANCH:-"(detached)"}"
    echo "kind=$KIND"
    echo "branch=$branch_label"
    echo "head=$HEAD_SHORT"
    echo "mode=$MODE"
    echo "delivery=$DELIVERY"
    echo "force_auto_branch=$FORCE_AUTO_BRANCH"
    echo "stage_all=$STAGE_ALL"
    exit 0
fi

# Phase A advisory / Phase B enforcement lease. In advisory mode a
# held_by_other or service_unavailable outcome is telemetry-only. When
# LEASE_PLANE_ENFORCED_SURFACE_KINDS includes this surface kind, a missing
# lease blocks the ship.
LEASE_BRANCH="${BRANCH:-detached-$HEAD_SHORT}"
LEASE_RESULT=$(python3 "$PROJECT_ROOT/scripts/dev/_ship_lease_advisory.py" acquire \
    --surface-id="resident:/ship_sh_$LEASE_BRANCH" \
    --surface-kind="ship_sh" \
    --intent="$MESSAGE" \
    --ttl-s=300 2>/dev/null || echo '{"outcome":"client_error","lease_id":null}')
LEASE_OUTCOME=$(printf '%s' "$LEASE_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("outcome",""))')
LEASE_ID=$(printf '%s' "$LEASE_RESULT" | python3 -c 'import json,sys; v=json.load(sys.stdin).get("lease_id"); print(v if v else "")')
LEASE_BLOCKED=$(printf '%s' "$LEASE_RESULT" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("blocked") else "0")')
echo "[ship] lease: $LEASE_OUTCOME"
if [[ "$LEASE_BLOCKED" == "1" ]]; then
    echo "[ship] blocked by Phase B lease enforcement" >&2
    exit 1
fi
if [[ -n "$LEASE_ID" ]]; then
    trap 'python3 "$PROJECT_ROOT/scripts/dev/_ship_lease_advisory.py" release --lease-id="$LEASE_ID" >/dev/null 2>&1 || true' EXIT
fi

# Append Watcher-Findings trailer if any unresolved findings touch staged files.
# COMMIT_MESSAGE is what `git commit -m` sees; MESSAGE stays clean for PR title.
WATCHER_FPS=$(collect_watcher_fingerprints || true)
if [[ -n "$WATCHER_FPS" ]]; then
    COMMIT_MESSAGE="$MESSAGE

Watcher-Findings: $WATCHER_FPS"
    echo "[ship] appended Watcher-Findings trailer: $WATCHER_FPS"
else
    COMMIT_MESSAGE="$MESSAGE"
fi

# S15-d gate: if this commit touches skills/, the plugin's mirror must be
# in sync with unitares canonical. Fires only when skills/ is staged AND the
# plugin checkout is reachable (script no-ops on operators without it).
if git diff --cached --name-only | grep -q '^skills/'; then
    if ! "$PROJECT_ROOT/scripts/dev/sync-plugin-skills.sh" --check; then
        echo
        echo "[ship] skills/ staged but plugin bundle is out of sync." >&2
        echo "[ship] run: ./scripts/dev/sync-plugin-skills.sh" >&2
        echo "[ship] then commit the plugin-side mirror update before shipping the unitares-side change." >&2
        exit 1
    fi
fi

if [[ "$KIND" != "runtime" && "$KIND" != "other" ]]; then
    echo "unknown staged-change classification: $KIND" >&2
    exit 2
fi

create_auto_branch_if_needed() {
    if [[ "$FORCE_AUTO_BRANCH" != "1" ]]; then
        return 0
    fi

    local slug
    slug=$(printf '%s' "$MESSAGE" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-' | cut -c1-40)
    if [[ -z "$slug" ]]; then
        slug="change"
    fi

    # Agent-scoped prefix so concurrent agents' auto-branches are self-identifying.
    # Override with UNITARES_SHIP_AGENT=<name>; otherwise detect from env.
    local agent_prefix="${UNITARES_SHIP_AGENT:-}"
    if [[ -z "$agent_prefix" ]]; then
        if [[ -n "${CLAUDECODE:-}" ]]; then
            agent_prefix="claude"
        else
            agent_prefix="codex"
        fi
    fi

    local new_branch="${agent_prefix}/auto/$(date +%Y%m%d-%H%M%S)-${slug}"
    echo "[ship] creating PR branch: $new_branch"
    git checkout -b "$new_branch"
    BRANCH="$new_branch"
}

create_or_show_pr() {
    local pr_kind="$1"
    local pr_title pr_body pr_url existing_url

    # GitHub caps PR titles at 256 chars; conventional-commit subjects are
    # ~72. Use only the first line so multi-line commit messages (subject +
    # body) don't blow past the GraphQL limit and fail PR creation. See
    # issue #289 — hit on PR #288 with a long-bodied commit.
    pr_title=$(printf '%s\n' "$MESSAGE" | head -n1)

    existing_url=$(gh pr view --json url --jq .url 2>/dev/null || true)
    if [[ -n "$existing_url" ]]; then
        echo "[ship] PR already exists for $BRANCH"
        echo "$existing_url"
        return 0
    fi

    case "$pr_kind" in
        draft_pr)
            pr_body="Auto-shipped by ship.sh as a draft PR. Local work is visible; mark ready after validation/review."
            pr_url=$(gh pr create --draft --title "$pr_title" --body "$pr_body")
            ;;
        open_pr)
            pr_body="Auto-shipped by ship.sh as an open PR. CI gate applies."
            pr_url=$(gh pr create --title "$pr_title" --body "$pr_body")
            ;;
        auto_merge)
            pr_body="Auto-shipped by ship.sh. Auto-merge requested; CI gate applies."
            pr_url=$(gh pr create --title "$pr_title" --body "$pr_body")
            ;;
        *)
            echo "internal error: unknown PR kind $pr_kind" >&2
            exit 2
            ;;
    esac
    echo "$pr_url"

    if [[ "$pr_kind" == "auto_merge" ]]; then
        gh pr merge --auto --squash "$pr_url" || \
            echo "[ship] auto-merge not enabled (branch protection may require manual setup); PR is open"
    fi
}

case "$DELIVERY" in
    direct)
        echo "[ship] direct path → commit + push on $BRANCH"
        git commit -m "$COMMIT_MESSAGE"
        # Push to the same-name branch on origin, not whatever upstream tracks
        # (a feature branch may track master and would otherwise push ambiguously).
        git push -u origin "HEAD:$BRANCH"
        ;;
    draft_pr|open_pr|auto_merge)
        create_auto_branch_if_needed
        echo "[ship] PR path → $BRANCH ($DELIVERY)"
        git commit -m "$COMMIT_MESSAGE"
        git push -u origin "$BRANCH"
        create_or_show_pr "$DELIVERY"
        ;;
    *)
        echo "internal error: unknown delivery path $DELIVERY" >&2
        exit 2
        ;;
esac
