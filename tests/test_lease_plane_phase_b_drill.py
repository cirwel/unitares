"""Tests for the controlled Phase B lease-plane drill runner."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "lease_plane" / "run_phase_b_drill.py"


@pytest.fixture(scope="module")
def drill():
    name = "run_phase_b_drill"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, drill):
        self.drill = drill
        self.acquire_requests = []
        self.release_requests = []

    def acquire(self, request):
        self.acquire_requests.append(request)
        if "blocker" in (request.intent or ""):
            return self.drill.AcquireOk.model_validate(
                {
                    "ok": True,
                    "lease": {
                        "lease_id": "11111111-1111-1111-1111-111111111111",
                        "surface_id": request.surface_id,
                        "surface_kind": request.surface_id.split(":", 1)[0],
                        "holder_agent_uuid": str(request.holder_agent_uuid),
                        "holder_class": request.holder_class,
                        "holder_kind": request.holder_kind,
                        "heartbeat_required": True,
                        "expires_at": datetime.now(timezone.utc).isoformat(),
                        "original_ttl_s": request.ttl_s,
                        "audit_session": request.audit_session,
                    },
                }
            )
        return self.drill.AcquireHeldByOther.model_validate(
            {
                "ok": False,
                "error": "held_by_other",
                "surface_id": request.surface_id,
                "blocking_lease_id": "11111111-1111-1111-1111-111111111111",
                "held_by_uuid": str(self.acquire_requests[-2].holder_agent_uuid),
                "expires_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def release(self, request):
        self.release_requests.append(request)
        return self.drill.SimpleOk(ok=True)


def test_run_drill_creates_labeled_conflicts_and_releases_blockers(drill):
    client = FakeClient(drill)

    report = drill.run_drill(
        "resident",
        count=3,
        run_id="run-1",
        audit_session="phase-b-drill:resident:run-1",
        client=client,
    )

    assert report.ok is True
    assert len(report.attempts) == 3
    assert all(a.challenger_outcome == "held_by_other" for a in report.attempts)
    assert all(a.release_ok for a in report.attempts)
    assert len(client.acquire_requests) == 6
    assert len(client.release_requests) == 3
    assert {r.audit_session for r in client.acquire_requests} == {"phase-b-drill:resident:run-1"}
    assert all(a.surface_id.startswith("resident:/phase_b_drill/run-1/") for a in report.attempts)


def test_run_drill_rejects_unknown_surface_kind(drill):
    with pytest.raises(drill.DrillError):
        drill.run_drill("bogus", client=FakeClient(drill))


def test_release_returns_false_on_exception(drill):
    class BrokenReleaseClient:
        def release(self, _request):
            raise RuntimeError("boom")

    assert drill.release(BrokenReleaseClient(), UUID("11111111-1111-1111-1111-111111111111")) is False
