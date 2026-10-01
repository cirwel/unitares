#!/usr/bin/env bash
# Standing-service launcher for the dialectic_live Phoenix app.
# Mirrors the lease_plane / wave3a_handlers pattern: source secrets, self-heal
# deps on restart, then run. Invoked by scripts/ops/com.unitares.dialectic-live.plist.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# Secrets (mode 600). Provides DIALECTIC_LIVE_SECRET_KEY_BASE and, optionally,
# UNITARES_HTTP_API_TOKEN for authenticated tool-calls.
# UNITARES_SECRETS_ENV, else ~/.config/unitares/secrets.env, else the
# pre-2026-09 ~/.config/cirwel path when only that one exists.
SECRETS="${UNITARES_SECRETS_ENV:-$HOME/.config/unitares/secrets.env}"
if [ -z "${UNITARES_SECRETS_ENV:-}" ] && [ ! -f "$SECRETS" ] && [ -f "$HOME/.config/cirwel/secrets.env" ]; then
    SECRETS="$HOME/.config/cirwel/secrets.env"
fi
if [ -f "$SECRETS" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS"
  set +a
fi

export MIX_ENV="${MIX_ENV:-prod}"
export PORT="${PORT:-8790}"
export PHX_SERVER=true

# Phoenix prod requires SECRET_KEY_BASE. Keep it app-scoped in secrets.env as
# DIALECTIC_LIVE_SECRET_KEY_BASE; generate once with: mix phx.gen.secret
if [ -z "${SECRET_KEY_BASE:-}" ]; then
  if [ -n "${DIALECTIC_LIVE_SECRET_KEY_BASE:-}" ]; then
    export SECRET_KEY_BASE="$DIALECTIC_LIVE_SECRET_KEY_BASE"
  else
    echo "FATAL: SECRET_KEY_BASE (or DIALECTIC_LIVE_SECRET_KEY_BASE) is unset." >&2
    echo "Generate one with 'mix phx.gen.secret' and add it to $SECRETS" >&2
    exit 1
  fi
fi

# Upstream governance MCP. Defaults target a local server; override via env.
export GOVERNANCE_WS_URL="${GOVERNANCE_WS_URL:-ws://127.0.0.1:8767/ws/eisv}"
export GOVERNANCE_TOOLS_URL="${GOVERNANCE_TOOLS_URL:-http://127.0.0.1:8767/v1/tools/call}"
export GOVERNANCE_START_FIREHOSE="${GOVERNANCE_START_FIREHOSE:-true}"

# macOS 27 SIGKILLs the linker-signed (ad-hoc) tailwind CLI on exec, so
# assets.deploy dies with 137 and launchd restarts us every ThrottleInterval
# forever (2026-09-30: ~13.5k loops). Re-signing ad-hoc clears it. esbuild is
# unaffected today; it gets the same check because it is fetched the same way.
heal_asset_binaries() {
  [ "$(uname -s)" = "Darwin" ] || return 0
  local bin rc
  for bin in _build/tailwind-* _build/esbuild-*; do
    [ -x "$bin" ] || continue
    rc=0
    "$bin" --help >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 137 ]; then
      echo "[start] $bin killed on exec (137); re-signing ad-hoc" >&2
      codesign --force -s - "$bin"
    fi
  done
}

# Self-heal deps + assets on restart (cheap no-op when already current).
# assets.setup fetches missing asset CLIs first so a fresh download is
# checked before assets.deploy runs it.
mix deps.get --only "$MIX_ENV"
mix assets.setup
heal_asset_binaries
mix assets.deploy

exec mix phx.server
