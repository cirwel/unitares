"""Tests for the dev file-lease helper."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "dev" / "file_lease.py"


@pytest.fixture(scope="module")
def file_lease():
    name = "file_lease"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, module, outcomes=None):
        self.module = module
        self.outcomes = list(outcomes or [])
        self.acquire_requests = []
        self.release_requests = []
        self.heartbeat_requests = []

    def acquire(self, request):
        self.acquire_requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "held":
            return self.module.AcquireHeldByOther.model_validate(
                {
                    "ok": False,
                    "error": "held_by_other",
                    "surface_id": request.surface_id,
                    "blocking_lease_id": "11111111-1111-1111-1111-111111111111",
                    "held_by_uuid": str(uuid4()),
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                    "retry_after_hint_ms": 500,
                }
            )
        return self.module.AcquireOk.model_validate(
            {
                "ok": True,
                "idempotent": False,
                "drift_warning": [],
                "lease": {
                    "lease_id": str(uuid4()),
                    "surface_id": request.surface_id,
                    "surface_kind": "file",
                    "holder_agent_uuid": str(request.holder_agent_uuid),
                    "holder_class": request.holder_class,
                    "holder_kind": request.holder_kind,
                    "heartbeat_required": True,
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=request.ttl_s)).isoformat(),
                    "original_ttl_s": request.ttl_s,
                    "earned_status": "provisional",
                },
            }
        )

    def release(self, request):
        self.release_requests.append(request)
        return self.module.SimpleOk(ok=True)

    def heartbeat(self, request):
        self.heartbeat_requests.append(request)
        return self.module.SimpleOk(ok=True)

    def status(self, _surface_id):
        return self.module.StatusOk(ok=True, lease=None)


def test_path_to_surface_id_uses_absolute_workspace_path(file_lease, tmp_path):
    surface_id = file_lease.path_to_surface_id("src/foo.py", cwd=tmp_path)
    assert surface_id == f"file://{tmp_path / 'src' / 'foo.py'}"


def test_acquire_paths_claims_each_path_with_one_holder(file_lease, tmp_path):
    holder = UUID("22222222-2222-2222-2222-222222222222")
    client = FakeClient(file_lease)

    attempts = file_lease.acquire_paths(
        ["a.py", "b.py"],
        client=client,
        holder_uuid=holder,
        ttl_s=120,
        intent="test edit",
        audit_session="test-session",
        cwd=tmp_path,
    )

    assert [attempt.outcome for attempt in attempts] == ["acquired_new", "acquired_new"]
    assert all(attempt.acquired for attempt in attempts)
    assert len(client.acquire_requests) == 2
    assert {req.holder_agent_uuid for req in client.acquire_requests} == {holder}
    assert {req.audit_session for req in client.acquire_requests} == {"test-session"}


def test_guard_blocks_on_contended_path_and_releases_acquired(file_lease, monkeypatch, tmp_path):
    client = FakeClient(file_lease, outcomes=["ok", "held"])
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)
    ran = False

    def fake_run(_command, check=False):
        nonlocal ran
        ran = True
        raise AssertionError("guard must not run the command when enforced lease acquisition is blocked")

    monkeypatch.setattr(file_lease.subprocess, "run", fake_run)

    rc = file_lease.main(
        [
            "guard",
            "--path",
            str(tmp_path / "a.py"),
            "--path",
            str(tmp_path / "b.py"),
            "--",
            "echo",
            "mutate",
        ]
    )

    assert rc == 1
    assert ran is False
    assert len(client.release_requests) == 1


def test_guard_runs_command_and_releases_on_success(file_lease, monkeypatch, tmp_path):
    client = FakeClient(file_lease)
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)

    class Completed:
        returncode = 7

    monkeypatch.setattr(file_lease.subprocess, "run", lambda command, check=False: Completed())

    rc = file_lease.main(
        [
            "guard",
            "--path",
            str(tmp_path / "a.py"),
            "--",
            "pytest",
            "tests/test_a.py",
        ]
    )

    assert rc == 7
    assert len(client.acquire_requests) == 1
    assert len(client.release_requests) == 1


def test_guard_changed_uses_git_changed_paths(file_lease, monkeypatch):
    client = FakeClient(file_lease)
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)
    monkeypatch.setattr(file_lease, "changed_paths", lambda include_untracked=True: ["scripts/dev/file_lease.py"])

    class Completed:
        returncode = 0

    monkeypatch.setattr(file_lease.subprocess, "run", lambda command, check=False: Completed())

    rc = file_lease.main(["guard", "--changed", "--", "true"])

    assert rc == 0
    assert len(client.acquire_requests) == 1
    assert client.acquire_requests[0].surface_id.endswith("/scripts/dev/file_lease.py")
    assert len(client.release_requests) == 1


def test_guard_changed_empty_runs_command_without_leases(file_lease, monkeypatch):
    client = FakeClient(file_lease)
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)
    monkeypatch.setattr(file_lease, "changed_paths", lambda include_untracked=True: [])

    class Completed:
        returncode = 0

    monkeypatch.setattr(file_lease.subprocess, "run", lambda command, check=False: Completed())

    rc = file_lease.main(["guard", "--changed", "--", "true"])

    assert rc == 0
    assert client.acquire_requests == []
    assert client.heartbeat_requests == []
    assert client.release_requests == []


def test_changed_paths_dedupes_git_outputs(file_lease, monkeypatch, tmp_path):
    outputs = {
        ("git", "diff", "--name-only"): "a.py\nb.py\n",
        ("git", "diff", "--cached", "--name-only"): "b.py\nc.py\n",
        ("git", "ls-files", "--others", "--exclude-standard"): "c.py\nd.py\n",
    }

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(args, cwd, check, stdout, stderr, text):
        return Result(outputs[tuple(args)])

    monkeypatch.setattr(file_lease.subprocess, "run", fake_run)

    assert file_lease.changed_paths(cwd=tmp_path) == ["a.py", "b.py", "c.py", "d.py"]


def test_hold_once_heartbeats_and_releases(file_lease, monkeypatch, tmp_path):
    client = FakeClient(file_lease)
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)
    monkeypatch.setattr(file_lease.time, "sleep", lambda _seconds: None)

    rc = file_lease.main(
        [
            "hold",
            "--path",
            str(tmp_path / "a.py"),
            "--heartbeat-interval-s",
            "0.01",
            "--once",
        ]
    )

    assert rc == 0
    assert len(client.acquire_requests) == 1
    assert len(client.heartbeat_requests) == 1
    assert len(client.release_requests) == 1
    assert client.heartbeat_requests[0].lease_id == client.release_requests[0].lease_id


def test_hold_blocks_on_contention_and_releases_partial_acquire(file_lease, monkeypatch, tmp_path):
    client = FakeClient(file_lease, outcomes=["ok", "held"])
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)

    rc = file_lease.main(
        [
            "hold",
            "--path",
            str(tmp_path / "a.py"),
            "--path",
            str(tmp_path / "b.py"),
            "--once",
        ]
    )

    assert rc == 1
    assert len(client.acquire_requests) == 2
    assert client.heartbeat_requests == []
    assert len(client.release_requests) == 1


def test_hold_changed_empty_once_is_quiet_noop(file_lease, monkeypatch):
    client = FakeClient(file_lease)
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)
    monkeypatch.setattr(file_lease, "changed_paths", lambda include_untracked=True: [])
    monkeypatch.setattr(file_lease.time, "sleep", lambda _seconds: None)

    rc = file_lease.main(["hold", "--changed", "--once"])

    assert rc == 0
    assert client.acquire_requests == []
    assert client.heartbeat_requests == []
    assert client.release_requests == []


def test_hold_changed_refresh_acquires_new_paths(file_lease, monkeypatch):
    client = FakeClient(file_lease)
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)
    monkeypatch.setattr(file_lease.time, "sleep", lambda _seconds: None)
    path_sets = iter([
        ["scripts/dev/file_lease.py"],
        ["scripts/dev/file_lease.py", "tests/test_file_lease_cli.py"],
    ])
    monkeypatch.setattr(file_lease, "changed_paths", lambda include_untracked=True: next(path_sets))

    rc = file_lease.main(["hold", "--changed", "--once"])

    assert rc == 0
    assert len(client.acquire_requests) == 2
    assert client.acquire_requests[0].surface_id.endswith("/scripts/dev/file_lease.py")
    assert client.acquire_requests[1].surface_id.endswith("/tests/test_file_lease_cli.py")
    assert len(client.heartbeat_requests) == 2
    assert len(client.release_requests) == 2


def test_hold_changed_refresh_blocks_on_new_contention(file_lease, monkeypatch):
    client = FakeClient(file_lease, outcomes=["ok", "held"])
    monkeypatch.setattr(file_lease, "make_client", lambda **_kwargs: client)
    monkeypatch.setattr(file_lease.time, "sleep", lambda _seconds: None)
    path_sets = iter([
        ["scripts/dev/file_lease.py"],
        ["scripts/dev/file_lease.py", "tests/test_file_lease_cli.py"],
    ])
    monkeypatch.setattr(file_lease, "changed_paths", lambda include_untracked=True: next(path_sets))

    rc = file_lease.main(["hold", "--changed", "--once"])

    assert rc == 1
    assert len(client.acquire_requests) == 2
    assert client.heartbeat_requests == []
    assert len(client.release_requests) == 1
