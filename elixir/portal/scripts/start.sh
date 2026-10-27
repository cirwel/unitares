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
#
# A missing or unusable configuration exits 0, not 1: the plist restarts the
# hub only after a failing exit, so a configuration problem (which a restart
# cannot fix) leaves it stopped with the reason in the log, not looping.
not_configured() {
  echo "FATAL: $1 The hub is not starting; launchd will not restart it." >&2
  exit 0
}

SECRETS="${UNITARES_SECRETS_ENV:-}"
if [[ -n "$SECRETS" && ( "$SECRETS" != /* || ! -f "$SECRETS" || ! -r "$SECRETS" ) ]]; then
  not_configured "UNITARES_SECRETS_ENV must name an absolute, readable regular file."
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
  not_configured "PORTAL_SECRET_KEY_BASE is unset. Generate one with 'mix phx.gen.secret' and add it to the selected file."
fi

# Self-heal deps and assets on restart (a cheap no-op when already current).
# build-assets.sh is lenient here: if a build fails while a previous one is on
# disk, it serves that instead of exiting into a KeepAlive restart loop.
mix deps.get --only "$MIX_ENV"
"$APP_DIR/scripts/build-assets.sh"

exec mix phx.server
