"""Fleet risk / verdict-pressure trend from core.agent_state (Risk tab).

The Risk section charted Chronicler's daily scrape, so a residentless install
had an empty tab. /v1/governance/trend computes the same trailing-7-day series
from the table those scrapers read. These tests pin the rolling arithmetic,
the retention-driven window cap, and the route's gate and cache.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src import governance_trend as gt
from src.http_routes import telemetry


def _day(d, risk_sum=0.0, risk_n=0, guide=0, pause=0):
    return {"day": d, "risk_sum": risk_sum, "risk_n": risk_n, "guide": guide, "pause": pause}


def test_trailing_week_is_a_weighted_mean_and_sums_counts():
    today = date(2026, 9, 27)
    daily = [
        _day(date(2026, 9, 21), risk_sum=1.0, risk_n=10, guide=2, pause=1),  # 6 days back: inside
        _day(date(2026, 9, 27), risk_sum=3.0, risk_n=10, guide=1, pause=0),
        _day(date(2026, 9, 20), risk_sum=9.0, risk_n=1, guide=50, pause=50),  # 7 days back: outside
    ]
    s = gt.rolling_series(daily, window_days=7, today=today)
    last = {k: v[-1] for k, v in s.items()}
    assert last["risk"] == {"ts": "2026-09-27T00:00:00Z", "value": 0.2}  # (1+3)/(10+10), not mean of means
    assert last["guide"]["value"] == 3
    assert last["pause"]["value"] == 1


def test_one_point_per_day_ending_today():
    s = gt.rolling_series([], window_days=14, today=date(2026, 9, 27))
    assert len(s["guide"]) == len(s["pause"]) == 14
    assert s["guide"][0]["ts"] == "2026-09-14T00:00:00Z"
    assert s["guide"][-1]["ts"] == "2026-09-27T00:00:00Z"


def test_a_week_without_risk_readings_gets_no_risk_point_not_zero():
    s = gt.rolling_series([], window_days=7, today=date(2026, 9, 27))
    assert s["risk"] == []
    assert all(p["value"] == 0 for p in s["pause"])


@pytest.mark.parametrize(("raw", "expected"), [("60", 60), ("180", 60), ("1", 7), ("junk", 60), (None, 60), ("30", 30)])
def test_window_is_capped_inside_retained_history(raw, expected):
    assert gt.clamp_window(raw) == expected


class _Conn:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def fetch(self, sql, since):
        self.calls.append((sql, since))
        return self.rows


@pytest.mark.asyncio
async def test_query_reads_the_window_plus_its_first_trailing_week():
    conn = _Conn([_day(date(2026, 9, 27), risk_sum=0.5, risk_n=2, guide=1)])
    out = await gt.query_governance_trend(conn, window_days=30, now=datetime(2026, 9, 27, 12, tzinfo=timezone.utc))
    sql, since = conn.calls[0]
    assert since == datetime(2026, 8, 23, tzinfo=timezone.utc)  # 30 + 6 days back
    assert "synthetic = false" in sql
    assert out["window_days"] == 30 and out["source"] == "core.agent_state"
    assert out["risk"][-1]["value"] == 0.25


# --- route --------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _local_posture(monkeypatch):
    for key in ("UNITARES_HTTP_API_TOKEN", "UNITARES_MCP_BEARER_TOKENS", "UNITARES_REST_STRICT"):
        monkeypatch.delenv(key, raising=False)
    telemetry._governance_trend_cache.clear()


def _client(peer):
    app = Starlette(routes=[Route("/v1/governance/trend", telemetry.http_governance_trend, methods=["GET"])])
    return TestClient(app, client=peer)


class _DB:
    def __init__(self, conn):
        self.conn, self.acquired = conn, 0

    def acquire(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                db.acquired += 1
                return db.conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def test_route_is_gated():
    r = _client(("203.0.113.7", 44444)).get("/v1/governance/trend")
    assert r.status_code == 401
    assert "risk" not in r.text


def test_route_serves_and_caches_per_window(monkeypatch):
    db = _DB(_Conn([_day(datetime.now(timezone.utc).date(), risk_sum=0.3, risk_n=1)]))
    monkeypatch.setattr("src.db.get_db", lambda: db)
    c = _client(("127.0.0.1", 50000))
    first = c.get("/v1/governance/trend?days=14").json()
    assert first["success"] and first["window_days"] == 14 and first["risk"][-1]["value"] == 0.3
    c.get("/v1/governance/trend?days=14")
    assert db.acquired == 1  # second read served from cache
    c.get("/v1/governance/trend?days=30")
    assert db.acquired == 2  # a different window is its own entry
