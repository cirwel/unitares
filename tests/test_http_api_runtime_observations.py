"""Contract tests for identity-bound runtime observations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_runtime_activity, http_runtime_observe
from src.runtime_observations import summarize_runtime_activity


AGENT_UUID = "86ae619f-87e0-4040-8f29-eacece0c7904"
SESSION_ID = "agent-86ae619f-87e"


class _FakeDB:
    def __init__(self, session):
        self.session = session
        self.touched: list[str] = []

    async def get_session(self, session_id):
        return self.session if session_id == SESSION_ID else None

    async def update_session_activity(self, session_id):
        self.touched.append(session_id)
        return True


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def recorder(monkeypatch):
    rows = []
    session = SimpleNamespace(
        agent_id=AGENT_UUID,
        is_active=True,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db = _FakeDB(session)

    import src.db as db_module
    import src.audit_db as audit_module

    monkeypatch.setattr(db_module, "get_db", lambda: db)

    async def append(entry, raw_hash=None):
        rows.append((entry, raw_hash))
        return True

    monkeypatch.setattr(audit_module, "append_audit_event_async", append)
    return rows, db


@pytest.fixture
def client():
    app = Starlette(
        routes=[
            Route("/v1/runtime/observe", http_runtime_observe, methods=["POST"]),
            Route("/v1/runtime/activity", http_runtime_activity, methods=["GET"]),
        ]
    )
    return TestClient(app, client=("127.0.0.1", 50000))


def _payload(kind="heartbeat"):
    return {
        "agent_uuid": AGENT_UUID,
        "client_session_id": SESSION_ID,
        "observation_kind": kind,
        "host_family": "codex",
        "slot_hash": "ab" * 16,
        "observed_at": "2026-08-02T09:00:00Z",
        "host_process_alive": True,
        "tool_count": 42,
        "tool_delta": 7,
        "window_seconds": 1800,
        "seconds_since_last_tool": 7200,
        "plugin_version": "0.4.12",
    }


def test_heartbeat_is_identity_bound_and_audit_only(client, recorder):
    rows, db = recorder
    response = client.post("/v1/runtime/observe", json=_payload())

    assert response.status_code == 201
    assert response.json()["eisv_written"] is False
    assert response.json()["epistemic_class"] == "substrate_observation"
    assert db.touched == [SESSION_ID]
    assert len(rows) == 1
    entry, raw_hash = rows[0]
    assert entry["agent_id"] == AGENT_UUID
    assert entry["session_id"] == SESSION_ID
    assert entry["event_type"] == "runtime_observation.heartbeat"
    assert entry["confidence"] == 0.0
    assert entry["details"]["measurement_scope"] == "host_process_liveness"
    assert entry["details"]["seconds_since_last_tool"] == 7200.0
    assert entry["details"]["agent_authored"] is False
    assert len(raw_hash) == 64


def test_activity_rollup_has_host_event_scope(client, recorder):
    rows, _db = recorder
    payload = _payload("activity_rollup")
    payload.pop("host_process_alive")
    response = client.post("/v1/runtime/observe", json=payload)

    assert response.status_code == 201
    assert rows[0][0]["event_type"] == "runtime_observation.activity_rollup"
    assert rows[0][0]["details"]["measurement_scope"] == "host_event_receipt"


def test_session_identity_mismatch_fails_closed(client, recorder):
    rows, db = recorder
    db.session.agent_id = "11111111-1111-4111-8111-111111111111"
    response = client.post("/v1/runtime/observe", json=_payload())

    assert response.status_code == 409
    assert response.json()["code"] == "identity_session_mismatch"
    assert rows == []


def test_inactive_session_and_false_heartbeat_are_rejected(client, recorder):
    rows, db = recorder
    db.session.is_active = False
    response = client.post("/v1/runtime/observe", json=_payload())
    assert response.status_code == 409
    assert response.json()["code"] == "session_inactive"

    db.session.is_active = True
    payload = _payload()
    payload["host_process_alive"] = False
    response = client.post("/v1/runtime/observe", json=payload)
    assert response.status_code == 400
    assert rows == []


def test_event_id_is_stable_for_retry(client, recorder):
    rows, _db = recorder
    first = client.post("/v1/runtime/observe", json=_payload()).json()["event_id"]
    retry = _payload()
    retry["tool_count"] = 43
    second = client.post("/v1/runtime/observe", json=retry).json()["event_id"]
    assert first == second
    assert rows[0][0]["event_id"] == rows[1][0]["event_id"]


def test_activity_summary_keeps_operational_and_reflective_provenance_separate():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {
            "agent_id": AGENT_UUID,
            "timestamp": "2026-08-02T11:50:00+00:00",
            "details": {
                "observation_kind": "activity_rollup",
                "host_family": "codex",
                "slot_hash": "ab" * 16,
                "observed_at": "2026-08-02T11:50:00+00:00",
                "tool_count": 42,
                "tool_delta": 7,
                "seconds_since_last_tool": 30,
            },
        },
        {
            "agent_id": AGENT_UUID,
            "timestamp": "2026-08-02T11:40:00+00:00",
            "details": {
                "observation_kind": "heartbeat",
                "host_family": "codex",
                "slot_hash": "ab" * 16,
                "observed_at": "2026-08-02T11:40:00+00:00",
                "host_process_alive": True,
                "tool_count": 35,
            },
        },
    ]
    reflections = [
        {
            "agent_id": AGENT_UUID,
            "label": "Codex runtime",
            "last_reflection_at": datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
            "reflection_count": 2,
            "last_interpretation_at": datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc),
            "last_unclassified_at": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
        }
    ]

    result = summarize_runtime_activity(events, reflections, now=now)

    assert result["summary"] == {
        "processes": 1,
        "agents": 1,
        "recent_processes": 1,
        "observations": 2,
        "processes_after_reflection": 1,
        "last_operational_at": "2026-08-02T11:50:00+00:00",
        "last_reflection_at": "2026-08-02T10:00:00+00:00",
    }
    process = result["processes"][0]
    assert process["agent_label"] == "Codex runtime"
    assert process["last_reflection_at"] == "2026-08-02T10:00:00+00:00"
    assert process["last_interpretation_at"] == "2026-08-02T11:00:00+00:00"
    assert process["reflection_count"] == 2
    assert process["tool_count"] == 42
    assert process["tools_in_window"] == 7
    assert process["host_process_alive"] is True
    assert process["operational_after_reflection"] is True


def test_runtime_activity_endpoint_bounds_query_and_returns_read_model(
    client, monkeypatch
):
    calls = []

    async def read_runtime_activity(*, window_hours, limit):
        calls.append((window_hours, limit))
        return {"success": True, "summary": {"processes": 0}, "processes": []}

    import src.runtime_observations as runtime_module

    monkeypatch.setattr(runtime_module, "read_runtime_activity", read_runtime_activity)
    response = client.get("/v1/runtime/activity?window_hours=9999&limit=99999")

    assert response.status_code == 200
    assert response.json()["summary"]["processes"] == 0
    assert calls == [(24 * 90, 5000)]


def test_runtime_activity_endpoint_rejects_invalid_query(client):
    response = client.get("/v1/runtime/activity?window_hours=forever")
    assert response.status_code == 400
    assert response.json()["success"] is False
