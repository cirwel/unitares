#!/usr/bin/env bash
# review.sh — review this branch's PR diff and post the record the `review`
# status check reads. The review-side counterpart of test-cache.sh: one
# command, keyed on the diff, a no-op when this diff already has a record.
#
#   ./scripts/dev/review.sh --reviewer claude    # choose based on this change's context
#   ./scripts/dev/review.sh --reviewer antigravity --agy-model claude-opus-5-5-high
#   UNITARES_REVIEWER=antigravity ./scripts/dev/review.sh
#   ./scripts/dev/review.sh                     # read existing review; no implicit provider
#   ./scripts/dev/review.sh --background --reviewer claude  # detach; join before readiness
#   ./scripts/dev/review.sh --fresh --reviewer claude  # re-review even if recorded
#   ./scripts/dev/review.sh --reviewer claude   # explicit choice also retries cooldown
#   ./scripts/dev/review.sh record FILE --reviewer-name NAME --independent
#   ./scripts/dev/review.sh dispose FILE        # dispositions for a FINDINGS record
#   ./scripts/dev/review.sh waive --reason TEXT --operator-approved  # operator only
#
# Semantics, record format and the CI half: scripts/dev/review_gate.py.
# Exit 0: completed/disposed. Exit 1: findings need author action.
# Exit 2: unavailable/incomplete review; report an explicit unreviewed handoff.
# Exit 3: a review passed, but a security-sensitive diff still needs a second
#         model family; run one of the --fresh --reviewer commands it prints.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

BACKGROUND=false
ARGS=()
for a in "$@"; do
    case "$a" in
        --background) BACKGROUND=true ;;
        *) ARGS+=("$a") ;;
    esac
done

case "${ARGS[0]:-}" in
    record|dispose|waive|key|sweep) CMD=("${ARGS[@]}") ;;
    *) CMD=(review "${ARGS[@]+"${ARGS[@]}"}") ;;
esac

if [[ "$BACKGROUND" == true ]]; then
    mkdir -p .review-cache
    log=".review-cache/review-$(date +%Y%m%d-%H%M%S).log"
    # nohup + setsid-equivalent: the review must outlive the shell (and the
    # agent session) that shipped the PR. Its record lands on the PR itself.
    nohup python3 scripts/dev/review_gate.py "${CMD[@]}" >"$log" 2>&1 </dev/null &
    echo "[review] running in background (pid $!) — log: $log"
    echo "[review] the record posts to the PR; the \`review\` check turns green when it is clean"
    echo "[review] (a security-sensitive diff needs clean reviews from two model families; run review.sh in the foreground to see what is missing)"
    exit 0
fi

exec python3 scripts/dev/review_gate.py "${CMD[@]}"
