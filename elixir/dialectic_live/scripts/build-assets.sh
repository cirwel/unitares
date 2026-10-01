#!/usr/bin/env bash
# Build dialectic_live's static assets: what `mix assets.deploy` does, split
# into the compile and digest steps so a failure can be judged by which one
# failed. Expects MIX_ENV in the environment, like its callers.
#
#   build-assets.sh            scripts/start.sh, every boot (lenient)
#   build-assets.sh --strict   scripts/ops/deploy-dialectic-live.sh (any failure exits 1)
#
# Why lenient at boot: under launchd KeepAlive a fatal error here is an
# endless restart loop (~13.5k restarts on 2026-09-30, from one killed
# tailwind CLI). Code changes still get a hard gate, because the deploy
# script builds with --strict before it restarts anything.
#
# Only a COMPILE failure is survivable. tailwind/esbuild write the
# undigested app.css/app.js; production serves the digested copies through
# cache_manifest.json, and only phx.digest writes those. So a failed compile
# leaves the previous digest intact, and serving it is honest. A failed
# DIGEST is not survivable: phx.digest writes the manifest first, then
# rewrites digested files (and .gz variants) in place, so after a failure
# neither the old nor the new build is known to be whole.
#
# The marker records that the digest on disk finished. It is removed before
# phx.digest runs and created after it succeeds, so an interrupted digest
# (killed, disk full) leaves no marker and the next boot cannot mistake
# its half-written output for a previous build.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

MANIFEST="$APP_DIR/priv/static/cache_manifest.json"
DIGEST_OK="$APP_DIR/_build/assets-digest.ok"

compiled=1
{ "$APP_DIR/scripts/prepare-asset-binaries.sh" && mix assets.compile; } || compiled=0

if [ "$compiled" -eq 0 ]; then
  if [ "$STRICT" -eq 0 ] && [ -f "$DIGEST_OK" ] && [ -f "$MANIFEST" ]; then
    echo "[assets] WARNING: asset compile failed; serving the previous build (digested $(date -r "$DIGEST_OK" '+%Y-%m-%d %H:%M'))" >&2
    exit 0
  fi
  if [ "$STRICT" -eq 1 ]; then
    echo "[assets] FATAL: asset compile failed (--strict)" >&2
  else
    echo "[assets] FATAL: asset compile failed and there is no completed previous build to serve" >&2
  fi
  exit 1
fi

mkdir -p "$APP_DIR/_build"
rm -f "$DIGEST_OK"
mix phx.digest
touch "$DIGEST_OK"
