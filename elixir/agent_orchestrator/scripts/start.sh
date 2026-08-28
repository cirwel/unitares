#!/usr/bin/env bash
# Launchd entrypoint for the Agent Orchestrator control surface (Elixir/OTP).
#
# Sources AGENT_ORCHESTRATOR_BEARER_TOKEN, the lease-plane bearer, and the
# shared Postgres URL (AGENT_ORCHESTRATOR_DATABASE_URL, GOVERNANCE_DATABASE_URL,
# or DB_POSTGRES_URL) from ~/.config/cirwel/secrets.env (mode 600), then execs
# `mix run --no-halt`. The HTTP control surface binds 127.0.0.1:8789 by default;
# override via AGENT_ORCHESTRATOR_HTTP_PORT or the :agent_orchestrator runtime
# config (see application.ex / config.exs).
#
# Manual invocation: `./elixir/agent_orchestrator/scripts/start.sh`
#
# Fail-closed posture: if secrets.env is missing or the bearer is unset, the
# application starts but HTTPAuth returns 503 on every request — never silently
# open. POST /v1/agents spawns an OS process, so an unauthenticated reach would
# be RCE; the localhost bind + bearer gate are the trust boundary.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ORCHESTRATOR_DIR="$REPO_ROOT/elixir/agent_orchestrator"
SECRETS_FILE="$HOME/.config/cirwel/secrets.env"

if [[ -f "$SECRETS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
else
    echo "[agent-orchestrator] WARNING: $SECRETS_FILE missing — starting fail-closed (HTTPAuth → 503)" >&2
fi

# Homebrew Elixir lives at /opt/homebrew/bin on Apple Silicon. PATH inherited
# from launchd's user environment is sparse, so set it explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$ORCHESTRATOR_DIR"

# --- Distributed node, for live introspection ---
# With AGENT_ORCHESTRATOR_NODE_COOKIE set (in secrets.env) the node starts
# *named* + *cookied*, so an operator can attach a remote shell to the RUNNING
# orchestrator:
#
#   iex --sname probe --cookie "$AGENT_ORCHESTRATOR_NODE_COOKIE" \
#       --remsh agent-orchestrator@$(hostname -s)
#
# and from there inspect live state (AgentOrchestrator.list/0, :sys.get_state/1)
# or attach a telemetry handler for a while and detach it again
# (AgentOrchestrator.Telemetry.attach_logger/0) — no redeploy, no restart. That
# is the whole reason this runs on the BEAM rather than as a plain daemon, and
# until now it was the one BEAM service that could not be reached this way.
#
# Security mirrors the lease plane exactly: the Erlang distribution port is
# authenticated ONLY by the cookie, and node access is arbitrary code execution.
# So (a) refuse to name the node without a cookie — falling back to the current
# UNNAMED launch, preserving exact existing behavior — and (b) pin epmd and the
# distribution listener to 127.0.0.1, matching this service's localhost trust
# boundary (the same boundary its bearer gate assumes).
NODE_SNAME="${AGENT_ORCHESTRATOR_NODE_SNAME:-agent-orchestrator}"

if [[ -n "${AGENT_ORCHESTRATOR_NODE_COOKIE:-}" ]]; then
    export ERL_EPMD_ADDRESS="127.0.0.1"
    exec elixir \
        --sname "$NODE_SNAME" \
        --cookie "$AGENT_ORCHESTRATOR_NODE_COOKIE" \
        --erl "-kernel inet_dist_use_interface {127,0,0,1}" \
        -S mix run --no-halt
else
    echo "[agent-orchestrator] AGENT_ORCHESTRATOR_NODE_COOKIE unset — starting UNNAMED (live remote shell disabled; set the cookie in secrets.env and restart to enable)" >&2
    exec mix run --no-halt
fi
