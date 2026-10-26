#!/usr/bin/env bash
# Launchd entrypoint for the Agent Orchestrator control surface (Elixir/OTP).
#
# Sources AGENT_ORCHESTRATOR_BEARER_TOKEN, the lease-plane bearer, and the
# shared Postgres URL (AGENT_ORCHESTRATOR_DATABASE_URL, GOVERNANCE_DATABASE_URL,
# or DB_POSTGRES_URL) from the explicit UNITARES_SECRETS_ENV file (mode 600), then execs
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
# Credentials come from the service environment, or an explicitly selected file.
SECRETS_FILE="${UNITARES_SECRETS_ENV:-}"
if [[ -n "$SECRETS_FILE" && ( "$SECRETS_FILE" != /* || ! -f "$SECRETS_FILE" || ! -r "$SECRETS_FILE" ) ]]; then
    echo "error: UNITARES_SECRETS_ENV must name an absolute, readable regular file" >&2
    exit 1
fi

if [[ -f "$SECRETS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
fi

# Homebrew Elixir lives at /opt/homebrew/bin on Apple Silicon. PATH inherited
# from launchd's user environment is sparse, so set it explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$ORCHESTRATOR_DIR"

# --- Launch profiles ---
# With AGENT_ORCHESTRATOR_PROFILES_GENERATE=1, regenerate the file
# AGENT_ORCHESTRATOR_PROFILES_FILE names from this checkout before boot
# (docs/proposals/active/inference-hosts-config-v0.md, step 4b). Opt-in,
# because the generator owns the whole file: a hand-written one would be
# replaced on every restart.
# The generator runs in an empty environment: the secrets file is sourced into
# this shell, and a profile must not pick a value up from it. A failure is
# reported, not fatal: the orchestrator then loads the last good file or none,
# and with the variable set a missing file still denies raw spawns.
if [[ "${AGENT_ORCHESTRATOR_PROFILES_GENERATE:-}" == "1" && -n "${AGENT_ORCHESTRATOR_PROFILES_FILE:-}" ]]; then
    PROFILES_PYTHON="${AGENT_ORCHESTRATOR_PROFILES_PYTHON:-$(command -v python3 || true)}"
    profile_args=(--out "$AGENT_ORCHESTRATOR_PROFILES_FILE" --repo-root "$REPO_ROOT"
                  --python "$PROFILES_PYTHON")
    if [[ -n "${AGENT_ORCHESTRATOR_PROFILES_ENV_FILE:-}" ]]; then
        profile_args+=(--env-file "$AGENT_ORCHESTRATOR_PROFILES_ENV_FILE")
    fi
    if [[ -z "$PROFILES_PYTHON" ]] || ! env -i PATH="$PATH" "$PROFILES_PYTHON" -I \
            "$REPO_ROOT/scripts/ops/orchestrator-profiles.py" "${profile_args[@]}" >&2; then
        echo "[agent-orchestrator] warning: launch profiles not regenerated; the last good file, if any, stays in force" >&2
    fi
fi

# --- Distributed node, for live introspection ---
# With AGENT_ORCHESTRATOR_NODE_COOKIE set (in secrets.env) the node starts
# *named* + *cookied*, so an operator can attach a remote shell to the RUNNING
# orchestrator:
#
#   scripts/ops/beam-attach.sh orchestrator
#
# (it supplies the cookie through a private ~/.erlang.cookie, never argv)
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

# The cookie never goes on a command line: `elixir --cookie`, `-args_file` and
# ERL_AFLAGS all end up in beam.smp's argv, where any process listing reads it.
# Erlang reads .erlang.cookie from the VM's boot-time HOME when distribution
# starts, so the cookie goes into a private file (dir 0700, file 0400) and the
# VM boots with HOME pointing there. NodeDistribution restores the real HOME
# (passed in AGENT_ORCHESTRATOR_NODE_REAL_HOME) before anything else runs, then starts the
# named node; children inherit the real HOME. MIX_HOME and HEX_HOME are pinned
# so mix still finds its archives and cache during boot.
if [[ -n "${AGENT_ORCHESTRATOR_NODE_COOKIE:-}" ]]; then
    COOKIE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}/unitares/beam/agent-orchestrator"
    mkdir -p "$COOKIE_HOME"
    chmod 700 "$COOKIE_HOME"
    COOKIE_FILE="$COOKIE_HOME/.erlang.cookie"
    [[ -f "$COOKIE_FILE" ]] && chmod 600 "$COOKIE_FILE"
    (umask 077; printf '%s' "$AGENT_ORCHESTRATOR_NODE_COOKIE" > "$COOKIE_FILE")
    chmod 400 "$COOKIE_FILE"
    unset AGENT_ORCHESTRATOR_NODE_COOKIE

    export ERL_EPMD_ADDRESS="127.0.0.1"
    # A clean environment: epmd is long-lived and must not inherit anything the
    # secrets file put into this shell. Node.start registers with a running
    # epmd but never launches one, which --sname used to do.
    env -i PATH="$PATH" ERL_EPMD_ADDRESS="$ERL_EPMD_ADDRESS" \
        ${ERL_EPMD_PORT:+ERL_EPMD_PORT="$ERL_EPMD_PORT"} epmd -daemon

    export MIX_HOME="${MIX_HOME:-$HOME/.mix}"
    export HEX_HOME="${HEX_HOME:-$HOME/.hex}"
    export AGENT_ORCHESTRATOR_NODE_SNAME="$NODE_SNAME"
    export AGENT_ORCHESTRATOR_NODE_REAL_HOME="$HOME"
    HOME="$COOKIE_HOME" exec elixir \
        --erl "-kernel inet_dist_use_interface {127,0,0,1}" \
        -S mix run --no-halt
else
    echo "[agent-orchestrator] AGENT_ORCHESTRATOR_NODE_COOKIE unset — starting UNNAMED (live remote shell disabled; set the cookie in secrets.env and restart to enable)" >&2
    exec mix run --no-halt
fi
