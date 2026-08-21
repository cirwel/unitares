"""Tests for GET /v1/enforcement/divergence (#1528) — the produced-vs-delivered
pause honesty meter.

Two layers, matching repo conventions:

- Handler tests mount just the route on a minimal Starlette app with the DB
  mocked (pattern: tests/test_http_harness_outcome.py). They cover the auth
  gate, the ``days`` clamp, the response shape, the null last-delivered case,
  and the error path.
- One integration test runs the handler's real SQL against ``governance_test``
  (``live_postgres_backend`` fixture; skips when the DB is unavailable, CI
  provisions it) with seeded ``audit.events`` rows, so the FILTER clauses and
  gap-suppression counting are exercised against actual Postgres semantics,
  not a mock's echo.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_routes.telemetry import http_enforcement_divergence

API_TOKEN = "test-http-api-token"


# ---------------------------------------------------------------------------
# Mock-DB plumbing
# ---------------------------------------------------------------------------

class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


TOTALS = {"produced": 3, "gap_suppressed": 1, "delivered": 2}
LAST = {"last_delivered": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)}
WEEKLY = [
    {"week": "08-04", "produced": 1, "delivered": 0},
    {"week": "08-11", "produced": 2, "delivered": 2},
]


def _mock_db(*, totals=TOTALS, last=LAST, weekly=None, error=None):
    """Fake DB dispatching on SQL text, not call order/count.

    Order-based side_effect lists made harmless refactors (query reordering,
    fetchrow→fetchval) fail 9 of 13 tests while catching nothing extra —
    keyword dispatch keeps the fake honest without freezing call sequence.
    ``calls`` records every (sql, args) pair for parameter assertions.
    """
    conn = SimpleNamespace()
    calls: list[tuple[str, tuple]] = []

    if error is not None:
        async def fetchrow(sql, *args):
            raise error
    else:
        async def fetchrow(sql, *args):
            calls.append((sql, args))
            if "max(ts)" in sql:
                return dict(last) if last is not None else None
            return dict(totals)

    async def fetch(sql, *args):
        calls.append((sql, args))
        return list(WEEKLY if weekly is None else weekly)

    async def fetchval(sql, *args):
        # A scalar-query refactor (fetchval instead of fetchrow) is a no-op
        # to the endpoint's contract; the fake must not fail it.
        calls.append((sql, args))
        if error is not None:
            raise error
        if "max(ts)" in sql:
            return (last or {}).get("last_delivered")
        return None

    conn.fetchrow = fetchrow
    conn.fetch = fetch
    conn.fetchval = fetchval
    db = SimpleNamespace(acquire=lambda: _AcquireCtx(conn))
    return db, calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    # Local posture, not hosted-bearer posture — that branch has its own gate.
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    app = Starlette(routes=[
        Route("/v1/enforcement/divergence", http_enforcement_divergence,
              methods=["GET"]),
    ])
    # TestClient's peer host is the literal string "testclient", which
    # _is_trusted_network rejects via ipaddress.ip_address ValueError — the
    # untrusted branch fires, so these requests exercise the bearer path.
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {API_TOKEN}"}


class TestAuth:
    def test_no_bearer_is_401(self, client):
        with patch("src.db.get_db") as get_db:
            r = client.get("/v1/enforcement/divergence")
        assert r.status_code == 401
        get_db.assert_not_called()

    def test_wrong_bearer_is_401(self, client):
        r = client.get("/v1/enforcement/divergence",
                       headers={"Authorization": "Bearer not-the-token"})
        assert r.status_code == 401

    def test_valid_bearer_is_200(self, client):
        db, _ = _mock_db()
        with patch("src.db.get_db", return_value=db):
            r = client.get("/v1/enforcement/divergence", headers=_auth())
        assert r.status_code == 200


class TestDaysClamp:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("", 90),              # default
            ("?days=abc", 90),     # unparseable falls back to default
            ("?days=0", 1),        # floor
            ("?days=-5", 1),       # floor
            ("?days=9999", 365),   # ceiling
            ("?days=30", 30),      # passthrough
        ],
    )
    def test_days_clamped_into_sql_param(self, client, query, expected):
        db, calls = _mock_db()
        with patch("src.db.get_db", return_value=db):
            r = client.get(f"/v1/enforcement/divergence{query}", headers=_auth())
        assert r.status_code == 200
        assert r.json()["window_days"] == expected
        # Every parameterized query must receive the clamped value —
        # order- and count-agnostic on purpose.
        parameterized = [args for _, args in calls if args]
        assert parameterized, "no parameterized DB call recorded"
        assert all(args[0] == expected for args in parameterized)


class TestResponseShape:
    def test_counters_last_delivered_and_weekly(self, client):
        db, _ = _mock_db()
        with patch("src.db.get_db", return_value=db):
            r = client.get("/v1/enforcement/divergence", headers=_auth())
        body = r.json()
        assert body["posture"] == "advisory"
        assert body["produced_pauses"] == 3
        assert body["gap_suppressed"] == 1
        assert body["delivered_pauses"] == 2
        assert body["last_delivered_at"] == "2026-08-20T12:00:00+00:00"
        assert body["weekly"] == [
            {"week": "08-04", "produced": 1, "delivered": 0},
            {"week": "08-11", "produced": 2, "delivered": 2},
        ]
        # The divergence caveat is part of the payload contract — operator
        # surfaces render it so verdict counts are never read as enforcement.
        assert "note" in body and "produced pause verdict" in body["note"].lower()

    def test_no_delivered_pause_ever_is_null_not_crash(self, client):
        db, _ = _mock_db(totals={"produced": 0, "gap_suppressed": 0, "delivered": 0},
                         last={"last_delivered": None}, weekly=[])
        with patch("src.db.get_db", return_value=db):
            r = client.get("/v1/enforcement/divergence", headers=_auth())
        body = r.json()
        assert r.status_code == 200
        assert body["produced_pauses"] == 0
        assert body["last_delivered_at"] is None
        assert body["weekly"] == []

    def test_db_error_is_500_without_detail_leak(self, client):
        db, _ = _mock_db(error=RuntimeError("connection refused at 10.0.0.5"))
        with patch("src.db.get_db", return_value=db):
            r = client.get("/v1/enforcement/divergence", headers=_auth())
        assert r.status_code == 500
        assert r.json() == {"error": "query failed"}
        # The exception text (host/port internals) must not reach the client.
        assert "10.0.0.5" not in r.text


# ---------------------------------------------------------------------------
# Integration: real SQL against governance_test
# ---------------------------------------------------------------------------

def _request(days: int) -> Request:
    """Build a real Starlette Request: untrusted peer + valid bearer."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/enforcement/divergence",
        "query_string": f"days={days}".encode(),
        "headers": [(b"authorization", f"Bearer {API_TOKEN}".encode())],
        "client": ("203.0.113.9", 40000),  # TEST-NET-3: never a trusted range
    }
    return Request(scope)


