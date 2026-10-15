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
# UNITARES_SECRETS_ENV, else ~/.config/unitares/secrets.env, else the
# pre-2026-09 ~/.config/cirwel path when only that one exists.
SECRETS_FILE="${UNITARES_SECRETS_ENV:-$HOME/.config/unitares/secrets.env}"
if [ -z "${UNITARES_SECRETS_ENV:-}" ] && [ ! -f "$SECRETS_FILE" ] && [ -f "$HOME/.config/cirwel/secrets.env" ]; then
    SECRETS_FILE="$HOME/.config/cirwel/secrets.env"
fi

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

# --- Self-heal dependencies before boot ---
# A long-lived VM masks an incomplete deps/ tree: the running node holds its
# modules in memory, but a fresh `mix run` fails the dependency check and
# crash-loops under launchd KeepAlive. 2026-06-19: plug/postgrex/plug_crypto
# were absent from this deploy checkout's deps/ — invisible while the 4-day-old
# VM ran (and while file-lease acquires silently failed open), and only surfaced
# as a full outage on the next restart. Fetch deps here so a restart self-repairs
# instead of crash-looping. Non-fatal on a transient fetch failure when deps are
# already present; `mix run` below still fails loudly if they genuinely can't
# load, so this never hides a real problem — it only closes the
# missing-deps-on-restart trap.
echo "[lease-plane] ensuring dependencies (mix deps.get)…" >&2
mix deps.get >&2 || echo "[lease-plane] WARN: mix deps.get failed (offline?) — continuing with on-disk deps" >&2

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

# The cookie never goes on a command line: `elixir --cookie`, `-args_file` and
# ERL_AFLAGS all end up in beam.smp's argv, where any process listing reads it.
# It travels in the environment instead; NodeDistribution starts the named node
# at boot, sets the cookie, and removes the variable before any child spawns.
# Two things --sname used to do now happen here: epmd is started (Node.start
# registers with a running epmd but never launches one), and a throwaway random
# cookie is set so starting distribution never reads ~/.erlang.cookie. The
# throwaway is visible in argv and stops working the moment NodeDistribution
# sets the real cookie, before the application does anything else.
if [[ -n "${LEASE_PLANE_NODE_COOKIE:-}" ]]; then
    export LEASE_PLANE_NODE_COOKIE
    export LEASE_PLANE_NODE_SNAME="$NODE_SNAME"
    export ERL_EPMD_ADDRESS="127.0.0.1"
    epmd -daemon
    THROWAWAY_COOKIE="t$(openssl rand -hex 16)"
    exec elixir \
        --erl "-setcookie $THROWAWAY_COOKIE -kernel inet_dist_use_interface {127,0,0,1}" \
        -S mix run --no-halt
else
    unset LEASE_PLANE_NODE_COOKIE
    echo "[lease-plane] LEASE_PLANE_NODE_COOKIE unset — starting UNNAMED (hot-reload disabled; set the cookie in secrets.env and restart to enable)" >&2
    exec mix run --no-halt
fi
