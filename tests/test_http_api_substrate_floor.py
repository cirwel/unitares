"""Tests for the identity-free substrate floor endpoints.

POST /v1/substrate/observe  — write a measurement, never an identity.
GET  /v1/substrate/dark_sessions — the coverage-gap dial.

The DB is faked: the point is that the floor write touches a plain table via a
plain INSERT and carries NO identity resolution, not that PG works.
"""

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import src.http_api as http_api
from src.http_api import http_substrate_observe, http_substrate_dark_sessions


class _FakeConn:
    def __init__(self, recorder, row=None):
        self.recorder = recorder
        self._row = row or {
            "distinct_slots": 2,
            "total_observations": 5,
            "unclaimed_slots": 2,
        }

    async def execute(self, query, *args):
        self.recorder.append(("execute", query, args))

    async def fetchrow(self, query, *args):
        self.recorder.append(("fetchrow", query, args))
        return self._row


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def recorder():
    return []


@pytest.fixture
def client(monkeypatch, recorder):
    import src.db as _db
    conn = _FakeConn(recorder)
    monkeypatch.setattr(_db, "get_db", lambda: _FakeDB(conn))
    app = Starlette(routes=[
        Route("/v1/substrate/observe", http_substrate_observe, methods=["POST"]),
        Route("/v1/substrate/dark_sessions", http_substrate_dark_sessions, methods=["GET"]),
    ])
    return TestClient(app)


def test_observe_rejects_missing_slot(client, recorder):
    r = client.post("/v1/substrate/observe", json={"event": "turn_stop"})
    assert r.status_code == 400
    assert r.json()["success"] is False
    # Rejected before any DB work.
    assert recorder == []


def test_observe_writes_identity_free_row(client, recorder):
    r = client.post("/v1/substrate/observe", json={
        "slot_key": "claude-session-xyz",
        "event": "turn_stop",
        "tool_count": 3,
        "summary_excerpt": "did work",
        "plugin_version": "9.9.9",
    })
    assert r.status_code == 201
    assert r.json()["success"] is True
    # Exactly one INSERT into the substrate table, no identity calls.
    assert len(recorder) == 1
    op, query, args = recorder[0]
    assert op == "execute"
    assert "core.substrate_observations" in query
    assert "claude-session-xyz" in args
    # The write must not reference any identity/agent table.
    assert "agent_state" not in query
    assert "identities" not in query


def test_observe_clamps_bad_tool_count(client, recorder):
    r = client.post("/v1/substrate/observe", json={
        "slot_key": "s1", "tool_count": "not-a-number",
    })
    assert r.status_code == 201
    _op, _q, args = recorder[0]
    # tool_count coerced to 0, not crashed.
    assert 0 in args


def test_dark_sessions_returns_counts(client):
    r = client.get("/v1/substrate/dark_sessions?window_hours=24")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["dark_sessions"] == 2
    assert body["total_observations"] == 5
    assert body["window_hours"] == 24.0


def test_dark_sessions_clamps_window(client):
    r = client.get("/v1/substrate/dark_sessions?window_hours=999999")
    assert r.status_code == 200
    # Clamped to the 90-day ceiling.
    assert r.json()["window_hours"] == 24 * 90
