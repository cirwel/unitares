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
        "details": {"host_family": host, "observed_at": at.isoformat()},
    }


def _probe(compacted, at, answer, *, answered_by=None, posted=None):
    return {
        "agent_id": answered_by or compacted,
        "timestamp": (posted or at + timedelta(minutes=1)).isoformat(),
        "event_id": f"p-{compacted}-{answer}",
        "details": {
            "probe_answer": answer,
            "probe_version": "v1",
            "compaction_agent_uuid": compacted,
            "compaction_observed_at": at.isoformat(),
            "guidance_present": True,
        },
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
    quiet_agent = "99999999-8888-4777-8666-555555555555"
    compactions = [
        _compaction(PARENT, T0, event_id="minted"),
        _compaction(kept_agent, T0, host="codex", event_id="kept"),
        _compaction(quiet_agent, T0, event_id="quiet"),
    ]
    probes = [_probe(PARENT, T0, "same_process", answered_by="child")]
    seen = {}

    async def query(**kwargs):
        seen.setdefault("queries", []).append(kwargs["event_types"])
        if kwargs["event_types"] == ["runtime_observation.context_compaction_probe"]:
            return probes
        return compactions

    async def aggregate(agent_id=None, start_time=None, event_types=None, **_):
        seen.setdefault("aggregate_types", event_types)
        # Rows other callers or detectors write against an identity must not
        # count as it acting again; its own knowledge write does. The fake
        # returns them all to check the collector filters, not just the query.
        rows = [
            {"event_type": "agent_archived", "last_ts": T0 + timedelta(hours=3)},
            {"event_type": "identity_hijack_suspected", "last_ts": T0 + timedelta(hours=3)},
            {"event_type": "runtime_observation.context_compaction",
             "last_ts": T0 + timedelta(hours=3)},
            # Someone else listing this identity's knowledge logs a read
            # against it; that is not the identity acting.
            {"event_type": "knowledge_read", "last_ts": T0 + timedelta(hours=3)},
        ]
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

    assert seen["queries"] == [
        ["runtime_observation.context_compaction"],
        ["runtime_observation.context_compaction_probe"],
    ]
    assert set(seen["aggregate_types"]) == {"auto_attest", "knowledge_write", "consultation"}
    assert report["complete"] is True
    assert seen["children_args"][0] == sorted([PARENT, kept_agent, quiet_agent])
    by_event = {row["event_id"]: row for row in report["rows"]}
    assert by_event["minted"]["outcome"] == "minted"
    assert by_event["kept"]["outcome"] == "kept"
    # Only rows others or detectors wrote against it: not kept.
    assert by_event["quiet"]["outcome"] == "no_later_activity"
    assert report["by_host"]["codex"]["kept"] == 1
    # The answer reaches its compaction even though the minted child posted it.
    assert by_event["minted"]["probe"]["answer"] == "same_process"
    assert by_event["minted"]["probe"]["answered_by"] == "child"
    assert by_event["kept"]["probe"] is None
    assert report["probe"]["inconsistent"] == 1
    assert report["probe"]["answered_by_successor"] == 1
    assert report["probe"]["complete"] is True




def test_compactions_at_the_cutoff_are_left_out_not_misclassified():
    through = T0 + timedelta(hours=1)
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0, event_id="read"), _compaction(OTHER, through, event_id="cut")],
        [_child("c", OTHER, through + timedelta(minutes=1))],
        {},
        through=through,
    )
    assert [row["event_id"] for row in report["rows"]] == ["read"]
    assert report["excluded_at_cutoff"] == 1
    assert report["complete"] is False


def test_an_incomplete_read_does_not_credit_a_later_child_to_the_last_compaction_read():
    through = T0 + timedelta(hours=1)
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0)],
        [_child("late", PARENT, T0 + timedelta(hours=2))],
        {PARENT: T0 + timedelta(minutes=30)},
        through=through,
    )
    assert report["rows"][0]["outcome"] == "kept"
    assert report["complete"] is False


@pytest.mark.asyncio
async def test_collector_pages_through_every_compaction(monkeypatch):
    import src.audit_db as audit_db
    import src.db as db_module
    from src.identity.compaction_outcomes import collect_compaction_outcomes

    rows = [
        _compaction(PARENT, T0 + timedelta(minutes=i), event_id=f"e{i}") for i in range(5)
    ]
    calls = []

    async def query(start_time=None, limit=None, **_):
        calls.append(start_time)
        start = datetime.fromisoformat(start_time)
        # Inclusive start, like the real query: the boundary row repeats.
        return [r for r in rows if datetime.fromisoformat(r["timestamp"]) >= start][:limit]

    async def aggregate(**_):
        return []

    class _Conn:
        async def fetch(self, *a):
            return []

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

    report = await collect_compaction_outcomes(since=T0 - timedelta(days=1), page_size=2)

    assert report["compactions"] == 5
    assert sorted(row["event_id"] for row in report["rows"]) == [f"e{i}" for i in range(5)]
    assert report["complete"] is True
    assert len(calls) > 1


