#!/usr/bin/env bash
# doc_audit.sh — Check all three Unitares repos for stale docs.
# Run manually: bash scripts/dev/doc_audit.sh

set -euo pipefail

GOV_DIR="${GOV_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
EISV_DIR="${EISV_DIR:-$GOV_DIR/../eisv-lumen}"
ANIMA_DIR="${ANIMA_DIR:-$GOV_DIR/../anima-mcp}"

STALE=0
note() { echo "  $1"; }
warn() { echo "  STALE: $1"; STALE=$((STALE + 1)); }
collect_tests() {
  local dir="$1"
  shift
  (
    cd "$dir" &&
      python3 -m pytest "$@" --collect-only -q 2>/dev/null || true
  ) |
    grep -oE '[0-9]+ tests collected' |
    tail -1 |
    grep -oE '^[0-9]+' || true
}

# --- unitares ---
echo "=== unitares ==="
if [ -d "$GOV_DIR/tests" ]; then
  gov_tests=$(collect_tests "$GOV_DIR")
  readme_num=$(
    grep -oE '[0-9,]+\+? collected' "$GOV_DIR/README.md" |
      head -1 |
      grep -oE '[0-9,]+' |
      tr -d ',' || true
  )
  if [ -z "$gov_tests" ]; then
    warn "Could not collect Unitares test count"
  elif [ -z "$readme_num" ]; then
    warn "README test-count wording not found"
  elif [ "$gov_tests" -lt "$readme_num" ]; then
    warn "README says at least $readme_num collected tests, actual: $gov_tests"
  else
    note "Test count OK ($gov_tests collected; README floor $readme_num)"
  fi
fi

gov_tools=$(grep -r '@mcp_tool' "$GOV_DIR/src/" 2>/dev/null | grep -v 'register=False' | wc -l | tr -d ' ')
note "Registered @mcp_tool count: $gov_tools"

# --- eisv-lumen ---
echo "=== eisv-lumen ==="
if [ -d "$EISV_DIR/tests" ]; then
  eisv_tests=$(collect_tests "$EISV_DIR" tests/)
  eisv_badge=$(grep -oE 'tests-[0-9]+' "$EISV_DIR/README.md" | grep -oE '[0-9]+' || true)
  if [ -z "$eisv_tests" ]; then
    warn "Could not collect eisv-lumen test count"
  elif [ -z "$eisv_badge" ]; then
    warn "README badge test count not found"
  elif [ "$eisv_tests" != "$eisv_badge" ]; then
    warn "README badge says $eisv_badge tests, actual: $eisv_tests"
  else
    note "Test badge OK ($eisv_tests)"
  fi
else
  note "eisv-lumen not found at $EISV_DIR (skipping)"
fi

# --- anima-mcp ---
echo "=== anima-mcp ==="
if [ -d "$ANIMA_DIR" ]; then
  # Check student model files exist
  if [ -d "$ANIMA_DIR/src/anima_mcp/eisv" ]; then
    note "EISV package exists"
  else
    warn "EISV package missing at $ANIMA_DIR/src/anima_mcp/eisv/"
  fi

  if grep -q "EISV Integration" "$ANIMA_DIR/README.md" 2>/dev/null; then
    note "EISV Integration section present in README"
  else
    warn "EISV Integration section missing from README"
  fi
else
  note "anima-mcp not found at $ANIMA_DIR (skipping)"
fi

# --- skills docs ---
echo "=== skills docs ==="
SKILLS_DIR="$HOME/.claude/skills/unitares-governance"
if [ -f "$SKILLS_DIR/SKILL.md" ]; then
  if grep -q 'thresholds.*field' "$SKILLS_DIR/SKILL.md" && grep -q 'do not hardcode' "$SKILLS_DIR/SKILL.md"; then
    note "Coherence threshold guidance OK (live thresholds)"
  else
    threshold=$(grep -oE 'Critical threshold at [0-9]+\.[0-9]+' "$SKILLS_DIR/SKILL.md" | grep -oE '[0-9]+\.[0-9]+' || true)
    if [ "$threshold" = "0.45" ]; then
      note "Coherence threshold OK (0.45)"
    elif [ -z "$threshold" ]; then
      warn "SKILL.md coherence threshold not found"
    else
      warn "SKILL.md coherence threshold is $threshold, expected 0.45"
    fi
  fi
fi

# --- summary ---
echo ""
if [ "$STALE" -eq 0 ]; then
  echo "All docs up to date."
else
  echo "$STALE stale item(s) found."
fi
exit "$STALE"
