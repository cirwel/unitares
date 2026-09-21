#!/usr/bin/env bash
# Cloud-session setup: install the UNITARES governance plugin into an
# ephemeral Claude Code cloud container.
#
# Why this exists. Plugin install state lives in ~/.claude/plugins on the
# machine that ran `/plugin`, per user and not per project. A cloud container
# is provisioned fresh, so that state never arrives. The documented fallback —
# declaring extraKnownMarketplaces in the repo's .claude/settings.json — is
# closed here by check-repo-scope.sh Rules 0 and 1, which refuse any tracked
# .claude/ file. A cloud-environment setup script is the remaining route, and
# it keeps the wiring out of the tracked tree entirely.
#
# docs/operations/cloud-session-plugin.md gives the setup-field wrapper. It
# verifies a dedicated environment's canonical remote and reads this payload
# from origin/master before executing it; do not run a checkout-relative copy
# from a shared environment. Once invoked, this script is idempotent and keeps
# every internal exit successful: a broken install leaves a session without
# governance hooks, which is the state it would have had anyway.
#
# This script does NOT set UNITARES_* variables. A setup script's exports die
# with its shell and never reach the agent process, so the operator declares
# them as environment variables on the cloud environment instead. The preflight
# below reports what is actually set.

set -uo pipefail

MARKETPLACE_SOURCE="${UNITARES_PLUGIN_SOURCE:-cirwel/unitares-governance-plugin}"
MARKETPLACE_NAME="unitares-governance"
PLUGIN_ID="unitares-governance@${MARKETPLACE_NAME}"

log() { printf '[unitares-setup] %s\n' "$*"; }

# Every exit path is success. A cloud session must start even when the plugin
# does not.
finish() { log "$1"; exit 0; }

command -v claude >/dev/null 2>&1 || finish "claude CLI not on PATH — skipping plugin install."

# --- install -------------------------------------------------------------

if claude plugin marketplace list 2>/dev/null | grep -q "${MARKETPLACE_NAME}"; then
  log "marketplace ${MARKETPLACE_NAME} already registered"
else
  log "adding marketplace ${MARKETPLACE_SOURCE}"
  if ! timeout 180 claude plugin marketplace add "${MARKETPLACE_SOURCE}" 2>&1 | sed 's/^/  /'; then
    finish "marketplace add failed — continuing without governance hooks."
  fi
fi

plugin_json=$(claude plugin list --json 2>/dev/null) || plugin_json=""
plugin_state=$(printf '%s' "${plugin_json}" | python3 -c '
import json
import sys

target = sys.argv[1]
for plugin in json.load(sys.stdin):
    if plugin.get("id") == target:
        print("enabled" if plugin.get("enabled") else "disabled")
        break
else:
    print("missing")
' "${PLUGIN_ID}" 2>/dev/null) || plugin_state="unknown"

case "${plugin_state}" in
  enabled)
    log "plugin ${PLUGIN_ID} already installed and enabled"
    ;;
  disabled)
    log "enabling ${PLUGIN_ID}"
    if ! timeout 180 claude plugin enable "${PLUGIN_ID}" 2>&1 | sed 's/^/  /'; then
      finish "plugin enable failed — continuing without governance hooks."
    fi
    ;;
  missing)
    log "installing ${PLUGIN_ID}"
    if ! timeout 180 claude plugin install "${PLUGIN_ID}" 2>&1 | sed 's/^/  /'; then
      finish "plugin install failed — continuing without governance hooks."
    fi
    ;;
  *)
    finish "could not inspect plugin state — continuing without governance hooks."
    ;;
esac

# --- preflight -----------------------------------------------------------
#
# Reports only. Nothing here blocks the setup script. Hooks fail open under the
# documented cloud posture; UNITARES_FILE_LEASES_REQUIRED must be false because
# that explicit fail-closed override takes precedence over disabling leases.

SERVER_URL="${UNITARES_SERVER_URL:-}"
preflight_ok=1
auth_probe_deferred=0
proxy_auth_value=$(printf '%s' "${UNITARES_CLOUD_PROXY_AUTH:-0}" \
  | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
case "${proxy_auth_value}" in
  1|true|on|yes) proxy_auth_configured=1 ;;
  *) proxy_auth_configured=0 ;;
esac

# Capture the response body and status separately. curl can return non-zero
# after receiving HTTP headers (for example, when a response times out), so do
# not erase a useful status with 000 merely because its process status failed.
probe_url() {
  local response
  if [ -n "${UNITARES_HTTP_API_TOKEN:-}" ]; then
    response=$(printf 'Authorization: Bearer %s\n' "${UNITARES_HTTP_API_TOKEN}" \
      | curl -sS --max-time 3 -H @- "$@" -w '\n%{http_code}' 2>/dev/null)
    PROBE_CURL_RC=$?
  else
    response=$(curl -sS --max-time 3 "$@" -w '\n%{http_code}' 2>/dev/null)
    PROBE_CURL_RC=$?
  fi
  case "${response}" in
    *$'\n'[0-9][0-9][0-9])
      PROBE_CODE="${response##*$'\n'}"
      PROBE_BODY="${response%$'\n'*}"
      ;;
    *)
      PROBE_CODE=000
      PROBE_BODY="${response}"
      ;;
  esac
}

if [ -z "${SERVER_URL}" ]; then
  preflight_ok=0
  log "WARN UNITARES_SERVER_URL unset — hooks will target http://localhost:8767,"
  log "     which does not exist in a cloud container. Hooks run and report"
  log "     OFFLINE. Set it on the cloud environment to reach a real server."
