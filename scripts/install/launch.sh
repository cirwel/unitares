#!/bin/sh
# Bootstrap a release checkout; keep the existing Compose/update contract.
set -eu

fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
case "${1:-up}" in
    -h|--help)
        echo 'Usage: sh launch.sh [up]'
        echo 'UNITARES_STACK_DIR defaults to $HOME/.unitares/stack.'
        exit 0 ;;
    up) [ "$#" -le 1 ] || fail 'up takes no arguments' ;;
    *) fail 'expected up or --help' ;;
esac
for tool in git docker curl; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required'
docker info >/dev/null 2>&1 || fail 'Start the Docker daemon, then retry'

stack=${UNITARES_STACK_DIR:-"$HOME/.unitares/stack"}
case "$stack" in /*) ;; *) fail 'UNITARES_STACK_DIR must be an absolute path' ;; esac
repo=https://github.com/cirwel/unitares.git
mkdir -p "$(dirname "$stack")"
# Resolve the parent, including symlinks, for comparison with Compose labels.
stack=$(cd "$(dirname "$stack")" && pwd -P)/$(basename "$stack")
lock="$stack.launch-lock"
mkdir "$lock" 2>/dev/null || fail "Another launcher holds $lock; remove it only if that launcher has exited"
staging=""
cleanup() {
    [ -z "$staging" ] || rm -rf "$staging"
    rmdir "$lock"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ ! -e "$stack" ] && [ ! -L "$stack" ]; then
    version=$(curl -fsSL --max-time 30 \
        https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION) \
        || fail 'Cannot resolve PUBLISHED_VERSION; no checkout was created'
    # Accept only the published stable release format, never a ref/option/path.
    version=$(printf '%s' "$version" | tr -d '[:space:]')
    printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
        || fail 'PUBLISHED_VERSION is not a stable version'
    staging=$(mktemp -d "$stack.clone.XXXXXX")
    git clone --branch "v$version" --depth 1 "$repo" "$staging"
    mv "$staging" "$stack"
    staging=""
fi
[ ! -L "$stack" ] || fail 'The managed checkout must not be a symlink'
[ -d "$stack/.git" ] || fail "$stack is not a managed Git checkout; choose an empty path"
[ "$(git -C "$stack" remote get-url origin)" = "$repo" ] \
    || fail 'The existing checkout has a different origin; leaving it untouched'
[ -z "$(git -C "$stack" status --porcelain --untracked-files=no)" ] \
    || fail 'The checkout has tracked edits; leaving it untouched'
[ -z "$(git -C "$stack" ls-files --others --exclude-standard -- db/postgres)" ] \
    || fail 'The checkout has untracked database files; leaving it untouched'
for file in docker-compose.yml db/postgres/docker-initdb.sh scripts/unitares .env.example; do
    [ -f "$stack/$file" ] || fail "Checkout is missing $file"
done
# The release commit also carries the SDK tag (sdk-vX.Y.Z); name the release.
installed=$(git -C "$stack" describe --tags --exact-match --match 'v[0-9]*' 2>/dev/null) \
    || fail 'The managed checkout is not on a release tag; use its existing update command'
# Fixed container names belong to an existing install, never adopt that stack.
for name in unitares-postgres-age unitares-redis unitares-lease-plane unitares-governance-mcp; do
    owner=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$name" 2>/dev/null || true)
    [ -z "$owner" ] || [ "$owner" = "$stack" ] \
        || fail "$name belongs to $owner; use that installation instead"
done
if [ ! -e "$stack/.env" ]; then
    (umask 077; cp "$stack/.env.example" "$stack/.env")
fi
[ -f "$stack/.env" ] || fail '.env must be a regular file'
cd "$stack"
printf 'Installed: %s\nCheckout: %s\n' "$installed" "$stack"
# Compose is idempotent: a second run keeps unchanged, running containers.
# A missing release image is an error, not an implicit source build.
docker compose up -d --wait --no-build
# Use the release's Python inside its container: no host Python dependency.
docker compose exec -T governance-mcp python - <<'PY'
import json
import os
import time
import urllib.error
import urllib.request

headers = {}
token = os.environ.get("UNITARES_MCP_BEARER_TOKEN") or os.environ.get("UNITARES_HTTP_API_TOKEN")
if token:
    headers["Authorization"] = "Bearer " + token
request = urllib.request.Request("http://127.0.0.1:8767/health/deep", headers=headers)
status = "unavailable"
for attempt in range(20):
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.load(response)
        status = body.get("status", "unknown")
        if status in ("healthy", "moderate"):
            print("Running version:", body.get("version", "not reported"))
            print("Deep health:", status)
            break
    except (OSError, ValueError):
        status = "unavailable"
    if attempt < 19:
        time.sleep(3)
else:
    raise SystemExit("Deep health did not become healthy/moderate: " + status)
PY
printf '\nManage this install from: %s\n' "$stack"
printf 'Check updates (host Python 3 required): ./scripts/unitares update --check\n'
printf 'Coordination acceptance: see docs/manual/02-install.md in the checkout.\n'
