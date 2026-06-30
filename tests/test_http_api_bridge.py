import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_bridge_summary, http_record_bridge_event


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def client():
    app = Starlette(
        routes=[
            Route("/v1/bridge/events", http_record_bridge_event, methods=["POST"]),
            Route("/v1/bridge/summary", http_bridge_summary, methods=["GET"]),
        ]
    )
    return TestClient(app)


def test_record_bridge_event_accepts_valid_payload(client, monkeypatch):
    calls = []

    async def fake_append(entry):
        calls.append(entry)
        return True

    monkeypatch.setattr("src.audit_db.append_audit_event_async", fake_append)

    r = client.post(
        "/v1/bridge/events",
        json={
            "event_type": "bridge.delivery",
            "source_event_id": "evt-1",
            "source_severity": "high",
            "channel_key": "alerts",
            "discord_message_id": "msg-1",
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["event"]["event_type"] == "bridge.delivery"
    assert calls[0]["event_type"] == "bridge.delivery"


def test_record_bridge_event_rejects_invalid_payload(client):
    r = client.post("/v1/bridge/events", json={"event_type": "bridge.nope"})

    assert r.status_code == 400
    assert r.json()["success"] is False


def test_bridge_summary_returns_attention_snapshot(client, monkeypatch):
    async def fake_query(**kwargs):
        return []

    monkeypatch.setattr("src.audit_db.query_audit_events_async", fake_query)

    r = client.get("/v1/bridge/summary?since=24h&include_events=false")

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["surface"] == "discord"
    assert "events" not in body
