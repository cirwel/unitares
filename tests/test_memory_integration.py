from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.identity.memory_integration import (
    score_memory_integration,
    score_memory_integration_batch,
    select_memory_integration_lineage_pairs,
)
from src.knowledge_graph import DiscoveryNode, ResponseTo


NOW = datetime(2026, 5, 6, tzinfo=timezone.utc)


class FakeKnowledgeGraph:
    def __init__(self, rows_by_agent=None, exc: Exception | None = None):
        self.rows_by_agent = rows_by_agent or {}
        self.exc = exc

    async def get_agent_discoveries(self, agent_id: str, limit: int | None = None):
        if self.exc:
            raise self.exc
        rows = list(self.rows_by_agent.get(agent_id, []))
        if limit is not None:
            return rows[:limit]
        return rows


class FakeAcquire:
    def __init__(self, rows):
        self.conn = FakeConnection(rows)

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        lineage_state = args[0]
        limit = args[1]
        rows = [
            row
            for row in self.rows
            if lineage_state == "all" or row["lineage_state"] == lineage_state
        ]
        return rows[:limit]


class FakeDB:
    def __init__(self, rows):
        self.acquire_context = FakeAcquire(rows)

    def acquire(self):
        return self.acquire_context


class SubscriptOnlyRow:
    """Small asyncpg.Record stand-in: supports row[key], not getattr/get."""

    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        return self.values[key]


def _discovery(
    discovery_id: str,
    agent_id: str,
    *,
    response_to: ResponseTo | None = None,
    status: str = "open",
) -> DiscoveryNode:
    return DiscoveryNode(
        id=discovery_id,
        agent_id=agent_id,
        type="insight",
        summary=f"{discovery_id} summary",
        timestamp="2026-05-05T00:00:00+00:00",
        status=status,
        response_to=response_to,
    )


def _parent_rows(count: int = 3) -> list[DiscoveryNode]:
    return [_discovery(f"p-{idx}", "parent") for idx in range(1, count + 1)]


@pytest.mark.asyncio
async def test_score_memory_integration_integrated_candidate():
    graph = FakeKnowledgeGraph(
        {
            "parent": _parent_rows(),
            "successor": [
                _discovery(
                    "s-1",
                    "successor",
                    response_to=ResponseTo("p-1", "extend"),
                ),
                _discovery(
                    "s-2",
                    "successor",
                    response_to=ResponseTo("p-2", "correction"),
                ),
                _discovery("s-3", "successor"),
            ],
        }
    )

    score = await score_memory_integration("parent", "successor", graph=graph, now=NOW)

    assert score.verdict == "integrated_candidate"
    assert score.parent_discoveries_seen == 3
    assert score.successor_discoveries_seen == 3
    assert score.cited_parent_discoveries == 2
    assert score.strong_extensions == 2
    assert score.weak_extensions == 0
    assert score.cited_discovery_ids == ["p-1", "p-2"]
    assert score.generated_discovery_ids == ["s-1", "s-2"]
    assert score.calibration_status == "seeded"
    assert score.to_dict()["verdict"] == "integrated_candidate"


@pytest.mark.asyncio
async def test_score_memory_integration_weak_signal():
    graph = FakeKnowledgeGraph(
        {
            "parent": _parent_rows(),
            "successor": [
                _discovery(
                    "s-1",
                    "successor",
                    response_to=ResponseTo("p-1", "support"),
                )
            ],
        }
    )

    score = await score_memory_integration("parent", "successor", graph=graph, now=NOW)

    assert score.verdict == "weak_signal"
    assert score.cited_parent_discoveries == 1
    assert score.strong_extensions == 0
    assert score.weak_extensions == 1


@pytest.mark.asyncio
async def test_score_memory_integration_absent_when_parent_corpus_sufficient():
    graph = FakeKnowledgeGraph(
        {
            "parent": _parent_rows(),
            "successor": [
                _discovery("s-1", "successor"),
                _discovery(
                    "s-2",
                    "successor",
                    response_to=ResponseTo("non-parent-discovery", "extend"),
                ),
            ],
        }
    )

    score = await score_memory_integration("parent", "successor", graph=graph, now=NOW)

    assert score.verdict == "absent"
    assert score.cited_parent_discoveries == 0
    assert score.strong_extensions == 0
    assert score.generated_discovery_ids == []


@pytest.mark.asyncio
async def test_score_memory_integration_insufficient_parent_memory_takes_precedence():
    graph = FakeKnowledgeGraph(
        {
            "parent": _parent_rows(count=2),
            "successor": [
                _discovery(
                    "s-1",
                    "successor",
                    response_to=ResponseTo("p-1", "extend"),
                ),
                _discovery(
                    "s-2",
                    "successor",
                    response_to=ResponseTo("p-2", "answer"),
                ),
            ],
        }
    )

    score = await score_memory_integration("parent", "successor", graph=graph, now=NOW)

    assert score.verdict == "insufficient_parent_memory"
    assert score.parent_discoveries_seen == 2
    assert score.strong_extensions == 2
    assert "parent memory corpus below threshold" in " ".join(score.reasons)


