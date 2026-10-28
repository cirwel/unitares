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
#
# From that file only this app's own names are taken: the two credentials
# above, SECRET_KEY_BASE, PORT, and settings named DIALECTIC_LIVE_*,
# GOVERNANCE_*, TIMELINE_* or PHX_*. Sourcing the whole file handed this
# process every credential in a shared secrets file (KG
# 2026-10-27T22:16:58.919702). The file is read in a separate process with an
# empty environment, and the chosen values come back through a pipe; none is
# printed.
#
# A configuration problem exits 0, not 1: launchd restarts the app only after
# a failing exit (KeepAlive SuccessfulExit=false), so a problem a restart
# cannot fix leaves it stopped with the reason in the log, not looping.
not_configured() {
  echo "FATAL: $1 dialectic_live is not starting; launchd will not restart it." >&2
  exit 0
}

load_own_settings() {
  local name value
  while IFS= read -r -d '' name && IFS= read -r -d '' value; do
    export "$name=$value"
  done < <(
    env -i HOME="$HOME" PATH="$PATH" /bin/bash -c '
      set -a
      # shellcheck disable=SC1090
      . "$1" >/dev/null 2>&1 || exit 1
      set +a
      for n in $(compgen -e); do
        case "$n" in
          DIALECTIC_LIVE_*|GOVERNANCE_*|TIMELINE_*|PHX_*|SECRET_KEY_BASE|UNITARES_HTTP_API_TOKEN|PORT)
            printf "%s\0%s\0" "$n" "${!n}" ;;
        esac
      done
    ' load-own-settings "$1"
  )
}

SECRETS="${UNITARES_SECRETS_ENV:-}"
if [[ -n "$SECRETS" && ( "$SECRETS" != /* || ! -f "$SECRETS" || ! -r "$SECRETS" ) ]]; then
  not_configured "UNITARES_SECRETS_ENV must name an absolute, readable regular file."
fi
if [ -f "$SECRETS" ]; then
  load_own_settings "$SECRETS"
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
    not_configured "SECRET_KEY_BASE (or DIALECTIC_LIVE_SECRET_KEY_BASE) is unset. Generate one with 'mix phx.gen.secret' and add it to the selected file."
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
