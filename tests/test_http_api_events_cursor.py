"""Tests for /api/events int-cursor protocol (CIRWEL/unitares#25).

The audit-DB supplement can return rows with UUID event_ids. When a client
supplies an int `since` cursor, those rows are unreachable — they'd replay
every poll. Drop them from the supplement under int-cursor mode; keep them
when no cursor is given (dashboard case).
"""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.event_detector import event_detector
from src.http_api import http_events


@pytest.fixture(autouse=True)
def clear_events():
    event_detector.clear_events()
    event_detector._recent_fingerprints.clear()
    event_detector._event_counter = 0
    yield
    event_detector.clear_events()
    event_detector._recent_fingerprints.clear()
    event_detector._event_counter = 0


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/api/events", http_events, methods=["GET"])])
    return TestClient(app, client=("127.0.0.1", 50000))


def _audit_row(event_id, event_type="cross_device_call"):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "agent_id": "agent-x",
        "timestamp": "2026-04-19T00:00:00+00:00",
        "details": {"type": event_type, "severity": "info", "message": "m"},
    }


def test_since_cursor_filters_uuid_audit_rows(client):
    """With `since`, audit rows whose event_id is a UUID must be dropped."""
    uuid_row = _audit_row("c6d8a2da-f61e-4ed4-8f88-c1b5918bac10")
    int_row = _audit_row(42)

    with patch(
        "src.audit_db.query_audit_events_async",
        new=AsyncMock(return_value=[uuid_row, int_row]),
    ):
        r = client.get("/api/events?since=0&limit=50")

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    ids = [e["event_id"] for e in body["events"]]
    assert "c6d8a2da-f61e-4ed4-8f88-c1b5918bac10" not in ids
    assert 42 in ids


def test_no_since_keeps_uuid_audit_rows(client):
    """Without `since`, the dashboard still sees UUID audit rows."""
    uuid_row = _audit_row("c6d8a2da-f61e-4ed4-8f88-c1b5918bac10")

    with patch(
        "src.audit_db.query_audit_events_async",
        new=AsyncMock(return_value=[uuid_row]),
    ):
        r = client.get("/api/events?limit=50")

    assert r.status_code == 200
    ids = [e["event_id"] for e in r.json()["events"]]
    assert "c6d8a2da-f61e-4ed4-8f88-c1b5918bac10" in ids
