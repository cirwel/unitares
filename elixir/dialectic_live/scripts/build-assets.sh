#!/usr/bin/env bash
# Build dialectic_live's static assets (`mix assets.deploy`), unless the build
# on disk is already the one for this checkout. Expects MIX_ENV in the
# environment, like its callers.
#
#   build-assets.sh            scripts/start.sh, every boot (lenient)
#   build-assets.sh --strict   scripts/ops/deploy-dialectic-live.sh (always
#                              builds; any failure exits 1)
#
# The deploy script builds with --strict before it restarts anything, so code
# changes get a hard gate there and a normal boot finds the build current and
# does no asset work. Boot builds only when the build is missing or stale: a
# restart that lands after another service fast-forwarded the shared deploy
# tree but before this app's own deploy, or a wiped priv/static or _build.
#
# A boot-time build failure is never fatal while a previous build is on disk:
# under launchd KeepAlive a fatal error here is an endless restart loop
# (~13.5k restarts on 2026-09-30, from one killed tailwind CLI). Boot then
# serves whatever is on disk, even a digest that a failed build left
# half-written. Degraded CSS on a local dashboard beats a restart loop, and
# the next boot or deploy rebuilds because the failed build left no stamp.
#
# STAMP says what the build on disk was built from: this app's git tree plus
# a checksum of the manifest the build wrote. The tree catches code changes.
# The checksum catches every other writer (a deploy rollback running an older
# build script, a hand-run `mix assets.deploy`), because each phx.digest run
# rewrites the manifest. With uncommitted changes, or outside a git checkout,
# there is no tree to trust and every boot builds.
#
# No lock: a boot build and a deploy build that overlap compile the same tree
# into the same files.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

MANIFEST="$APP_DIR/priv/static/cache_manifest.json"
STAMP="$APP_DIR/_build/assets.stamp"

# GIT_OPTIONAL_LOCKS=0: the deploy tree is shared, so this check must not
# refresh its index while another service's deploy is running git there.
tree=""
if [ -z "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain -- . 2>/dev/null || echo unknown)" ]; then
  tree="$(git rev-parse HEAD:./ 2>/dev/null || true)"
fi

built_from() {
  printf '%s %s\n' "$tree" "$(shasum -a 256 <"$MANIFEST" | cut -d' ' -f1)"
}

if [ "$STRICT" -eq 0 ] && [ -n "$tree" ] && [ -f "$MANIFEST" ] && [ -f "$STAMP" ] &&
  [ "$(cat "$STAMP")" = "$(built_from)" ]; then
  echo "[assets] build is current (tree ${tree:0:12}); nothing to do"
  exit 0
fi

mkdir -p "$APP_DIR/_build"
rm -f "$STAMP"
if "$APP_DIR/scripts/prepare-asset-binaries.sh" && mix assets.deploy; then
  if [ -n "$tree" ]; then
    built_from >"$STAMP"
  fi
  exit 0
fi

if [ "$STRICT" -eq 1 ]; then
  echo "[assets] FATAL: asset build failed (--strict)" >&2
  exit 1
fi
if [ -f "$MANIFEST" ]; then
  echo "[assets] WARNING: asset build failed; serving the build already on disk (manifest from $(date -r "$MANIFEST" '+%Y-%m-%d %H:%M'))" >&2
  exit 0
fi
echo "[assets] FATAL: asset build failed and there is no previous build to serve" >&2
exit 1
