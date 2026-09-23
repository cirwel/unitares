#!/usr/bin/env bash
# review.sh — review this branch's PR diff and post the record the `review`
# status check reads. The review-side counterpart of test-cache.sh: one
# command, keyed on the diff, a no-op when this diff already has a record.
#
#   ./scripts/dev/review.sh                     # start or join review; ship.sh does this
#   ./scripts/dev/review.sh --background        # explicitly detach; join before readiness
#   ./scripts/dev/review.sh --fresh             # re-review even if recorded
#   ./scripts/dev/review.sh --reviewer claude   # override (default: the other model)
#   ./scripts/dev/review.sh record FILE --reviewer-name NAME   # a council/human review
#   ./scripts/dev/review.sh dispose FILE        # dispositions for a FINDINGS record
#
# Semantics, record format and the CI half: scripts/dev/review_gate.py.

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
    record|dispose|key) CMD=("${ARGS[@]}") ;;
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
    exit 0
fi

exec python3 scripts/dev/review_gate.py "${CMD[@]}"