_SEED_AGENT = "3a7f2c91-5b64-4e08-9d13-8c2f6a4e7b50"

# Payload shapes mirror production (verified live 2026-08-21): gap_suppressed
# is a JSON BOOLEAN in 100% of the ~92k auto_attest rows that carry it, and
# it is false on ~99.8% of them. Rows are chosen so that each FILTER conjunct
# is separable AND the expected counts are pairwise distinct (4/2/1) — with
# symmetric counts, flipping the gap predicate to 'false', reading
# decision='proceed', or swapping the gap/delivered aggregates would all
# count the same. The gap=false pause and the gap=true PROCEED row must stay
# out of the gap_suppressed count, and the lifecycle_paused row sits at a
# different age (2 days) than every auto_attest row (1 day) so a wrong-event
# max(ts) is detectable.
_SEED_SQL = f"""
INSERT INTO audit.events
    (ts, event_id, agent_id, session_id, event_type, confidence, payload, raw_hash)
VALUES
    (now() - interval '1 day',  gen_random_uuid(), '{_SEED_AGENT}', '{_SEED_AGENT}',
     'auto_attest', 0.9, '{{"decision": "pause"}}'::jsonb, 'div-h1'),
    (now() - interval '1 day',  gen_random_uuid(), '{_SEED_AGENT}', '{_SEED_AGENT}',
     'auto_attest', 0.9, '{{"decision": "pause", "gap_suppressed": true}}'::jsonb, 'div-h2'),
    (now() - interval '1 day',  gen_random_uuid(), '{_SEED_AGENT}', '{_SEED_AGENT}',
     'auto_attest', 0.9, '{{"decision": "pause", "gap_suppressed": true}}'::jsonb, 'div-h2b'),
    (now() - interval '1 day',  gen_random_uuid(), '{_SEED_AGENT}', '{_SEED_AGENT}',
     'auto_attest', 0.9, '{{"decision": "pause", "gap_suppressed": false}}'::jsonb, 'div-h3'),
    (now() - interval '1 day',  gen_random_uuid(), '{_SEED_AGENT}', '{_SEED_AGENT}',
     'auto_attest', 0.9, '{{"decision": "proceed", "gap_suppressed": true}}'::jsonb, 'div-h4'),
    (now() - interval '2 days', gen_random_uuid(), '{_SEED_AGENT}', '{_SEED_AGENT}',
     'lifecycle_paused', 0.0, '{{}}'::jsonb, 'div-h5'),
    (now() - interval '10 days', gen_random_uuid(), '{_SEED_AGENT}', '{_SEED_AGENT}',
     'auto_attest', 0.9, '{{"decision": "pause"}}'::jsonb, 'div-h6')
"""


@pytest.mark.asyncio
async def test_sql_filters_against_live_db(live_postgres_backend, monkeypatch):
    """Seeded rows → the handler's own SQL counts them correctly.

    days=7 window: 4 produced pauses, exactly 2 gap-suppressed (the
    gap=false pause and the gap=true PROCEED row must not count), 1
    delivered — pairwise-distinct counts so predicate inversions and
    aggregate swaps cannot pass. The 10-day-old pause must be excluded.
    last_delivered_at must come from the lifecycle_paused row (~2 days
    old), not from the newer auto_attest rows (~1 day old).
    """
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)

    be = live_postgres_backend
    async with be.acquire() as conn:
        await conn.execute(_SEED_SQL)

    with patch("src.db.get_db", return_value=be):
        response = await http_enforcement_divergence(_request(days=7))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["window_days"] == 7
    assert body["produced_pauses"] == 4
    assert body["gap_suppressed"] == 2
    assert body["delivered_pauses"] == 1
    # last_delivered_at must be the lifecycle_paused row's ts (~2 days old).
    # A max(ts) taken from the wrong event type would land ~1 day old.
    assert body["last_delivered_at"] is not None
    last = datetime.fromisoformat(body["last_delivered_at"])
    age = datetime.now(timezone.utc) - last
    assert timedelta(hours=46) < age < timedelta(hours=50), (
        f"last_delivered_at age {age} is not the seeded lifecycle_paused row"
    )
    # Weekly buckets partition the same window: totals must reconcile.
    assert sum(w["produced"] for w in body["weekly"]) == 4
    assert sum(w["delivered"] for w in body["weekly"]) == 1
