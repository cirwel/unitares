"""Scheduled synthesis and the consolidation_hint wire (registry Theme 2).

The store path's consolidation_hint must reach the scheduled pass, the pass
must stay bounded, and the default mode must preview without writing. No live
DB or LLM: the graph is a fake and the densest-topic sweep is patched out.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Dict, List

import pytest

from src import kg_synthesis_schedule as sched
from src.knowledge_graph import DiscoveryNode


@pytest.fixture(autouse=True)
def _clean_queue(monkeypatch):
    monkeypatch.delenv(sched.MODE_ENV, raising=False)
    monkeypatch.delenv(sched.INTERVAL_ENV, raising=False)
    sched._hint_queue.clear()
    sched._hint_failures.clear()
    sched._last_run = None
    yield
    sched._hint_queue.clear()
    sched._hint_failures.clear()
    sched._last_run = None


def _disc(did: str, tags: List[str], status: str = "open") -> DiscoveryNode:
    return DiscoveryNode(id=did, agent_id="a1", type="note", summary=f"s {did}", tags=tags, status=status)


class FakeGraph:
    def __init__(self, members_by_tag: Dict[str, List[DiscoveryNode]]):
        self._members = members_by_tag
        self.added: List[DiscoveryNode] = []

    async def query(self, tags=None, limit=50, exclude_archived=False, **kwargs):
        return list(self._members.get(tags[0] if tags else None, []))

    async def add_discovery(self, discovery):
        self.added.append(discovery)


@pytest.fixture
def no_sweep(monkeypatch):
    calls = []

    async def fake_topics(graph, **kwargs):
        calls.append(kwargs)
        return {"topics_considered": 0, "rollups": [], "errors": []}

    monkeypatch.setattr("src.mcp_handlers.knowledge.synthesis.synthesize_topics", fake_topics)
    return calls


def test_default_mode_is_shadow_and_bad_values_fall_back(monkeypatch):
    assert sched.schedule_mode() == "shadow"
    monkeypatch.setenv(sched.MODE_ENV, "APPLY")
    assert sched.schedule_mode() == "apply"
    monkeypatch.setenv(sched.MODE_ENV, "yes-please")
    assert sched.schedule_mode() == "shadow"
    monkeypatch.setenv(sched.INTERVAL_ENV, "-3")
    assert sched.interval_hours() == sched.DEFAULT_INTERVAL_HOURS


def test_hint_topics_prefers_tags_shared_with_duplicates_and_drops_lane_tags():
    topics = sched.hint_topics(
        ["retrieval", "Redis", "source-claude-memory", "ephemeral"],
        [["redis", "cache"], ["redis"], ["retrieval"]],
    )
    assert topics == ["redis", "retrieval"]
    # Nothing shared: the finding's own semantic tags name the topic.
    assert sched.hint_topics(["identity", "host-mac"], [["other"]]) == ["identity"]


def test_hint_queue_is_bounded_deduplicated_and_off_mode_ignores_hints(monkeypatch):
    for i in range(sched.MAX_HINT_QUEUE + 5):
        sched.record_consolidation_hint([f"t{i}"], discovery_id=f"d{i}")
    sched.record_consolidation_hint(["t10"], discovery_id="d-again")
    pending = sched.pending_hints()
    assert len(pending) == sched.MAX_HINT_QUEUE
    assert "t0" not in pending  # oldest evicted first
    assert pending["t10"] == ["d10", "d-again"]
    assert list(pending)[-1] == "t10"  # a repeat hint moves to the back

    sched._hint_queue.clear()
    monkeypatch.setenv(sched.MODE_ENV, "off")
    assert sched.record_consolidation_hint(["x"], discovery_id="d") == []
    assert sched.pending_hints() == {}


def test_shadow_cycle_previews_hinted_topics_without_writing(no_sweep):
    graph = FakeGraph({"redis": [_disc(f"2026-01-0{i}", ["redis"]) for i in range(1, 5)]})
    sched.record_consolidation_hint(["redis"], discovery_id="2026-01-04")

    report = asyncio.run(sched.run_synthesis_cycle(graph=graph))

    assert report["mode"] == "shadow" and report["dry_run"] is True
    assert graph.added == []
    assert report["hinted_topics"] == ["redis"]
    assert report["rollups_previewed"] == 1 and report["rollups_written"] == 0
    assert report["rollups"][0]["trigger"] == "consolidation_hint"
    assert report["rollups"][0]["summary_source"] == "deterministic"  # shadow never calls the LLM
    assert sched.pending_hints() == {}  # drained
    assert no_sweep and no_sweep[0]["dry_run"] is True and no_sweep[0]["use_llm"] is False
    assert sched.last_run_report()["hinted_topics"] == ["redis"]


def test_apply_cycle_writes_rollup(no_sweep, monkeypatch):
    async def no_llm(*args, **kwargs):
        return None

    monkeypatch.setattr("src.mcp_handlers.knowledge.synthesis.call_local_llm_structured", no_llm)
    graph = FakeGraph({"redis": [_disc(f"2026-01-0{i}", ["redis"]) for i in range(1, 5)]})
    sched.record_consolidation_hint(["redis"])

    report = asyncio.run(sched.run_synthesis_cycle("apply", graph=graph))

    assert report["rollups_written"] == 1
    assert [d.id for d in graph.added] == ["rollup::redis"]
    assert no_sweep[0]["dry_run"] is False


def test_cycle_drains_a_bounded_number_of_hints(no_sweep):
    graph = FakeGraph({})
    for i in range(sched.MAX_HINTED_TOPICS_PER_RUN + 3):
        sched.record_consolidation_hint([f"topic{i}"])

    report = asyncio.run(sched.run_synthesis_cycle(graph=graph))

    assert len(report["hinted_topics"]) == sched.MAX_HINTED_TOPICS_PER_RUN
    assert len(sched.pending_hints()) == 3  # left for the next cycle


def test_failing_topic_is_isolated_and_reported(no_sweep):
    class BrokenGraph(FakeGraph):
        async def query(self, tags=None, **kwargs):
            if tags == ["bad"]:
                raise RuntimeError("graph down")
            return await super().query(tags=tags, **kwargs)

    graph = BrokenGraph({"good": [_disc(f"2026-01-0{i}", ["good"]) for i in range(1, 4)]})
    sched.record_consolidation_hint(["bad", "good"])

    report = asyncio.run(sched.run_synthesis_cycle(graph=graph))

    assert report["errors"] == [{"topic": "bad", "error": "graph down"}]
    assert [r["topic"] for r in report["rollups"]] == ["good"]


class FakeTopicDB:
    """Densest-topic candidates in rank order; honours ``limit`` like the real query."""

    def __init__(self, topics: List[str]):
        self.topics = topics
        self.limits: List[int] = []

    async def kg_topic_candidates(self, *, min_members, limit, **kwargs):
        self.limits.append(limit)
        return [{"topic": t, "member_count": 5} for t in self.topics[:limit]]


def _no_llm(monkeypatch):
    async def no_llm(*args, **kwargs):
        return None

    monkeypatch.setattr("src.mcp_handlers.knowledge.synthesis.call_local_llm_structured", no_llm)


def test_sweep_never_resynthesizes_a_topic_the_hints_covered(monkeypatch):
    # Review finding (PR #2948): a hinted topic that is also among the densest
    # was synthesized and written twice per apply cycle, and the report hid it.
    _no_llm(monkeypatch)
    db = FakeTopicDB(["redis", "cache"])
    monkeypatch.setattr("src.db.get_db", lambda: db)
    members = {t: [_disc(f"2026-01-0{i}-{t}", [t]) for i in range(1, 5)] for t in ("redis", "cache")}
    graph = FakeGraph(members)
    sched.record_consolidation_hint(["redis"])

    report = asyncio.run(sched.run_synthesis_cycle("apply", graph=graph))

    assert [d.id for d in graph.added] == ["rollup::redis", "rollup::cache"]
    assert [(r["topic"], r["trigger"]) for r in report["rollups"]] == [
        ("redis", "consolidation_hint"), ("cache", "sweep"),
    ]
    assert report["rollups_written"] == 2
    assert report["topics_considered"] == 2


def test_synthesize_topics_skip_still_fills_the_limit(monkeypatch):
    from src.mcp_handlers.knowledge.synthesis import synthesize_topics

    db = FakeTopicDB(["a", "b", "c", "d"])
    monkeypatch.setattr("src.db.get_db", lambda: db)
    graph = FakeGraph({t: [_disc(f"2026-01-0{i}-{t}", [t]) for i in range(1, 4)] for t in "abcd"})

    out = asyncio.run(synthesize_topics(graph, limit=2, use_llm=False, dry_run=True, skip_topics=["a"]))

    assert [r["topic"] for r in out["rollups"]] == ["b", "c"]
    assert out["topics_considered"] == 2
    assert db.limits == [3]


def test_failed_hint_is_retried_next_cycle_then_dropped(no_sweep):
    # Review finding (PR #2948): a hint drained and then failed was discarded,
    # and the densest-topic sweep need never pick its topic up again.
    class BrokenGraph(FakeGraph):
        async def query(self, tags=None, **kwargs):
            raise RuntimeError("db down")

    graph = BrokenGraph({})
    sched.record_consolidation_hint(["flaky"], discovery_id="d1")

    for attempt in range(1, sched.MAX_HINT_ATTEMPTS):
        report = asyncio.run(sched.run_synthesis_cycle(graph=graph))
        assert report["hinted_topics"] == ["flaky"]
        assert report["requeued_topics"] == ["flaky"]
        assert sched.pending_hints() == {"flaky": ["d1"]}
    # The sweep is told to leave a failed hinted topic for the retry.
    assert all(call["skip_topics"] == ["flaky"] for call in no_sweep)

    report = asyncio.run(sched.run_synthesis_cycle(graph=graph))
    assert report["requeued_topics"] == []
    assert sched.pending_hints() == {}


def test_recovered_hint_resets_its_failure_count(no_sweep):
    class FlakyGraph(FakeGraph):
        fail = True

        async def query(self, tags=None, **kwargs):
            if FlakyGraph.fail:
                raise RuntimeError("db down")
            return await super().query(tags=tags, **kwargs)

    graph = FlakyGraph({"flaky": [_disc(f"2026-01-0{i}", ["flaky"]) for i in range(1, 4)]})
    sched.record_consolidation_hint(["flaky"])
    asyncio.run(sched.run_synthesis_cycle(graph=graph))
    FlakyGraph.fail = False
    report = asyncio.run(sched.run_synthesis_cycle(graph=graph))

    assert [r["topic"] for r in report["rollups"]] == ["flaky"]
    assert sched._hint_failures == {}


def test_cycle_does_not_wrap_db_work_in_wait_for(monkeypatch, no_sweep):
    # Review finding (PR #2948): the house rule forbids asyncio.wait_for around
    # ExecutorPool-backed asyncpg work; asyncpg's command_timeout bounds each
    # statement instead, and an aggregate sweep timeout cancelled every topic
    # after a stuck first one.
    wrapped = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(aw, *args, **kwargs):
        code = getattr(aw, "cr_code", None)
        wrapped.append(code.co_name if code else repr(aw))
        return await real_wait_for(aw, *args, **kwargs)

    monkeypatch.setattr(sched.asyncio, "wait_for", spy_wait_for)
    graph = FakeGraph({"redis": [_disc(f"2026-01-0{i}", ["redis"]) for i in range(1, 5)]})
    sched.record_consolidation_hint(["redis"])

    report = asyncio.run(sched.run_synthesis_cycle(graph=graph))

    assert report["errors"] == []
    assert not [name for name in wrapped if name.startswith("synthesize_topic")]


def test_background_task_returns_immediately_when_off(monkeypatch):
    monkeypatch.setenv(sched.MODE_ENV, "off")
    asyncio.run(asyncio.wait_for(sched.kg_synthesis_background_task(), timeout=1.0))


def test_background_task_runs_a_cycle_after_the_startup_delay(monkeypatch):
    ran = []

    async def fake_cycle(mode=None, graph=None):
        ran.append(mode)
        return {
            "mode": mode, "dry_run": True, "hinted_topics": [], "topics_considered": 0,
            "rollups_previewed": 0, "rollups_written": 0, "rollups": [], "errors": [],
        }

    monkeypatch.setattr(sched, "run_synthesis_cycle", fake_cycle)

    async def main():
        task = asyncio.create_task(sched.kg_synthesis_background_task(startup_delay=0))
        for _ in range(20):
            await asyncio.sleep(0)
            if ran:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert ran == ["shadow"]


def test_store_consolidation_hint_queues_topics():
    from src.mcp_handlers.knowledge import handlers

    similar = [_disc(f"2026-01-0{i}", ["redis", "cache"]) for i in range(1, 4)]
    state = SimpleNamespace(
        similar=similar,
        similar_discoveries=[d.to_dict(include_details=False) for d in similar],
        discovery=_disc("2026-01-09", ["redis", "latency"]),
    )
    response: dict = {}

    handlers._attach_store_related_discoveries(response, state)

    assert "consolidation_hint" in response
    assert sched.pending_hints() == {"redis": ["2026-01-09"]}


def test_store_below_hint_threshold_queues_nothing():
    from src.mcp_handlers.knowledge import handlers

    similar = [_disc("2026-01-01", ["redis"]), _disc("2026-01-02", ["redis"], status="resolved")]
    state = SimpleNamespace(
        similar=similar,
        similar_discoveries=[d.to_dict(include_details=False) for d in similar],
        discovery=_disc("2026-01-09", ["redis"]),
    )
    response: dict = {}

    handlers._attach_store_related_discoveries(response, state)

    assert "consolidation_hint" not in response
    assert sched.pending_hints() == {}
