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
#   * Cookies may live in the explicit UNITARES_SECRETS_ENV file (mode 600) and in
#     ~/.erlang.cookie (mode 400). Anything that can read those can already do
#     this; the boundary is the filesystem, not this script.
#
# All nodes pin epmd and their distribution listener to 127.0.0.1, so this only
# works from the same host.

set -euo pipefail

# Credentials come from the service environment, or an explicitly selected file.
SECRETS_FILE="${UNITARES_SECRETS_ENV:-}"
if [[ -n "$SECRETS_FILE" && ( "$SECRETS_FILE" != /* || ! -f "$SECRETS_FILE" || ! -r "$SECRETS_FILE" ) ]]; then
    echo "error: UNITARES_SECRETS_ENV must name an absolute, readable regular file" >&2
    exit 1
fi
HOST="$(hostname -s)"

usage() {
    cat >&2 <<'USAGE'
Usage:
  beam-attach.sh --list
  beam-attach.sh <service> [--eval "elixir code"]

Services:
  orchestrator          agent-orchestrator      AGENT_ORCHESTRATOR_NODE_COOKIE
  lease-plane           unitares-lease-plane    LEASE_PLANE_NODE_COOKIE
  Additional services: UNITARES_BEAM_SERVICES_FILE (service, node, cookie variable TSV)

Without --eval you get an interactive IEx attached to the live node. Exit with
Ctrl-\ (Ctrl-C twice kills the REMOTE node, not your shell).
USAGE
    exit 2
}

# Core integrations are public; external service/node names are explicit
# deployment configuration. Empty cookie variable retains ~/.erlang.cookie.
SERVICES=(
  "orchestrator|agent-orchestrator|AGENT_ORCHESTRATOR_NODE_COOKIE"
  "lease-plane|unitares-lease-plane|LEASE_PLANE_NODE_COOKIE"
)
if [[ -n "${UNITARES_BEAM_SERVICES_FILE:-}" ]]; then
  [[ -r "$UNITARES_BEAM_SERVICES_FILE" ]] || { echo "unreadable BEAM service inventory" >&2; exit 2; }
  while IFS=$'\t' read -r svc node cookie || [[ -n "$svc$node$cookie" ]]; do
    [[ -n "$svc$node$cookie" ]] || continue
    if [[ ! "$svc" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ || ! "$node" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ || ( -n "$cookie" && ! "$cookie" =~ ^[A-Z][A-Z0-9_]*$ ) ]]; then
      echo "invalid BEAM service inventory entry" >&2; exit 2
    fi
    for entry in "${SERVICES[@]}"; do
      [[ "${entry%%|*}" != "$svc" ]] || { echo "duplicate BEAM service: $svc" >&2; exit 2; }
    done
    SERVICES+=("$svc|$node|$cookie")
  done < "$UNITARES_BEAM_SERVICES_FILE"
fi
sname_for() {
  local entry svc node cookie
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r svc node cookie <<< "$entry"
    if [[ "$svc" == "$1" ]]; then printf '%s\n' "$node"; return 0; fi
  done
  return 1
}
cookie_var_for() {
  local entry svc node cookie
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r svc node cookie <<< "$entry"
    if [[ "$svc" == "$1" ]]; then printf '%s\n' "$cookie"; return 0; fi
  done
  return 1
}

if [[ $# -eq 0 ]]; then usage; fi

if [[ "$1" == "--list" ]]; then
    echo "epmd-registered nodes on $HOST:"
    epmd -names 2>/dev/null | sed -n 's/^name \(.*\) at port .*/  \1/p' || echo "  (epmd not running)"
    echo
    echo "Known services (a service missing above is running UNNAMED):"
    for entry in "${SERVICES[@]}"; do
        svc="${entry%%|*}"
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
    COOKIE="${!cookie_var:-}"
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

# The cookie never goes on a command line: `--cookie` lands in beam.smp's argv,
# where any process listing reads it. This client node instead runs with a
# private HOME whose ~/.erlang.cookie holds it (mode 400, removed on exit).
COOKIE_HOME=""
# Always returns 0: under set -e a failing EXIT trap would replace the
# client's exit status (no cookie directory exists for default-cookie services).
cleanup_cookie_home() { if [[ -n "$COOKIE_HOME" ]]; then rm -rf "$COOKIE_HOME"; fi; return 0; }
trap cleanup_cookie_home EXIT
# A signal's default action would kill this shell before its EXIT trap runs:
# Ctrl-\ (SIGQUIT, the documented way to detach an IEx shell) reaches the whole
# foreground group. A handler is reset to the default in the client after
# exec, so the client still gets the signal; this shell runs the cleanup once
# the client has exited.
trap 'cleanup_cookie_home' QUIT
trap 'cleanup_cookie_home; exit 130' INT TERM HUP
with_cookie_home() {
    COOKIE_HOME="$(mktemp -d)"
    (umask 077; printf '%s' "$1" > "$COOKIE_HOME/.erlang.cookie")
    chmod 400 "$COOKIE_HOME/.erlang.cookie"
}
args=(--sname "$LOCAL_SNAME")
# A service without its own cookie uses the default ~/.erlang.cookie, so the
# real HOME stays in place for it.
if [[ -n "$COOKIE" ]]; then
    with_cookie_home "$COOKIE"
    COOKIE=""
fi
run_home="${COOKIE_HOME:-$HOME}"

if [[ -n "$EVAL" ]]; then
    HOME="$run_home" elixir "${args[@]}" --rpc-eval "$TARGET" "$EVAL"
else
    echo "[beam-attach] attaching to $TARGET — Ctrl-\\ to detach (Ctrl-C twice kills the REMOTE node)" >&2
    HOME="$run_home" iex "${args[@]}" --remsh "$TARGET"
fi
