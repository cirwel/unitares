"""Tests for GET /v1/timeline — every agent's events in one window.

Two layers, matching tests/test_http_enforcement_divergence.py:

- Handler tests mount the route on a minimal Starlette app with the DB mocked.
  They cover the auth gate, parameter validation and clamps, the kind and
  substrate/trajectory filters, the merged response shape and the error path.
- One integration test runs the handler's real SQL against ``governance_test``
  (``live_postgres_backend``; skips when the DB is unavailable, CI provisions
  it) with seeded rows in every source table.
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

from src.http_routes.timeline import TIMELINE_KINDS, http_timeline

API_TOKEN = "test-http-api-token"
NOW = datetime.now(timezone.utc)
PARENT = "5c0f7a2e-1d4b-4c8e-9a31-2b6d8e4f0a17"
CHILD = "9e3b6d10-7f2a-4e5c-8b19-4a0c2d6e8f53"


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _rows():
    return {
        "checkin": [
            {"state_id": 11, "recorded_at": NOW - timedelta(minutes=5), "identity_id": 1,
             "epistemic_class": "agent_report", "action": "guide",
             "verdict": "safe", "risk_score": 0.31,
             "report_excerpt": "Opened PR #3045.", "confidence_reported": "0.7",
             "has_self_report": True},
            {"state_id": 10, "recorded_at": NOW - timedelta(minutes=50), "identity_id": 1,
             "epistemic_class": "substrate_interpretation", "action": "approve",
             "verdict": "safe", "risk_score": None,
             "report_excerpt": None, "confidence_reported": None,
             "has_self_report": False},
        ],
        "outcome": [
            {"ts": NOW - timedelta(minutes=3), "outcome_id": "0b5c", "agent_id": PARENT,
             "outcome_type": "test_failed", "outcome_score": 0.0, "is_bad": True,
             "verification_source": "external_signal", "reported_confidence": "0.9",
             "prediction_id": "p-1", "decision_action": "proceed"},
        ],
        "lifecycle_paused": [
            {"ts": NOW - timedelta(minutes=40), "event_id": "e1", "agent_id": PARENT,
             "event_type": "lifecycle_paused",
             "payload": json.dumps({"reason": "risk over threshold"})},
        ],
        "knowledge_write": [
            {"ts": NOW - timedelta(minutes=30), "event_id": "e2", "agent_id": PARENT,
             "event_type": "knowledge_write",
             "payload": {"summary": "x" * 400, "discovery_id": "d-1",
                         "discovery_type": "note"}},
        ],
        "lineage_declared": [
            {"ts": NOW - timedelta(minutes=45), "event_id": "e3", "agent_id": CHILD,
             "event_type": "lineage_declared",
             "payload": {"parent_id": PARENT, "successor_class": "ephemeral"}},
        ],
        "dialectic": [
            {"session_id": "s-1", "paused_agent_id": PARENT, "reviewer_agent_id": None,
             "status": "resolved", "phase": "resolved", "session_type": "review",
             "created_at": NOW - timedelta(minutes=20),
             "updated_at": NOW - timedelta(minutes=10)},
        ],
        "identities": [
            {"identity_id": 1, "agent_id": PARENT, "parent_agent_id": None, "spawn_reason": None,
             "created_at": NOW - timedelta(days=1), "last_activity_at": NOW,
             "status": "active", "label": "parent-agent", "model_type": "claude",
             "trust_tier": "established", "tags": ["persistent", "autonomous"],
             "host_harness": "claude-code", "host_session_id": "sess-1", "host_resumable": True},
            {"identity_id": 2, "agent_id": CHILD, "parent_agent_id": PARENT, "spawn_reason": "subagent",
             "created_at": NOW - timedelta(minutes=45), "last_activity_at": NOW,
             "status": "active", "label": None, "model_type": None,
             "trust_tier": None, "tags": None,
             "host_harness": None, "host_session_id": None, "host_resumable": False},
        ],
    }


def _mock_db(*, rows=None, error=None):
    """Fake DB dispatching on SQL text, not call order."""
    data = rows if rows is not None else _rows()
    calls: list[tuple[str, tuple]] = []

    async def fetch(sql, *args):
        calls.append((sql, args))
        if error is not None:
            raise error
        if "core.agent_state" in sql:
            return list(data["checkin"])
        if "audit.outcome_events" in sql:
            return list(data["outcome"])
        if "audit.events" in sql:
            out = []
            for event_type in args[2]:
                out.extend(data.get(event_type, []))
            return out
        if "core.dialectic_sessions" in sql:
            return list(data["dialectic"])
        if "WHERE agent_id = $1" in sql:
            return [{"identity_id": r["identity_id"]}
                    for r in data["identities"] if r["agent_id"] == args[0]]
        if "FROM core.identities" in sql:
            return [r for r in data["identities"]
                    if r["identity_id"] in args[0] or r["agent_id"] in args[1]]
        raise AssertionError(f"unexpected SQL: {sql}")

    conn = SimpleNamespace(fetch=fetch)
    return SimpleNamespace(acquire=lambda: _AcquireCtx(conn)), calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    app = Starlette(routes=[Route("/v1/timeline", http_timeline, methods=["GET"])])
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _get(client, query="", db=None):
    db = db or _mock_db()[0]
    with patch("src.db.get_db", return_value=db):
        return client.get(f"/v1/timeline{query}", headers=_auth())


class TestAuth:
    def test_no_bearer_is_401(self, client):
        with patch("src.db.get_db") as get_db:
            r = client.get("/v1/timeline")
        assert r.status_code == 401
        get_db.assert_not_called()

    def test_valid_bearer_is_200(self, client):
        assert _get(client).status_code == 200


class TestValidation:
    def test_unknown_parameter_is_400_not_a_wider_read(self, client):
        db, calls = _mock_db()
        r = _get(client, "?agent=abc", db=db)
        assert r.status_code == 400
        assert "agent" in r.json()["error"]
        assert calls == []

    def test_unknown_kind_is_400(self, client):
        db, calls = _mock_db()
        r = _get(client, "?kinds=checkin,verdicts", db=db)
        assert r.status_code == 400
        assert "verdicts" in r.json()["error"]
        assert r.json()["safe_options"] == list(TIMELINE_KINDS)
        assert calls == []

    @pytest.mark.parametrize(
        ("query", "hours", "limit"),
        [
            ("", 6, 500),
            ("?hours=abc&limit=xyz", 6, 500),
            ("?hours=0&limit=0", 1, 1),
            ("?hours=9999&limit=99999", 168, 2000),
            ("?hours=24&limit=50", 24, 50),
        ],
    )
    def test_hours_and_limit_clamped(self, client, query, hours, limit):
        db, calls = _mock_db()
        body = _get(client, query, db=db).json()
        assert body["window"]["hours"] == hours
        assert body["limit_per_kind"] == limit
        start = datetime.fromisoformat(body["window"]["start"])
        end = datetime.fromisoformat(body["window"]["end"])
        assert end - start == timedelta(hours=hours)
        for sql, args in calls:
            if "FROM core.identities" not in sql:
                assert args[0] == start
                assert args[-1] == limit + 1  # one extra row detects truncation


class TestFilters:
    def test_kinds_limits_which_sources_are_read(self, client):
        db, calls = _mock_db()
        body = _get(client, "?kinds=outcome", db=db).json()
        assert body["kinds"] == ["outcome"]
        assert {e["kind"] for e in body["events"]} == {"outcome"}
        assert not any("core.agent_state" in sql for sql, _ in calls)
        assert not any("audit.events" in sql for sql, _ in calls)

    def test_substrate_checkins_included_by_default(self, client):
        db, calls = _mock_db()
        _get(client, "?kinds=checkin", db=db)
        (args,) = [a for s, a in calls if "core.agent_state" in s]
        assert set(args[2]) == {
            "agent_report", "substrate_observation", "substrate_interpretation",
        }

    def test_include_substrate_false_reads_only_agent_reports(self, client):
        db, calls = _mock_db()
        _get(client, "?kinds=checkin&include_substrate=false", db=db)
        (args,) = [a for s, a in calls if "core.agent_state" in s]
        assert args[2] == ["agent_report"]

    def test_trajectory_outcomes_excluded_unless_asked(self, client):
        db, calls = _mock_db()
        _get(client, "?kinds=outcome", db=db)
        _get(client, "?kinds=outcome&include_trajectory=1", db=db)
        excluded = [a[2] for s, a in calls if "audit.outcome_events" in s]
        assert excluded == ["trajectory_validated", None]

    def test_agent_id_is_passed_to_every_source(self, client):
        db, calls = _mock_db()
        _get(client, f"?agent_id={PARENT}", db=db)
        for sql, args in calls:
            if "core.agent_state" in sql:
                assert args[1] == [1]  # the agent's identity rows
            elif "FROM core.identities" not in sql:
                assert args[1] == PARENT

    def test_unknown_agent_reads_no_checkins(self, client):
        db, calls = _mock_db()
        body = _get(client, "?agent_id=00000000-0000-4000-8000-000000000000", db=db).json()
        assert not any("core.agent_state" in s for s, _ in calls)
        assert body["counts"]["checkin"] == 0


class TestResponseShape:
    def test_events_merged_oldest_first_with_fields(self, client):
        body = _get(client).json()
        assert body["success"] is True
        stamps = [e["ts"] for e in body["events"]]
        assert stamps == sorted(stamps)
        assert body["counts"] == {
            "checkin": 2, "outcome": 1, "lifecycle": 1,
            "knowledge": 1, "lineage": 1, "dialectic": 1,
        }
        by_kind = {}
        for e in body["events"]:
            by_kind.setdefault(e["kind"], []).append(e)

        authored = {e["authored_by"] for e in by_kind["checkin"]}
        assert authored == {"agent", "substrate"}
        substrate, agent = by_kind["checkin"]  # oldest first
        assert (agent["state_id"], agent["report_excerpt"]) == (11, "Opened PR #3045.")
        assert agent["confidence_reported"] == 0.7  # the jsonb text, as a number
        assert (substrate["state_id"], substrate["report_excerpt"]) == (10, None)
        assert "confidence_reported" not in substrate  # no self-report recorded

        (outcome,) = by_kind["outcome"]
        assert outcome["reported_confidence"] == 0.9
        assert outcome["is_bad"] is True

        (pause,) = by_kind["lifecycle"]
        assert pause["event_type"] == "lifecycle_paused"
        assert pause["reason"] == "risk over threshold"  # payload given as a JSON string

        (knowledge,) = by_kind["knowledge"]
        assert len(knowledge["summary"]) == 280
        assert knowledge["summary"].endswith("…")

        (lineage,) = by_kind["lineage"]
        assert lineage["agent_id"] == CHILD
        assert lineage["parent_agent_id"] == PARENT

        (dialectic,) = by_kind["dialectic"]
        assert dialectic["end"] is not None

    def test_agents_lookup_covers_every_named_agent(self, client):
        body = _get(client).json()
        checkins = [e for e in body["events"] if e["kind"] == "checkin"]
        assert {e["agent_id"] for e in checkins} == {PARENT}  # from identity_id
        assert [a["agent_id"] for a in body["agents"]] == sorted([PARENT, CHILD])
        child = next(a for a in body["agents"] if a["agent_id"] == CHILD)
        assert child["parent_agent_id"] == PARENT
        assert child["tags"] == []
        parent = next(a for a in body["agents"] if a["agent_id"] == PARENT)
        assert parent["tags"] == ["persistent", "autonomous"]
        assert parent["host_session"] == {
            "harness": "claude-code", "session_id": "sess-1", "resumable": True,
        }
        assert child["host_session"] is None

    def test_active_dialectic_has_no_end(self, client):
        rows = _rows()
        rows["dialectic"][0]["status"] = "active"
        db, _ = _mock_db(rows=rows)
        (d,) = [e for e in _get(client, db=db).json()["events"] if e["kind"] == "dialectic"]
        assert d["end"] is None

    def test_only_a_kind_over_the_limit_is_truncated(self, client):
        db, _ = _mock_db()
        body = _get(client, "?limit=1", db=db).json()
        assert "checkin" in body["truncated"]  # two rows at limit 1
        assert body["counts"]["checkin"] == 1
        assert "outcome" not in body["truncated"]  # exactly one row is complete

    def test_parent_match_applies_to_lineage_only(self, client):
        db, calls = _mock_db()
        _get(client, f"?agent_id={PARENT}", db=db)
        audit = {a[2][0]: a[3] for s, a in calls if "audit.events" in s}
        assert audit["lineage_declared"] is True
        assert audit["knowledge_write"] is False
        assert audit["lifecycle_paused"] is False

    def test_empty_window_is_a_live_empty_answer(self, client):
        empty = {k: [] for k in _rows()}
        db, _ = _mock_db(rows=empty)
        body = _get(client, db=db).json()
        assert body["success"] is True
        assert body["events"] == []
        assert body["agents"] == []
        assert body["truncated"] == []

    def test_db_error_is_500_without_detail_leak(self, client):
        db, _ = _mock_db(error=RuntimeError("connection refused at 10.0.0.5"))
        r = _get(client, db=db)
        assert r.status_code == 500
        assert r.json() == {"success": False, "error": "query failed"}
        assert "10.0.0.5" not in r.text


# ---------------------------------------------------------------------------
# Integration: real SQL against governance_test
# ---------------------------------------------------------------------------

def _request(query: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/timeline",
        "query_string": query.encode(),
        "headers": [(b"authorization", f"Bearer {API_TOKEN}".encode())],
        "client": ("203.0.113.9", 40000),
    })


_SEED_SQL = f"""
INSERT INTO core.agents (id, api_key) VALUES ('{PARENT}', 'k1'), ('{CHILD}', 'k2');

