"""Contract tests for identity-bound host observations."""

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
    assert response.json()["session_activity_refreshed"] is False
    assert response.json()["agent_runtime_evidence"] is False
    assert db.touched == []
    assert len(rows) == 1
    entry, raw_hash = rows[0]
    assert entry["agent_id"] == AGENT_UUID
    assert entry["session_id"] == SESSION_ID
    assert entry["event_type"] == "runtime_observation.heartbeat"
    assert entry["confidence"] == 0.0
    assert entry["details"]["measurement_scope"] == "hook_parent_process_liveness"
    assert entry["details"]["host_process_scope"] == "hook_parent"
    assert entry["details"]["session_activity_evidence"] is False
    assert entry["details"]["agent_runtime_evidence"] is False
    assert entry["details"]["seconds_since_last_tool"] == 7200.0
    assert entry["details"]["agent_authored"] is False
    assert len(raw_hash) == 64


def test_activity_rollup_has_completed_tool_scope(client, recorder):
    rows, db = recorder
    payload = _payload("activity_rollup")
    payload.pop("host_process_alive")
    response = client.post("/v1/runtime/observe", json=payload)

    assert response.status_code == 201
    assert response.json()["session_activity_refreshed"] is True
    assert db.touched == [SESSION_ID]
    assert rows[0][0]["event_type"] == "runtime_observation.activity_rollup"
    assert rows[0][0]["details"]["measurement_scope"] == "completed_tool_event_receipts"
    assert rows[0][0]["details"]["session_activity_evidence"] is True
    assert rows[0][0]["details"]["agent_runtime_evidence"] is False


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
            "event_id": "8f4bb851-dfed-4e12-b5b9-33820df47274",
            "timestamp": "2026-08-02T11:50:00+00:00",
            "details": {
                "observation_kind": "activity_rollup",
                "host_family": "codex",
                "execution_mode": "automation",
                "execution_mode_source": "explicit_env",
                "model": "gpt-5.4",
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
            "interpretation_count": 1,
            "last_bootstrap_at": datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc),
            "bootstrap_count": 1,
            "last_unclassified_at": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
            "last_reflection_state": {
                "action": "proceed",
                "provenance_context": {
                    "task_label": "weekly release notes",
                    "task_outcome": "drafted",
                    "tool_surface": ["github", "git"],
                    "private_unbounded_field": "not exported",
                },
            },
        }
    ]

    result = summarize_runtime_activity(events, reflections, now=now)

    assert result["summary"] == {
        "processes": 1,
        "observed_slots": 1,
        "agents": 1,
        "recent_processes": 1,
        "recent_tool_activity_slots": 1,
        "recent_host_heartbeat_slots": 1,
        "observations": 2,
        "processes_after_reflection": 1,
        "host_observations_after_agent_report": 1,
        "slots_without_agent_report": 0,
        "last_operational_at": "2026-08-02T11:50:00+00:00",
        "last_host_observation_at": "2026-08-02T11:50:00+00:00",
        "last_reflection_at": "2026-08-02T10:00:00+00:00",
        "last_agent_report_at": "2026-08-02T10:00:00+00:00",
        "execution_modes": {"automation": 1},
    }
    process = result["processes"][0]
    assert process["agent_label"] == "Codex runtime"
    assert process["last_reflection_at"] == "2026-08-02T10:00:00+00:00"
    assert process["last_interpretation_at"] == "2026-08-02T11:00:00+00:00"
    assert process["substrate_interpretation_count"] == 1
    assert process["bootstrap_count"] == 1
    assert process["state_update_profile"] == "agent_report_present"
    assert process["reflection_count"] == 2
    assert process["tool_count"] == 42
    assert process["tools_in_window"] == 7
    assert process["host_process_alive"] is False
    assert process["hook_parent_process_observed_alive"] is True
    assert process["operational_after_reflection"] is True
    assert process["execution_mode"] == "automation"
    assert process["execution_mode_source"] == "explicit_env"
    assert process["model"] == "gpt-5.4"
    capsule = process["restoration_capsule"]
    assert capsule["schema"] == "unitares.restoration_capsule.v2"
    assert capsule["host_observation"]["event_id"] == (
        "8f4bb851-dfed-4e12-b5b9-33820df47274"
    )
    assert capsule["execution"] == {
        "mode": "automation",
        "mode_source": "explicit_env",
        "host_family": "codex",
        "model": "gpt-5.4",
        "slot_hash": "ab" * 16,
        "plugin_version": "",
    }
    assert capsule["reflection"]["context"] == {
        "task_label": "weekly release notes",
        "task_outcome": "drafted",
        "tool_surface": ["github", "git"],
        "governance_action": "proceed",
    }
    assert capsule["continuity"] == {
        "relationship": "tool_events_after_agent_report",
        "missing": [],
        "restore_basis": "host_observation_and_authored_context",
    }


