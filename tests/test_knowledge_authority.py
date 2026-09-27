"""Authority-aware retrieval and imported-memory promotion contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.knowledge_authority import (
    GOVERNED_CLAIM,
    IMPORTED_CONTEXT,
    NATIVE_FINDING,
    PROMOTION_SCHEMA,
    PROMOTION_TAG,
    assess_authority,
    rank_by_authority,
)
from src.knowledge_graph import DiscoveryNode
from tests.helpers import parse_result


def _discovery(
    discovery_id: str,
    *,
    tags: list[str] | None = None,
    provenance: dict | None = None,
    summary: str = "claim",
) -> DiscoveryNode:
    return DiscoveryNode(
        id=discovery_id,
        agent_id="agent-a",
        type="insight",
        summary=summary,
        tags=tags or [],
        provenance=provenance,
    )


def _promotion_receipt() -> dict:
    return {
        "source": "explicit_promotion",
        "knowledge_authority": {
            "schema": PROMOTION_SCHEMA,
            "source_id": "memory-1",
            "evidence_ids": ["evidence-1"],
        },
    }


def test_authority_classification_is_harness_neutral_and_receipt_gated():
    imported = _discovery("memory-1", tags=["source-any-harness-memory"])
    native = _discovery("finding-1")
    forged = _discovery("forged", tags=[PROMOTION_TAG])
    governed = _discovery(
        "claim-1",
        tags=[PROMOTION_TAG],
        provenance=_promotion_receipt(),
    )

    assert assess_authority(imported).tier == IMPORTED_CONTEXT
    assert assess_authority(native).tier == NATIVE_FINDING
    assert assess_authority(forged).tier == NATIVE_FINDING
    assert assess_authority(governed).tier == GOVERNED_CLAIM


def test_authority_ranking_corrects_close_false_memory_but_preserves_strong_match():
    imported = _discovery("memory-1", tags=["memory-sync"])
    native = _discovery("finding-1")

    corrected, changed = rank_by_authority(
        [imported, native],
        relevance_scores={"memory-1": 0.82, "finding-1": 0.55},
    )
    assert changed is True
    assert [row.id for row in corrected] == ["finding-1", "memory-1"]

    strong_match, changed = rank_by_authority(
        [imported, native],
        relevance_scores={"memory-1": 0.95, "finding-1": 0.30},
    )
    assert changed is False
    assert [row.id for row in strong_match] == ["memory-1", "finding-1"]


def test_authority_ranking_can_be_disabled_for_raw_relevance_inspection():
    imported = _discovery("memory-1", tags=["memory-import"])
    native = _discovery("finding-1")
    ranked, changed = rank_by_authority(
        [imported, native],
        relevance_scores={"memory-1": 0.82, "finding-1": 0.55},
        enabled=False,
    )
    assert changed is False
    assert [row.id for row in ranked] == ["memory-1", "finding-1"]


@pytest.mark.asyncio
async def test_search_pipeline_annotates_and_prefers_native_authority():
    from src.mcp_handlers.knowledge.handlers import (
        _KnowledgeSearchState,
        _filter_and_rerank_candidates,
        _parse_knowledge_search_request,
        _serialize_search_discoveries,
    )

    imported = _discovery("memory-1", tags=["source-bridge-memory"])
    native = _discovery("finding-1")
    request = _parse_knowledge_search_request({"query": "production", "limit": 2})
    state = _KnowledgeSearchState(request=request, graph=AsyncMock())
    state.search_mode = "semantic"
    state.candidates = [imported, native]
    state.semantic_scores = {"memory-1": 0.82, "finding-1": 0.55}

    await _filter_and_rerank_candidates(state)
    serialized = _serialize_search_discoveries(state, include_details=False)

    assert [row.id for row in state.results] == ["finding-1", "memory-1"]
    assert state.authority_reranked is True
    assert serialized[0]["authority"]["tier"] == NATIVE_FINDING
    assert serialized[1]["authority"]["tier"] == IMPORTED_CONTEXT


@pytest.mark.asyncio
async def test_search_authority_all_preserves_raw_backend_order():
    from src.mcp_handlers.knowledge.handlers import (
        _KnowledgeSearchState,
        _filter_and_rerank_candidates,
        _parse_knowledge_search_request,
    )

    imported = _discovery("memory-1", tags=["memory-sync"])
    native = _discovery("finding-1")
    request = _parse_knowledge_search_request({
        "query": "production",
        "limit": 2,
        "authority_mode": "all",
    })
    state = _KnowledgeSearchState(request=request, graph=AsyncMock())
    state.search_mode = "semantic"
    state.candidates = [imported, native]
    state.semantic_scores = {"memory-1": 0.82, "finding-1": 0.55}

    await _filter_and_rerank_candidates(state)

    assert [row.id for row in state.results] == ["memory-1", "finding-1"]
    assert state.authority_reranked is False


@pytest.mark.asyncio
async def test_promote_memory_claim_creates_server_receipt_and_leaves_source():
    from src.mcp_handlers.knowledge.handlers import handle_promote_memory_claim

    source = _discovery("memory-1", tags=["source-external-memory"])
    evidence = _discovery("evidence-1", summary="Independent verification")
    graph = AsyncMock()
    graph.get_discovery = AsyncMock(side_effect=lambda discovery_id: {
        "memory-1": source,
        "evidence-1": evidence,
    }.get(discovery_id))
    graph.add_discovery = AsyncMock()

    arguments = {
        "discovery_id": "memory-1",
        "evidence_ids": ["evidence-1"],
        "summary": "The verified bounded claim",
        "details": "Evidence and limitations.",
        "verification_basis": "Repository configuration and a passing integration test agree.",
        "decision_standard": "At least one non-memory artifact independently supports the claim.",
        "client_session_id": "session-1",
    }

    with (
        patch(
            "src.mcp_handlers.knowledge.handlers.require_registered_agent",
            return_value=("agent-a", None),
        ),
        patch(
            "src.mcp_handlers.support.agent_auth.verify_agent_ownership",
            return_value=True,
        ),
        patch(
            "src.mcp_handlers.utils.check_agent_can_operate",
            return_value=None,
        ),
        patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            AsyncMock(return_value=graph),
        ),
        patch(
            "src.mcp_handlers.knowledge.handlers._capture_store_provenance",
            AsyncMock(return_value=({"source": "explicit_store"}, None)),
        ),
        patch(
            "src.mcp_handlers.knowledge.handlers._broadcast_knowledge_write",
            AsyncMock(),
        ),
    ):
        result = parse_result(await handle_promote_memory_claim(arguments))

    assert result["success"] is True
    assert result["source_id"] == "memory-1"
    assert result["authority"]["tier"] == GOVERNED_CLAIM
    assert result["promotion_receipt"]["evidence_ids"] == ["evidence-1"]
    graph.add_discovery.assert_awaited_once()
    stored = graph.add_discovery.await_args.args[0]
    assert stored.related_to == ["memory-1", "evidence-1"]
    assert stored.provenance["source"] == "explicit_promotion"
    assert PROMOTION_TAG in stored.tags
    assert source.status == "open"


@pytest.mark.asyncio
async def test_promote_memory_rejects_memory_only_corroboration():
    from src.mcp_handlers.knowledge.handlers import handle_promote_memory_claim

    source = _discovery("memory-1", tags=["memory-sync"])
    evidence = _discovery("memory-2", tags=["source-other-memory"])
    graph = AsyncMock()
    graph.get_discovery = AsyncMock(side_effect=lambda discovery_id: {
        "memory-1": source,
        "memory-2": evidence,
    }.get(discovery_id))

    with (
        patch(
            "src.mcp_handlers.knowledge.handlers.require_registered_agent",
            return_value=("agent-a", None),
        ),
        patch(
            "src.mcp_handlers.support.agent_auth.verify_agent_ownership",
            return_value=True,
        ),
        patch(
            "src.mcp_handlers.utils.check_agent_can_operate",
            return_value=None,
        ),
        patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            AsyncMock(return_value=graph),
        ),
    ):
        result = parse_result(await handle_promote_memory_claim({
            "discovery_id": "memory-1",
            "evidence_ids": ["memory-2"],
            "summary": "claim",
            "verification_basis": "another memory said so",
            "decision_standard": "agreement",
        }))

    assert result["success"] is False
    assert "cannot independently corroborate" in result["error"]


# Channel messages (operator decision 2026-09-27): agent-to-agent notes on a
# `channel-<topic>` lane get the imported-memory multiplier. Measured that day
# on the relational table: 78 rows match, 61 of them open.


def test_channel_message_is_the_tag_pair_or_the_summary_prefix():
    from src.knowledge_authority import CHANNEL_MESSAGE

    tag_pair = _discovery("m1", tags=["channel-resource-agent", "to-codex", "review"])
    prefixed = _discovery("m2", summary="[channel:beam-verbs] ack", tags=["review"])
    assert assess_authority(tag_pair).tier == CHANNEL_MESSAGE
    assert assess_authority(prefixed).tier == CHANNEL_MESSAGE
    assert assess_authority(tag_pair).to_dict()["ranking_multiplier"] == 0.55

    # A `channel-` topic tag with no addressee is an ordinary finding:
    # `channel-detection` is about how the server detects a client channel.
    topic = _discovery("f1", tags=["identity", "channel-detection"])
    addressed_only = _discovery("f2", tags=["to-codex", "coordination"])
    assert assess_authority(topic).tier == NATIVE_FINDING
    assert assess_authority(addressed_only).tier == NATIVE_FINDING


def test_imported_memory_wins_over_channel_when_both_markers_are_present():
    both = _discovery("m1", tags=["memory-sync", "channel-x", "to-claude"])
    assert assess_authority(both).tier == IMPORTED_CONTEXT


def test_channel_message_loses_close_contests_but_keeps_a_strong_match():
    channel = _discovery("m1", tags=["channel-resource-agent", "to-claude"])
    native = _discovery("finding-1")
    close, changed = rank_by_authority(
        [channel, native], relevance_scores={"m1": 0.82, "finding-1": 0.55}
    )
    assert changed is True
    assert [row.id for row in close] == ["finding-1", "m1"]
    strong, changed = rank_by_authority(
        [channel, native], relevance_scores={"m1": 0.95, "finding-1": 0.30}
    )
    assert changed is False
    assert [row.id for row in strong] == ["m1", "finding-1"]


@pytest.mark.asyncio
async def test_a_channel_tag_filter_reads_that_lane_in_its_own_order():
    from src.mcp_handlers.knowledge.handlers import (
        _KnowledgeSearchState,
        _authority_ranking_enabled,
        _filter_and_rerank_candidates,
        _parse_knowledge_search_request,
    )

    assert _authority_ranking_enabled(_parse_knowledge_search_request({"query": "x"}))
    request = _parse_knowledge_search_request({
        "query": "review", "limit": 2, "tags": ["channel-resource-agent"],
    })
    assert not _authority_ranking_enabled(request)

    channel = _discovery("m1", tags=["channel-resource-agent", "to-claude"])
    native = _discovery("finding-1", tags=["channel-resource-agent"])
    state = _KnowledgeSearchState(request=request, graph=AsyncMock())
    state.search_mode = "semantic"
    state.candidates = [channel, native]
    state.semantic_scores = {"m1": 0.82, "finding-1": 0.55}
    await _filter_and_rerank_candidates(state)
    assert [row.id for row in state.results] == ["m1", "finding-1"]


@pytest.mark.asyncio
async def test_channel_results_disclose_the_authority_policy():
    from src.mcp_handlers.knowledge.handlers import (
        _KnowledgeSearchState,
        _attach_search_diagnostics,
        _parse_knowledge_search_request,
    )

    request = _parse_knowledge_search_request({"query": "review"})
    state = _KnowledgeSearchState(request=request, graph=AsyncMock())
    state.results = [
        _discovery("m1", tags=["channel-resource-agent", "to-claude"]),
        _discovery("finding-1"),
    ]
    response: dict = {}
    _attach_search_diagnostics(response, state)
    assert response["authority_policy"]["result_tiers"] == {
        "channel_message": 1,
        "native_finding": 1,
    }


def test_only_a_filter_made_of_lane_tags_is_exempt():
    # Review on #2537: tags match any-of, so a mixed filter also returns
    # ordinary findings and must keep the authority order.
    from src.mcp_handlers.knowledge.handlers import (
        _authority_ranking_enabled,
        _parse_knowledge_search_request,
    )

    def enabled(tags):
        return _authority_ranking_enabled(
            _parse_knowledge_search_request({"query": "x", "tags": tags})
        )

    assert not enabled(["channel-resource-agent"])
    assert not enabled(["source-claude-memory", "channel-resource-agent"])
    assert enabled(["channel-resource-agent", "review"])
    assert enabled(["memory-sync", "review"])


@pytest.mark.asyncio
async def test_authority_sees_past_the_page_on_the_default_path():
    # Review on #2537 (P1): with hybrid and the reranker off, the candidate
    # list was cut to `limit` before authority ranking, so a native finding
    # just below a page of channel notes could never be lifted.
    from src.mcp_handlers.knowledge.handlers import (
        _KnowledgeSearchState,
        _filter_and_rerank_candidates,
        _parse_knowledge_search_request,
    )

    channel = [
        _discovery(f"m{i}", tags=["channel-resource-agent", "to-claude"]) for i in range(3)
    ]
    native = _discovery("finding-1")
    request = _parse_knowledge_search_request({"query": "review", "limit": 2})
    state = _KnowledgeSearchState(request=request, graph=AsyncMock())
    state.search_mode = "semantic"
    state.hybrid_on = False
    state.rerank_on = False
    state.candidates = [*channel, native]
    state.semantic_scores = {"m0": 0.82, "m1": 0.81, "m2": 0.80, "finding-1": 0.60}

    await _filter_and_rerank_candidates(state)

    assert [row.id for row in state.results] == ["finding-1", "m0"]