INSERT INTO core.identities (agent_id, api_key_hash, metadata, parent_agent_id, spawn_reason)
VALUES ('{PARENT}', 'h1', '{{"label": "parent-agent", "trust_tier": {{"name": "established"}},
                              "tags": ["persistent", "autonomous"],
                              "host_session": {{"harness": "codex", "session_id": "s-9",
                                                "workspace": "/srv/work"}}}}'::jsonb, NULL, NULL),
       ('{CHILD}', 'h2', '{{"tags": "not-a-list"}}'::jsonb, '{PARENT}', 'subagent');

INSERT INTO core.agent_state (identity_id, recorded_at, state_json, epistemic_class, synthetic, risk_score)
SELECT identity_id, now() - interval '10 minutes',
       '{{"action": "guide", "verdict": "safe", "report_excerpt": "Opened PR #3045.",
          "eisv_telemetry": {{"self_report": {{"confidence_reported": 0.7}}}}}}'::jsonb,
       'agent_report', false, 0.4
FROM core.identities WHERE agent_id = '{PARENT}'
UNION ALL
SELECT identity_id, now() - interval '20 minutes',
       '{{"action": "approve", "verdict": "safe",
          "eisv_telemetry": {{"self_report": null}}}}'::jsonb, 'substrate_interpretation', false, NULL
