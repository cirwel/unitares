"""Tests for GET /v1/enforcement/pause-outcomes — what surrounds a pause.

Handler tests mount the route with a keyword-dispatching fake DB (pattern:
tests/test_http_enforcement_divergence.py). One integration test runs the real
SQL against ``governance_test`` with seeded rows so the exit matching, the
tool_usage grouping, the refused-reflection filter and the first-authored lag
are exercised against Postgres, not a mock's echo.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_routes.telemetry import http_pause_outcomes

API_TOKEN = "test-http-api-token"


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _mock_db(*, error=None, authored=None):
    conn = SimpleNamespace()
    calls: list[tuple[str, tuple]] = []

    async def fetch(sql, *args):
        calls.append((sql, args))
        if error is not None:
            raise error
        if "lifecycle_paused" in sql:
            return [{"ended_by": "self_recovery", "n": 2},
                    {"ended_by": "none_recorded", "n": 5}]
        return [{"action": "review", "success": False, "error_type": "tool_error", "n": 3},
                {"action": "check", "success": True, "error_type": "", "n": 4}]

    async def fetchval(sql, *args):
        calls.append((sql, args))
        return 6

    async def fetchrow(sql, *args):
        calls.append((sql, args))
        if authored is not None:
            return authored
        return {"began_authored": 10, "began_non_authored": 40,
                "began_unknown_provenance": 3,
                "later_authored": 8, "p50_lag_seconds": 324.18}

    conn.fetch, conn.fetchval, conn.fetchrow = fetch, fetchval, fetchrow
    return SimpleNamespace(acquire=lambda: _AcquireCtx(conn)), calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    app = Starlette(routes=[
        Route("/v1/enforcement/pause-outcomes", http_pause_outcomes, methods=["GET"]),
    ])
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {API_TOKEN}"}


def test_no_bearer_is_401(client):
    with patch("src.db.get_db") as get_db:
        r = client.get("/v1/enforcement/pause-outcomes")
    assert r.status_code == 401
    get_db.assert_not_called()


@pytest.mark.parametrize(("query", "expected"), [("", 30), ("?days=0", 1),
                                                 ("?days=9999", 365), ("?days=x", 30)])
def test_days_is_clamped_into_every_query(client, query, expected):
    db, calls = _mock_db()
    with patch("src.db.get_db", return_value=db):
        r = client.get(f"/v1/enforcement/pause-outcomes{query}", headers=_auth())
    assert r.status_code == 200
    assert r.json()["window_days"] == expected
    assert calls and all(args == (expected,) for _, args in calls)


def test_response_shape(client):
    db, _ = _mock_db()
    with patch("src.db.get_db", return_value=db):
        body = client.get("/v1/enforcement/pause-outcomes", headers=_auth()).json()
    assert body["delivered_pauses"] == {
        "total": 7, "ended_by": {"self_recovery": 2, "none_recorded": 5},
    }
    assert {"action": "review", "call_succeeded": False, "error_type": "tool_error", "n": 3} in body["self_recovery"]
    assert {"action": "check", "call_succeeded": True, "error_type": None, "n": 4} in body["self_recovery"]
    assert body["refused_recovery_reflections"] == 6
    assert body["first_authored_checkin"] == {
        "began_authored": 10, "began_non_authored": 40,
        "began_unknown_provenance": 3,
        "later_authored": 8, "p50_lag_seconds": 324.2,
    }
    assert "not agent intent" in body["note"]


def test_no_lag_sample_is_null(client):
    db, _ = _mock_db(authored={"began_authored": 0, "began_non_authored": 0,
                               "began_unknown_provenance": 0,
                               "later_authored": 0, "p50_lag_seconds": None})
    with patch("src.db.get_db", return_value=db):
        body = client.get("/v1/enforcement/pause-outcomes", headers=_auth()).json()
    assert body["first_authored_checkin"]["p50_lag_seconds"] is None


def test_db_error_is_500_without_detail_leak(client):
    db, _ = _mock_db(error=RuntimeError("secret connection string"))
    with patch("src.db.get_db", return_value=db):
        r = client.get("/v1/enforcement/pause-outcomes", headers=_auth())
    assert r.status_code == 500
    assert "secret" not in r.text


# ---------------------------------------------------------------------------
# Integration: real SQL against governance_test
# ---------------------------------------------------------------------------

def _request(days: int) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/v1/enforcement/pause-outcomes",
        "query_string": f"days={days}".encode(),
        "headers": [(b"authorization", f"Bearer {API_TOKEN}".encode())],
        "client": ("203.0.113.9", 40000),
    })


@pytest.mark.asyncio
async def test_sql_against_live_db(live_postgres_backend, monkeypatch):
    """Seeded rows with pairwise-distinct expected counts.

    Agent A: paused, then resumed by self-recovery. Agent B: paused, resumed
    via dialectic. Agent C: paused, then a resume that predates the pause
    (must not count as its exit) -> none_recorded. A pause 40 days old is
    outside the 7-day window. Agent D's identity begins with a hook row and
    writes its own report 300s later; agent E begins authored.
    """
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    a, b, c, a2 = (str(uuid4()) for _ in range(4))
    tag = uuid4().hex[:8]
    now = datetime.now(timezone.utc)

    be = live_postgres_backend
    async with be.acquire() as conn:
        async def event(ts, agent, etype, payload):
            await conn.execute(
                "INSERT INTO audit.events (ts, event_id, agent_id, session_id, event_type,"
                " confidence, payload, raw_hash) VALUES ($1, gen_random_uuid(), $2, $2, $3,"
                " 0.0, $4::jsonb, $5)",
                ts, agent, etype, json.dumps(payload), f"po-{tag}-{uuid4().hex[:6]}",
            )

        await event(now - timedelta(days=2), a, "lifecycle_paused", {"reason": "x"})
        await event(now - timedelta(days=1), a, "lifecycle_resumed", {"reason": "Self-recovery: ok"})
        await event(now - timedelta(days=3), b, "lifecycle_paused", {"reason": "x"})
        await event(now - timedelta(days=2), b, "lifecycle_resumed",
                    {"reason": "Resumed via dialectic synthesis: ok"})
        await event(now - timedelta(days=4), c, "lifecycle_resumed", {"reason": "Self-recovery: early"})
        await event(now - timedelta(days=1), c, "lifecycle_paused", {"reason": "x"})
        await event(now - timedelta(days=40), a, "lifecycle_paused", {"reason": "old"})
        # Agent A2: paused twice, one resume after the second. That resume must
        # close only the second pause; the first stays none_recorded.
        await event(now - timedelta(days=3), a2, "lifecycle_paused", {"reason": "x"})
        await event(now - timedelta(days=2), a2, "lifecycle_paused", {"reason": "x"})
        await event(now - timedelta(days=1), a2, "lifecycle_resumed", {"reason": "Self-recovery: ok"})

        for action, success, err in (("review", False, "tool_error"),
                                     ("review", False, "tool_error"),
                                     ("check", True, None)):
            await conn.execute(
                "INSERT INTO audit.tool_usage (ts, agent_id, tool_name, success, error_type, payload)"
                " VALUES ($1, $2, 'self_recovery', $3, $4, $5::jsonb)",
                now - timedelta(hours=5), a, success, err, json.dumps({"action": action}),
            )

        await conn.execute(
            "INSERT INTO knowledge.discoveries (id, agent_id, type, severity, summary)"
            " VALUES ($1, $2, 'recovery_reflection', 'medium', 'refused')",
            f"po-{tag}-refl", a,
        )

        async def identity(agent_id):
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key')", agent_id)
            return await conn.fetchval(
                "INSERT INTO core.identities (agent_id, api_key_hash) VALUES ($1, 'test-hash')"
                " RETURNING identity_id", agent_id)

        async def state(identity_id, ts, ec):
            await conn.execute(
                "INSERT INTO core.agent_state (identity_id, recorded_at, entropy, integrity,"
                " volatility, coherence, regime, state_json, synthetic, epistemic_class)"
                " VALUES ($1, $2, 0.5, 0.5, 0.1, 0.5, 'nominal', '{}'::jsonb, false, $3)",
                identity_id, ts, ec)

        d_id = await identity(f"po-{tag}-d")
        await state(d_id, now - timedelta(hours=3), "substrate_interpretation")
        await state(d_id, now - timedelta(hours=3) + timedelta(seconds=300), "agent_report")
        e_id = await identity(f"po-{tag}-e")
        await state(e_id, now - timedelta(hours=2), "agent_report")
        u_id = await identity(f"po-{tag}-u")
        await state(u_id, now - timedelta(hours=4), None)
        await state(u_id, now - timedelta(hours=1), "agent_report")

    with patch("src.db.get_db", return_value=be):
        response = await http_pause_outcomes(_request(days=7))

    assert response.status_code == 200
    body = json.loads(response.body)
    ended = body["delivered_pauses"]["ended_by"]
    assert ended.get("self_recovery", 0) >= 1
    assert ended.get("dialectic", 0) >= 1
    # C's pre-pause resume and A2's first pause are both unresolved.
    assert ended.get("none_recorded", 0) >= 2
    review_fail = [r for r in body["self_recovery"]
                   if r["action"] == "review" and not r["call_succeeded"]]
    assert review_fail and review_fail[0]["n"] >= 2
    assert body["refused_recovery_reflections"] >= 1
    lag = body["first_authored_checkin"]
    assert lag["later_authored"] >= 1 and lag["began_authored"] >= 1
    assert lag["p50_lag_seconds"] is not None
    assert lag["began_unknown_provenance"] >= 1
