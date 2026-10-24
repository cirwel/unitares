#!/usr/bin/env bash
# Standing-service launcher for the dialectic_live Phoenix app.
# Mirrors the lease_plane / wave3a_handlers pattern: source secrets, self-heal
# deps on restart, then run. Invoked by scripts/ops/com.unitares.dialectic-live.plist.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# Secrets (mode 600). Provides DIALECTIC_LIVE_SECRET_KEY_BASE and, optionally,
# UNITARES_HTTP_API_TOKEN for authenticated tool-calls.
# Credentials come from the service environment, or an explicitly selected file.
SECRETS="${UNITARES_SECRETS_ENV:-}"
if [[ -n "$SECRETS" && ( "$SECRETS" != /* || ! -f "$SECRETS" || ! -r "$SECRETS" ) ]]; then
    echo "error: UNITARES_SECRETS_ENV must name an absolute, readable regular file" >&2
    exit 1
fi
if [ -f "$SECRETS" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS"
  set +a
fi

export MIX_ENV="${MIX_ENV:-prod}"
export PORT="${PORT:-8790}"
export PHX_SERVER=true

# Phoenix prod requires SECRET_KEY_BASE. Keep it app-scoped in secrets.env as
# DIALECTIC_LIVE_SECRET_KEY_BASE; generate once with: mix phx.gen.secret
if [ -z "${SECRET_KEY_BASE:-}" ]; then
  if [ -n "${DIALECTIC_LIVE_SECRET_KEY_BASE:-}" ]; then
    export SECRET_KEY_BASE="$DIALECTIC_LIVE_SECRET_KEY_BASE"
  else
    echo "FATAL: SECRET_KEY_BASE (or DIALECTIC_LIVE_SECRET_KEY_BASE) is unset." >&2
    echo "Generate one with 'mix phx.gen.secret' and add it to $SECRETS" >&2
    exit 1
  fi
fi

# Upstream governance MCP. Defaults target a local server; override via env.
export GOVERNANCE_WS_URL="${GOVERNANCE_WS_URL:-ws://127.0.0.1:8767/ws/eisv}"
export GOVERNANCE_TOOLS_URL="${GOVERNANCE_TOOLS_URL:-http://127.0.0.1:8767/v1/tools/call}"
export GOVERNANCE_START_FIREHOSE="${GOVERNANCE_START_FIREHOSE:-true}"

# Self-heal deps + assets on restart (cheap no-op when already current).
# build-assets.sh skips the asset build when the deploy already built this
# checkout, and is lenient here: if a build fails while a previous one is on
# disk, it serves that instead of exiting into a KeepAlive restart loop.
mix deps.get --only "$MIX_ENV"
"$APP_DIR/scripts/build-assets.sh"

exec mix phx.server