FROM core.identities WHERE agent_id = '{PARENT}'
UNION ALL
SELECT identity_id, now() - interval '30 minutes', '{{}}'::jsonb, 'agent_report', true, NULL
FROM core.identities WHERE agent_id = '{PARENT}'
UNION ALL
SELECT identity_id, now() - interval '3 days', '{{}}'::jsonb, 'agent_report', false, NULL
FROM core.identities WHERE agent_id = '{PARENT}';

INSERT INTO audit.outcome_events (ts, agent_id, outcome_type, is_bad, outcome_score, verification_source, detail)
VALUES (now() - interval '5 minutes', '{PARENT}', 'test_failed', true, 0.0, 'external_signal',
        '{{"reported_confidence": 0.9, "prediction_id": "p-1"}}'::jsonb),
       (now() - interval '6 minutes', '{PARENT}', 'trajectory_validated', false, 1.0, 'server_observation',
        '{{}}'::jsonb);

INSERT INTO audit.events (ts, agent_id, session_id, event_type, payload, raw_hash)
VALUES (now() - interval '40 minutes', '{PARENT}', '{PARENT}', 'lifecycle_paused',
        '{{"reason": "risk over threshold"}}'::jsonb, 'tl-1'),
       (now() - interval '35 minutes', '{PARENT}', '{PARENT}', 'knowledge_write',
        '{{"summary": "parser bug", "discovery_id": "d-1", "discovery_type": "note"}}'::jsonb, 'tl-2'),
       (now() - interval '45 minutes', '{CHILD}', '{CHILD}', 'lineage_declared',
        '{{"parent_id": "{PARENT}", "successor_class": "ephemeral"}}'::jsonb, 'tl-3'),
       (now() - interval '15 minutes', '{PARENT}', '{PARENT}', 'auto_attest',
        '{{"decision": "proceed"}}'::jsonb, 'tl-4'),
       (now() - interval '8 minutes', '{CHILD}', '{CHILD}', 'lifecycle_archived',
        '{{"reason": "subagent returned", "parent_id": "{PARENT}"}}'::jsonb, 'tl-5');

