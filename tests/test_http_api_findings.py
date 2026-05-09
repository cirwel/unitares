"""Tests for POST /api/findings — external finding ingestion."""

import os

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.event_detector import event_detector
from src.http_api import http_record_finding


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
    """Unset token so _check_http_auth falls through to the no-token path."""
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/api/findings", http_record_finding, methods=["POST"])])
    return TestClient(app)


def test_accepts_valid_finding(client):
    payload = {
        "type": "sentinel_finding",
        "severity": "high",
        "message": "fleet coherence dipped",
        "agent_id": "sentinel-01",
        "agent_name": "Sentinel",
        "fingerprint": "abcd1234",
    }
    r = client.post("/api/findings", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["event"]["event_id"] == 1
    assert body["event"]["type"] == "sentinel_finding"
    assert body["deduped"] is False


def test_deduped_finding_returns_success_but_marked(client):
    payload = {
        "type": "sentinel_finding", "severity": "high", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "dedup-me",
    }
    r1 = client.post("/api/findings", json=payload)
    r2 = client.post("/api/findings", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["deduped"] is False
    assert r2.json()["deduped"] is True
    assert r2.json()["event"] is None


def test_rejects_missing_required_fields(client):
    r = client.post("/api/findings", json={"type": "x"})
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_rejects_invalid_severity(client):
    r = client.post("/api/findings", json={
        "type": "x", "severity": "BOGUS", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "fp",
    })
    assert r.status_code == 400


def test_rejects_invalid_type_prefix(client):
    r = client.post("/api/findings", json={
        "type": "verdict_change", "severity": "info", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "fp",
    })
    assert r.status_code == 400


def test_accepted_finding_calls_broadcaster_for_persistence(client, monkeypatch):
    """Accepted findings must reach broadcaster.broadcast_event so _persist_event
    writes them to audit.events. Without this, a server restart empties the
    in-memory ring buffer and all findings are lost.
    Regression for phase-2 fix (KG discovery 2026-04-25T10:49:00.729859)."""
    from src.broadcaster import broadcaster_instance

    broadcast_calls = []

    async def _capture(event_type, agent_id=None, payload=None):
        broadcast_calls.append({"event_type": event_type, "agent_id": agent_id})

    monkeypatch.setattr(broadcaster_instance, "broadcast_event", _capture)

    payload = {
        "type": "sentinel_finding",
        "severity": "high",
        "message": "fleet coherence dipped",
        "agent_id": "sentinel-01",
        "agent_name": "Sentinel",
        "fingerprint": "persist-regression-fp",
    }
    r = client.post("/api/findings", json=payload)
    assert r.status_code == 200
    assert r.json()["deduped"] is False

    assert len(broadcast_calls) == 1, "broadcast_event must fire once per accepted finding"
    assert broadcast_calls[0]["event_type"] == "sentinel_finding"
    assert broadcast_calls[0]["agent_id"] == "sentinel-01"

    # Simulate restart: clear the in-memory ring buffer
    event_detector.clear_events()
    assert event_detector._recent_events == []
    # The finding survives in audit.events because broadcast_event → _persist_event was called.


def test_deduped_finding_skips_broadcaster(client, monkeypatch):
    """Deduped findings must not trigger a redundant broadcast_event call."""
    from src.broadcaster import broadcaster_instance

    broadcast_calls = []

    async def _capture(event_type, agent_id=None, payload=None):
        broadcast_calls.append(event_type)

    monkeypatch.setattr(broadcaster_instance, "broadcast_event", _capture)

    payload = {
        "type": "sentinel_finding", "severity": "high", "message": "m",
        "agent_id": "a", "agent_name": "n", "fingerprint": "dedup-broadcast-fp",
    }
    client.post("/api/findings", json=payload)
    client.post("/api/findings", json=payload)  # deduped

    assert len(broadcast_calls) == 1  # only the first accepted posting
