"""Server side of the shared timeline parity fixture.

``tests/fixtures/timeline_parity/`` pins the rules the live Timeline pane in
``elixir/dialectic_live`` (``DialecticLive.TimelineAdapter``) copies from this
server, so the two normalisers cannot drift apart. The Elixir suite asserts the
same files (``test/dialectic_live/timeline_parity_test.exs``).

This side checks each rule against the server code that owns it:

- the knowledge summary cut, through ``http_timeline`` itself with the DB
  mocked, so the test exercises the route's own code rather than a copy of it;
- the lifecycle type set, against ``_LIFECYCLE_EVENT_TYPES``;
- the verdict rule, against ``src.broadcaster.verdict_of``;
- every eisv_update, lifecycle and knowledge_write frame in ``frames.json``,
  against the same three rules.

The frame dispatch itself (which frame type maps to which kind, and what is
dropped) has no server counterpart: the server does not map frames. So
``test_frame_cases_follow_the_server_rules`` restates the dispatch to check the
fixture's consistency, and only its uses of the cut, the lifecycle set and
``verdict_of`` are anchored to server code.

Portal proposal: ``docs/proposals/active/portal-live-timeline-v0.md`` §3.1.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.broadcaster import verdict_of
from src.http_routes.telemetry import _LIFECYCLE_EVENT_TYPES
from src.http_routes.timeline import _SUMMARY_CHARS, http_timeline

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "timeline_parity"
API_TOKEN = "test-http-api-token"
AGENT = "00000000-0000-4000-8000-0000000000a1"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


SUMMARY = _load("summary_cut")
LIFECYCLE = _load("lifecycle_types")
VERDICT = _load("verdict")
FRAMES = _load("frames")


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _knowledge_db(summary):
    """A DB that holds one knowledge_write audit row carrying ``summary``."""
    now = datetime.now(timezone.utc)

    async def fetch(sql, *args):
        if "audit.events" in sql:
            if "knowledge_write" not in args[2]:
                return []
            return [{
                "ts": now - timedelta(minutes=1), "event_id": "e-1", "agent_id": AGENT,
                "event_type": "knowledge_write",
                "payload": {"summary": summary, "discovery_id": "d-1", "discovery_type": "note"},
            }]
        if "FROM core.identities" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    conn = SimpleNamespace(fetch=fetch)
    return SimpleNamespace(acquire=lambda: _AcquireCtx(conn))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    app = Starlette(routes=[Route("/v1/timeline", http_timeline, methods=["GET"])])
    return TestClient(app)


def _served_summary(client, summary):
    with patch("src.db.get_db", return_value=_knowledge_db(summary)):
        r = client.get(
            "/v1/timeline?kinds=knowledge",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
    assert r.status_code == 200, r.text
    (event,) = r.json()["events"]
    return event["summary"]


def test_fixture_states_the_servers_cut_length():
    assert SUMMARY["chars"] == _SUMMARY_CHARS
    assert SUMMARY["ellipsis"] == "…"


@pytest.mark.parametrize("case", SUMMARY["cases"], ids=lambda c: c["name"])
def test_summary_cut_matches_the_route(client, case):
    assert _served_summary(client, case["input"]) == case["expected"]


@pytest.mark.parametrize(
    "case", [c for c in SUMMARY["cases"] if c["input"] is not None], ids=lambda c: c["name"]
)
def test_fixture_counts_code_points(case):
    # The Elixir side must count code points, not graphemes; the fixture says
    # how many there are so a reader can see which cases distinguish the two.
    assert case["input_code_points"] == len(case["input"])


def test_lifecycle_types_match_the_history_set():
    assert LIFECYCLE["types"] == list(_LIFECYCLE_EVENT_TYPES)


@pytest.mark.parametrize("case", VERDICT["cases"], ids=lambda c: c["name"])
def test_verdict_rule_matches_broadcaster(case):
    assert verdict_of(case["frame"]) == case["expected"]


@pytest.mark.parametrize("case", FRAMES["cases"], ids=lambda c: c["name"])
def test_frame_cases_follow_the_server_rules(client, case):
    frame, result = case["frame"], case["result"]
    frame_type = frame.get("type") or ""
    has_agent = bool(frame.get("agent_id"))

    if frame_type.startswith("dialectic"):
        assert result == "doorbell"
    elif not has_agent:
        assert result == "drop"
    elif frame_type == "eisv_update":
        assert result == "mark"
        mark = case["mark"]
        assert mark["kind"] == "checkin" and mark["authored_by"] == "unknown"
        if isinstance(frame.get("decision"), dict):
            assert mark["assessment"] == {"source": "eisv", "verdict": verdict_of(frame)}
        else:
            assert "assessment" not in mark
    elif frame_type in _LIFECYCLE_EVENT_TYPES:
        assert result == "mark_and_doorbell"
        assert case["mark"]["event_type"] == frame_type
    elif frame_type == "knowledge_write":
        assert result == "mark"
        assert case["mark"]["summary"] == _served_summary(client, frame["summary"])
    else:
        assert result == "drop"

    if result in ("mark", "mark_and_doorbell"):
        assert case["mark"]["agent_id"] == frame["agent_id"]
        assert case["mark"]["ts"] == frame["timestamp"]
        assert case["mark"]["provisional"] is True


def test_every_history_lifecycle_type_has_a_frame_case():
    mapped = {
        c["frame"]["type"] for c in FRAMES["cases"] if c["result"] == "mark_and_doorbell"
    }
    assert mapped == set(_LIFECYCLE_EVENT_TYPES)
