"""Tests for GET /v1/lifecycle/recent — recent lifecycle / circuit-breaker events."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_lifecycle_recent, _LIFECYCLE_EVENT_TYPES


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def client():
    app = Starlette(routes=[
        Route("/v1/lifecycle/recent", http_lifecycle_recent, methods=["GET"]),
    ])
    return TestClient(app, client=("127.0.0.1", 50000))


def _audit_row(event_type, agent_id="agent-1", reason="boom", details_extra=None):
    details = {"reason": reason}
    if details_extra:
        details.update(details_extra)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "event_type": event_type,
        "confidence": 1.0,
        "details": details,
        "event_id": "abc-123",
    }


def test_returns_pause_with_full_details_and_label(client):
    """The endpoint must return reason + full details payload, AND resolve
    agent_id → label so the consumer doesn't need a second lookup."""
    rows = [_audit_row(
        "circuit_breaker_trip",
        agent_id="f92dcea8-uuid",
        reason="UNITARES high-risk verdict (risk_score=0.65)",
        details_extra={"risk_score": 0.65, "coherence": 0.47},
    )]
    fake_meta = SimpleNamespace(label="Sentinel")
    with patch("src.audit_db.query_audit_events_async",
               AsyncMock(return_value=rows)), \
         patch("src.agent_metadata_model.agent_metadata",
               {"f92dcea8-uuid": fake_meta}):
        r = client.get("/v1/lifecycle/recent")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["count"] == 1
    evt = body["events"][0]
    assert evt["event_type"] == "circuit_breaker_trip"
    assert evt["agent_label"] == "Sentinel"
    assert "high-risk" in evt["reason"]
    assert evt["details"]["risk_score"] == 0.65, \
        "details payload must pass through — that's the whole point of this endpoint"


def test_filter_by_agent_label_resolves_to_uuid(client):
    """Passing agent_id=Sentinel should be resolved to the UUID before
    hitting audit.events (which only stores UUIDs)."""
    rows = [_audit_row("lifecycle_paused", agent_id="f92dcea8-uuid")]
    fake_meta = SimpleNamespace(label="Sentinel")

    captured_kwargs = {}

    async def _capture(**kwargs):
        captured_kwargs.update(kwargs)
        return rows

    with patch("src.audit_db.query_audit_events_async", side_effect=_capture), \
         patch("src.agent_metadata_model.agent_metadata",
               {"f92dcea8-uuid": fake_meta}):
        r = client.get("/v1/lifecycle/recent?agent_id=Sentinel")
    assert r.status_code == 200
    assert captured_kwargs.get("agent_id") == "f92dcea8-uuid", \
        "label 'Sentinel' must be resolved to its UUID before the DB query"


def test_filters_to_lifecycle_event_types_only(client):
    """The endpoint must restrict to lifecycle/circuit_breaker types so it
    isn't a firehose — that's what /api/events is for."""
    captured_kwargs = {}

    async def _capture(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    with patch("src.audit_db.query_audit_events_async", side_effect=_capture), \
         patch("src.agent_metadata_model.agent_metadata", {}):
        r = client.get("/v1/lifecycle/recent")
    assert r.status_code == 200
    types = captured_kwargs.get("event_types") or []
    assert "circuit_breaker_trip" in types
    assert "lifecycle_paused" in types
    assert "lifecycle_resumed" in types
    # And the same set as the module constant
    assert set(types) == set(_LIFECYCLE_EVENT_TYPES)
