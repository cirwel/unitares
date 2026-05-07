#!/usr/bin/env bash
# ship.sh — agent-friendly commit-and-deliver
#
# Routes changes to the right delivery path based on what they touch:
#   - Runtime code (agents/, src/mcp_handlers/, src/mcp_server*, src/core.py,
#     src/background_tasks.py) → feature branch + PR + auto-merge-on-green.
#   - Everything else → direct commit + push on the current branch.
#
# The split exists because multiple agents push to this repo concurrently.
# Runtime changes need a rollback artifact (the PR) and cross-agent
# visibility; docs/tests/helpers don't, and PR friction for every tiny
# edit would slow the fleet down.
#
# Usage:
#   ./scripts/dev/ship.sh "commit message"
#   ./scripts/dev/ship.sh --classify          # just print "runtime" or "other"
#
# Requirements: staged changes (git add already done), gh CLI authed.

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
)

classify() {
    local files; files=$(git diff --cached --name-only)
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

# Emit unresolved Watcher fingerprints touching staged files, comma-separated.
# Empty if nothing staged, no findings file, or no matches. Closes the race
# where ship.sh would otherwise compose a commit message before the operator
# processed a Watcher chime — CLAUDE.md asks for fingerprints in commit
# messages, but post-edit-hook → next-turn-chime is a separate channel from
# commit-message authorship. This pulls them back together.
collect_watcher_fingerprints() {
    local files; files=$(git diff --cached --name-only)
    [[ -n "$files" ]] || return 0
    local findings="$PROJECT_ROOT/data/watcher/findings.jsonl"
    [[ -f "$findings" ]] || return 0
    awk -v root="$PROJECT_ROOT" '{print root"/"$0}' <<< "$files" \
        | python3 "$PROJECT_ROOT/scripts/dev/_ship_watcher_fingerprints.py" "$findings"
}

if [[ "${1:-}" == "--classify" ]]; then
    classify
    exit 0
fi

MESSAGE="${1:-}"
if [[ -z "$MESSAGE" ]]; then
    echo "usage: ship.sh \"commit message\"" >&2
    exit 2
fi

KIND=$(classify)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Phase A advisory lease (RFC v0.5 §6.1). Telemetry-only: a held_by_other
# or service_unavailable outcome MUST NOT block the ship. We log the
# outcome and proceed regardless, and release on EXIT so the lease is
# returned even if a downstream step (commit, push, PR create) fails.
LEASE_RESULT=$(python3 "$PROJECT_ROOT/scripts/dev/_ship_lease_advisory.py" acquire \
    --surface-id="resident:/ship_sh_$BRANCH" \
    --surface-kind="ship_sh" \
    --intent="$MESSAGE" \
    --ttl-s=300 2>/dev/null || echo '{"outcome":"client_error","lease_id":null}')
LEASE_OUTCOME=$(printf '%s' "$LEASE_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("outcome",""))')
LEASE_ID=$(printf '%s' "$LEASE_RESULT" | python3 -c 'import json,sys; v=json.load(sys.stdin).get("lease_id"); print(v if v else "")')
echo "[ship] lease: $LEASE_OUTCOME"
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

case "$KIND" in
    empty)
        echo "nothing staged — stage files with 'git add' first" >&2
        exit 2 ;;
    runtime)
        SLUG=$(printf '%s' "$MESSAGE" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-' | cut -c1-40)
        # Agent-scoped prefix so concurrent agents' auto-branches are self-identifying.
        # Override with UNITARES_SHIP_AGENT=<name>; otherwise detect from env.
        AGENT_PREFIX="${UNITARES_SHIP_AGENT:-}"
        if [[ -z "$AGENT_PREFIX" ]]; then
            if [[ -n "${CLAUDECODE:-}" ]]; then
                AGENT_PREFIX="claude"
            else
                AGENT_PREFIX="codex"
            fi
        fi
        NEW_BRANCH="${AGENT_PREFIX}/auto/$(date +%Y%m%d-%H%M%S)-${SLUG}"
        echo "[ship] runtime path → $NEW_BRANCH (PR + auto-merge)"
        git checkout -b "$NEW_BRANCH"
        git commit -m "$COMMIT_MESSAGE"
        git push -u origin "$NEW_BRANCH"
        # GitHub caps PR titles at 256 chars; conventional-commit subjects are
        # ~72. Use only the first line so multi-line commit messages (subject +
        # body) don't blow past the GraphQL limit and fail PR creation. See
        # issue #289 — hit on PR #288 with a long-bodied commit.
        PR_TITLE=$(printf '%s\n' "$MESSAGE" | head -n1)
        PR_URL=$(gh pr create --title "$PR_TITLE" --body "Auto-shipped by ship.sh — runtime path. Auto-merge is enabled; CI gate applies.")
        echo "$PR_URL"
        gh pr merge --auto --squash "$PR_URL" || \
            echo "[ship] auto-merge not enabled (branch protection may require manual setup); PR is open"
        ;;
    other)
        echo "[ship] non-runtime → direct commit + push on $BRANCH"
        git commit -m "$COMMIT_MESSAGE"
        # Push to the same-name branch on origin, not whatever upstream tracks
        # (a feature branch may track master and would otherwise push ambiguously).
        git push origin "HEAD:$BRANCH"
        ;;
esac
