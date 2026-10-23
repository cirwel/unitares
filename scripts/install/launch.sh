#!/bin/sh
# Bootstrap a release checkout; keep the existing Compose/update contract.
set -eu

fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
separate=0
for arg in "$@"; do
    case "$arg" in
    -h|--help)
        echo 'Usage: sh launch.sh [up] [--separate]'
        echo 'UNITARES_STACK_DIR defaults to $HOME/.unitares/stack.'
        echo '--separate requires explicit GOVERNANCE_HOST_PORT and LEASE_PLANE_HOST_PORT, outside 8767/8788.'
        echo 'Choose an installation: https://github.com/cirwel/unitares/blob/master/docs/install/CHOOSING_INSTALLATION.md'
        exit 0 ;;
    up) ;;
    --separate) separate=1 ;;
    *) fail 'expected up, --separate or --help' ;;
    esac
done
stack=${UNITARES_STACK_DIR:-"$HOME/.unitares/stack"}
case "$stack" in /*) ;; *) fail 'UNITARES_STACK_DIR must be an absolute path' ;; esac

# Probe installation metadata only: never source native.env, start a service,
# or infer the owner of an HTTP endpoint from its version string.
native=0
brew_prefix=""
brew_command=$(command -v brew 2>/dev/null || true)
if [ -z "$brew_command" ]; then
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
        if [ -x "$candidate" ]; then brew_command=$candidate; break; fi
    done
fi
if [ -n "$brew_command" ]; then
    brew_prefix=$(HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 "$brew_command" --prefix 2>/dev/null) \
        || fail 'Cannot inspect the Homebrew prefix; check brew before installing another stack'
    case "$brew_prefix" in /*) ;; *) fail 'Homebrew returned an invalid prefix' ;; esac
    if [ -e "$brew_prefix/opt/unitares" ] || [ -L "$brew_prefix/opt/unitares" ]; then
        printf 'Existing Homebrew installation: %s\n' "$brew_prefix/opt/unitares" >&2
        echo 'Package presence alone does not establish a configured native server.' >&2
    fi
fi
native_config=${UNITARES_NATIVE_ENV:-"${brew_prefix:-/opt/homebrew}/etc/unitares/native.env"}
if [ -f "$native_config" ]; then
    printf 'Existing native configuration: %s\n' "$native_config" >&2
    native=1
fi
if [ -f "$HOME/Library/LaunchAgents/com.unitares.governance-mcp.plist" ]; then
    printf 'Existing native LaunchAgent: %s\n' "$HOME/Library/LaunchAgents/com.unitares.governance-mcp.plist" >&2
    native=1
fi
if [ "$native" = 1 ] && [ "$separate" = 0 ]; then
    if [ -d "$stack/.git" ] && [ -f "$stack/.env" ]; then
        echo 'Native configuration also exists; reusing the existing managed Docker checkout after validation. Its ports and data will be preserved.' >&2
    else
        fail 'Use the existing native installation, or explicitly choose a separate Docker install with --separate and two alternate host ports. Nothing was installed. See https://github.com/cirwel/unitares/blob/master/docs/install/CHOOSING_INSTALLATION.md'
    fi
fi
if [ "$separate" = 1 ]; then
    # Explicit environment values override .env in Compose. Do not evaluate
    # someone else's .env as shell code or guess its interpolated port values.
    for port in "${GOVERNANCE_HOST_PORT:-}" "${LEASE_PLANE_HOST_PORT:-}"; do
        case "$port" in ''|*[!0-9]*|0*|8767|8788) fail '--separate requires two explicit alternate host ports (1-65535, excluding 8767/8788)' ;; esac
        [ "${#port}" -le 5 ] && [ "$port" -le 65535 ] \
            || fail '--separate host ports must be in 1-65535'
    done
    [ "$GOVERNANCE_HOST_PORT" != "$LEASE_PLANE_HOST_PORT" ] \
        || fail '--separate requires different server and lease-plane ports'
fi
for tool in git docker curl; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required'
docker info >/dev/null 2>&1 || fail 'Start the Docker daemon, then retry'

repo=https://github.com/cirwel/unitares.git
# Check existing Compose ownership before creating a checkout or lock. Resolve
# an existing parent without creating it, so symlinked paths still compare.
if [ -d "$(dirname "$stack")" ]; then
    stack=$(cd "$(dirname "$stack")" && pwd -P)/$(basename "$stack")
fi
check_compose_ownership() {
    for name in unitares-postgres-age unitares-redis unitares-lease-plane unitares-governance-mcp; do
        if owner=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$name" 2>/dev/null); then
            [ "$owner" = "$stack" ] \
                || fail "$name belongs to ${owner:-an unknown checkout}; use that installation instead"
        fi
    done
}
check_compose_ownership
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
check_compose_ownership
if [ ! -e "$stack/.env" ]; then
    [ ! -L "$stack/.env" ] || fail '.env is a broken symlink; leaving it untouched'
    (
        umask 077
        cp "$stack/.env.example" "$stack/.env"
        if [ "$separate" = 1 ]; then
            printf '\nGOVERNANCE_HOST_PORT=%s\nLEASE_PLANE_HOST_PORT=%s\n' \
                "$GOVERNANCE_HOST_PORT" "$LEASE_PLANE_HOST_PORT" >> "$stack/.env"
        fi
    )
fi
[ -f "$stack/.env" ] || fail '.env must be a regular file'
if [ "$separate" = 1 ]; then
    # Save fresh choices above so later CLI updates keep the same ports. An
    # existing file is operator-owned: require literal matching assignments,
    # rather than silently rewriting it or evaluating dotenv expressions.
    for key in GOVERNANCE_HOST_PORT LEASE_PLANE_HOST_PORT; do
        saved=$(awk -v key="$key" '
            /^[[:space:]]*#/ { next }
            { line=$0; sub(/^[[:space:]]*export[[:space:]]+/, "", line)
              if (line ~ ("^[[:space:]]*" key "([[:space:]]*(#.*)?)?$")) invalid=1
              if (line ~ ("^[[:space:]]*" key "[[:space:]]*:")) invalid=1
              pos=index(line, "="); name=substr(line, 1, pos-1)
              gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
              if (pos && name==key) {
                  # Reject assignment forms the CLI port reader skips.
                  if ($0 !~ ("^[[:space:]]*" key "=")) invalid=1
                  value=substr(line, pos+1)
              } }
            END {
                if (invalid) { print "unsupported port assignment"; exit }
                quote=substr(value, 1, 1)
                if ((quote=="\"" || quote==sprintf("%c", 39)) && substr(value, length(value), 1)==quote)
                    value=substr(value, 2, length(value)-2)
                print value
            }
        ' "$stack/.env")
        case "$key" in GOVERNANCE_HOST_PORT) expected=$GOVERNANCE_HOST_PORT ;; *) expected=$LEASE_PLANE_HOST_PORT ;; esac
        [ "$saved" = "$expected" ] \
            || fail "Set the literal $key=$expected in $stack/.env before retrying; existing configuration was not changed"
    done
    # Check a later invocation without this command's port overrides resolves
    # identically. Compare Compose's complete config internally: line-reading
    # `config --environment` is ambiguous when a value contains newlines.
    # Never print these configurations, which may contain credentials.
    explicit_config=$(docker compose --project-directory "$stack" config) \
        || fail 'Cannot resolve the Compose configuration; no services were started'
    saved_config=$(
        unset GOVERNANCE_HOST_PORT LEASE_PLANE_HOST_PORT
        docker compose --project-directory "$stack" config
    ) || fail 'Cannot resolve the saved Compose configuration; no services were started'
    [ "$saved_config" = "$explicit_config" ] \
        || fail "Compose and the CLI disagree on saved ports; use single literal assignments in $stack/.env before retrying"
    unset explicit_config saved_config
fi
cd "$stack"
printf 'Installation: Docker Compose\nInstalled: %s\nCheckout: %s\n' "$installed" "$stack"
if [ "$separate" = 1 ]; then
    printf 'Separate install: server port %s, lease-plane port %s\n' "$GOVERNANCE_HOST_PORT" "$LEASE_PLANE_HOST_PORT"
fi
# Pull first: inside `up`, a failed pull surfaces only as "No such image".
# Only missing images: refreshing the floating redis tag would restart Redis.
docker compose pull --policy missing --quiet \
    || fail 'Could not pull the release images; check the network and retry'
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
printf 'Optional: connect a model for advice and local reviews with ./scripts/unitares model.\n'
printf 'Ollama is one supported choice; core startup does not require a model.\n'
