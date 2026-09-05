#!/usr/bin/env bash
# Compatibility entrypoint for the resident merge conductor.
#
# Keep this filename so the installed report-only LaunchAgent upgrades in place. GitHub's
# repository-level native updater already refreshes armed PRs; the historical
# polling updater is therefore retired when this entrypoint lands. Existing
# plists have no conductor flags and safely select report-only mode. Execute
# mode is supported only from the separately credentialed LaunchDaemon/service
# boundary documented in the merge automation plan.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Execute mode requires the shared lease plane and dedicated review GitHub App.
# Keep the bearer and App private-key location out of the plist and load the
# operator-owned, mode-0600 secrets file at process start (the same pattern used
# by ship.sh).
SECRETS_FILE="${UNITARES_SECRETS_ENV:-${HOME}/.config/cirwel/secrets.env}"
execute_was_set=0
review_was_set=0
if [[ -n "${UNITARES_MERGE_CONDUCTOR_EXECUTE+x}" ]]; then
    execute_was_set=1
    explicit_execute="$UNITARES_MERGE_CONDUCTOR_EXECUTE"
fi
if [[ -n "${UNITARES_MERGE_CONDUCTOR_REVIEW+x}" ]]; then
    review_was_set=1
    explicit_review="$UNITARES_MERGE_CONDUCTOR_REVIEW"
fi
validate_secrets_file() {
    local path="$1"
    local file_uid file_mode
    if [[ -L "$path" || ! -f "$path" ]]; then
        echo "merge-conductor: secrets file must be a regular non-symlink" >&2
        return 1
    fi
    if [[ "$(uname -s)" == "Darwin" ]]; then
        file_uid="$(stat -f '%u' "$path")"
        file_mode="$(stat -f '%Lp' "$path")"
    else
        file_uid="$(stat -c '%u' "$path")"
        file_mode="$(stat -c '%a' "$path")"
    fi
    if [[ "$file_uid" != "$(id -u)" || "$file_mode" != "600" ]]; then
        echo \
            "merge-conductor: secrets file must be owned by the service UID with mode 0600" \
            >&2
        return 1
    fi
}

if [[ -e "$SECRETS_FILE" || -L "$SECRETS_FILE" ]]; then
    validate_secrets_file "$SECRETS_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    set +a
fi
# An explicit launchd/manual environment is control-plane configuration and
# wins over same-named values in the secrets file. The secrets file supplies
# credentials and may provide flags only for legacy plists that omitted them.
if [[ "$execute_was_set" == "1" ]]; then
    export UNITARES_MERGE_CONDUCTOR_EXECUTE="$explicit_execute"
fi
if [[ "$review_was_set" == "1" ]]; then
    export UNITARES_MERGE_CONDUCTOR_REVIEW="$explicit_review"
fi

repo="${UNITARES_MERGE_REPO:-${PR_BABYSITTER_REPO:-cirwel/unitares}}"
args=(--repo "$repo")
execute_requested=0

case "${UNITARES_MERGE_CONDUCTOR_EXECUTE:-0}" in
    1|true|TRUE|yes|YES|on|ON)
        args+=(--execute)
        execute_requested=1
        ;;
esac
case "${UNITARES_MERGE_CONDUCTOR_REVIEW:-0}" in
    1|true|TRUE|yes|YES|on|ON) args+=(--review) ;;
esac

python_bin="${UNITARES_MERGE_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
    python_bin="$(command -v python3 || true)"
fi
if [[ -z "$python_bin" || "$python_bin" != /* || ! -x "$python_bin" ]]; then
    echo "merge-conductor: configured Python interpreter is not an executable absolute path" >&2
    exit 1
fi

# A writable venv can execute a .pth file before merge_conductor.py gets a
# chance to inspect itself. In execute mode, use the OS Python in isolated,
# no-site mode to attest the configured interpreter, complete import graph, and
# deploy tree before launching any configured Python code. The conductor starts
# with -I -S as a second, in-process check and admits only the manifest paths.
if [[ "$execute_requested" == "1" ]]; then
    /usr/bin/python3 -I -S - "$python_bin" "$PROJECT_ROOT" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

manifest_path = Path("/etc/unitares/merge-service-boundary.json")
configured_python = Path(sys.argv[1])
project_root = Path(sys.argv[2])


def fail(message):
    raise SystemExit(f"merge-conductor: Python boundary preflight failed: {message}")


def check_ancestry(path, label):
    if not path.is_absolute():
        fail(f"{label} is not absolute")
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except OSError as exc:
            fail(f"{label} is unresolved at {component}: {exc}")
        if stat.S_ISLNK(metadata.st_mode):
            fail(f"{label} contains a symlink at {component}")
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            fail(f"{label} is replaceable at {component}")
        if component == Path("/"):
            break


def check_tree(path, label):
    check_ancestry(path, label)
    root = path.resolve(strict=True)
    metadata = root.stat()
    if stat.S_ISREG(metadata.st_mode):
        return
    if not stat.S_ISDIR(metadata.st_mode):
        fail(f"{label} is not a regular file/directory")
    for current, directories, files in os.walk(root, followlinks=False):
        for candidate in (
            Path(current),
            *(Path(current) / name for name in directories + files),
        ):
            item = candidate.lstat()
            if stat.S_ISLNK(item.st_mode):
                try:
                    candidate.resolve(strict=True).relative_to(root)
                except (OSError, ValueError):
                    fail(f"{label} contains an escaping symlink: {candidate}")
                continue
            if item.st_uid != 0 or stat.S_IMODE(item.st_mode) & 0o022:
                fail(f"{label} contains replaceable code: {candidate}")


try:
    manifest_meta = manifest_path.lstat()
    if (
        not stat.S_ISREG(manifest_meta.st_mode)
        or manifest_meta.st_uid != 0
        or stat.S_IMODE(manifest_meta.st_mode) & 0o022
    ):
        fail("service-boundary manifest is not root-owned/read-only")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    fail(f"service-boundary manifest is unreadable: {exc}")

if payload.get("version") != 3:
    fail("service-boundary manifest version is not 3")
try:
    manifest_python = Path(payload["python_executable_path"])
    manifest_root = Path(payload["code_root"])
    import_roots = [Path(value) for value in payload["python_import_roots"]]
except (KeyError, TypeError) as exc:
    fail(f"Python boundary fields are malformed: {exc}")
if not import_roots:
    fail("Python import-root list is empty")
try:
    if configured_python != manifest_python:
        fail("configured interpreter path does not exactly match the manifest")
    if configured_python.resolve(strict=True) != manifest_python.resolve(strict=True):
        fail("configured interpreter does not match the manifest")
    if project_root.resolve(strict=True) != manifest_root.resolve(strict=True):
        fail("deployed code root does not match the manifest")
except OSError as exc:
    fail(f"configured Python boundary is unresolved: {exc}")

check_tree(manifest_python, "Python executable")
check_tree(manifest_root, "conductor deploy tree")
for index, import_root in enumerate(import_roots):
    check_tree(import_root, f"Python import root {index}")
PY
fi

exec "$python_bin" -I -S "$PROJECT_ROOT/scripts/ops/merge_conductor.py" "${args[@]}"
