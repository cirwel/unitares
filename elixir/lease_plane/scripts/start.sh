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

# --- Distributed node for BEAM hot-code-reload ---
# When LEASE_PLANE_NODE_COOKIE is set (in secrets.env) we start the node
# *named* + *cookied* so an operator can attach a remote shell or drive an
# in-place module swap (scripts/ops/hot-reload.sh) WITHOUT a restart — the
# substrate answer to the running-process-vs-master-commit drift class.
#
# Security: the Erlang distribution port is authenticated ONLY by the cookie
# (node access == arbitrary code execution), so we (a) refuse to name the
# node without a cookie — falling back to the pre-hot-reload UNNAMED launch,
# preserving exact current behavior — and (b) pin epmd + the distribution
# listener to 127.0.0.1, matching the lease plane's localhost trust boundary.
NODE_SNAME="${LEASE_PLANE_NODE_SNAME:-unitares-lease-plane}"

if [[ -n "${LEASE_PLANE_NODE_COOKIE:-}" ]]; then
    export ERL_EPMD_ADDRESS="127.0.0.1"
    exec elixir \
        --sname "$NODE_SNAME" \
        --cookie "$LEASE_PLANE_NODE_COOKIE" \
        --erl "-kernel inet_dist_use_interface {127,0,0,1}" \
        -S mix run --no-halt
else
    echo "[lease-plane] LEASE_PLANE_NODE_COOKIE unset — starting UNNAMED (hot-reload disabled; set the cookie in secrets.env and restart to enable)" >&2
    exec mix run --no-halt
fi
