#!/usr/bin/env bash
# rotate-secrets.sh — rotate UNITARES bearer-token secrets.
#
# Rotation steps:
#   1. Generate new secret values (32 bytes, base64url).
#   2. Write them into the operator secrets file, for the keys it defines.
#      Jobs that launch through the configured secrets wrapper
#      hold no value in their plist and read this file when they start.
#   3. Write them into the installed LaunchAgent plists that reference them.
#   4. Surgical-strip each anchor in ~/.unitares/anchors/:
#        drop continuity_token + client_session_id, keep agent_uuid.
#   5. Bounce the governance-mcp launchd service, and name the other
#      wrapped jobs that need a reload to pick up the new values.
#
# Residents wake on their normal cadence and resume via PATH 0 UUID-direct
# identity lookup (shipped 2026-04-17). They do NOT fresh-onboard and do
# NOT get new UUIDs.
#
# Why this exists: on 2026-04-19 the ad-hoc rotation runbook wiped the
# anchors/ dir wholesale. Every resident then fresh-onboarded with a new
# UUID. Preserve anchor identity while rotating only the selected credentials.

set -euo pipefail

ANCHOR_DIR="${HOME}/.unitares/anchors"
LAUNCHAGENTS_DIR="${HOME}/Library/LaunchAgents"
GOVERNANCE_PLIST="${LAUNCHAGENTS_DIR}/com.unitares.governance-mcp.plist"
SENTINEL_PLIST="${LAUNCHAGENTS_DIR}/com.unitares.sentinel-beam.plist"
DATE_STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${HOME}/.unitares/rotation-backup-${DATE_STAMP}"
OPS_DIR="$(cd "$(dirname "$0")" && pwd)"
SENTINEL_PLIST_TOOL="${UNITARES_SENTINEL_PLIST_TOOL:-$OPS_DIR/sentinel-plist.py}"
PLIST_BUDDY="${UNITARES_PLIST_BUDDY:-/usr/libexec/PlistBuddy}"
LAUNCHCTL="${UNITARES_LAUNCHCTL:-launchctl}"
ROTATED_KEYS_RE='UNITARES_(CONTINUITY_TOKEN_SECRET|HTTP_API_TOKEN)'

log()  { printf '\033[1;34m[rotate]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[rotate]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[rotate]\033[0m %s\n' "$*" >&2; exit 1; }

# --- Pre-flight ---
[[ -d "${ANCHOR_DIR}" ]] || die "anchor dir missing: ${ANCHOR_DIR}"
[[ -f "${GOVERNANCE_PLIST}" ]] || die "governance plist missing: ${GOVERNANCE_PLIST}"

# PlistBuddy's `Set` fails when a key is absent. Historically that error was
# suppressed for every plist, so a template-created Sentinel could remain on
# the old/missing bearer after an otherwise successful rotation. Refuse before
# writing any plist if an installed Sentinel is not structurally rotatable.
if [[ -f "${SENTINEL_PLIST}" ]]; then
  python3 "${SENTINEL_PLIST_TOOL}" rotation-preflight --plist "${SENTINEL_PLIST}"
fi

