#!/usr/bin/env bash
# Launchd entrypoint for the Wave 3a BEAM handler app (Elixir/OTP).
#
# Sources WAVE_3A_BEAM_TOKEN + WAVE_3A_PROBE_TOKEN from
# the file explicitly named by UNITARES_SECRETS_ENV (mode 600), then execs
# `mix run --no-halt`. Bind defaults to 127.0.0.1:8770; override via
# the :wave3a_handlers runtime config or env (see config/config.exs).
#
# Used by: scripts/ops/com.unitares.wave3a-handlers.plist (NOT loaded by
# default — operator must `launchctl load` when ready for cutover).
# Manual invocation: `./elixir/wave3a_handlers/scripts/start.sh`
#
# Fail-closed posture: if secrets.env is missing or WAVE_3A_BEAM_TOKEN is
# unset, the application starts but HTTPAuth returns 503 on every
# bearer-gated route — never silently open. See application.ex:43 and
# http_auth.ex.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HANDLERS_DIR="$REPO_ROOT/elixir/wave3a_handlers"
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
# from launchd's user environment is sparse, so we set it explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$HANDLERS_DIR"
exec mix run --no-halt
