#!/usr/bin/env bash
# Launchd entrypoint for BEAM Sentinel.
#
# Sources operator secrets, enables the Wave 1 runtime children, then execs
# `mix run --no-halt` from the Sentinel app directory.
#
# Used by: scripts/ops/com.unitares.sentinel-beam.plist.template
# Manual invocation: `./elixir/sentinel/scripts/start.sh`

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SENTINEL_DIR="$REPO_ROOT/elixir/sentinel"
SECRETS_FILE="$HOME/.config/cirwel/secrets.env"

if [[ -f "$SECRETS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
else
    echo "[sentinel-beam] WARNING: $SECRETS_FILE missing - HTTP/lease auth may fail closed" >&2
fi

# Homebrew Elixir lives at /opt/homebrew/bin on Apple Silicon. PATH inherited
# from launchd's user environment is sparse, so set it explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Bind BEAM Sentinel to Python Sentinel's state/identity surfaces. These are
# defaults only; launchd may pass explicit values for non-standard installs.
export UNITARES_SENTINEL_STATE_FILE="${UNITARES_SENTINEL_STATE_FILE:-$REPO_ROOT/.sentinel_state}"
export UNITARES_SENTINEL_SESSION_FILE="${UNITARES_SENTINEL_SESSION_FILE:-$HOME/.unitares/anchors/sentinel.json}"
export UNITARES_SENTINEL_LEGACY_SESSION_FILE="${UNITARES_SENTINEL_LEGACY_SESSION_FILE:-$REPO_ROOT/.sentinel_session}"

# Wave 1 production runtime: poll lease-plane events, ingest EISV over WS,
# emit findings, and post governance check-ins. Config defaults remain safe
# for tests and ad-hoc `mix run`; the launchd entrypoint opts in explicitly.
export UNITARES_SENTINEL_START_APPLICATION="${UNITARES_SENTINEL_START_APPLICATION:-true}"
export UNITARES_SENTINEL_START_POSTGREX="${UNITARES_SENTINEL_START_POSTGREX:-true}"
export UNITARES_SENTINEL_START_FINCH="${UNITARES_SENTINEL_START_FINCH:-true}"
export UNITARES_SENTINEL_START_FLEET_STATE="${UNITARES_SENTINEL_START_FLEET_STATE:-true}"
export UNITARES_SENTINEL_START_WEBSOCKET="${UNITARES_SENTINEL_START_WEBSOCKET:-true}"
export UNITARES_SENTINEL_START_FLEET_FINDING_EMITTER="${UNITARES_SENTINEL_START_FLEET_FINDING_EMITTER:-true}"
export UNITARES_SENTINEL_START_POLLER="${UNITARES_SENTINEL_START_POLLER:-true}"
export UNITARES_SENTINEL_EMIT_FINDINGS="${UNITARES_SENTINEL_EMIT_FINDINGS:-true}"
export UNITARES_SENTINEL_EMIT_CHECKINS="${UNITARES_SENTINEL_EMIT_CHECKINS:-true}"

cd "$SENTINEL_DIR"
exec mix run --no-halt
