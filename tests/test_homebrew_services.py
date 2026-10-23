"""Native service controls preserve the installation and stop only their own writers."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.install import homebrew_services as native

ROOT = Path(__file__).resolve().parents[1]
CONFIG = '''DB_POSTGRES_URL=postgresql://probe:private@localhost:5432/governance
UNITARES_LEASE_PLANE_DATABASE_URL=postgresql://probe:private@localhost:5432/governance
REDIS_URL=redis://127.0.0.1:6379/0
LEASE_PLANE_BEARER_TOKEN=fixture-only-token
UNITARES_LEASE_PLANE_HTTP_IP=127.0.0.1
UNITARES_LEASE_PLANE_HTTP_PORT=8788
UNITARES_LEASE_PLANE_URL=http://127.0.0.1:8788
LEASE_PLANE_BASE_URL=http://127.0.0.1:8788
UNITARES_GOVERNANCE_URL=http://127.0.0.1:8767
UNITARES_MODEL_ID=
'''


@pytest.fixture
def manager(tmp_path, monkeypatch):
    brew = native.Homebrew.__new__(native.Homebrew)
    brew.prefix = tmp_path / 'brew'
    brew.app = brew.prefix / 'opt/unitares/libexec/app'
    brew.config = brew.prefix / 'etc/unitares/native.env'
    brew.config.parent.mkdir(parents=True)
    brew.config.write_text(CONFIG)
    brew.config.chmod(0o600)
    brew.env = {'PATH': '/usr/bin:/bin'}
    brew.brew = 'brew'
    for name, binary in (('unitares', 'unitares-server'), ('unitares-lease-plane', 'unitares-lease-plane'), ('postgresql@17', 'psql'), ('redis', 'redis-server')):
        path = brew.prefix / 'opt' / name / 'bin' / binary
        path.parent.mkdir(parents=True)
        path.write_text('#!/bin/sh\n')
        path.chmod(0o755)
    for key in ('UNITARES_NATIVE_ENV', 'HOMEBREW_USER_CONFIG_HOME'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    records = {}
    for name in native.SERVICES:
        binary = 'unitares-server' if name == 'unitares' else name
        records[name] = {'name': name, 'running': False, 'loaded': False, 'registered': False,
                         'pid': None, 'command': str(brew.prefix / 'opt' / name / 'bin' / binary)}
    monkeypatch.setattr(brew, 'info', lambda: records)
    calls = []
    def call(*args):
        calls.append(args)
        if args[0] == 'services':
            records[args[2]]['running'] = args[1] == 'start'
        return ''
    monkeypatch.setattr(brew, 'call', call)
    monkeypatch.setattr(brew, 'check_ports', lambda records: None)
    monkeypatch.setattr(brew, 'wait_port', lambda name, timeout: calls.append(('ready', name)))
    monkeypatch.setattr(brew, 'check_schema', lambda: calls.append(('schema',)))
    monkeypatch.setattr(native.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=lambda *a, **k: io.StringIO('{"status":"healthy"}')))
    return brew, records, calls


@pytest.mark.parametrize('value', ['$(touch /never-run)', '`id`', 'foo; id', 'foo & id', '"$HOME"', 'foo\\bar'])
def test_config_refuses_shell_execution_without_echoing_value(tmp_path, value):
    path = tmp_path / 'native.env'
    path.write_text('SECRET=' + value + '\n')
    path.chmod(0o600)
    with pytest.raises(native.Refusal) as error:
        native.read_config(path)
    assert value not in str(error.value)


def test_config_accepts_literal_quoted_values_and_empty_fields(tmp_path):
    path = tmp_path / 'native.env'
    path.write_text('export DB="postgresql://user:p%26w@localhost:5432/db" # note\nTOKEN=\'a b$literal\'\nEMPTY=\n')
    path.chmod(0o600)
    assert native.read_config(path) == {'DB': 'postgresql://user:p%26w@localhost:5432/db', 'TOKEN': 'a b$literal', 'EMPTY': ''}


@pytest.mark.parametrize('mode', [0o644, 0o660])
def test_config_requires_private_permissions(manager, mode):
    brew, _, _ = manager
    brew.config.chmod(mode)
    with pytest.raises(native.Refusal, match='mode 600'):
        brew.configure()


def test_config_refuses_symlink(manager):
    brew, _, _ = manager
    target = brew.config.with_suffix('.saved')
    brew.config.rename(target)
    brew.config.symlink_to(target)
    with pytest.raises(native.Refusal, match='regular file'):
        brew.configure()


@pytest.mark.parametrize('old,new', [
    ('probe:private@localhost', 'probe:private@remote.example'),
    ('probe:private@localhost', 'probe@localhost'),
    ('http://127.0.0.1:8767', 'http://127.0.0.1:18767'),
    ('HTTP_PORT=8788', 'HTTP_PORT=8767'),
    ('HTTP_IP=127.0.0.1', 'HTTP_IP=0.0.0.0'),
    ('fixture-only-token', 'replace-before-starting'),
])
def test_incompatible_native_settings_fail_before_services(manager, old, new):
    brew, _, calls = manager
    brew.config.write_text(CONFIG.replace(old, new))
    with pytest.raises(native.Refusal):
        brew.start()
    assert calls == []


def test_shell_override_cannot_select_different_config_than_launchd(manager, monkeypatch):
    brew, _, calls = manager
    monkeypatch.setenv('UNITARES_NATIVE_ENV', '/another/native.env')
    with pytest.raises(native.Refusal, match='override'):
        brew.start()
    assert calls == []


def test_service_environment_override_requires_its_own_procedure(manager, monkeypatch, tmp_path):
    brew, _, calls = manager
    home = tmp_path / 'config'
    (home / 'services').mkdir(parents=True)
    (home / 'services/unitares.env').write_text('UNITARES_NATIVE_ENV=/another/native.env')
    monkeypatch.setenv('HOMEBREW_USER_CONFIG_HOME', str(home))
    with pytest.raises(native.Refusal, match='Custom Homebrew environment'):
        brew.start()
    assert calls == []


def test_start_orders_dependencies_schema_then_writers(manager, capsys):
    brew, _, calls = manager
    brew.start()
    assert calls == [
        ('services', 'start', 'postgresql@17'), ('ready', 'postgresql@17'),
        ('services', 'start', 'redis'), ('ready', 'redis'), ('schema',),
        ('services', 'start', 'unitares-lease-plane'), ('ready', 'unitares-lease-plane'),
        ('services', 'start', 'unitares'), ('ready', 'unitares'),
    ]
    assert 'Deep health: healthy' in capsys.readouterr().out


def test_start_plan_is_read_only(manager):
    brew, _, calls = manager
    before = brew.config.read_bytes()
    brew.start(check=True)
    assert calls == []
    assert brew.config.read_bytes() == before


def test_start_reuses_running_services_without_restart(manager):
    brew, records, calls = manager
    for record in records.values():
        record['running'] = True
    brew.start()
    assert not any(call[0] == 'services' for call in calls)
    assert ('schema',) in calls


def test_schema_failure_never_starts_writers(manager, monkeypatch):
    brew, _, calls = manager
    def refuse():
        raise native.Refusal('schema is pending')
    monkeypatch.setattr(brew, 'check_schema', refuse)
    with pytest.raises(native.Refusal, match='pending'):
        brew.start()
    assert [call for call in calls if call[0] == 'services'] == [
        ('services', 'start', 'postgresql@17'), ('services', 'start', 'redis')]


def test_partial_start_failure_cleans_up_only_new_writers(manager, monkeypatch):
    brew, records, calls = manager
    records['postgresql@17']['running'] = True
    records['redis']['running'] = True
    records['unitares-lease-plane']['running'] = True
    original = brew.call
    def call(*args):
        if args == ('services', 'start', 'unitares'):
            calls.append(args)
            raise native.Refusal('start failed')
        return original(*args)
    monkeypatch.setattr(brew, 'call', call)
    with pytest.raises(native.Refusal, match='start failed'):
        brew.start()
    assert ('services', 'stop', 'unitares') in calls
    assert ('services', 'stop', 'unitares-lease-plane') not in calls
    assert records['unitares-lease-plane']['running']


def test_stop_leaves_shared_dependencies_running(manager):
    brew, records, calls = manager
    for record in records.values():
        record['running'] = True
    brew.config.unlink()  # A broken config must not make stop impossible.
    brew.stop()
    assert calls == [('services', 'stop', 'unitares'), ('services', 'stop', 'unitares-lease-plane')]
    assert records['postgresql@17']['running'] and records['redis']['running']


def test_stop_attempts_both_writers_even_when_first_fails(manager, monkeypatch):
    brew, records, calls = manager
    for name in native.WRITERS:
        records[name]['running'] = True
    original = brew.call
    def call(*args):
        if args == ('services', 'stop', 'unitares'):
            calls.append(args)
            raise native.Refusal('stop failed')
        return original(*args)
    monkeypatch.setattr(brew, 'call', call)
    with pytest.raises(native.Refusal, match='stop failed'):
        brew.stop()
    assert calls == [('services', 'stop', 'unitares'), ('services', 'stop', 'unitares-lease-plane')]


def test_foreign_listener_is_refused_before_start(manager, monkeypatch):
    brew, records, calls = manager
    monkeypatch.setattr(brew, 'check_ports', native.Homebrew.check_ports.__get__(brew))
    monkeypatch.setattr(native.socket, 'create_connection', lambda *a, **k: io.StringIO())
    monkeypatch.setattr(native, 'run', lambda *a, **k: SimpleNamespace(stdout='999\n'))
    records['postgresql@17'].update(running=True, pid=123)
    with pytest.raises(native.Refusal, match='occupied outside'):
        brew.start()
    assert calls == []


def test_schema_check_is_read_only_and_uses_installed_runtime(manager, monkeypatch):
    brew, _, _ = manager
    brew.configure()
    python = brew.prefix / 'opt/unitares/libexec/venv/bin/python'
    script = brew.app / 'scripts/dev/apply_migrations.py'
    for path in (python, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    script.write_text('import os, sys\nassert sys.argv[1:]==["--check","--db-url",os.environ["DB_POSTGRES_URL"]]\n')
    calls = []
    def execute(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.run([sys.executable, *args[1:]], env=kwargs['env'], capture_output=True)
    monkeypatch.setattr(native, 'run', execute)
    native.Homebrew.check_schema(brew)
    args, options = calls[0]
    assert args[0] == str(python) and args[1] == '-c' and args[-1] == str(script)
    assert 'postgresql://' not in ' '.join(args)
    assert options['env']['DB_POSTGRES_URL'] == 'postgresql://probe:private@localhost:5432/governance'


@pytest.mark.parametrize('packaged,explicit', [(True, False), (False, True)])
@pytest.mark.parametrize('action', ['start', 'stop', 'logs'])
def test_cli_selects_homebrew_package_or_explicit_option(tmp_path, packaged, explicit, action):
    root = tmp_path / ('keg/libexec/app' if packaged else 'source')
    (root / 'scripts/install').mkdir(parents=True)
    cli = root / 'scripts/unitares'
    shutil.copy2(ROOT / 'scripts/unitares', cli)
    (root / 'scripts/install/homebrew_services.py').write_text('import json,sys; print(json.dumps(sys.argv[1:]))\n')
    if packaged:
        wrapper = tmp_path / 'keg/bin/unitares-server'
        wrapper.parent.mkdir()
        wrapper.write_text('#!/bin/sh\n')
        wrapper.chmod(0o755)
    args = [str(cli), action] + (['--homebrew'] if explicit else []) + ['--check']
    result = subprocess.run(args, capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [action, '--check']
