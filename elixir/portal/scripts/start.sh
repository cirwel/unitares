#!/usr/bin/env bash
# Standing-service launcher for the operator hub (elixir/portal, 127.0.0.1:8791).
# Self-heals deps and assets on restart, then runs. Invoked by
# scripts/ops/com.unitares.portal.plist.template.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# Credentials come only from the environment the service manager gives this
# process: PORTAL_SECRET_KEY_BASE, and for sign-in and the timeline
# PORTAL_HUB_TOKEN and PORTAL_READ_TOKEN. Unlike dialectic_live's start.sh,
# this one never sources a secrets file. A file of shared secrets would hand
# the hub, a browser-facing process, every credential in it; supply exactly
# these keys instead (for example with a wrapper that exports only the keys
# it is told to). They are never printed.
#
# A missing configuration exits 0, not 1: the plist restarts the hub only
# after a failing exit, so a configuration problem (which a restart cannot
# fix) leaves it stopped with the reason in the log, not looping.
not_configured() {
  echo "FATAL: $1 The hub is not starting; launchd will not restart it." >&2
  exit 0
}

export MIX_ENV="${MIX_ENV:-prod}"
export PHX_SERVER=true

if [ -z "${PORTAL_SECRET_KEY_BASE:-}" ]; then
  not_configured "PORTAL_SECRET_KEY_BASE is unset. Generate one with 'mix phx.gen.secret' and supply it in the service environment."
fi

# Self-heal deps and assets on restart (a cheap no-op when already current).
# build-assets.sh is lenient here: if a build fails while a previous one is on
# disk, it serves that instead of exiting into a KeepAlive restart loop.
mix deps.get --only "$MIX_ENV"
"$APP_DIR/scripts/build-assets.sh"

exec mix phx.server
