"""Telemetry projections on the per-agent append-only history endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.eisv_telemetry import build_eisv_telemetry_envelope
from src.http_api import http_agent_history


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _DB:
    def __init__(self, rows):
        self.conn = type("Conn", (), {})()
        self.conn.fetch = AsyncMock(return_value=rows)

    def acquire(self):
        return _Acquire(self.conn)


def _envelope():
    return build_eisv_telemetry_envelope(
        metrics={"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
                 "primary_eisv_source": "behavioral"},
        behavioral_snapshot={
            "E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
            "confidence": 0.8, "raw_obs": [0.7, 0.8, 0.2],
            "obs_source": "physical",
        },
        submitted_sensor={"E": 0.7, "I": 0.8, "S": 0.2, "V": -0.1},
        submitted_source="physical",
        derivation={"kind": "caller_published_sensor", "missing_inputs": []},
        policy_evaluation={"action": "proceed", "sub_action": "guide"},
        enforcement={"requested": False, "applied": False},
        measurement_id="measurement-1",
    )


def _row():
    return {
        "recorded_at": datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc),
        "e": 0.6,
        "i": 0.8,
        "s_entropy": 0.2,
        "v": -0.2,
        "coherence": 0.5,
        "risk_score": 0.1,
        "state_json": {"E": 0.6, "eisv_telemetry": _envelope()},
        "epistemic_class": "agent_report",
        "telemetry_available": True,
        "total": 1,
        "agent_report_total": 1,
        "substrate_total": 0,
        "telemetry_total": 1,
    }


def _client():
    app = Starlette(routes=[
        Route("/v1/agents/{agent_id}/history", http_agent_history, methods=["GET"]),
    ])
    return TestClient(app)


def test_history_returns_compact_source_and_actuator_summary_by_default():
    db = _DB([_row()])
    with patch("src.http_api._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get("/v1/agents/agent-1/history")

    assert response.status_code == 200
    point = response.json()["points"][0]
    assert point["telemetry"]["measurement_source"] == "physical"
    assert point["telemetry"]["behavioral_confidence"] == 0.8
    assert point["epistemic_class"] == "agent_report"
    assert point["telemetry_available"] is True
    assert "telemetry_envelope" not in point
    assert response.json()["telemetry_included"] is False
    assert response.json()["observation_summary"] == {
        "state_rows": 1,
        "agent_reports": 1,
        "substrate_rows": 0,
        "other_rows": 0,
        "telemetry_envelopes": 1,
    }


def test_history_can_opt_into_the_full_append_only_envelope():
    db = _DB([_row()])
    with patch("src.http_api._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get(
            "/v1/agents/agent-1/history?include_telemetry=true"
        )

    point = response.json()["points"][0]
    assert point["telemetry_envelope"]["schema"] == "eisv.telemetry.v1"
    assert point["telemetry_envelope"]["measurement_id"] == "measurement-1"
    assert response.json()["telemetry_included"] is True


def test_history_exposes_legacy_substrate_rows_without_calling_them_reports():
    row = _row()
    row.update({
        "state_json": {"E": 0.6, "epistemic_class": "substrate_interpretation"},
        "epistemic_class": "substrate_interpretation",
        "telemetry_available": False,
        "total": 42,
        "agent_report_total": 0,
        "substrate_total": 42,
        "telemetry_total": 0,
    })
    db = _DB([row])
    with patch("src.http_api._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get("/v1/agents/agent-1/history")

    payload = response.json()
    assert payload["observation_summary"] == {
        "state_rows": 42,
        "agent_reports": 0,
        "substrate_rows": 42,
        "other_rows": 0,
        "telemetry_envelopes": 0,
    }
    assert payload["points"][0]["telemetry"]["schema"] == (
        "eisv.telemetry.summary.legacy"
    )
    assert payload["points"][0]["telemetry"]["missing_inputs"] == [
        "eisv_telemetry"
    ]