@pytest.mark.asyncio
async def test_score_memory_integration_inconclusive_on_kg_read_failure():
    graph = FakeKnowledgeGraph(exc=RuntimeError("database unavailable"))

    score = await score_memory_integration("parent", "successor", graph=graph, now=NOW)

    assert score.verdict == "inconclusive"
    assert score.confidence == 0.0
    assert score.parent_discoveries_seen == 0
    assert "KG read failed" in score.reasons[0]


@pytest.mark.asyncio
async def test_score_memory_integration_excludes_archived_parent_memory():
    graph = FakeKnowledgeGraph(
        {
            "parent": _parent_rows() + [_discovery("p-archived", "parent", status="archived")],
            "successor": [
                _discovery(
                    "s-1",
                    "successor",
                    response_to=ResponseTo("p-archived", "extend"),
                )
            ],
        }
    )

    score = await score_memory_integration("parent", "successor", graph=graph, now=NOW)

    assert score.verdict == "absent"
    assert score.parent_discoveries_seen == 3
    assert score.cited_parent_discoveries == 0


@pytest.mark.asyncio
async def test_select_memory_integration_lineage_pairs_filters_state_and_limit():
    db = FakeDB(
        [
            SubscriptOnlyRow(
                {
                    "successor_id": "s-provisional",
                    "parent_id": "p-1",
                    "lineage_state": "provisional",
                    "lineage_declared_at": NOW,
                    "confirmed_at": None,
                    "chain_obs_count": 0,
                }
            ),
            SubscriptOnlyRow(
                {
                    "successor_id": "s-confirmed",
                    "parent_id": "p-2",
                    "lineage_state": "confirmed",
                    "lineage_declared_at": NOW,
                    "confirmed_at": NOW,
                    "chain_obs_count": 4,
                }
            ),
        ]
    )

    pairs = await select_memory_integration_lineage_pairs(
        lineage_state="confirmed",
        limit=1,
        db=db,
    )

    assert len(pairs) == 1
    assert pairs[0].successor_id == "s-confirmed"
    assert pairs[0].parent_id == "p-2"
    assert pairs[0].lineage_state == "confirmed"
    assert pairs[0].chain_obs_count == 4
    assert pairs[0].to_dict()["confirmed_at"] == NOW.isoformat()
    sql, args = db.acquire_context.conn.fetch_calls[0]
    assert "FROM core.identities" in sql
    assert args == ("confirmed", 1)


@pytest.mark.asyncio
async def test_score_memory_integration_batch_summarizes_shadow_verdicts():
    db = FakeDB(
        [
            {
                "successor_id": "successor-integrated",
                "parent_id": "parent-integrated",
                "lineage_state": "provisional",
                "lineage_declared_at": NOW,
                "confirmed_at": None,
                "chain_obs_count": 0,
            },
            {
                "successor_id": "successor-absent",
                "parent_id": "parent-absent",
                "lineage_state": "provisional",
                "lineage_declared_at": NOW,
                "confirmed_at": None,
                "chain_obs_count": 0,
            },
        ]
    )
    graph = FakeKnowledgeGraph(
        {
            "parent-integrated": [
                _discovery("pi-1", "parent-integrated"),
                _discovery("pi-2", "parent-integrated"),
                _discovery("pi-3", "parent-integrated"),
            ],
            "successor-integrated": [
                _discovery(
                    "si-1",
                    "successor-integrated",
                    response_to=ResponseTo("pi-1", "extend"),
                ),
                _discovery(
                    "si-2",
                    "successor-integrated",
                    response_to=ResponseTo("pi-2", "answer"),
                ),
            ],
            "parent-absent": [
                _discovery("pa-1", "parent-absent"),
                _discovery("pa-2", "parent-absent"),
                _discovery("pa-3", "parent-absent"),
            ],
            "successor-absent": [
                _discovery("sa-1", "successor-absent"),
            ],
        }
    )

    result = await score_memory_integration_batch(
        lineage_state="provisional",
        limit=5,
        db=db,
        graph=graph,
        now=NOW,
    )

    assert result["pair_count"] == 2
    assert result["verdict_counts"] == {
        "integrated_candidate": 1,
        "absent": 1,
    }
    assert result["items"][0]["pair"]["successor_id"] == "successor-integrated"
    assert result["items"][0]["score"]["verdict"] == "integrated_candidate"
    assert "read-only" in result["note"]
