#!/usr/bin/env bash
#
# wave-3a-pre-cutover-check.sh — generic pre-cutover verification for the
# Wave 3a §1.1 handlers (PR #6+).
#
# Spec: docs/proposals/beam-wave-3a-read-only-handlers.md v0.2 §2.4
# (identity gate) and §5 ("pre-cutover script: verifies the handler reads
# `pre_onboard` via `get_tool_identity_requirement`").
#
# Generalization of scripts/ops/wave-3a-pre-cutover-health-check.sh (PR #5,
# kept as-is for operator muscle memory): same two checks, parameterized by
# tool name. The env flag to flip afterwards is derived from the tool name
# (WAVE_3A_<TOOL_NAME_UPPER>_ON_BEAM) and must exist as a row in
# src/wave3a_routing.py::_ENV_FLAG_ROUTES — checked as a third post-condition
# so the operator can't flip a flag the startup hook will never read.
#
# Checks:
#   1. <tool> is registered in TOOL_HANDLERS.
#   2. get_tool_identity_requirement(<tool>) returns exactly "pre_onboard".
#   3. The derived env flag has a row in _ENV_FLAG_ROUTES.
#
# Exit codes:
#   0 — all checks passed; safe to flip the flag.
#   1 — handler is missing from TOOL_HANDLERS.
#   2 — handler exists but `requires_identity != "pre_onboard"`.
#   3 — Python import failed (sanity error, run from repo root).
#   4 — derived env flag has no _ENV_FLAG_ROUTES row.
#   5 — usage error (no tool name given).
#
# Usage (run from anywhere on disk):
#
#     bash scripts/ops/wave-3a-pre-cutover-check.sh get_server_info
#
# Suggested operator workflow (RFC §3.1 cutover shape):
#
#     1. Pull and restart MCP after merging the handler PR.
#     2. Run this script with the tool name. Verify exit 0.
#     3. Confirm the BEAM listener is healthy (PR #4 launchd plist loaded):
#            curl http://127.0.0.1:8770/health
#     4. Edit ~/.config/cirwel/secrets.env:
#            export WAVE_3A_<TOOL_NAME_UPPER>_ON_BEAM=true
#     5. Restart the MCP (so apply_env_flag_routes picks up the flag).
#     6. Sanity-check: `curl http://127.0.0.1:8767/v1/admin/wave3a/routing-table`
#        with operator token; verify the tool is in the response.
#     7. Cut a call through MCP; verify the response carries
#        `protocol_version: "wave3a.v1"` (BEAM path).
#
# Rollback: `bash scripts/ops/wave-3a-rollback.sh --tool <tool_name>`.

set -euo pipefail

TOOL_NAME="${1:-}"
if [[ -z "${TOOL_NAME}" ]]; then
    echo "usage: $0 <tool_name>   (e.g. $0 get_server_info)" >&2
    exit 5
fi

# Resolve repo root from this script's location so the operator can run it
# from anywhere on disk without `cd`.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "$REPO_ROOT"

TOOL_NAME="$TOOL_NAME" python3 - <<'PY'
import os
import sys


def fail(code: int, message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


tool_name = os.environ["TOOL_NAME"]
env_flag = f"WAVE_3A_{tool_name.upper()}_ON_BEAM"

try:
    from src.mcp_handlers import TOOL_HANDLERS
    from src.mcp_handlers.decorators import get_tool_identity_requirement
    from src.wave3a_routing import _ENV_FLAG_ROUTES
except Exception as exc:  # noqa: BLE001 — diagnostic surface
    fail(
        3,
        f"error: failed to import src modules: {exc!r}\n"
        "       Run from the repo root (this script does so automatically;\n"
        "       if you see this, the working tree is incomplete).",
    )

if tool_name not in TOOL_HANDLERS:
    fail(
        1,
        f"error: {tool_name!r} is NOT registered in TOOL_HANDLERS.\n"
        f"       Wave 3a routing cannot fall back to Python — DO NOT flip\n"
        f"       {env_flag}.",
    )

req = get_tool_identity_requirement(tool_name)
if req != "pre_onboard":
    fail(
        2,
        f"error: get_tool_identity_requirement({tool_name!r}) returned "
        f"{req!r}, expected 'pre_onboard'.\n"
        "       Per RFC §2.4 (council fold), the middleware's attribute "
        "lookup is\n"
        "       the operative gate; a non-'pre_onboard' value means callers "
        "without\n"
        "       an onboarded identity will be rejected at the dispatch "
        "middleware\n"
        f"       before reaching the BEAM proxy. DO NOT flip {env_flag}.",
    )

row = _ENV_FLAG_ROUTES.get(env_flag)
if row is None or row.get("tool_name") != tool_name:
    fail(
        4,
        f"error: {env_flag!r} has no matching row in "
        "src/wave3a_routing.py::_ENV_FLAG_ROUTES.\n"
        "       Flipping the flag would be a silent no-op at MCP startup —\n"
        "       add the routing row (RFC §5) before cutover.",
    )

print(
    f"ok: {tool_name!r} registered in TOOL_HANDLERS, "
    f"requires_identity={req!r}, routing row present for {env_flag}.\n"
    f"Safe to set {env_flag}=true in ~/.config/cirwel/secrets.env and "
    "restart the MCP."
)
PY
