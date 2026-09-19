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
# Wire it as the setup script of the cloud environment (claude.ai -> cloud
# environments). It runs after the repository is cloned:
#
#     bash scripts/dev/cloud-session-setup.sh
#
# Idempotent, and never fails the session: a broken install leaves a session
# without governance hooks, which is the state it would have had anyway.
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

if claude plugin list 2>/dev/null | grep -q "${PLUGIN_ID}"; then
  log "plugin ${PLUGIN_ID} already installed"
else
  log "installing ${PLUGIN_ID}"
  if ! timeout 180 claude plugin install "${PLUGIN_ID}" 2>&1 | sed 's/^/  /'; then
    finish "plugin install failed — continuing without governance hooks."
  fi
fi

# --- preflight -----------------------------------------------------------
#
# Reports only. Nothing here blocks: every hook in the bundle fails open, so an
# unreachable server costs a degraded SessionStart banner, not a broken session.

SERVER_URL="${UNITARES_SERVER_URL:-}"

if [ -z "${SERVER_URL}" ]; then
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
  # A blocked domain returns 403 from the egress proxy rather than hanging, so
  # a 3s bound is generous.
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "${SERVER_URL}/health" 2>/dev/null) || code=000
  case "${code}" in
    200) log "server /health reachable (200)" ;;
    000) log "WARN ${SERVER_URL}/health unreachable. If the domain is not on" \
             "the environment's network allowlist the egress proxy refuses it;" \
             "add it there, or accept OFFLINE governance for this session." ;;
    *)   log "WARN ${SERVER_URL}/health returned ${code}" ;;
  esac
fi

if [ -z "${UNITARES_HTTP_API_TOKEN:-}" ]; then
  log "WARN UNITARES_HTTP_API_TOKEN unset — writes to the server will not be"
  log "     attributable even if the server is reachable."
fi

# The lease plane is a loopback service on the operator's machine. Absent here,
# the pre-edit hook fails open at connection-refused speed (~0.1s, measured),
# so disabling it is tidiness rather than a fix.
if [ "${UNITARES_FILE_LEASES_ENABLED:-1}" != "0" ]; then
  log "note file leases enabled but no lease plane in-container; pre-edit fails open."
fi

log "done"
exit 0