else
  log "UNITARES_SERVER_URL=${SERVER_URL}"
  case "${SERVER_URL}" in
    https://*) ;;
    *) log "WARN not https:// — container egress is proxied; plain-HTTP and" \
           "non-standard ports do not leave the sandbox." ;;
  esac
  BASE_URL="${SERVER_URL%/}"
  case "${BASE_URL}" in
    */mcp)
      preflight_ok=0
      log "WARN UNITARES_SERVER_URL must be the server base URL, without /mcp;" \
          "hooks append /health and /v1/tools/call themselves."
      ;;
  esac

  if [ "${proxy_auth_configured}" -eq 1 ] \
      && [ -z "${UNITARES_HTTP_API_TOKEN:-}" ]; then
    # Environment API credentials and their host reachability are available
    # only after Claude Code starts, never to setup-script requests. Defer the
    # whole network preflight instead of manufacturing OFFLINE warnings here.
    auth_probe_deferred=1
    log "note hook network/authentication probes deferred; setup requests do not"
    log "     receive the environment API credential. Verify from a running hook."
  else
    # Probe both routes used by the plugin hooks. The health route proves basic
    # reachability. The deliberately invalid REST request proves the bearer and
    # DNS-rebinding gates without invoking a tool or changing server state. Its
    # distinctive validation error prevents a proxy's generic 400 from looking
    # like a usable UNITARES endpoint.
    HEALTH_URL="${BASE_URL}/health"
    probe_url "${HEALTH_URL}"
    if [ "${PROBE_CURL_RC}" -ne 0 ]; then
      preflight_ok=0
      if [ "${PROBE_CODE}" = "000" ]; then
        log "WARN ${HEALTH_URL} unreachable (curl ${PROBE_CURL_RC}). If the domain" \
            "is not on the environment's network allowlist the egress proxy refuses it."
      else
        log "WARN ${HEALTH_URL} transfer failed after HTTP ${PROBE_CODE}" \
            "(curl ${PROBE_CURL_RC}); the session-start hook may be OFFLINE."
      fi
    else
      case "${PROBE_CODE}" in
        200) log "server health route usable (200)" ;;
        *)
          preflight_ok=0
          log "WARN ${HEALTH_URL} returned ${PROBE_CODE}; the session-start hook may be OFFLINE."
          ;;
      esac
    fi

    TOOLS_URL="${BASE_URL}/v1/tools/call"
    probe_url -H 'Content-Type: application/json' --data '{}' "${TOOLS_URL}"
    code="${PROBE_CODE}"
    if [ "${PROBE_CURL_RC}" -ne 0 ]; then
      preflight_ok=0
      if [ "${code}" = "000" ]; then
        log "WARN ${TOOLS_URL} unreachable (curl ${PROBE_CURL_RC}). If the domain" \
            "is not on the environment's network allowlist the egress proxy refuses it."
      else
        log "WARN ${TOOLS_URL} transfer failed after HTTP ${code}" \
            "(curl ${PROBE_CURL_RC}); hooks may be OFFLINE."
      fi
    else
      case "${code}" in
        400)
          if [[ "${PROBE_BODY}" == *"Missing 'name' field"* ]]; then
            log "server tool route usable (authenticated validation response)"
          else
            preflight_ok=0
            log "WARN ${TOOLS_URL} returned an unrecognized 400; hooks may be OFFLINE."
          fi
          ;;
        401)
          preflight_ok=0
          if [ -n "${UNITARES_HTTP_API_TOKEN:-}" ]; then
            log "WARN ${TOOLS_URL} rejected the configured bearer (401); hooks are OFFLINE."
          else
            log "WARN ${TOOLS_URL} requires a bearer (401); set UNITARES_HTTP_API_TOKEN."
          fi
          ;;
        421)
          preflight_ok=0
          log "WARN ${TOOLS_URL} rejected its external Host (421); add the hostname" \
              "to UNITARES_MCP_ALLOWED_HOSTS on the server."
          ;;
        *)
          preflight_ok=0
          log "WARN ${TOOLS_URL} returned ${code}; hooks may be OFFLINE."
          ;;
      esac
    fi
  fi
fi

if [ -z "${UNITARES_HTTP_API_TOKEN:-}" ]; then
  if [ "${proxy_auth_configured}" -eq 1 ]; then
    log "note UNITARES_HTTP_API_TOKEN intentionally unset; the environment proxy"
    log "     supplies hook authentication after launch. Attribution is session-bound."
  else
    preflight_ok=0
    log "WARN UNITARES_HTTP_API_TOKEN unset — REST hooks need another accepted"
    log "     authentication path or they receive 401. Attribution is session-bound."
  fi
fi

# The lease plane is a loopback service on the operator's machine. Absent here,
# the pre-edit hook normally fails open at connection-refused speed. The
# explicit REQUIRED policy is different: it overrides ENABLED=0 and blocks.
lease_required=$(printf '%s' "${UNITARES_FILE_LEASES_REQUIRED:-0}" \
  | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
case "${lease_required}" in
  1|true|on|yes)
    preflight_ok=0
    log "WARN UNITARES_FILE_LEASES_REQUIRED is true; pre-edit will block without" \
        "an in-container lease plane. Set it to 0 for the documented cloud posture."
    ;;
  *)
    if [ "${UNITARES_FILE_LEASES_ENABLED:-1}" != "0" ]; then
      log "note file leases enabled but no lease plane in-container; pre-edit fails open."
    fi
    ;;
esac

if [ "${preflight_ok}" -eq 1 ]; then
  if [ "${auth_probe_deferred}" -eq 1 ]; then
    log "done — hook authentication verification deferred until session start."
  else
    log "done"
  fi
else
  log "done with warnings — governance hooks are not fully usable."
fi
exit 0
