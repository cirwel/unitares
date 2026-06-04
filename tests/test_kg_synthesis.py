"""Tests for on-demand knowledge-graph synthesis (Issue #1).

Covers the pure rollup-construction logic and the synthesis orchestration with a
fake graph backend — no live DB or LLM required. The synthesis pass must:
  * compound discrete discoveries into a topic rollup row (deterministic id),
  * persist rollups as ordinary discoveries so there is no schema change,
  * never let a rollup become a member of itself (no feedback loop),
  * degrade to a deterministic narrative when the LLM is unreachable,
  * skip topics below the member threshold, and isolate per-topic failures.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import synthesis as syn


def _disc(did: str, summary: str, tags: List[str], *, dtype: str = "note", status: str = "open") -> DiscoveryNode:
    return DiscoveryNode(id=did, agent_id="a1", type=dtype, summary=summary, tags=tags, status=status)


class FakeGraph:
    """Minimal backend stand-in: query-by-tag + record add_discovery calls."""

    def __init__(self, members_by_tag: Dict[str, List[DiscoveryNode]]):
        self._members = members_by_tag
        self.added: List[DiscoveryNode] = []

    async def query(self, tags=None, limit=50, exclude_archived=False, **kwargs) -> List[DiscoveryNode]:
        tag = tags[0] if tags else None
        return list(self._members.get(tag, []))

    async def add_discovery(self, discovery: DiscoveryNode) -> None:
        self.added.append(discovery)


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #

def test_rollup_id_is_deterministic_and_prefixed():
    assert syn.rollup_id("identity") == "rollup::identity"
    assert syn.rollup_id("identity") == syn.rollup_id("identity")


def test_is_rollup_detects_type_and_id():
    assert syn.is_rollup({"id": "rollup::x", "type": "topic_rollup"})
    assert syn.is_rollup({"id": "rollup::x", "type": "note"})  # id prefix alone
    assert syn.is_rollup({"id": "2026-01-01", "type": "topic_rollup"})  # type alone
    assert not syn.is_rollup({"id": "2026-01-01", "type": "note"})
    assert syn.is_rollup(_disc("rollup::y", "s", ["y"], dtype="topic_rollup"))


def test_extract_related_topics_excludes_self_and_ranks_by_frequency():
    members = [
        {"tags": ["auth", "redis", "cache"]},
        {"tags": ["auth", "redis"]},
        {"tags": ["auth", "latency"]},
    ]
    related = syn.extract_related_topics(members, "auth")
    assert "auth" not in related  # the topic itself is never its own cross-ref
    assert related[0] == "redis"  # most frequent co-occurrence first
    assert set(related) == {"redis", "cache", "latency"}


def test_build_deterministic_summary_carries_counts_and_members():
    members = [
        {"summary": "leak in pool", "type": "bug_found", "status": "open"},
        {"summary": "fixed pool", "type": "bug_fix", "status": "resolved"},
    ]
    text = syn.build_deterministic_summary("pool", members, ["redis"])
    assert "pool" in text
    assert "2 discoveries" in text
    assert "1 open" in text
    assert "redis" in text
    assert "leak in pool" in text


def test_make_rollup_node_shape():
    members = [_disc(f"d{i}", f"summary {i}", ["topicx"]).to_dict(include_details=False) for i in range(3)]
    node = syn._make_rollup_node("topicx", members, "A narrative.\nMore.", "llm", ["other"], writer_id="sys")
    assert node.id == "rollup::topicx"
    assert node.type == syn.ROLLUP_TYPE
    assert node.agent_id == "sys"
    assert "topicx" in node.tags and "rollup" in node.tags
    assert node.related_to == ["d0", "d1", "d2"]
    assert node.summary.startswith("[rollup] topicx:")
    assert "other" in node.details  # related topics surfaced in details
    assert node.provenance["synthesis"]["summary_source"] == "llm"
    assert node.provenance["source"] == "kg_synthesis"


def test_make_rollup_node_carries_staleness_watermark():
    # Member ids are UTC-ISO timestamps; the watermark must be the newest one
    # across ALL members (not just the capped/shown slice), so a reader can tell
    # whether the rollup is behind the topic's current newest discovery.
    members = [
        _disc("2026-01-01T00:00:00", "old", ["t"]).to_dict(include_details=False),
        _disc("2026-06-04T12:00:00", "newest", ["t"]).to_dict(include_details=False),
        _disc("2026-03-15T00:00:00", "mid", ["t"]).to_dict(include_details=False),
    ]
    node = syn._make_rollup_node("t", members, "n", "deterministic", [], writer_id="sys")
    synth = node.provenance["synthesis"]
    assert synth["newest_member_id"] == "2026-06-04T12:00:00"
    # synthesized_at is a parseable UTC-aware ISO timestamp.
    from datetime import datetime
    parsed = datetime.fromisoformat(synth["synthesized_at"])
    assert parsed.tzinfo is not None


def test_make_rollup_node_watermark_spans_beyond_member_cap():
    # The newest member can sit past MAX_MEMBERS_PER_ROLLUP in the input list;
    # the watermark must still find it even though related_to is capped.
    members = [
        _disc(f"2026-01-{i + 1:02d}T00:00:00", f"s{i}", ["t"]).to_dict(include_details=False)
        for i in range(syn.MAX_MEMBERS_PER_ROLLUP + 3)
    ]
    node = syn._make_rollup_node("t", members, "n", "deterministic", [], writer_id="sys")
    newest = max(m["id"] for m in members)
    assert node.provenance["synthesis"]["newest_member_id"] == newest
    assert len(node.related_to) == syn.MAX_MEMBERS_PER_ROLLUP  # edges still capped


def test_make_rollup_node_caps_related_to_at_max_members():
    many = [_disc(f"d{i}", f"s{i}", ["t"]).to_dict(include_details=False) for i in range(syn.MAX_MEMBERS_PER_ROLLUP + 5)]
    node = syn._make_rollup_node("t", many, "n", "deterministic", [], writer_id="sys")
    assert len(node.related_to) == syn.MAX_MEMBERS_PER_ROLLUP


# --------------------------------------------------------------------------- #
# Narrative generation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_generate_narrative_deterministic_when_llm_disabled():
    members = [{"summary": "x", "type": "note", "status": "open"}]
    text, source = await syn._generate_narrative("t", members, [], use_llm=False)
    assert source == "deterministic"
    assert "t" in text


@pytest.mark.asyncio
async def test_generate_narrative_uses_llm_when_available(monkeypatch):
    async def fake_llm(*args, **kwargs):
        return "  Compounded narrative.  "

    monkeypatch.setattr(syn, "call_local_llm", fake_llm)
    members = [{"summary": "x", "type": "note", "status": "open"}]
    text, source = await syn._generate_narrative("t", members, ["u"], use_llm=True)
    assert source == "llm"
    assert text == "Compounded narrative."  # stripped


@pytest.mark.asyncio
async def test_generate_narrative_falls_back_when_llm_returns_none(monkeypatch):
    async def fake_llm(*args, **kwargs):
        return None

    monkeypatch.setattr(syn, "call_local_llm", fake_llm)
    members = [{"summary": "x", "type": "note", "status": "open"}]
    text, source = await syn._generate_narrative("t", members, [], use_llm=True)
    assert source == "deterministic"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_synthesize_topic_skips_below_min_members():
    graph = FakeGraph({"thin": [_disc("d0", "s", ["thin"])]})
    report = await syn.synthesize_topic(graph, "thin", use_llm=False, min_members=3)
    assert report["action"] == "skipped"
    assert graph.added == []


@pytest.mark.asyncio
async def test_synthesize_topic_persists_and_excludes_existing_rollup():
    members = [_disc(f"d{i}", f"s{i}", ["auth"]) for i in range(3)]
    # An existing rollup row tagged 'auth' must not count as a member of itself.
    members.append(_disc("rollup::auth", "old rollup", ["auth"], dtype="topic_rollup"))
    graph = FakeGraph({"auth": members})

    report = await syn.synthesize_topic(graph, "auth", use_llm=False)
    assert report["action"] == "synthesized"
    assert report["member_count"] == 3  # rollup excluded
    assert len(graph.added) == 1
    written = graph.added[0]
    assert written.id == "rollup::auth"
    assert "rollup::auth" not in written.related_to


@pytest.mark.asyncio
async def test_synthesize_topic_dry_run_does_not_persist():
    members = [_disc(f"d{i}", f"s{i}", ["auth"]) for i in range(3)]
    graph = FakeGraph({"auth": members})
    report = await syn.synthesize_topic(graph, "auth", use_llm=False, dry_run=True)
    assert report["action"] == "previewed"
    assert graph.added == []


@pytest.mark.asyncio
async def test_synthesize_topics_single_topic_skips_db_candidates(monkeypatch):
    members = [_disc(f"d{i}", f"s{i}", ["auth"]) for i in range(3)]
    graph = FakeGraph({"auth": members})

    # If a single topic is named, the densest-topics DB aggregate must not run.
    def _boom():
        raise AssertionError("kg_topic_candidates should not be called for single-topic synthesis")

    monkeypatch.setattr("src.db.get_db", _boom)
    result = await syn.synthesize_topics(graph, topic="auth", use_llm=False)
    assert result["rollups_written"] == 1
    assert result["dry_run"] is False


@pytest.mark.asyncio
async def test_synthesize_topics_sweeps_candidates_and_isolates_errors(monkeypatch):
    good = [_disc(f"g{i}", f"s{i}", ["good"]) for i in range(3)]
    graph = FakeGraph({"good": good})  # "bad" tag absent -> query returns []

    class FakeDB:
        async def kg_topic_candidates(self, min_members, limit, exclude_types):
            assert syn.ROLLUP_TYPE in exclude_types
            return [{"topic": "good"}, {"topic": "bad"}]

    monkeypatch.setattr("src.db.get_db", lambda: FakeDB())

    # Make "bad" blow up inside synthesize_topic to prove failure isolation.
    real_topic = syn.synthesize_topic

    async def flaky(graph, topic, **kwargs):
        if topic == "bad":
            raise RuntimeError("kaboom")
        return await real_topic(graph, topic, **kwargs)

    monkeypatch.setattr(syn, "synthesize_topic", flaky)

    result = await syn.synthesize_topics(graph, use_llm=False)
    assert result["topics_considered"] == 2
    assert result["rollups_written"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["topic"] == "bad"
