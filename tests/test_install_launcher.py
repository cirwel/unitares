"""Exercise the bootstrap with real shallow Git clones and a fake Docker CLI."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install/launch.sh"


@pytest.fixture
def launcher(tmp_path):
    source = tmp_path / "release"
    source.mkdir()
    for name in ("docker-compose.yml", "db/postgres/docker-initdb.sh", "scripts/unitares", ".env.example"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n")
    (source / ".gitignore").write_text(".env\n")
    git = shutil.which("git")
    def run_git(*args):
        subprocess.run([git, "-C", str(source), *args], check=True, capture_output=True)
    run_git("init")
    run_git("add", ".")
    run_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "release")
    run_git("tag", "v3.4.0")
    # Real release commits also carry the SDK tag; only the v* tag names the release.
    run_git("tag", "sdk-v0.5.0")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = {
        "curl": '#!/bin/sh\nprintf "%s" "${TEST_VERSION:-3.4.0}"\n',
        "docker": '''#!/bin/sh
printf '%s\n' "$*" >> "$TEST_DOCKER_LOG"
case "$*" in
  info) exit "${TEST_DAEMON_RC:-0}" ;;
  inspect*) [ -n "${TEST_OWNER:-}" ] && printf '%s' "$TEST_OWNER"; exit 0 ;;
  "compose pull"*) exit "${TEST_PULL_RC:-0}" ;;
  *exec*) cat > "$TEST_HEALTH_CODE"; exit "${TEST_HEALTH_RC:-0}" ;;
esac
''',
        "git": f'''#!/bin/sh
if [ "$1" = clone ]; then
    "{git}" clone --branch "$3" --depth 1 "file://$TEST_SOURCE" "$7" || exit $?
    exec "{git}" -C "$7" remote set-url origin https://github.com/cirwel/unitares.git
fi
exec "{git}" "$@"
''',
    }
    for name, content in commands.items():
        path = bin_dir / name
        path.write_text(content)
        path.chmod(0o755)
    stack = tmp_path / "managed space" / "stack"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               UNITARES_STACK_DIR=str(stack), TEST_SOURCE=str(source),
               TEST_DOCKER_LOG=str(tmp_path / "docker.log"),
               TEST_HEALTH_CODE=str(tmp_path / "health.py"))
    def launch(**overrides):
        return subprocess.run(["sh", str(SCRIPT)], env=env | overrides,
                              text=True, capture_output=True)
    return stack, env, launch


def test_clone_repeat_and_preserve_config(launcher):
    stack, env, launch = launcher
    first = launch()
    assert first.returncode == 0, first.stderr
    assert "Installed: v3.4.0" in first.stdout
    assert (stack / ".git/shallow").exists()
    assert (stack / "db/postgres/docker-initdb.sh").exists()
    (stack / ".env").write_text("GOVERNANCE_HOST_PORT=18767\n")
    # A newer publication must not move an existing managed checkout.
    second = launch(TEST_VERSION="99.0.0")
    assert second.returncode == 0, second.stderr
    assert "Installed: v3.4.0" in second.stdout
    assert (stack / ".env").read_text() == "GOVERNANCE_HOST_PORT=18767\n"
    calls = Path(env["TEST_DOCKER_LOG"]).read_text()
    assert "up -d --wait --no-build" in calls
    # A repeat run must not refresh floating tags (redis:8-alpine) and restart them.
    assert "compose pull --policy missing" in calls
    assert not Path(str(stack) + ".launch-lock").exists()


@pytest.mark.parametrize("version", ["../../master", "--help", "3.4.0;bad", ""])
def test_invalid_publication_does_not_clone(launcher, version):
    stack, _, launch = launcher
    # Empty curl result is distinct from an unset variable in this fake.
    if not version:
        version = " "
    result = launch(TEST_VERSION=version)
    assert result.returncode != 0
    assert not stack.exists()


def test_daemon_failure_has_no_filesystem_effect(launcher):
    stack, _, launch = launcher
    assert launch(TEST_DAEMON_RC="1").returncode != 0
    assert not stack.parent.exists()


def test_existing_directory_not_adopted(launcher):
    stack, _, launch = launcher
    stack.mkdir(parents=True)
    (stack / "keep").write_text("mine")
    assert launch().returncode != 0
    assert (stack / "keep").read_text() == "mine"


def test_foreign_container_refused_before_up(launcher):
    _, env, launch = launcher
    result = launch(TEST_OWNER="/another/checkout")
    assert result.returncode != 0
    assert "belongs to" in result.stderr
    assert "up -d" not in Path(env["TEST_DOCKER_LOG"]).read_text()


def test_pull_failure_stops_before_up(launcher):
    # Inside `up`, a failed pull surfaced only as "No such image" (seen in CI).
    _, env, launch = launcher
    result = launch(TEST_PULL_RC="1")
    assert result.returncode != 0
    assert "Could not pull the release images" in result.stderr
    assert "up -d" not in Path(env["TEST_DOCKER_LOG"]).read_text()


def test_health_failure_propagates_and_retry_works(launcher):
    _, _, launch = launcher
    assert launch(TEST_HEALTH_RC="1").returncode != 0
    assert launch().returncode == 0


def test_tracked_edits_refused(launcher):
    stack, _, launch = launcher
    assert launch().returncode == 0
    (stack / "docker-compose.yml").write_text("edited")
    result = launch()
    assert result.returncode != 0
    assert "tracked edits" in result.stderr


def test_lock_prevents_concurrent_launch(launcher):
    stack, _, launch = launcher
    stack.parent.mkdir(parents=True)
    lock = Path(str(stack) + ".launch-lock")
    lock.mkdir()
    assert launch().returncode != 0
    assert lock.exists()



@pytest.mark.parametrize("status, succeeds", [("healthy", True), ("moderate", True), ("critical", False)])
def test_container_health_gate(launcher, monkeypatch, status, succeeds):
    import io
    import json
    import runpy
    import time
    import urllib.request

    _, env, launch = launcher
    assert launch().returncode == 0
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.StringIO(json.dumps({"status": status, "version": "3.4.0"})))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    if succeeds:
        runpy.run_path(env["TEST_HEALTH_CODE"])
    else:
        with pytest.raises(SystemExit, match="critical"):
            runpy.run_path(env["TEST_HEALTH_CODE"])
