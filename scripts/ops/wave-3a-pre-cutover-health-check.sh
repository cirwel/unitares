#!/usr/bin/env bash
#
# wave-3a-pre-cutover-health-check.sh — pre-cutover verification for PR #5.
#
# Spec: docs/proposals/beam-wave-3a-read-only-handlers.md v0.2 §2.4
# (identity gate) and §5 PR #5 ("pre-cutover script: verifies
# `health_check` reads `pre_onboard` via `get_tool_identity_requirement`").
#
# Per the v0.2 RFC §2.4 council fold, the operative mechanism that lets
# `pre_onboard` tools run without an onboarded identity is the middleware's
# attribute lookup, NOT the `@mcp_tool(..., requires_identity="pre_onboard")`
# decorator attribute being "informational". The decorator declares; the
# middleware reads. The two surfaces have drifted before (the historical
# hardcoded allowlist in `identity_step.py` is now a docstring comment).
#
# This script verifies the load-bearing post-conditions immediately before
# an operator flips WAVE_3A_HEALTH_CHECK_ON_BEAM:
#
#   1. `health_check` is registered in TOOL_HANDLERS.
#   2. `get_tool_identity_requirement("health_check")` returns the string
#      "pre_onboard" (exact match, not "informational" or "required" — both
#      would break the BEAM-side dispatch).
#
# Exit codes:
#   0 — both checks passed; safe to flip the flag.
#   1 — handler is missing from TOOL_HANDLERS.
#   2 — handler exists but `requires_identity != "pre_onboard"`.
#   3 — Python import failed (sanity error, run from repo root).
#
# Usage (run from anywhere on disk):
#
#     bash scripts/ops/wave-3a-pre-cutover-health-check.sh
#
# Suggested operator workflow:
#
#     1. Pull and restart MCP after merging PR #5.
#     2. Run this script. Verify exit 0.
#     3. Load the PR #4 BEAM listener launchd plist.
#     4. Edit ~/.config/cirwel/secrets.env:
#            export WAVE_3A_HEALTH_CHECK_ON_BEAM=true
#     5. Restart the MCP (so apply_env_flag_routes picks up the flag).
#     6. Sanity-check: `curl http://127.0.0.1:8767/v1/admin/wave3a/routing-table`
#        with operator token; verify `health_check` is in the response.
#     7. Cut a `health_check` call through MCP; verify the response has
#        `protocol_version: "wave3a.v1"` (BEAM path).
#
# Rollback: `bash scripts/ops/wave-3a-rollback.sh --tool health_check`.

set -euo pipefail

# Resolve repo root from this script's location so the operator can run it
# from anywhere on disk without `cd`.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "$REPO_ROOT"

python3 - <<'PY'
import sys


def fail(code: int, message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


try:
    from src.mcp_handlers import TOOL_HANDLERS
    from src.mcp_handlers.decorators import get_tool_identity_requirement
except Exception as exc:  # noqa: BLE001 — diagnostic surface
    fail(
        3,
        f"error: failed to import src.mcp_handlers: {exc!r}\n"
        "       Run from the repo root (this script does so automatically;\n"
        "       if you see this, the working tree is incomplete).",
    )

TOOL_NAME = "health_check"

if TOOL_NAME not in TOOL_HANDLERS:
    fail(
        1,
        f"error: {TOOL_NAME!r} is NOT registered in TOOL_HANDLERS.\n"
        f"       Wave 3a routing cannot fall back to Python — DO NOT flip\n"
        "       WAVE_3A_HEALTH_CHECK_ON_BEAM.",
    )

req = get_tool_identity_requirement(TOOL_NAME)
if req != "pre_onboard":
    fail(
        2,
        f"error: get_tool_identity_requirement({TOOL_NAME!r}) returned "
        f"{req!r}, expected 'pre_onboard'.\n"
        "       Per RFC §2.4 (council fold), the middleware's attribute "
        "lookup is\n"
        "       the operative gate; a non-'pre_onboard' value means callers "
        "without\n"
        "       an onboarded identity will be rejected at the dispatch "
        "middleware\n"
        "       before reaching the BEAM proxy. DO NOT flip "
        "WAVE_3A_HEALTH_CHECK_ON_BEAM.",
    )

print(
    f"ok: {TOOL_NAME!r} registered in TOOL_HANDLERS, "
    f"requires_identity={req!r}.\n"
    "Safe to set WAVE_3A_HEALTH_CHECK_ON_BEAM=true in "
    "~/.config/cirwel/secrets.env and restart the MCP."
)
PY
