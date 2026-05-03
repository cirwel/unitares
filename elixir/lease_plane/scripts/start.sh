#!/usr/bin/env bash
# Launchd entrypoint for the Surface Lease Plane (Elixir/OTP).
#
# Sources LEASE_PLANE_BEARER_TOKEN + LEASE_FORCE_RELEASE_TOKEN from
# ~/.config/cirwel/secrets.env (mode 600 — not keychain), then execs
# `mix run --no-halt`. Bind defaults to 127.0.0.1:8788; override via
# the :lease_plane runtime config or env (see application.ex).
#
# Used by: scripts/ops/com.unitares.lease-plane.plist
# Manual invocation: `./elixir/lease_plane/scripts/start.sh`
#
# Fail-closed posture: if secrets.env is missing or the bearer is
# unset, the application starts but HTTPAuth returns 503 on every
# request — never silently open. See application.ex:43.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LEASE_PLANE_DIR="$REPO_ROOT/elixir/lease_plane"
SECRETS_FILE="$HOME/.config/cirwel/secrets.env"

if [[ -f "$SECRETS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
else
    echo "[lease-plane] WARNING: $SECRETS_FILE missing — starting fail-closed (HTTPAuth → 503)" >&2
fi

# Homebrew Elixir lives at /opt/homebrew/bin on Apple Silicon. PATH inherited
# from launchd's user environment is sparse, so we set it explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$LEASE_PLANE_DIR"
exec mix run --no-halt