INSERT INTO core.dialectic_sessions
    (session_id, paused_agent_id, reviewer_agent_id, status, phase, created_at, updated_at)
VALUES ('tl-session', '{PARENT}', '{CHILD}', 'resolved', 'resolved',
        now() - interval '25 minutes', now() - interval '12 minutes');
"""


@pytest.mark.asyncio
async def test_sql_against_live_db(live_postgres_backend, monkeypatch):
    """Seeded rows in every source → the handler's own SQL picks the right ones.

    Excluded on purpose: the synthetic check-in, the 3-day-old check-in, the
    trajectory_validated outcome and the auto_attest event (not a timeline
    kind). Filtering to the parent still returns the child's lineage row.
    """
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)

    be = live_postgres_backend
    async with be.acquire() as conn:
        await conn.execute(_SEED_SQL)

    with patch("src.db.get_db", return_value=be):
        response = await http_timeline(_request("hours=6"))
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["counts"] == {
        "checkin": 2, "outcome": 1, "lifecycle": 2,
        "knowledge": 1, "lineage": 1, "dialectic": 1,
    }
    events = body["events"]
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    checkins = [e for e in events if e["kind"] == "checkin"]
    assert {(e["authored_by"], e["action"]) for e in checkins} == {
        ("agent", "guide"), ("substrate", "approve"),
    }
    substrate, agent = checkins
    assert isinstance(agent["state_id"], int) and agent["state_id"] != substrate["state_id"]
    assert agent["report_excerpt"] == "Opened PR #3045."
    assert agent["confidence_reported"] == pytest.approx(0.7)
    # Its telemetry stores "self_report": null (no self-report), so the field is omitted.
    assert substrate["report_excerpt"] is None and "confidence_reported" not in substrate
    (outcome,) = [e for e in events if e["kind"] == "outcome"]
    assert outcome["outcome_type"] == "test_failed"
    assert outcome["reported_confidence"] == pytest.approx(0.9)
    assert outcome["prediction_id"] == "p-1"
    (pause,) = [e for e in events if e["kind"] == "lifecycle" and e["agent_id"] == PARENT]
    assert pause["reason"] == "risk over threshold"
    (dialectic,) = [e for e in events if e["kind"] == "dialectic"]
    assert dialectic["end"] is not None

    parent = next(a for a in body["agents"] if a["agent_id"] == PARENT)
    assert parent["label"] == "parent-agent"
    assert parent["trust_tier"] == "established"
    assert parent["tags"] == ["persistent", "autonomous"]
    child = next(a for a in body["agents"] if a["agent_id"] == CHILD)
    assert child["parent_agent_id"] == PARENT
    assert child["tags"] == []  # a non-array tags value reads as none
    # The workspace is stored but never served: only whether one exists.
    assert parent["host_session"] == {"harness": "codex", "session_id": "s-9", "resumable": True}
    assert "/srv/work" not in json.dumps(body)
    assert child["host_session"] is None

    with patch("src.db.get_db", return_value=be):
        filtered = json.loads((await http_timeline(
            _request(f"agent_id={PARENT}&include_substrate=false&include_trajectory=true")
        )).body)
    assert filtered["counts"]["checkin"] == 1
    assert filtered["counts"]["outcome"] == 2
    assert filtered["counts"]["lineage"] == 1  # the child's row names the parent
    # The child's archive carries parent_id too, but it is the child's event.
    assert filtered["counts"]["lifecycle"] == 1

    with patch("src.db.get_db", return_value=be):
        child_view = json.loads((await http_timeline(_request(f"agent_id={CHILD}"))).body)
    assert child_view["counts"]["dialectic"] == 1  # matched as reviewer
    assert child_view["counts"]["lifecycle"] == 1
