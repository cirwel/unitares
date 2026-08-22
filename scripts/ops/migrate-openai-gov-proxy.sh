#!/usr/bin/env bash
# One-time migration for com.unitares.openai-governance-proxy:
#
#   development checkout + global Python
#       -> dedicated deploy worktree + isolated virtualenv
#
# Safe and idempotent. The destination is prepared before the plist changes;
# the plist is backed up and linted before reload; a failed reload or listener
# check restores the original plist and restarts the original configuration.
set -euo pipefail

REPO="${HOST_ADAPTER_REPO:-$HOME/projects/unitares-host-adapter}"
DEPLOY="${HOST_ADAPTER_DEPLOY:-$HOME/projects/unitares-host-adapter-deploy}"
LABEL="com.unitares.openai-governance-proxy"
PLIST="${OPENAI_GOV_PROXY_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${UNITARES_PROXY_PORT:-11435}"
VENV="$DEPLOY/.venv"
UID_NUM="$(id -u)"
TAG="migrate-openai-proxy"
LAUNCHCTL="${OPENAI_GOV_PROXY_LAUNCHCTL:-launchctl}"
LSOF="${OPENAI_GOV_PROXY_LSOF:-lsof}"
PLISTBUDDY="${OPENAI_GOV_PROXY_PLISTBUDDY:-/usr/libexec/PlistBuddy}"
PLUTIL="${OPENAI_GOV_PROXY_PLUTIL:-plutil}"
VERIFY_ATTEMPTS="${OPENAI_GOV_PROXY_VERIFY_ATTEMPTS:-20}"
VERIFY_INTERVAL="${OPENAI_GOV_PROXY_VERIFY_INTERVAL:-1}"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

[ -f "$PLIST" ] || { echo "[$TAG] REFUSING: LaunchAgent is not installed at $PLIST" >&2; exit 1; }
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || { echo "[$TAG] REFUSING: source checkout is not a git worktree: $REPO" >&2; exit 1; }

proxy_pid() {
  "$LAUNCHCTL" list 2>/dev/null | awk -v l="$LABEL" '$3==l && $1!="-"{print $1; exit}'
}

launchd_targets_deploy() {
  local loaded
  loaded="$("$LAUNCHCTL" print "gui/$UID_NUM/$LABEL" 2>/dev/null)" || return 1
  [[ "$loaded" == *"$VENV/bin/python3"* && "$loaded" == *"$DEPLOY/src"* ]]
}

proxy_ready() {
  local pid
  launchd_targets_deploy || return 1
  pid="$(proxy_pid)"
  [ -n "$pid" ] || return 1
  "$LSOF" -nP -a -p "$pid" -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null \
    | awk 'NR>1{found=1} END{exit !found}'
}

WAS_LOADED=0
"$LAUNCHCTL" print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 && WAS_LOADED=1

if grep -qF "$VENV/bin/python3" "$PLIST" && grep -qF "$DEPLOY/src" "$PLIST"; then
  if proxy_ready; then
    echo "[$TAG] OK — $LABEL already runs from $DEPLOY"
    exit 0
  fi
  echo "[$TAG] plist already names $DEPLOY, but the loaded job/socket does not; repairing the reload"
fi

CURRENT_PYTHON="$("$PLISTBUDDY" -c 'Print :ProgramArguments:0' "$PLIST" 2>/dev/null || true)"
BASE_PYTHON="${HOST_ADAPTER_PYTHON_BASE:-$CURRENT_PYTHON}"
if [ ! -x "$BASE_PYTHON" ]; then
  BASE_PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
fi
if [ ! -x "$BASE_PYTHON" ]; then
  BASE_PYTHON="$(command -v python3 || true)"
fi
[ -x "$BASE_PYTHON" ] \
  || { echo "[$TAG] REFUSING: no usable base Python (set HOST_ADAPTER_PYTHON_BASE)" >&2; exit 1; }

if [ "$DRY_RUN" = 1 ]; then
  echo "[$TAG] DRY would prepare detached deploy worktree: $DEPLOY <- $REPO origin/master"
  echo "[$TAG] DRY would create $VENV with $BASE_PYTHON and install .[proxy]"
  echo "[$TAG] DRY would repoint ProgramArguments[0] and PYTHONPATH in $PLIST"
  if [ "$WAS_LOADED" = 1 ]; then
    echo "[$TAG] DRY would reload $LABEL and require launchd's PID to listen on :$PORT"
  else
    echo "[$TAG] DRY would leave $LABEL stopped/unloaded after updating its plist"
  fi
  exit 0
fi

deploy_lib_acquire_lock "$TAG" "$DEPLOY"
deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY" --detach

if [ ! -x "$VENV/bin/python3" ]; then
  echo "[$TAG] creating isolated venv at $VENV (base: $BASE_PYTHON)"
  "$BASE_PYTHON" -m venv "$VENV"
fi
echo "[$TAG] installing proxy dependencies into $VENV"
"$VENV/bin/python3" -m pip install --disable-pip-version-check --quiet -e "${DEPLOY}[proxy]"

BACKUP="$PLIST.bak.$(date +%Y%m%d%H%M%S)"
cp "$PLIST" "$BACKUP"
RELOAD_ATTEMPTED=0

restore_plist() {
  cp "$BACKUP" "$PLIST"
  if [ "$RELOAD_ATTEMPTED" = 1 ] && [ "$WAS_LOADED" = 1 ]; then
    "$LAUNCHCTL" bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    "$LAUNCHCTL" bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null \
      || "$LAUNCHCTL" load "$PLIST" 2>/dev/null \
      || echo "[$TAG] CRITICAL — original plist was restored but launchd did not reload it" >&2
  fi
}

if ! "$PLISTBUDDY" -c "Set :ProgramArguments:0 $VENV/bin/python3" "$PLIST" \
  || ! "$PLISTBUDDY" -c "Set :EnvironmentVariables:PYTHONPATH $DEPLOY/src" "$PLIST" \
  || ! "$PLUTIL" -lint "$PLIST" >/dev/null 2>&1; then
  echo "[$TAG] FAILED — could not write a valid deploy plist; restoring $BACKUP" >&2
  restore_plist
  exit 1
fi

if [ "$WAS_LOADED" != 1 ]; then
  echo "[$TAG] DONE — plist now targets $DEPLOY; service was not loaded, so it remains stopped"
  echo "[$TAG] backup: $BACKUP"
  exit 0
fi

echo "[$TAG] reloading $LABEL so launchd reads the new interpreter and PYTHONPATH"
RELOAD_ATTEMPTED=1
"$LAUNCHCTL" bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if ! "$LAUNCHCTL" bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null \
  && ! "$LAUNCHCTL" load "$PLIST" 2>/dev/null; then
  echo "[$TAG] FAILED — launchd rejected the deploy plist; restoring $BACKUP" >&2
  restore_plist
  exit 1
fi

echo "[$TAG] verifying launchd's PID owns 127.0.0.1:$PORT"
if deploy_lib_poll "$VERIFY_ATTEMPTS" "$VERIFY_INTERVAL" proxy_ready; then
  echo "[$TAG] DONE — proxy now serves from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD)"
  echo "[$TAG] backup: $BACKUP"
  exit 0
fi

echo "[$TAG] FAILED — migrated proxy did not listen on :$PORT; restoring $BACKUP" >&2
restore_plist
exit 1
