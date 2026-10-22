"""Compaction outcome classification: minted, kept, or no later activity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.identity.compaction_outcomes import classify_compaction_outcomes


T0 = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)
PARENT = "7beb6c9f-6f18-4806-abf6-5212446be881"
OTHER = "11111111-2222-4333-8444-555555555555"


def _compaction(agent_id, at, host="claude", event_id="e"):
    return {
        "agent_id": agent_id,
        "timestamp": at.isoformat(),
        "event_id": event_id,
        "details": {"host_family": host},
    }


def _child(child_id, parent, created):
    return {"agent_id": child_id, "parent_agent_id": parent, "created_at": created}


def test_a_compaction_child_after_the_event_is_minted():
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0)],
        [_child("a2689be9-9c98-4831-b786-4ae5ba09a4f8", PARENT, T0 + timedelta(minutes=2))],
        {},
    )
    row = report["rows"][0]
    assert row["outcome"] == "minted"
    assert row["minted_children"] == ["a2689be9-9c98-4831-b786-4ae5ba09a4f8"]
    assert report["outcomes"] == {"minted": 1, "kept": 0, "no_later_activity": 0}
    assert report["by_host"]["claude"]["minted"] == 1


def test_later_activity_without_a_child_is_kept():
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0, host="codex")], [], {PARENT: T0 + timedelta(hours=1)}
    )
    assert report["rows"][0]["outcome"] == "kept"
    assert report["by_host"] == {"codex": {"minted": 0, "kept": 1, "no_later_activity": 0}}


def test_activity_only_before_the_compaction_is_no_later_activity():
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0)], [], {PARENT: T0 - timedelta(minutes=5)}
    )
    assert report["rows"][0]["outcome"] == "no_later_activity"


def test_a_child_created_before_the_compaction_does_not_count():
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0)],
        [_child("c1", PARENT, T0 - timedelta(minutes=1))],
        {PARENT: T0 + timedelta(minutes=1)},
    )
    assert report["rows"][0]["outcome"] == "kept"


def test_a_child_is_attributed_to_the_compaction_it_follows():
    second = T0 + timedelta(hours=2)
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0, event_id="first"), _compaction(PARENT, second, event_id="second")],
        [_child("c2", PARENT, second + timedelta(minutes=1))],
        {PARENT: second + timedelta(minutes=30)},
    )
    by_event = {row["event_id"]: row for row in report["rows"]}
    assert by_event["first"]["outcome"] == "kept"
    assert by_event["second"]["outcome"] == "minted"
    assert by_event["second"]["minted_children"] == ["c2"]


def test_another_agents_child_and_unusable_rows_are_ignored():
    report = classify_compaction_outcomes(
        [
            _compaction(PARENT, T0),
            {"agent_id": "", "timestamp": T0.isoformat()},
            {"agent_id": OTHER, "timestamp": "not a time"},
        ],
        [_child("c3", OTHER, T0 + timedelta(minutes=1))],
        {},
    )
    assert report["compactions"] == 1
    assert report["rows"][0]["outcome"] == "no_later_activity"


@pytest.mark.asyncio
async def test_collector_joins_children_and_audit_aggregates(monkeypatch):
    import src.audit_db as audit_db
    import src.db as db_module
    from src.identity.compaction_outcomes import collect_compaction_outcomes

    kept_agent = OTHER
    compactions = [
        _compaction(PARENT, T0, event_id="minted"),
        _compaction(kept_agent, T0, host="codex", event_id="kept"),
    ]
    seen = {}

    async def query(**kwargs):
        seen["query"] = kwargs
        return compactions

    async def aggregate(agent_id=None, start_time=None, **_):
        # A later runtime observation alone must not count as the identity
        # writing again; a later knowledge write does.
        rows = [{"event_type": "runtime_observation.context_compaction",
                 "last_ts": T0 + timedelta(hours=3)}]
        if agent_id == kept_agent:
            rows.append({"event_type": "knowledge_write", "last_ts": T0 + timedelta(hours=1)})
        return rows

    class _Conn:
        async def fetch(self, sql, agent_ids, since):
            seen["children_args"] = (sorted(agent_ids), since)
            return [{"agent_id": "child", "parent_agent_id": PARENT,
                     "created_at": T0 + timedelta(minutes=3)}]

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    class _DB:
        def acquire(self):
            return _Acquire()

    monkeypatch.setattr(audit_db, "query_audit_events_async", query)
    monkeypatch.setattr(audit_db, "aggregate_audit_events_async", aggregate)
    monkeypatch.setattr(db_module, "get_db", lambda: _DB())

    report = await collect_compaction_outcomes(since=T0 - timedelta(days=1))

    assert seen["query"]["event_types"] == ["runtime_observation.context_compaction"]
    assert seen["children_args"][0] == sorted([PARENT, kept_agent])
    by_event = {row["event_id"]: row for row in report["rows"]}
    assert by_event["minted"]["outcome"] == "minted"
    assert by_event["kept"]["outcome"] == "kept"
    assert report["by_host"]["codex"]["kept"] == 1
