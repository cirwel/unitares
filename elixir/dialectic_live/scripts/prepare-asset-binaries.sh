#!/usr/bin/env bash
# Make the tailwind/esbuild CLIs in _build/ present and executable before
# `mix assets.deploy` runs them. Called by scripts/build-assets.sh whenever it
# builds: on every deploy, and on a boot that finds the build stale.
# Expects MIX_ENV in the environment, like its callers.
#
# Why: macOS 27 SIGKILLs the linker-signed (ad-hoc) tailwind CLI on exec,
# including a fresh upstream download, so assets.deploy dies with 137. Under
# launchd KeepAlive that became ~13.5k restarts before 2026-09-30. A real
# ad-hoc signature (`codesign --force -s -`) clears it. esbuild is unaffected
# today; it gets the same check because it is fetched the same way.
#
# Heal BEFORE assets.setup as well as after: `tailwind.install --if-missing`
# decides "missing" by running `--help`, so a killed binary would otherwise be
# deleted and re-downloaded (linker-signed again) on every build, and an
# offline build would fail where a re-sign alone would have worked.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# Exit status of `$1 --help`, without tripping set -e.
probe() {
  local rc=0
  "$1" --help >/dev/null 2>&1 || rc=$?
  echo "$rc"
}

heal() {
  [ "$(uname -s)" = "Darwin" ] || return 0
  local bin sig
  for bin in _build/tailwind-* _build/esbuild-*; do
    [ -x "$bin" ] || continue
    [ "$(probe "$bin")" -eq 137 ] || continue
    sig="$(codesign -dv "$bin" 2>&1 || true)"
    # Only re-sign what carries no real signature. A properly signed binary
    # killed on exec is a different problem; leave it for a human.
    case "$sig" in
      *linker-signed*|*"not signed at all"*) ;;
      *)
        echo "[assets] $bin killed on exec (137) but not linker-signed; not re-signing" >&2
        return 1
        ;;
    esac
    echo "[assets] $bin killed on exec (137), linker-signed; re-signing ad-hoc" >&2
    codesign --force -s - "$bin"
    if [ "$(probe "$bin")" -eq 137 ]; then
      echo "[assets] $bin still killed on exec after re-signing" >&2
      return 1
    fi
  done
}

heal
mix assets.setup
heal