def test_heartbeat_only_never_marks_slot_as_active_agent_runtime():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {
            "agent_id": AGENT_UUID,
            "event_id": "8f4bb851-dfed-4e12-b5b9-33820df47275",
            "timestamp": "2026-08-02T11:55:00+00:00",
            "details": {
                "observation_kind": "heartbeat",
                "host_family": "codex",
                "slot_hash": "cd" * 16,
                "observed_at": "2026-08-02T11:55:00+00:00",
                "host_process_alive": True,
                "tool_count": 0,
            },
        }
    ]
    state_rows = [
        {
            "agent_id": AGENT_UUID,
            "label": "Codex host evidence",
            "last_reflection_at": None,
            "reflection_count": 0,
            "last_interpretation_at": datetime(2026, 8, 2, 11, 50, tzinfo=timezone.utc),
            "interpretation_count": 1,
            "last_bootstrap_at": None,
            "bootstrap_count": 0,
        }
    ]

    result = summarize_runtime_activity(events, state_rows, now=now)

    assert result["summary"]["recent_processes"] == 0
    assert result["summary"]["recent_tool_activity_slots"] == 0
    assert result["summary"]["recent_host_heartbeat_slots"] == 1
    assert result["summary"]["slots_without_agent_report"] == 1
    assert result["summary"]["host_observations_after_agent_report"] == 0
    assert result["summary"]["last_operational_at"] is None
    process = result["processes"][0]
    assert process["tool_activity_recent"] is False
    assert process["host_heartbeat_recent"] is True
    assert process["operational_recent"] is False
    assert process["state_update_profile"] == "substrate_only"
    assert process["agent_report_count"] == 0
    assert process["host_process_scope"] == "hook_parent"
    assert process["host_process_alive"] is False
    assert process["hook_parent_process_observed_alive"] is True
    assert (
        "never proof of continuous agent runtime"
        in result["semantics"]["host_observation"]
    )


def test_initialization_only_is_not_promoted_to_agent_checkin():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    events = [
        {
            "agent_id": AGENT_UUID,
            "timestamp": "2026-08-02T11:55:00+00:00",
            "details": {
                "observation_kind": "activity_rollup",
                "host_family": "codex",
                "slot_hash": "ef" * 16,
                "observed_at": "2026-08-02T11:55:00+00:00",
                "tool_count": 1,
                "tool_delta": 1,
            },
        }
    ]
    state_rows = [
        {
            "agent_id": AGENT_UUID,
            "label": "Codex initialization",
            "last_reflection_at": None,
            "reflection_count": 0,
            "last_interpretation_at": None,
            "interpretation_count": 0,
            "last_bootstrap_at": datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc),
            "bootstrap_count": 1,
        }
    ]

    result = summarize_runtime_activity(events, state_rows, now=now)
    process = result["processes"][0]

    assert process["tool_activity_recent"] is True
    assert process["agent_report_count"] == 0
    assert process["bootstrap_count"] == 1
    assert process["state_update_profile"] == "initialization_only"
    assert result["summary"]["slots_without_agent_report"] == 1


def test_runtime_observation_rejects_unproven_execution_mode(client):
    payload = _payload()
    payload["execution_mode"] = "probably-automation"
    response = client.post("/v1/runtime/observe", json=payload)
    assert response.status_code == 400
    assert "execution_mode" in response.json()["error"]


@pytest.mark.parametrize(
    ("execution_mode", "execution_mode_source"),
    [
        ("automation", "unspecified"),
        ("unknown", "explicit_env"),
    ],
)
def test_runtime_observation_rejects_contradictory_execution_provenance(
    client, execution_mode, execution_mode_source
):
    payload = _payload()
    payload["execution_mode"] = execution_mode
    payload["execution_mode_source"] = execution_mode_source
    response = client.post("/v1/runtime/observe", json=payload)
    assert response.status_code == 400
    assert "explicit provenance" in response.json()["error"]


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
