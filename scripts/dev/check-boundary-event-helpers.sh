#!/usr/bin/env bash
# check-boundary-event-helpers.sh — Wave 3 §6.4 payload-construction lint
# (§14 prereq PR #3).
#
# Enforcement model: the dotted boundary/measurement event-type LITERALS may
# appear only in the canonical constants/helpers modules (and tests/docs).
# Emission sites import the constants and build payloads via the helpers —
# an inline literal at an emission site is how payload contracts drift and
# how typo'd event_types silently vanish from replay queries.
#
# Canonical homes (the allowlist):
#   src/coordination_events.py                         (Python constants)
#   governance_core/coordination_events_helpers.py     (Python payload makers)
#   elixir/lease_plane/lib/unitares_lease_plane/coordination_payloads.ex
#                                                      (BEAM constants + makers)
# Plus: tests/**, elixir/**/test/**, docs/**, scripts/ops/*.sql (comparator
# SQL legitimately names event types in comments), and *.md.
#
# Exit codes: 0 = clean; 1 = violation(s) found.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Require a quote character immediately before the literal: CODE literals are
# quoted ("..."/'...'); prose mentions in docstrings/comments (backticked or
# bare) are not. A quoted mention inside a docstring would still trip — keep
# prose unquoted or use backticks.
PATTERN='["'"'"'](coordination_failure\.beam_python_boundary\.|measurement\.lease_plane\.|measurement\.governance_mcp\.|measurement\.beam_python_boundary\.)'

violations=$(
  grep -rnE "$PATTERN" \
    "$ROOT/src" "$ROOT/governance_core" "$ROOT/scripts" "$ROOT/agents" "$ROOT/elixir" \
    --include='*.py' --include='*.ex' --include='*.exs' --include='*.sh' \
    2>/dev/null |
    grep -v "src/coordination_events.py" |
    grep -v "governance_core/coordination_events_helpers.py" |
    grep -v "elixir/lease_plane/lib/unitares_lease_plane/coordination_payloads.ex" |
    grep -vE "/tests?/" |
    grep -v "scripts/dev/check-boundary-event-helpers.sh" |
    grep -v "scripts/ops/wave-3-shadow-divergence-check.sql" |
    grep -v "scripts/ops/wave-0-channel-report.sh" || true
# Allowlist rationale: the comparator SQL and the channel report are
# READ-side surfaces (queries over already-emitted rows), not emission
# sites — the containment policy targets payload/type construction at
# write time.
)

if [[ -n "$violations" ]]; then
  echo "[boundary-helpers] VIOLATION: boundary/measurement event-type literals" >&2
  echo "outside the canonical constants/helpers modules. Import the constant" >&2
  echo "from src/coordination_events.py (Python) or use" >&2
  echo "UnitaresLeasePlane.CoordinationPayloads (Elixir) instead:" >&2
  echo "$violations" >&2
  exit 1
fi

echo "[boundary-helpers] clean — event-type literals confined to canonical modules"
