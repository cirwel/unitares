#!/usr/bin/env bash
# Standing-service launcher for the operator hub (elixir/portal, 127.0.0.1:8791).
# Mirrors dialectic_live's: source secrets, self-heal deps and assets on
# restart, then run. Invoked by scripts/ops/com.unitares.portal.plist.template.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# Credentials come from the service environment, or an explicitly selected
# file (mode 600): PORTAL_SECRET_KEY_BASE, and for sign-in and the timeline
# PORTAL_HUB_TOKEN and PORTAL_READ_TOKEN. They are never printed.
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
export PHX_SERVER=true

if [ -z "${PORTAL_SECRET_KEY_BASE:-}" ]; then
  echo "FATAL: PORTAL_SECRET_KEY_BASE is unset." >&2
  echo "Generate one with 'mix phx.gen.secret' and add it to the selected secrets file." >&2
  exit 1
fi

# Self-heal deps and assets on restart (a cheap no-op when already current).
# build-assets.sh is lenient here: if a build fails while a previous one is on
# disk, it serves that instead of exiting into a KeepAlive restart loop.
mix deps.get --only "$MIX_ENV"
"$APP_DIR/scripts/build-assets.sh"

exec mix phx.server
