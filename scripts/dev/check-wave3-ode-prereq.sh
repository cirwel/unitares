#!/usr/bin/env bash
# check-wave3-ode-prereq.sh — Wave 3 §14 ordering gate.
#
# RFC (docs/proposals/beam-wave-3-handler-dispatch.md §14): all ten prereq
# PRs land BEFORE any commit in elixir/handler_dispatch/. This lint engages
# only when an elixir/handler_dispatch tree exists; until then it passes,
# so it is safe to wire into CI from prereq PR #1 onward.
#
# When engaged, it requires:
#   1. The prereq-PR #1 artifacts (shadow migrations, comparator, drift gate).
#   2. The §5.2 boundary-cost audit artifact. The RFC names the audit output
#      docs/handoffs/wave-3-section-5-2-boundary-audit-<date>.md, but
#      docs/handoffs/ is gitignored — un-checkable in CI. ADAPTATION
#      (documented for council review in prereq PR #1): a committed summary at
#      docs/proposals/wave-3-section-5-2-boundary-audit-summary.md satisfies
#      the gate in CI; the local gitignored handoff also satisfies it for
#      local runs. Either is sufficient.
#
# Exit codes: 0 = gate passes (or not engaged); 1 = gate violated.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if [[ ! -e "$ROOT/elixir/handler_dispatch" ]]; then
  echo "[wave3-prereq] elixir/handler_dispatch absent — gate not engaged (pass)"
  exit 0
fi

fail=0

required=(
  "db/postgres/migrations/043_identities_shadow.sql"
  "db/postgres/migrations/044_agents_shadow.sql"
  "db/postgres/schema_drift_check.sh"
  "scripts/ops/wave-3-shadow-divergence-check.sql"
  "scripts/ops/wave3_shadow_divergence_check.py"
  "scripts/ops/wave3-shadow-replay.sh"
)
for f in "${required[@]}"; do
  if [[ ! -e "$ROOT/$f" ]]; then
    echo "[wave3-prereq] MISSING prereq artifact: $f" >&2
    fail=1
  fi
done

audit_ok=0
if [[ -e "$ROOT/docs/proposals/wave-3-section-5-2-boundary-audit-summary.md" ]]; then
  audit_ok=1
fi
if compgen -G "$ROOT/docs/handoffs/wave-3-section-5-2-boundary-audit-*.md" >/dev/null 2>&1; then
  audit_ok=1
fi
if [[ "$audit_ok" -ne 1 ]]; then
  echo "[wave3-prereq] MISSING §5.2 boundary-cost audit artifact (committed summary" >&2
  echo "  docs/proposals/wave-3-section-5-2-boundary-audit-summary.md, or local" >&2
  echo "  docs/handoffs/wave-3-section-5-2-boundary-audit-<date>.md)" >&2
  fail=1
fi

if [[ "$fail" -eq 0 ]]; then
  echo "[wave3-prereq] gate engaged and satisfied"
fi
exit $fail
