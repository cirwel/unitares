#!/usr/bin/env bash
# Launchd entrypoint for the Agent Orchestrator control surface (Elixir/OTP).
#
# Sources AGENT_ORCHESTRATOR_BEARER_TOKEN (and the lease-plane bearer, so
# spawned agents can register presence) from ~/.config/cirwel/secrets.env
# (mode 600), then execs `mix run --no-halt`. The HTTP control surface binds
# 127.0.0.1:8789 by default; override via AGENT_ORCHESTRATOR_HTTP_PORT or the
# :agent_orchestrator runtime config (see application.ex / config.exs).
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

exec mix run --no-halt
