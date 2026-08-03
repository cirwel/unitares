#!/usr/bin/env bash
# check-deploy-lib.sh — contract check for the deploy-script library extraction.
#
# The per-service deploy scripts (deploy-mcp / gateway / sentinel / lease-plane
# / wave3a) share their lock, plist-preflight, and ff+nudge blocks via
# scripts/ops/deploy-lib.sh. Before the extraction those blocks were pasted
# five times and drifted (stale lock comments; deploy-lease-plane.sh shipped
# with NO plist preflight at all — the false-success gap the preflight exists
# to close). This check makes re-inlining a CI failure instead of a review
# hope:
#
#   1. every per-service script sources deploy-lib.sh
#   2. the lock-key derivation exists exactly once, in the lib (the key must
#      stay byte-stable so an old-copy script and a lib-based script still
#      mutually exclude during a partial-rollout window)
#   3. no script re-implements the mkdir lock outside the lib
#   4. every deploy shell script parses (bash -n)
#   5. shellcheck, when installed, is advisory (warnings never fail the gate —
#      versions differ across machines; determinism beats coverage here)
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OPS="$ROOT/scripts/ops"
LIB="$OPS/deploy-lib.sh"

SERVICE_SCRIPTS=(
  "$OPS/deploy-mcp.sh"
  "$OPS/deploy-gateway.sh"
  "$OPS/deploy-sentinel.sh"
  "$OPS/deploy-lease-plane.sh"
  "$OPS/deploy-wave3a.sh"
)
ALL_SCRIPTS=("$LIB" "${SERVICE_SCRIPTS[@]}" "$OPS/deploy-status.sh" "$OPS/deploy-apply.sh" "$OPS/nudge-lease-plane.sh")

fail=0
err() { echo "check-deploy-lib: FAIL: $*" >&2; fail=1; }

[[ -f "$LIB" ]] || { err "missing $LIB"; echo "check-deploy-lib: cannot continue" >&2; exit 1; }

# 1. every per-service script sources the lib
for s in "${SERVICE_SCRIPTS[@]}"; do
  [[ -f "$s" ]] || { err "missing expected deploy script: $s"; continue; }
  grep -q 'deploy-lib\.sh' "$s" || err "$(basename "$s") does not source deploy-lib.sh"
done

# 2. the lock-key derivation lives exactly once, in the lib. Match on the
#    distinctive derivation fragment, not the variable name.
LOCK_KEY_FRAGMENT="unitares-deploy\$(printf"
hits="$(grep -l -F "$LOCK_KEY_FRAGMENT" "$OPS"/*.sh 2>/dev/null || true)"
if [[ "$hits" != "$LIB" ]]; then
  err "lock-key derivation must appear exactly once (in deploy-lib.sh); found in: ${hits:-nowhere}"
fi

# 3. no per-service mkdir-lock re-implementation outside the lib
for s in "${SERVICE_SCRIPTS[@]}"; do
  [[ -f "$s" ]] || continue
  if grep -q 'mkdir "\$LOCK_DIR"' "$s"; then
    err "$(basename "$s") re-implements the mkdir lock — use deploy_lib_acquire_lock"
  fi
done

# 4. syntax-check everything
for s in "${ALL_SCRIPTS[@]}"; do
  [[ -f "$s" ]] || continue
  bash -n "$s" || err "bash -n failed for $(basename "$s")"
done

# 5. functional sandbox tests (lock semantics, lock-key byte-stability,
#    preflight policies, ff-worktree, poll) — throwaway fixtures only
bash "$ROOT/scripts/dev/test-deploy-lib.sh" || err "functional sandbox tests failed (scripts/dev/test-deploy-lib.sh)"

# 6. advisory shellcheck (never fails the gate)
if command -v shellcheck >/dev/null 2>&1; then
  # SC1091: sourced file not followed — the lib is sourced via a computed path.
  shellcheck -x -e SC1091 "$LIB" "${SERVICE_SCRIPTS[@]}" \
    || echo "check-deploy-lib: shellcheck reported findings (advisory only)" >&2
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "check-deploy-lib: OK"
