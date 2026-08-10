#!/usr/bin/env bash
# Attach to a RUNNING BEAM service — interactive shell or one-shot eval.
#
# The point of running these services on the BEAM is that you can inspect and
# change a live one without restarting it: read a GenServer's state, count live
# agents, attach a telemetry handler for two minutes and detach it again. That
# only works if you can reach the node, which means knowing its sname and its
# cookie. This script knows both, per service, so you don't have to.
#
#   ./scripts/ops/beam-attach.sh --list
#   ./scripts/ops/beam-attach.sh orchestrator
#   ./scripts/ops/beam-attach.sh orchestrator --eval "AgentOrchestrator.list()"
#   ./scripts/ops/beam-attach.sh orchestrator --eval \
#       "AgentOrchestrator.Telemetry.attach_logger()"
#
# SECURITY. An Erlang distribution connection is full trust: whoever can attach
# can run arbitrary code in that node. Two consequences worth stating rather
# than discovering:
#
#   * Each service keeps its OWN cookie. That is why this script maps
#     service -> cookie instead of clustering everything under one. A shared
#     cookie would let any one compromised BEAM service pivot into all the
#     others, including the orchestrator, whose entire job is spawning
#     processes.
#   * Cookies live in ~/.config/cirwel/secrets.env (mode 600) and in
#     ~/.erlang.cookie (mode 400). Anything that can read those can already do
#     this; the boundary is the filesystem, not this script.
#
# All nodes pin epmd and their distribution listener to 127.0.0.1, so this only
# works from the same host.

set -euo pipefail

SECRETS_FILE="${UNITARES_SECRETS_ENV:-$HOME/.config/cirwel/secrets.env}"
HOST="$(hostname -s)"

usage() {
    cat >&2 <<'USAGE'
Usage:
  beam-attach.sh --list
  beam-attach.sh <service> [--eval "elixir code"]

Services:
  orchestrator          agent-orchestrator      AGENT_ORCHESTRATOR_NODE_COOKIE
  lease-plane           unitares-lease-plane    LEASE_PLANE_NODE_COOKIE
  dispatch-beam         dispatch_beam           ~/.erlang.cookie (default)
  dispatch-beam-codex   dispatch_beam_codex     ~/.erlang.cookie (default)

Without --eval you get an interactive IEx attached to the live node. Exit with
Ctrl-\ (Ctrl-C twice kills the REMOTE node, not your shell).
USAGE
    exit 2
}

# service -> sname, and service -> cookie variable ("" means the default
# ~/.erlang.cookie, which is what a node started with --sname and no --cookie
# uses).
sname_for() {
    case "$1" in
        orchestrator)        echo "agent-orchestrator" ;;
        lease-plane)         echo "unitares-lease-plane" ;;
        dispatch-beam)       echo "dispatch_beam" ;;
        dispatch-beam-codex) echo "dispatch_beam_codex" ;;
        *) return 1 ;;
    esac
}

cookie_var_for() {
    case "$1" in
        orchestrator) echo "AGENT_ORCHESTRATOR_NODE_COOKIE" ;;
        lease-plane)  echo "LEASE_PLANE_NODE_COOKIE" ;;
        *)            echo "" ;;
    esac
}

if [[ $# -eq 0 ]]; then usage; fi

if [[ "$1" == "--list" ]]; then
    echo "epmd-registered nodes on $HOST:"
    epmd -names 2>/dev/null | sed -n 's/^name \(.*\) at port .*/  \1/p' || echo "  (epmd not running)"
    echo
    echo "Known services (a service missing above is running UNNAMED):"
    for svc in orchestrator lease-plane dispatch-beam dispatch-beam-codex; do
        sname="$(sname_for "$svc")"
        if epmd -names 2>/dev/null | grep -q "^name $sname "; then
            printf '  %-22s %-24s reachable\n' "$svc" "$sname"
        else
            printf '  %-22s %-24s NOT named\n' "$svc" "$sname"
        fi
    done
    exit 0
fi

SERVICE="$1"; shift
SNAME="$(sname_for "$SERVICE")" || { echo "unknown service: $SERVICE" >&2; usage; }

EVAL=""
if [[ $# -gt 0 ]]; then
    [[ "$1" == "--eval" ]] || usage
    [[ $# -eq 2 ]] || usage
    EVAL="$2"
fi

if ! epmd -names 2>/dev/null | grep -q "^name $SNAME "; then
    echo "[beam-attach] '$SERVICE' is not registered with epmd as '$SNAME'." >&2
    cookie_var="$(cookie_var_for "$SERVICE")"
    if [[ -n "$cookie_var" ]]; then
        echo "[beam-attach] It is probably running UNNAMED. Set $cookie_var in" >&2
        echo "[beam-attach] $SECRETS_FILE and restart the service to enable attaching." >&2
    else
        echo "[beam-attach] Is the service running?" >&2
    fi
    exit 1
fi

# Only the one cookie we need crosses over, in a subshell — not the whole
# secrets file into this process's environment.
COOKIE=""
cookie_var="$(cookie_var_for "$SERVICE")"
if [[ -n "$cookie_var" ]]; then
    if [[ -f "$SECRETS_FILE" ]]; then
        COOKIE="$( (set -a; . "$SECRETS_FILE" >/dev/null 2>&1; printf '%s' "${!cookie_var:-}") || true)"
    fi

    if [[ -z "$COOKIE" ]]; then
        echo "[beam-attach] $cookie_var is not set in $SECRETS_FILE, but '$SNAME' is named." >&2
        echo "[beam-attach] Refusing to guess — a wrong cookie is an auth failure, not a hint." >&2
        exit 1
    fi
fi

# A distinct local sname per invocation so two concurrent attaches never clash
# in epmd.
LOCAL_SNAME="attach-$$"
TARGET="$SNAME@$HOST"

args=(--sname "$LOCAL_SNAME")
[[ -n "$COOKIE" ]] && args+=(--cookie "$COOKIE")

if [[ -n "$EVAL" ]]; then
    exec elixir "${args[@]}" --rpc-eval "$TARGET" "$EVAL"
else
    echo "[beam-attach] attaching to $TARGET — Ctrl-\\ to detach (Ctrl-C twice kills the REMOTE node)" >&2
    exec iex "${args[@]}" --remsh "$TARGET"
fi
