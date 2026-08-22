#!/usr/bin/env bash
# Deploy com.unitares.openai-governance-proxy from a DEDICATED worktree of
# cirwel/unitares-host-adapter. The service used to load `src/` directly from
# the operator's development checkout, so deploy-status could report drift but
# deploy-apply could not safely pull or restart it.
#
# One-time topology migration:
#   scripts/ops/migrate-openai-gov-proxy.sh
#
# After migration this script fast-forwards only the deploy worktree, refreshes
# its isolated virtualenv when dependencies changed, restarts the LaunchAgent,
# and verifies that launchd's PID owns the proxy's listening socket. A request
# probe is deliberately NOT used: every non-chat path is forwarded to Ollama,
# so a 200 from / or /health would prove the upstream is alive, not this proxy.
set -euo pipefail

REPO="${HOST_ADAPTER_REPO:-$HOME/projects/unitares-host-adapter}"
DEPLOY="${HOST_ADAPTER_DEPLOY:-$HOME/projects/unitares-host-adapter-deploy}"
LABEL="com.unitares.openai-governance-proxy"
PLIST="${OPENAI_GOV_PROXY_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_PROXY_PORT:-11435}"
VENV="$DEPLOY/.venv"
UID_NUM="$(id -u)"
TAG="deploy-openai-proxy"
LAUNCHCTL="${OPENAI_GOV_PROXY_LAUNCHCTL:-launchctl}"
LSOF="${OPENAI_GOV_PROXY_LSOF:-lsof}"
VERIFY_ATTEMPTS="${OPENAI_GOV_PROXY_VERIFY_ATTEMPTS:-20}"
VERIFY_INTERVAL="${OPENAI_GOV_PROXY_VERIFY_INTERVAL:-1}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

MIGRATE="$(cd "$(dirname "$0")" && pwd)/migrate-openai-gov-proxy.sh"
MIGRATION_RECIPE="[$TAG]   $MIGRATE"

# `cirwel update` has already refreshed origin via deploy-status. Name the
# source change before any topology refusal so BEHIND(1) answers "behind on
# what?" instead of exposing only a count.
PENDING_SOURCE="$(git -C "$REPO" log -1 --oneline HEAD..origin/master -- src 2>/dev/null || true)"
[ -n "$PENDING_SOURCE" ] && echo "[$TAG] pending source: $PENDING_SOURCE"

# Both values are load-bearing. The interpreter isolates dependencies; the
# PYTHONPATH selects the code. Checking only one permits a split configuration
# that restarts successfully while still importing the development checkout.
deploy_lib_require_plist_target "$TAG" "$PLIST" "$VENV/bin/python3" \
  --require-exists \
  --recipe "$MIGRATION_RECIPE" \
  --recipe-handles-reload
deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY/src" \
  --require-exists \
  --recipe "$MIGRATION_RECIPE" \
  --recipe-handles-reload

launchd_targets_deploy() {
  local loaded
  loaded="$("$LAUNCHCTL" print "gui/$UID_NUM/$LABEL" 2>/dev/null)" || return 1
  [[ "$loaded" == *"$VENV/bin/python3"* && "$loaded" == *"$DEPLOY/src"* ]]
}

# Editing a plist does not alter an already-loaded launchd definition. Refuse
# that split state explicitly: kickstart would otherwise restart the old
# definition, the socket probe would pass, and the deploy would lie.
if ! launchd_targets_deploy; then
  echo "[$TAG] REFUSING: the plist file is migrated, but launchd is not loaded from $DEPLOY." >&2
  echo "[$TAG] Run the idempotent migration/reload: $MIGRATE" >&2
  exit 2
fi

deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY" --detach
PREV="$DEPLOY_LIB_PREV"
NOW="$(git -C "$DEPLOY" rev-parse HEAD)"

# A deploy worktree's venv is gitignored. Create it from the same framework
# Python the pre-migration LaunchAgent used unless the operator overrides it.
BASE_PYTHON="${HOST_ADAPTER_PYTHON_BASE:-/Library/Frameworks/Python.framework/Versions/3.14/bin/python3}"
if [ ! -x "$BASE_PYTHON" ]; then
  BASE_PYTHON="$(command -v python3)"
fi

NEED_DEPS=0
[ -x "$VENV/bin/python3" ] || NEED_DEPS=1
if [ "$PREV" != "$NOW" ] && ! git -C "$DEPLOY" diff --quiet "$PREV" "$NOW" -- pyproject.toml; then
  NEED_DEPS=1
fi

install_dependencies() {
  if [ ! -x "$VENV/bin/python3" ]; then
    echo "[$TAG] creating isolated venv at $VENV (base: $BASE_PYTHON)"
    "$BASE_PYTHON" -m venv "$VENV"
  fi
  echo "[$TAG] installing proxy dependencies into $VENV"
  "$VENV/bin/python3" -m pip install --disable-pip-version-check --quiet -e "${DEPLOY}[proxy]"
}

if [ "$NEED_DEPS" = 1 ] && ! install_dependencies; then
  echo "[$TAG] FAILED — dependency install failed; rolling the worktree back to ${PREV:0:8} and NOT restarting." >&2
  git -C "$DEPLOY" reset --hard "$PREV"
  exit 1
fi

proxy_pid() {
  "$LAUNCHCTL" list 2>/dev/null | awk -v l="$LABEL" '$3==l && $1!="-"{print $1; exit}'
}

proxy_ready() {
  local pid
  launchd_targets_deploy || return 1
  pid="$(proxy_pid)"
  [ -n "$pid" ] || return 1
  "$LSOF" -nP -a -p "$pid" -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null \
    | awk 'NR>1{found=1} END{exit !found}'
}

echo "[$TAG] restarting $LABEL"
"$LAUNCHCTL" kickstart -k "gui/$UID_NUM/$LABEL"

echo "[$TAG] verifying launchd's PID owns 127.0.0.1:$PORT"
if deploy_lib_poll "$VERIFY_ATTEMPTS" "$VERIFY_INTERVAL" proxy_ready; then
  echo "[$TAG] OK — OpenAI governance proxy listening on :$PORT (serving from $DEPLOY @ ${NOW:0:8})"
  exit 0
fi

echo "[$TAG] FAILED — $LABEL did not own a listening socket on :$PORT after restart." >&2
echo "[$TAG] Rolling the worktree back to ${PREV:0:8} and restarting the prior code." >&2
git -C "$DEPLOY" reset --hard "$PREV"
if [ "$NEED_DEPS" = 1 ]; then
  install_dependencies || echo "[$TAG] WARNING — rollback dependency refresh failed" >&2
fi
"$LAUNCHCTL" kickstart -k "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if deploy_lib_poll "$VERIFY_ATTEMPTS" "$VERIFY_INTERVAL" proxy_ready; then
  echo "[$TAG] rollback recovered the proxy; inspect $HOME/Library/Logs/unitares-openai-proxy.error.log" >&2
else
  echo "[$TAG] CRITICAL — rollback did not recover the proxy. Check launchctl and the proxy error log." >&2
fi
exit 1