# Every anchor must already have an agent_uuid — if any don't, abort loudly;
# operator needs to re-bootstrap that resident explicitly.
missing=()
for f in "${ANCHOR_DIR}"/*.json; do
  [[ -e "$f" ]] || continue
  if ! python3 -c "
import json, sys
d = json.load(open('$f'))
sys.exit(0 if d.get('agent_uuid') else 1)
" 2>/dev/null; then
    missing+=("$f")
  fi
done
if (( ${#missing[@]} > 0 )); then
  die "anchors missing agent_uuid (cannot do surgical rotation): ${missing[*]}"
fi

log "backup dir: ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
cp -a "${ANCHOR_DIR}" "${BACKUP_DIR}/anchors"

# --- Generate new secrets ---
new_continuity_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
new_http_api_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

# --- Write into the secrets files, before any plist ---
# Every file a reader may use: the wrapper's and the one start.sh sources
# (sentinel-plist.py secrets-files). The Sentinel step below confirms a wrapped
# Sentinel will start with the new bearer, so this must come first. A key a
# file does not define is skipped there.
# Captured first (not read from a process substitution) so a failing lookup
# stops the rotation under set -e.
secrets_files="$(python3 "${SENTINEL_PLIST_TOOL}" secrets-files)"
backed_up=0
while IFS= read -r secrets_file; do
  [[ -n "${secrets_file}" ]] || continue
  backed_up=$((backed_up + 1))
  cp -p "${secrets_file}" "${BACKUP_DIR}/secrets-${backed_up}.env"
  log "backed up ${secrets_file} as secrets-${backed_up}.env"
done <<< "${secrets_files}"
if (( backed_up > 0 )); then
  printf '%s' "${new_continuity_secret}" | \
    python3 "${SENTINEL_PLIST_TOOL}" set-secret --key UNITARES_CONTINUITY_TOKEN_SECRET
  printf '%s' "${new_http_api_token}" | \
    python3 "${SENTINEL_PLIST_TOOL}" set-secret --key UNITARES_HTTP_API_TOKEN
fi

# --- Write into installed com.unitares.*.plist files ---
log "rotating secrets in LaunchAgents plists..."
for plist in "${LAUNCHAGENTS_DIR}"/com.unitares.*.plist; do
  [[ -e "$plist" ]] || continue
  cp "$plist" "${BACKUP_DIR}/$(basename "$plist")"
  "${PLIST_BUDDY}" -c "Set :EnvironmentVariables:UNITARES_CONTINUITY_TOKEN_SECRET ${new_continuity_secret}" "$plist" 2>/dev/null || true
  if [[ "$plist" == "${SENTINEL_PLIST}" ]]; then
    # stdin keeps the bearer out of argv and the helper requires the key to
    # exist, so rotation cannot silently skip Sentinel again.
    printf '%s' "${new_http_api_token}" | \
      python3 "${SENTINEL_PLIST_TOOL}" rotate-token --plist "$plist"
  else
    "${PLIST_BUDDY}" -c "Set :EnvironmentVariables:UNITARES_HTTP_API_TOKEN ${new_http_api_token}" "$plist" 2>/dev/null || true
  fi
done

# --- Surgical anchor strip: drop continuity_token + client_session_id,
#     keep agent_uuid. ---
log "surgical anchor strip..."
for f in "${ANCHOR_DIR}"/*.json; do
  [[ -e "$f" ]] || continue
  python3 - "$f" <<'PY'
import json, os, sys, tempfile
path = sys.argv[1]
with open(path) as fh:
    d = json.load(fh)
uuid = d.get("agent_uuid")
if not uuid:
    sys.exit(f"refusing to strip {path}: no agent_uuid")
new = {"agent_uuid": uuid}
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as fh:
    json.dump(new, fh)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
done

# --- Bounce governance-mcp so it picks up the new secrets. ---
log "restarting governance-mcp..."
"${LAUNCHCTL}" unload "${GOVERNANCE_PLIST}" 2>/dev/null || true
"${LAUNCHCTL}" load   "${GOVERNANCE_PLIST}"

# Wrapped jobs read the secrets file only when they start; a long-running one
# keeps the old value until it is reloaded. Names only: grep -q never prints.
wrapped=()
for plist in "${LAUNCHAGENTS_DIR}"/*.plist; do
  [[ -e "$plist" && "$plist" != "${GOVERNANCE_PLIST}" ]] || continue
  if "${PLIST_BUDDY}" -c 'Print :EnvironmentVariables:UNITARES_SECRETS_WRAPPER' "$plist" >/dev/null 2>&1 \
     && grep -q -E "${ROTATED_KEYS_RE}" "$plist"; then
    wrapped+=("$(basename "$plist" .plist)")
  fi
done
if (( ${#wrapped[@]} > 0 )); then
  warn "these wrapped jobs take a rotated key from a secrets file at launch;"
  warn "reload the long-running ones: ${wrapped[*]}"
fi

log "rotation complete. backup at ${BACKUP_DIR}"
log "residents will re-auth via PATH 0 on their next cycle."
