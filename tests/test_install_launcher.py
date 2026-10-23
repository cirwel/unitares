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
        "brew": '#!/bin/sh\nprintf "%s" "$TEST_BREW_PREFIX"\n',
        "curl": '#!/bin/sh\nprintf "%s" "${TEST_VERSION:-3.4.0}"\n',
        "docker": '''#!/bin/sh
printf '%s\n' "$*" >> "$TEST_DOCKER_LOG"
case "$*" in
  info) exit "${TEST_DAEMON_RC:-0}" ;;
  inspect*)
    [ "${TEST_UNLABELLED:-0}" = 1 ] && exit 0
    [ -n "${TEST_OWNER:-}" ] || exit 1
    printf '%s' "$TEST_OWNER"; exit 0 ;;
  "compose pull"*) exit "${TEST_PULL_RC:-0}" ;;
  "compose up"*) printf '%s %s' "${GOVERNANCE_HOST_PORT:-}" "${LEASE_PLANE_HOST_PORT:-}" > "$TEST_PORTS_LOG" ;;
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
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(tmp_path / 'home'),
               TEST_BREW_PREFIX=str(tmp_path / 'brew'), UNITARES_NATIVE_ENV='',
               UNITARES_STACK_DIR=str(stack), TEST_SOURCE=str(source),
               TEST_DOCKER_LOG=str(tmp_path / "docker.log"),
               TEST_PORTS_LOG=str(tmp_path / "ports.log"),
               TEST_HEALTH_CODE=str(tmp_path / "health.py"))
    def launch(*args, **overrides):
        return subprocess.run(["sh", str(SCRIPT), *args], env=env | overrides,
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
    stack, env, launch = launcher
    result = launch(TEST_OWNER="/another/checkout")
    assert result.returncode != 0
    assert "belongs to" in result.stderr
    assert "up -d" not in Path(env["TEST_DOCKER_LOG"]).read_text()
    assert not stack.parent.exists()


def test_unlabelled_container_is_not_adopted(launcher):
    stack, _, launch = launcher
    result = launch(TEST_UNLABELLED='1')
    assert result.returncode != 0
    assert 'unknown checkout' in result.stderr
    assert not stack.parent.exists()


def test_existing_homebrew_refused_before_docker_or_filesystem_changes(launcher):
    stack, env, launch = launcher
    native = Path(env['TEST_BREW_PREFIX']) / 'opt/unitares'
    native.mkdir(parents=True)
    config = Path(env['TEST_BREW_PREFIX']) / 'etc/unitares/native.env'
    config.parent.mkdir(parents=True)
    config.write_text('native configuration')
    result = launch(TEST_DAEMON_RC='1')
    assert result.returncode != 0
    assert str(native) in result.stderr
    assert '--separate' in result.stderr
    assert not stack.parent.exists()
    assert not Path(env['TEST_DOCKER_LOG']).exists()


def test_homebrew_cli_only_does_not_block_docker(launcher):
    stack, env, launch = launcher
    (Path(env['TEST_BREW_PREFIX']) / 'opt/unitares').mkdir(parents=True)
    result = launch()
    assert result.returncode == 0, result.stderr
    assert 'Package presence alone' in result.stderr
    assert (stack / '.env').exists()


def test_existing_managed_stack_survives_later_native_configuration(launcher):
    stack, env, launch = launcher
    assert launch().returncode == 0
    config = 'GOVERNANCE_HOST_PORT=8767\nLEASE_PLANE_HOST_PORT=8788\n'
    (stack / '.env').write_text(config)
    native = Path(env['TEST_BREW_PREFIX']) / 'etc/unitares/native.env'
    native.parent.mkdir(parents=True)
    native.write_text('native configuration')
    result = launch(TEST_OWNER=str(stack))
    assert result.returncode == 0, result.stderr
    assert 'reusing the existing managed Docker checkout' in result.stderr
    assert (stack / '.env').read_text() == config
    # The reuse exception does not bypass checkout validation.
    (stack / 'docker-compose.yml').write_text('edited')
    rejected = launch(TEST_OWNER=str(stack))
    assert rejected.returncode != 0
    assert 'tracked edits' in rejected.stderr


def test_native_config_detected_without_reading_or_executing_it(launcher, tmp_path):
    stack, env, launch = launcher
    config = tmp_path / 'native.env'
    marker = tmp_path / 'must-not-exist'
    content = f'DB_POSTGRES_URL=secret-value\ntouch "{marker}"\n'
    config.write_text(content)
    result = launch(UNITARES_NATIVE_ENV=str(config))
    assert result.returncode != 0
    assert str(config) in result.stderr
    assert 'secret-value' not in result.stderr + result.stdout
    assert not marker.exists()
    assert config.read_text() == content
    assert not stack.parent.exists()


def test_native_launchagent_is_detected(launcher):
    stack, env, launch = launcher
    plist = Path(env['HOME']) / 'Library/LaunchAgents/com.unitares.governance-mcp.plist'
    plist.parent.mkdir(parents=True)
    plist.write_text('fixture')
    result = launch()
    assert result.returncode != 0
    assert str(plist) in result.stderr
    assert not stack.parent.exists()


@pytest.mark.parametrize('server,lease', [('', ''), ('18767', ''), ('', '18788'),
    ('8767', '18788'), ('8788', '18788'), ('18767', '8767'), ('18767', '8788'),
    ('18767', '18767'), ('0', '18788'), ('65536', '18788'), ('1;bad', '18788'),
    ('018767', '18788'), ('9' * 100, '18788')])
def test_separate_install_requires_two_valid_alternate_ports(launcher, server, lease):
    stack, env, launch = launcher
    result = launch('--separate', GOVERNANCE_HOST_PORT=server, LEASE_PLANE_HOST_PORT=lease)
    assert result.returncode != 0
    assert not stack.parent.exists()
    assert not Path(env['TEST_DOCKER_LOG']).exists()


def test_explicit_separate_install_preserves_native_install(launcher):
    stack, env, launch = launcher
    native = Path(env['TEST_BREW_PREFIX']) / 'opt/unitares'
    native.mkdir(parents=True)
    marker = native / 'keep'
    marker.write_text('native data')
    for _ in range(2):
        result = launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788')
        assert result.returncode == 0, result.stderr
        assert 'Installation: Docker Compose' in result.stdout
        assert 'server port 18767, lease-plane port 18788' in result.stdout
    assert Path(env['TEST_PORTS_LOG']).read_text() == '18767 18788'
    assert marker.read_text() == 'native data'
    config = (stack / '.env').read_text()
    assert 'GOVERNANCE_HOST_PORT=18767\n' in config
    assert 'LEASE_PLANE_HOST_PORT=18788\n' in config
    changed = launch('--separate', GOVERNANCE_HOST_PORT='28767', LEASE_PLANE_HOST_PORT='28788')
    assert changed.returncode != 0
    assert 'existing configuration was not changed' in changed.stderr
    assert (stack / '.env').read_text() == config


def test_separate_rejects_later_conflicting_dotenv_assignment(launcher):
    stack, _, launch = launcher
    assert launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788').returncode == 0
    with (stack / '.env').open('a') as stream:
        stream.write('export GOVERNANCE_HOST_PORT = 8767\n')
    result = launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788')
    assert result.returncode != 0
    assert 'Set the literal GOVERNANCE_HOST_PORT=18767' in result.stderr


@pytest.mark.parametrize('quote', ["'", '"'])
def test_separate_accepts_quoted_numeric_ports_without_rewriting(launcher, quote):
    stack, _, launch = launcher
    assert launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788').returncode == 0
    config = f'GOVERNANCE_HOST_PORT={quote}18767{quote}\nLEASE_PLANE_HOST_PORT={quote}18788{quote}\n'
    (stack / '.env').write_text(config)
    result = launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788')
    assert result.returncode == 0, result.stderr
    assert (stack / '.env').read_text() == config


@pytest.mark.parametrize('value', ['18767 # note', '${OTHER_PORT}', '"${OTHER_PORT}"',
                                  ' 18767', '18767 ', '"18767" ', '18767\r'])
def test_separate_refuses_port_forms_the_cli_cannot_resolve(launcher, value):
    stack, _, launch = launcher
    assert launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788').returncode == 0
    config = f'GOVERNANCE_HOST_PORT={value}\nLEASE_PLANE_HOST_PORT=18788\n'
    (stack / '.env').write_text(config)
    result = launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788')
    assert result.returncode != 0
    assert (stack / '.env').read_bytes() == config.encode()


@pytest.mark.parametrize('assignment', [
    'export GOVERNANCE_HOST_PORT=18767',
    'GOVERNANCE_HOST_PORT = 18767',
    'GOVERNANCE_HOST_PORT=8767\nexport GOVERNANCE_HOST_PORT=18767',
    'export GOVERNANCE_HOST_PORT=8767\nGOVERNANCE_HOST_PORT=18767',
    'GOVERNANCE_HOST_PORT=18767\nGOVERNANCE_HOST_PORT',
    'GOVERNANCE_HOST_PORT=18767\nexport GOVERNANCE_HOST_PORT # unset',
])
def test_separate_refuses_assignments_the_cli_skips(launcher, assignment):
    stack, _, launch = launcher
    assert launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788').returncode == 0
    config = assignment + '\nLEASE_PLANE_HOST_PORT=18788\n'
    (stack / '.env').write_text(config)
    result = launch('--separate', GOVERNANCE_HOST_PORT='18767', LEASE_PLANE_HOST_PORT='18788')
    assert result.returncode != 0
    assert (stack / '.env').read_text() == config


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