def test_probe_answers_sit_beside_outcomes_and_consistency_is_counted():
    kept, minted = PARENT, OTHER
    unsure = "99999999-8888-4777-8666-555555555555"
    report = classify_compaction_outcomes(
        [_compaction(kept, T0), _compaction(minted, T0), _compaction(unsure, T0)],
        [_child("c", minted, T0 + timedelta(minutes=2))],
        {kept: T0 + timedelta(hours=1), unsure: T0 + timedelta(hours=1)},
        probes=[
            _probe(kept, T0, "same_process"),
            _probe(minted, T0, "same_process", answered_by="c"),
            _probe(unsure, T0, "cannot_tell"),
        ],
    )
    probe = report["probe"]
    assert probe["asked"] == 3
    assert probe["answers"] == {
        "same_process": 2, "new_process": 0, "cannot_tell": 1, "no_answer": 0,
    }
    assert probe["by_answer_and_outcome"]["same_process"] == {
        "minted": 1, "kept": 1, "no_later_activity": 0,
    }
    # cannot_tell is counted, never scored either way.
    assert (probe["decisive_pairs"], probe["consistent"], probe["inconsistent"]) == (2, 1, 1)
    assert probe["answered_by_successor"] == 1


def test_a_probe_attaches_only_to_the_compaction_it_names():
    later = T0 + timedelta(hours=2)
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0, event_id="first"), _compaction(PARENT, later, event_id="second")],
        [],
        {PARENT: later + timedelta(minutes=5)},
        probes=[
            _probe(PARENT, later, "new_process"),
            # Another identity's compaction at the same moment is not this one.
            _probe(OTHER, T0, "same_process"),
        ],
    )
    by_event = {row["event_id"]: row for row in report["rows"]}
    assert by_event["first"]["probe"] is None
    assert by_event["second"]["probe"]["answer"] == "new_process"
    assert (report["probe"]["consistent"], report["probe"]["inconsistent"]) == (0, 1)


def test_only_the_first_answer_to_a_compaction_is_read():
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0)],
        [],
        {PARENT: T0 + timedelta(hours=1)},
        probes=[
            _probe(PARENT, T0, "new_process", posted=T0 + timedelta(minutes=9)),
            _probe(PARENT, T0, "same_process", posted=T0 + timedelta(minutes=1)),
            {"agent_id": PARENT, "timestamp": T0.isoformat(), "details": {"probe_answer": "same_process"}},
        ],
    )
    assert report["rows"][0]["probe"]["answer"] == "same_process"
    assert report["probe"]["duplicates_ignored"] == 1
    assert report["probe"]["asked"] == 1


def test_no_probes_leaves_every_row_unasked():
    report = classify_compaction_outcomes([_compaction(PARENT, T0)], [], {})
    assert report["rows"][0]["probe"] is None
    assert report["probe"]["asked"] == 0
    assert report["probe"]["decisive_pairs"] == 0


def test_an_unrelated_identity_cannot_answer_for_a_compaction():
    # Any bound identity can post a probe naming any compaction; only the
    # compacted identity or a child it minted counts.
    intruder = "99999999-8888-4777-8666-555555555555"
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0)],
        [],
        {PARENT: T0 + timedelta(hours=1)},
        probes=[
            _probe(PARENT, T0, "new_process", answered_by=intruder,
                   posted=T0 + timedelta(seconds=5)),
            _probe(PARENT, T0, "same_process", posted=T0 + timedelta(minutes=1)),
        ],
    )
    assert report["rows"][0]["probe"]["answer"] == "same_process"
    assert report["rows"][0]["probe"]["answered_by"] == PARENT
    assert report["probe"]["unrelated_ignored"] == 1
    assert report["probe"]["duplicates_ignored"] == 0


def test_a_child_that_is_not_this_compactions_successor_cannot_answer():
    # A compaction child minted after the NEXT compaction belongs to that one.
    later = T0 + timedelta(hours=2)
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0, event_id="first"), _compaction(PARENT, later, event_id="second")],
        [_child("late-child", PARENT, later + timedelta(minutes=1))],
        {PARENT: later + timedelta(minutes=30)},
        probes=[_probe(PARENT, T0, "new_process", answered_by="late-child")],
    )
    by_event = {row["event_id"]: row for row in report["rows"]}
    assert by_event["first"]["probe"] is None
    assert report["probe"]["unrelated_ignored"] == 1


def test_an_answer_naming_no_classified_compaction_is_counted_not_lost():
    report = classify_compaction_outcomes(
        [_compaction(PARENT, T0)],
        [],
        {PARENT: T0 + timedelta(hours=1)},
        probes=[_probe(PARENT, T0 + timedelta(seconds=3), "same_process")],
    )
    assert report["rows"][0]["probe"] is None
    assert report["probe"]["unmatched_ignored"] == 1
    assert report["probe"]["asked"] == 0
