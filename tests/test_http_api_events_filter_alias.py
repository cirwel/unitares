"""Tests for /api/events filter-key handling (dogfood finding 9028fa1e).

A mistyped or unsupported filter key used to be dropped silently, so the
response came back 200 with the FULL unfiltered set. A caller asking for one
event type got everything, with nothing indicating the filter had been ignored.

The probe hit this with `event_type`, which is what the MCP
observe(action='audit_events') surface calls the same parameter — so the two
surfaces disagreed and an operator could not tell which was right.
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
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_db_supplement():
    """Keep these tests about filter-key handling, not the audit supplement."""
    with patch("src.audit_db.query_audit_events_async", new=AsyncMock(return_value=[])):
        yield


def _seed(**kinds):
    for event_type, count in kinds.items():
        for i in range(count):
            # record_event requires a unique fingerprint — it dedups on it,
            # so seeded events need distinct ones or only the first survives.
            event_detector.record_event({
                "type": event_type,
                "message": f"{event_type} {i}",
                "severity": "low",
                "fingerprint": f"seed-{event_type}-{i}",
            })


def test_event_type_is_accepted_as_alias_for_type(client):
    _seed(dogfood_friction_finding=2, agent_new=3)
    r = client.get("/api/events?event_type=dogfood_friction_finding&limit=10")
    assert r.status_code == 200
    types = {e["type"] for e in r.json()["events"]}
    assert types == {"dogfood_friction_finding"}


def test_type_and_event_type_agree(client):
    _seed(dogfood_friction_finding=2, agent_new=3)
    a = client.get("/api/events?type=dogfood_friction_finding&limit=10").json()
    b = client.get("/api/events?event_type=dogfood_friction_finding&limit=10").json()
    assert [e["type"] for e in a["events"]] == [e["type"] for e in b["events"]]


def test_type_wins_when_both_supplied(client):
    """Existing callers passing `type` must be unaffected by the new alias."""
    _seed(dogfood_friction_finding=2, agent_new=3)
    r = client.get("/api/events?type=agent_new&event_type=dogfood_friction_finding&limit=10")
    assert {e["type"] for e in r.json()["events"]} == {"agent_new"}


def test_unknown_filter_key_is_rejected_not_silently_ignored(client):
    """The core regression: an unsupported key must never degrade to an
    unfiltered success. That is how a wrong query looks like a right one."""
    _seed(agent_new=3)
    r = client.get("/api/events?evnt_type=agent_new&limit=10")
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert "evnt_type" in body["error"]
    assert "safe_options" in body and "event_type" in body["safe_options"]
    assert "next_step" in body


def test_supported_keys_still_work(client):
    _seed(agent_new=2)
    for qs in ("limit=1", "agent_id=x", "type=agent_new", "since=0", ""):
        assert client.get(f"/api/events?{qs}").status_code == 200


def test_unknown_key_reported_even_alongside_valid_ones(client):
    r = client.get("/api/events?type=agent_new&bogus=1&limit=5")
    assert r.status_code == 400
    assert "bogus" in r.json()["error"]
