#!/usr/bin/env bash
#
# wave-3a-rollback.sh — drop entries from the Wave 3a per-tool routing table.
#
# Spec: docs/proposals/beam-wave-3a-read-only-handlers.md v0.2 §3.1
# ("Cutover and rollback shape"). The Wave 3a routing table lives in the
# Python MCP transport (src/wave3a_routing.py) and is mutated at runtime by
# this script via the admin endpoint at /v1/admin/wave3a/routing-table.
#
# Modes:
#   --all                       Drop every row from the routing table.
#   --tool <name>               Drop a single tool from the routing table.
#   --list                      Show current routes (no mutation).
#   -h | --help                 Print this banner.
#
# Auth: requires UNITARES_OPERATOR_TOKEN in the environment (single token,
# matched against the server's UNITARES_OPERATOR_TOKENS allowlist). For
# convenience, ~/.config/cirwel/secrets.env is sourced if present.
#
# Endpoint discovery: defaults to http://127.0.0.1:8767. Override via
# UNITARES_GOVERNANCE_MCP_URL.
#
# Fail-closed: if the MCP server is unreachable, the script exits non-zero
# with a diagnostic. The operator can then restart the MCP, which itself
# starts with an empty routing table (§3.1 invariant).
#
# Smoke test (RFC §5 PR #3): running `--all` against an empty table MUST
# exit 0 cleanly. This exercises the rollback contract before any handler
# is ported. Covered by
# tests/integration/test_wave_3a_routing_table.py::test_rollback_empty_table_smoke.

set -euo pipefail

# --- argument parsing -------------------------------------------------------

usage() {
    sed -n '2,28p' "$0" | sed -e 's/^# \{0,1\}//'
}

MODE=""
TOOL_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)
            MODE="all"
            shift
            ;;
        --tool)
            if [[ $# -lt 2 ]]; then
                echo "error: --tool requires a tool name" >&2
                exit 2
            fi
            MODE="tool"
            TOOL_NAME="$2"
            shift 2
            ;;
        --list)
            MODE="list"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            echo "" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$MODE" ]]; then
    echo "error: one of --all, --tool <name>, or --list is required" >&2
    echo "" >&2
    usage >&2
    exit 2
fi

# --- environment ------------------------------------------------------------

SECRETS_FILE="${HOME}/.config/cirwel/secrets.env"
if [[ -z "${UNITARES_OPERATOR_TOKEN:-}" && -r "$SECRETS_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$SECRETS_FILE"; set +a
fi

if [[ -z "${UNITARES_OPERATOR_TOKEN:-}" ]]; then
    echo "error: UNITARES_OPERATOR_TOKEN not set (looked in env and ${SECRETS_FILE})" >&2
    echo "       The token must be present in the server's UNITARES_OPERATOR_TOKENS allowlist." >&2
    exit 3
fi

BASE_URL="${UNITARES_GOVERNANCE_MCP_URL:-http://127.0.0.1:8767}"
ENDPOINT="${BASE_URL}/v1/admin/wave3a/routing-table"

# --- dispatch ---------------------------------------------------------------

# curl flags:
#   -sS     silent but show errors
#   -f      fail on HTTP >= 400 (so we exit non-zero on auth failure)
#   --max-time 5  fail-closed if the MCP is unreachable
#   -w '\n%{http_code}\n'  echo status code on last line
COMMON_CURL_ARGS=(
    -sS
    --max-time 5
    -H "X-Unitares-Operator: ${UNITARES_OPERATOR_TOKEN}"
    -H "Accept: application/json"
)

case "$MODE" in
    all)
        # DELETE with no path segment clears all routes.
        HTTP_RESPONSE=$(curl "${COMMON_CURL_ARGS[@]}" \
            -X DELETE \
            -w '\n%{http_code}' \
            "$ENDPOINT") || {
            echo "error: MCP unreachable at ${ENDPOINT}" >&2
            exit 4
        }
        ;;
    tool)
        HTTP_RESPONSE=$(curl "${COMMON_CURL_ARGS[@]}" \
            -X DELETE \
            -w '\n%{http_code}' \
            "${ENDPOINT}/${TOOL_NAME}") || {
            echo "error: MCP unreachable at ${ENDPOINT}/${TOOL_NAME}" >&2
            exit 4
        }
        ;;
    list)
        HTTP_RESPONSE=$(curl "${COMMON_CURL_ARGS[@]}" \
            -X GET \
            -w '\n%{http_code}' \
            "$ENDPOINT") || {
            echo "error: MCP unreachable at ${ENDPOINT}" >&2
            exit 4
        }
        ;;
esac

HTTP_BODY=$(printf '%s' "$HTTP_RESPONSE" | sed '$d')
HTTP_STATUS=$(printf '%s' "$HTTP_RESPONSE" | tail -n 1)

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "error: HTTP ${HTTP_STATUS} from ${ENDPOINT}" >&2
    echo "$HTTP_BODY" >&2
    exit 5
fi

# Success — echo the body for the operator to verify.
echo "$HTTP_BODY"
exit 0
