#!/usr/bin/env bash
# Build the hub's stylesheet (`mix assets.deploy` -> priv/static/assets/css/hub.css)
# unless the build on disk is already the one for this checkout. Expects
# MIX_ENV in the environment, like its callers.
#
#   build-assets.sh            scripts/start.sh, every boot (lenient)
#   build-assets.sh --strict   scripts/ops/deploy-portal.sh (always builds;
#                              any failure exits 1)
#
# The same design as elixir/dialectic_live/scripts/build-assets.sh, simpler
# because the hub builds one file and no digest:
#   * a boot-time failure is never fatal while a previous build is on disk,
#     so a killed Tailwind CLI cannot become a launchd KeepAlive restart loop;
#   * STAMP records this app's git tree plus the built file's checksum, so a
#     boot after a deploy build finds it current and does nothing;
#   * LOCK (a flock on fd 9, taken by perl) serializes the check and the build,
#     so a launchd restart landing mid-deploy waits instead of interleaving.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

OUTPUT="$APP_DIR/priv/static/assets/css/hub.css"
STAMP="$APP_DIR/_build/assets.stamp"
LOCK="$APP_DIR/_build/assets-build.lock"
LOCK_WAIT="${PORTAL_ASSETS_LOCK_WAIT:-300}"

serve_or_fail() {
  if [ "$STRICT" -eq 0 ] && [ -s "$OUTPUT" ]; then
    echo "[assets] WARNING: $1; serving the build already on disk ($(date -r "$OUTPUT" '+%Y-%m-%d %H:%M'))" >&2
    exit 0
  fi
  if [ "$STRICT" -eq 1 ]; then
    echo "[assets] FATAL: $1 (--strict)" >&2
  else
    echo "[assets] FATAL: $1, and there is no previous build to serve" >&2
  fi
  exit 1
}

mkdir -p "$APP_DIR/_build"
exec 9>>"$LOCK"
if ! perl -MFcntl=:flock -e '
  open(my $fh, ">&=", 9) or die "fd 9: $!\n";
  my $deadline = time + $ARGV[0];
  until (flock($fh, LOCK_EX | LOCK_NB)) { exit 1 if time >= $deadline; sleep 1 }
' "$LOCK_WAIT"; then
  serve_or_fail "another asset build still holds $LOCK after ${LOCK_WAIT}s"
fi

# GIT_OPTIONAL_LOCKS=0: the deploy tree is shared, so this check must not
# refresh its index while another service's deploy is running git there.
tree=""
if [ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain -- . 2>/dev/null || echo unknown)" ]; then
  tree="$(git rev-parse HEAD:./ 2>/dev/null || true)"
fi

built_from() {
  printf '%s %s\n' "$tree" "$(shasum -a 256 <"$OUTPUT" | cut -d' ' -f1)"
}

if [ "$STRICT" -eq 0 ] && [ -n "$tree" ] && [ -s "$OUTPUT" ] && [ -f "$STAMP" ] &&
  [ "$(cat "$STAMP")" = "$(built_from)" ]; then
  echo "[assets] build is current (tree ${tree:0:12}); nothing to do"
  exit 0
fi

rm -f "$STAMP"
if "$APP_DIR/scripts/prepare-asset-binaries.sh" && mix assets.deploy && [ -s "$OUTPUT" ]; then
  if [ -n "$tree" ]; then
    built_from >"$STAMP"
  fi
  exit 0
fi
serve_or_fail "asset build failed"
