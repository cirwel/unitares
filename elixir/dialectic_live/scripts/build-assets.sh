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
# DIGEST_OK says the digest on disk finished. The project's `phx.digest`
# alias owns it (cleared before Phoenix's digest, written after it succeeds;
# mix.exs).
#
# A lock serializes builds: launchd can restart the service (and so run this
# script) while the deploy script is building, and two interleaved digests
# could leave one's marker beside the other's half-written output. It is a
# kernel flock(2) on fd 9, so a holder that dies releases it and there is no
# stale-lock recovery to race. bash has no flock builtin and macOS has no
# flock(1), so perl (present on macOS and CI runners) takes it on the
# inherited descriptor; the lock belongs to the open file description and
# outlives the perl process. The `phx.digest` alias refuses to run unless
# DIALECTIC_LIVE_ASSETS_LOCKED is set, so this script is the one digest path.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

MANIFEST="$APP_DIR/priv/static/cache_manifest.json"
DIGEST_OK="$APP_DIR/_build/assets-digest.ok"
LOCK="$APP_DIR/_build/assets-build.lock"
LOCK_WAIT="${DIALECTIC_LIVE_ASSETS_LOCK_WAIT:-300}"

mkdir -p "$APP_DIR/_build"
exec 9>>"$LOCK"
if ! perl -MFcntl=:flock -e '
  open(my $fh, ">&=", 9) or die "fd 9: $!\n";
  my $deadline = time + $ARGV[0];
  until (flock($fh, LOCK_EX | LOCK_NB)) { exit 1 if time >= $deadline; sleep 1 }
' "$LOCK_WAIT"; then
  echo "[assets] FATAL: another asset build still holds $LOCK after ${LOCK_WAIT}s" >&2
  exit 1
fi
export DIALECTIC_LIVE_ASSETS_LOCKED=1

# Children inherit fd 9 on purpose: a step that outlives a killed wrapper
# (a digest still writing) keeps holding the lock. None of them leaves a
# daemon behind (mix runs without distribution; tailwind/esbuild are one-shot).
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

mix phx.digest
