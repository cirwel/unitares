"""The bundle must fail closed and preserve the MCP stdout boundary."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def supervisor():
    path = Path(__file__).resolve().parents[1] / 'docker/glama/supervise.py'
    spec = importlib.util.spec_from_file_location('glama_supervisor', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_child_death_is_fatal(supervisor):
    supervisor.CHILDREN = [('redis', Mock(poll=Mock(return_value=7), returncode=7))]
    with pytest.raises(RuntimeError, match=r'redis exited \(7\)'):
        supervisor.check_children()


def test_shutdown_interrupts_readiness(supervisor):
    supervisor.STOP = True
    probe = Mock()
    with pytest.raises(InterruptedError):
        supervisor.wait_ready('postgres', probe, 5)
    probe.assert_not_called()


def test_readiness_timeout_is_named(supervisor):
    with pytest.raises(RuntimeError, match='postgres readiness timed out'):
        supervisor.wait_ready('postgres', lambda: False, 0)


def test_services_cannot_write_mcp_stdout(supervisor, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(supervisor.subprocess, 'Popen', popen)
    supervisor.start('postgres', ['postgres'])
    assert popen.call_args.kwargs['stdout'] is supervisor.sys.stderr
    assert popen.call_args.kwargs['stdin'] is supervisor.subprocess.DEVNULL
    supervisor.start('mcp', ['python'], mcp=True)
    assert popen.call_args.kwargs['stdout'] is None
    assert popen.call_args.kwargs['stdin'] is None


def test_shutdown_reverses_order_and_checkpoints_postgres(supervisor, monkeypatch):
    postgres = Mock(pid=10, poll=Mock(return_value=None))
    mcp = Mock(pid=20, poll=Mock(return_value=None))
    supervisor.CHILDREN = [('postgres', postgres), ('mcp', mcp)]
    kill = Mock()
    monkeypatch.setattr(supervisor.os, 'killpg', kill)
    supervisor.shutdown()
    assert [c.args for c in kill.call_args_list] == [(20,supervisor.signal.SIGTERM),(10,supervisor.signal.SIGINT)]


def test_schema_mismatch_refuses_start_before_any_child(supervisor, monkeypatch, tmp_path):
    pg = tmp_path/'postgres'; pg.mkdir()
    (pg/'PG_VERSION').write_text('18')
    marker = tmp_path/'schema.sha256'; marker.write_text('old')
    monkeypatch.setattr(supervisor, 'PG', pg)
    monkeypatch.setattr(supervisor, 'MARKER', marker)
    monkeypatch.setattr(supervisor, 'configure', lambda: (None, {}))
    original = supervisor.Path.read_text
    monkeypatch.setattr(supervisor.Path, 'read_text', lambda p: 'new' if str(p)=='/opt/unitares-schema.sha256' else original(p))
    start = Mock(); monkeypatch.setattr(supervisor, 'start', start)
    with pytest.raises(RuntimeError, match='schema fingerprint mismatch'):
        supervisor.main()
    start.assert_not_called()
