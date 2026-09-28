"""Text a knowledge write sends to the AGE graph is bounded before storage.

On AGE every value reaches Cypher through GraphMixin._sanitize_cypher_param,
which refuses a string (or a dict serialized with json.dumps) over 128 KiB.
The refusal fails the whole write: an update rolls back, update_discovery
returns False and the handler reported "Discovery not found"; a store raised
the sanitizer's own "Cypher dict param too long" text. #2530 bounded details
on update. These bound the rest a caller controls:

- update: summary (MAX_UPDATED_SUMMARY_LEN) and tags (MAX_TAGS, MAX_TAG_LEN);
- store, batch store, note and promote: tags, and the node metadata
  (related_files, the provenance fields a call sends, response_to), measured
  as the JSON the AGE store writes (MAX_DISCOVERY_METADATA_LEN).

Each is refused with INVALID_PARAM before anything is written.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.age_queries import create_discovery_node
from src.db.mixins.graph import GraphMixin
from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers as kg_handlers
from src.mcp_handlers.knowledge.limits import (
    MAX_DISCOVERY_METADATA_LEN,
    MAX_SUMMARY_LEN,
    MAX_TAG_LEN,
    MAX_TAGS,
    MAX_UPDATED_SUMMARY_LEN,
)
from src.storage.knowledge_graph_age import KnowledgeGraphAGE
from tests.helpers import parse_result

CYPHER_LIMIT = GraphMixin._MAX_PARAM_LENGTH


class _GraphHost(GraphMixin):
    """Bare mixin host: only the sanitizer and interpolation are used."""

    def __init__(self):
        self._age_graph = "test_graph"


def _stored(**overrides) -> DiscoveryNode:
    fields = dict(
        id="d-1", agent_id="a-1", type="bug_found", summary="s", status="open",
        severity="low",
    )
    fields.update(overrides)
    return DiscoveryNode(**fields)


def _node_cypher_raises(discovery: DiscoveryNode) -> bool:
    """Whether the AGE store's node statement for this discovery fails.

    Builds the node Cypher the way KnowledgeGraphAGE.add_discovery does and
    runs it through the real interpolation.
    """
    cypher, params = create_discovery_node(
        discovery_id=discovery.id,
        agent_id=discovery.agent_id,
        discovery_type=discovery.type,
        summary=discovery.summary,
        details=discovery.details,
        tags=discovery.tags,
        metadata=KnowledgeGraphAGE._build_discovery_metadata(discovery),
    )
    try:
        _GraphHost()._interpolate_params(cypher, params)
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------------------
# Where the bounds sit
# ---------------------------------------------------------------------------


def test_every_bound_is_far_below_the_cypher_parameter_limit():
    assert MAX_UPDATED_SUMMARY_LEN < CYPHER_LIMIT
    assert MAX_DISCOVERY_METADATA_LEN < CYPHER_LIMIT
    # The whole tag list as the AGE update writes it (one JSON string).
    longest_list = json.dumps(["t" * MAX_TAG_LEN] * MAX_TAGS)
    assert len(longest_list) < CYPHER_LIMIT // 10


def test_update_accepts_every_summary_a_store_writes():
    """A store keeps MAX_SUMMARY_LEN and appends '...' when it truncates, so
    a stored summary sent back unchanged must pass the update bound."""
    state = kg_handlers._KnowledgeStoreState(
        request=MagicMock(arguments={}), graph=None, summary="w" * 50_000,
    )
    kg_handlers._truncate_store_content(state)
    batch_summary, _ = kg_handlers._truncate_batch_summary("w" * 50_000)
    assert len(state.summary) > MAX_SUMMARY_LEN
    assert len(state.summary) <= MAX_UPDATED_SUMMARY_LEN
    assert len(batch_summary) <= MAX_UPDATED_SUMMARY_LEN


# ---------------------------------------------------------------------------
# Tags: counted and measured after normalization
# ---------------------------------------------------------------------------


class TestTagBounds:
    def test_the_limit_itself_passes(self):
        tags = [f"topic-{i}" for i in range(MAX_TAGS)]
        assert kg_handlers._oversized_tags(tags, "x") is None
        assert kg_handlers._oversized_tags(["t" * MAX_TAG_LEN], "x") is None

    def test_one_tag_over_the_count_is_refused(self):
        tags = [f"topic-{i}" for i in range(MAX_TAGS + 1)]
        message, action = kg_handlers._oversized_tags(tags, "Nothing was changed.")
        assert f"tags has {MAX_TAGS + 1} entries" in message
        assert f"the limit is {MAX_TAGS}" in message
        assert message.endswith("Nothing was changed.")
        assert f"at most {MAX_TAGS} tags" in action

    def test_one_character_over_the_length_is_refused(self):
        message, action = kg_handlers._oversized_tags(
            ["short", "t" * (MAX_TAG_LEN + 1)], "Nothing was stored."
        )
        assert "1 tag(s) are over" in message
        assert f"the longest is {MAX_TAG_LEN + 1:,}" in message
        assert f"at most {MAX_TAG_LEN} characters" in action

    def test_duplicates_that_normalize_together_count_once(self):
        """normalize_tags folds case and separators and drops repeats, so
        the stored list, not the sent one, is what is counted."""
        tags = ["Postgres", "postgres", "PostgreSQL"] * 40
        assert kg_handlers._oversized_tags(tags, "x") is None

    def test_long_raw_text_is_measured_as_normalized(self):
        """Punctuation runs fold to one hyphen, so a tag within the bound
        after normalization passes even when its raw form is longer."""
        raw = "a" + "!" * 500 + "b"
        assert kg_handlers._oversized_tags([raw], "x") is None

    def test_a_comma_string_is_read_as_the_list_it_normalizes_to(self):
        many = ",".join(f"t{i}" for i in range(MAX_TAGS + 1))
        assert kg_handlers._oversized_tags(many, "x") is not None

    def test_unreadable_tags_are_left_to_the_write_path(self):
        assert kg_handlers._oversized_tags([object()], "x") is None
        assert kg_handlers._oversized_tags(None, "x") is None

    def test_tags_the_live_graph_uses_pass(self):
        """The largest tag lists findings carry: 17 tags, memory-slug tags
        up to 47 characters."""
        tags = [f"slug-{'x' * 42}"] + [f"tag-{i}" for i in range(16)]
        assert kg_handlers._oversized_tags(tags, "x") is None


# ---------------------------------------------------------------------------
# Update: summary and tags
# ---------------------------------------------------------------------------


def _age_graph(stored: DiscoveryNode):
    """KnowledgeGraphAGE whose graph_query runs the real Cypher interpolation."""
    host = _GraphHost()
    db = MagicMock()
    db.graph_available = AsyncMock(return_value=True)

    async def graph_query(cypher, params, conn=None):
        host._interpolate_params(cypher, params)
        return [{"d.id": params["discovery_id"]}]

    db.graph_query = AsyncMock(side_effect=graph_query)

    @asynccontextmanager
    async def transaction():
        yield MagicMock()

    db.transaction = transaction

    kg = KnowledgeGraphAGE()
    kg._db = db

    async def _get_db():
        return db

    kg._get_db = _get_db  # type: ignore[assignment]
    kg._sync_updated_discovery_row = AsyncMock()  # type: ignore[assignment]
    kg._sync_age_tag_edges = AsyncMock()  # type: ignore[assignment]
    kg._delete_orphan_age_tags = AsyncMock()  # type: ignore[assignment]
    kg._refresh_embedding = AsyncMock()  # type: ignore[assignment]
    kg.get_discovery = AsyncMock(return_value=stored)  # type: ignore[assignment]
    return kg, db


@pytest.fixture
def handler_env():
    server = MagicMock()
    server.agent_metadata = {}
    server.monitors = {}
    with (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", server),
        patch("src.mcp_handlers.knowledge.handlers.record_ms"),
        patch(
            "src.mcp_handlers.knowledge.handlers._broadcast_knowledge_write",
            AsyncMock(),
        ),
        # The lineage read behind a store's provenance_chain queries the
        # database; these tests make no database call.
        patch(
            "src.mcp_handlers.knowledge.handlers._build_s7_provenance_chain_with_fallback",
            AsyncMock(return_value=None),
        ),
    ):
        yield server


async def _update_through_age(arguments: dict, stored: DiscoveryNode | None = None):
    kg, db = _age_graph(stored or _stored())
    with patch(
        "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
        new_callable=AsyncMock,
        return_value=kg,
    ):
        data = parse_result(
            await kg_handlers.handle_update_discovery_status_graph(arguments)
        )
    return data, db


class TestUpdateSummaryAndTags:
    @pytest.mark.asyncio
    async def test_a_summary_past_the_cypher_limit_is_refused_not_reported_missing(
        self, handler_env
    ):
        """The defect: the sanitizer raised, the update rolled back, and the
        caller was told the discovery did not exist."""
        data, db = await _update_through_age(
            {"agent_id": "a-1", "discovery_id": "d-1", "summary": "s" * (CYPHER_LIMIT + 1)}
        )
        assert data["success"] is False
        assert data["error_code"] == "INVALID_PARAM"
        assert "not found" not in data["error"].lower()
        assert f"the limit is {MAX_UPDATED_SUMMARY_LEN:,}" in data["error"]
        assert "Nothing was changed" in data["error"]
        assert "details" in data["recovery"]["action"]
        db.graph_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_character_over_the_bound_is_refused(self, handler_env):
        data, db = await _update_through_age(
            {"agent_id": "a-1", "discovery_id": "d-1",
             "summary": "s" * (MAX_UPDATED_SUMMARY_LEN + 1)}
        )
        assert data["error_code"] == "INVALID_PARAM"
        assert data["error"].startswith(
            f"summary is {MAX_UPDATED_SUMMARY_LEN + 1:,} characters"
        )
        db.graph_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_summary_at_the_bound_reaches_the_graph(self, handler_env):
        summary = "s" * MAX_UPDATED_SUMMARY_LEN
        data, db = await _update_through_age(
            {"agent_id": "a-1", "discovery_id": "d-1", "summary": summary}
        )
        assert data["success"] is True
        assert db.graph_query.await_args.args[1]["val_summary"] == summary

    @pytest.mark.asyncio
    async def test_tags_past_the_cypher_limit_are_refused_not_reported_missing(
        self, handler_env
    ):
        """The update sends the whole list as one JSON string."""
        tags = [f"t{i:06d}-" + "x" * 100 for i in range(1_200)]
        assert len(json.dumps(tags)) > CYPHER_LIMIT
        data, db = await _update_through_age(
            {"agent_id": "a-1", "discovery_id": "d-1", "tags": tags}
        )
        assert data["success"] is False
        assert data["error_code"] == "INVALID_PARAM"
        assert "not found" not in data["error"].lower()
        assert "tags has 1200 entries" in data["error"]
        db.graph_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_single_overlong_tag_is_refused(self, handler_env):
        data, db = await _update_through_age(
            {"agent_id": "a-1", "discovery_id": "d-1", "tags": ["x" * 200_000]}
        )
        assert data["error_code"] == "INVALID_PARAM"
        assert "tag(s) are over" in data["error"]
        db.graph_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tags_within_the_bounds_reach_the_graph(self, handler_env):
        tags = [f"topic-{i}" for i in range(MAX_TAGS)]
        data, db = await _update_through_age(
            {"agent_id": "a-1", "discovery_id": "d-1", "tags": tags}
        )
        assert data["success"] is True
        assert json.loads(db.graph_query.await_args.args[1]["val_tags"]) == tags

    @pytest.mark.asyncio
    async def test_the_refusal_comes_before_the_graph_is_opened(self, handler_env):
        opened = AsyncMock()
        with patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph", opened
        ):
            data = parse_result(
                await kg_handlers.handle_update_discovery_status_graph(
                    {"discovery_id": "d-1", "summary": "s" * (MAX_UPDATED_SUMMARY_LEN + 1)}
                )
            )
        assert data["error_code"] == "INVALID_PARAM"
        opened.assert_not_awaited()


# ---------------------------------------------------------------------------
# Store, batch store, note and promote: tags and metadata
# ---------------------------------------------------------------------------


def test_oversized_related_files_used_to_fail_the_age_node_statement():
    """The defect on store, shown on the statement the AGE store builds."""
    discovery = _stored(references_files=["src/" + "p" * 200] * 700)
    assert _node_cypher_raises(discovery)
    assert kg_handlers._oversized_metadata(discovery) is not None


def test_metadata_at_live_scale_passes_and_builds():
    """Nine related_files and a full provenance record, about the largest a
    finding carries, are far inside the bound."""
    discovery = _stored(
        references_files=[f"src/module_{i}/some_file_name.py" for i in range(9)],
        provenance={"memory_context": "m" * 1_500, "task_label": "label"},
        related_to=[f"2026-09-2{i}T00:00:00.000000+00:00" for i in range(5)],
    )
    assert kg_handlers._oversized_metadata(discovery) is None
    assert not _node_cypher_raises(discovery)


def test_metadata_is_measured_as_the_age_store_serializes_it():
    """json.dumps escapes non-ASCII, so the stored JSON can be several times
    the character count sent; the bound reads the stored form."""
    one_part = MAX_DISCOVERY_METADATA_LEN // 6 + 100
    discovery = _stored(references_files=["é" * one_part])
    assert len(one_part * "é") < MAX_DISCOVERY_METADATA_LEN
    assert kg_handlers._oversized_metadata(discovery) is not None


def test_the_metadata_refusal_names_the_largest_parts_by_parameter():
    discovery = _stored(
        references_files=["f" * 40_000],
        provenance={"memory_context": "m" * 100},
    )
    message, action = kg_handlers._oversized_metadata(discovery)
    assert f"the limit is {MAX_DISCOVERY_METADATA_LEN:,}" in message
    assert "Nothing was stored" in message
    assert "Largest parts, in characters: related_files 40,004" in message
    assert "related_files" in action and "memory_context" in action


class TestSingleStore:
    @pytest.mark.asyncio
    async def test_too_many_tags_are_refused_before_the_graph_is_opened(self, handler_env):
        opened = AsyncMock()
        with patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph", opened):
            data = parse_result(
                await kg_handlers.handle_store_knowledge_graph(
                    {
                        "summary": "a finding",
                        "discovery_type": "insight",
                        "tags": [f"t{i}" for i in range(MAX_TAGS + 1)],
                    }
                )
            )
        assert data["success"] is False
        assert data["error_code"] == "INVALID_PARAM"
        assert "Nothing was stored" in data["error"]
        opened.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversized_related_files_are_refused_before_storage(self, handler_env):
        graph = AsyncMock()
        graph.find_similar = AsyncMock(return_value=[])
        with patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            AsyncMock(return_value=graph),
        ):
            data = parse_result(
                await kg_handlers.handle_store_knowledge_graph(
                    {
                        "summary": "a finding",
                        "discovery_type": "insight",
                        "related_files": ["src/" + "p" * 200] * 700,
                    }
                )
            )
        assert data["success"] is False
        assert data["error_code"] == "INVALID_PARAM"
        assert "Cypher" not in data["error"]
        assert "related_files" in data["error"]
        graph.add_discovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_oversized_provenance_field_is_refused_before_storage(self, handler_env):
        graph = AsyncMock()
        graph.find_similar = AsyncMock(return_value=[])
        with patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            AsyncMock(return_value=graph),
        ):
            data = parse_result(
                await kg_handlers.handle_store_knowledge_graph(
                    {
                        "summary": "a finding",
                        "discovery_type": "insight",
                        "memory_context": "m" * (MAX_DISCOVERY_METADATA_LEN + 1),
                    }
                )
            )
        assert data["error_code"] == "INVALID_PARAM"
        assert "provenance (memory_context" in data["error"]
        graph.add_discovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_live_scale_store_is_stored(self, handler_env):
        graph = AsyncMock()
        graph.find_similar = AsyncMock(return_value=[])
        with patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            AsyncMock(return_value=graph),
        ):
            data = parse_result(
                await kg_handlers.handle_store_knowledge_graph(
                    {
                        "summary": "a finding",
                        "discovery_type": "insight",
                        "tags": [f"tag-{i}" for i in range(17)],
                        "related_files": [f"src/file_{i}.py" for i in range(9)],
                        "memory_context": "the KG and two transcripts",
                    }
                )
            )
        assert data["success"] is True
        graph.add_discovery.assert_awaited_once()


class TestBatchStore:
    @pytest.mark.asyncio
    async def test_an_oversized_item_fails_alone(self, handler_env):
        graph = AsyncMock()
        graph.find_similar = AsyncMock(return_value=[])
        with patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            AsyncMock(return_value=graph),
        ):
            data = parse_result(
                await kg_handlers.handle_store_knowledge_graph(
                    {
                        "discoveries": [
                            {"summary": "fine", "discovery_type": "insight", "tags": ["a"]},
                            {
                                "summary": "too many tags",
                                "discovery_type": "insight",
                                "tags": [f"t{i}" for i in range(MAX_TAGS + 1)],
                            },
                            {
                                "summary": "large provenance",
                                "discovery_type": "insight",
                                "provenance": {"note": "p" * (MAX_DISCOVERY_METADATA_LEN + 1)},
                            },
                        ]
                    }
                )
            )
        assert data["success_count"] == 1
        assert data["error_count"] == 2
        errors = data["errors"]
        assert errors[0].startswith(f"Discovery 1: tags has {MAX_TAGS + 1} entries")
        assert "It was not stored." in errors[0]
        assert errors[1].startswith("Discovery 2: This finding's metadata would be")
        assert graph.add_discovery.await_count == 1


class TestNote:
    @pytest.mark.asyncio
    async def test_too_many_tags_are_refused_before_the_graph_is_opened(self, handler_env):
        opened = AsyncMock()
        with patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph", opened):
            data = parse_result(
                await kg_handlers.handle_knowledge_note(
                    {"summary": "a note", "tags": [f"t{i}" for i in range(MAX_TAGS + 1)]}
                )
            )
        assert data["error_code"] == "INVALID_PARAM"
        opened.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_oversized_provenance_field_is_refused_before_storage(self, handler_env):
        graph = AsyncMock()
        graph.find_similar = AsyncMock(return_value=[])
        with patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            AsyncMock(return_value=graph),
        ):
            data = parse_result(
                await kg_handlers.handle_knowledge_note(
                    {
                        "summary": "a note",
                        "memory_context": "m" * (MAX_DISCOVERY_METADATA_LEN + 1),
                    }
                )
            )
        assert data["error_code"] == "INVALID_PARAM"
        graph.add_discovery.assert_not_awaited()


class TestPromote:
    async def _promote(self, **extra):
        from src.knowledge_authority import PROMOTION_TAG  # noqa: F401

        source = DiscoveryNode(
            id="memory-1", agent_id="agent-a", type="insight", summary="claim",
            tags=["source-external-memory"],
        )
        evidence = DiscoveryNode(
            id="evidence-1", agent_id="agent-a", type="insight", summary="check",
        )
        graph = AsyncMock()
        graph.get_discovery = AsyncMock(
            side_effect=lambda discovery_id: {
                "memory-1": source, "evidence-1": evidence,
            }.get(discovery_id)
        )
        arguments = {
            "discovery_id": "memory-1",
            "evidence_ids": ["evidence-1"],
            "summary": "The verified bounded claim",
            "verification_basis": "A passing integration test.",
            "decision_standard": "One non-memory artifact supports it.",
            **extra,
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
            patch("src.mcp_handlers.utils.check_agent_can_operate", return_value=None),
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
            data = parse_result(
                await kg_handlers.handle_promote_memory_claim(arguments)
            )
        return data, graph

    @pytest.mark.asyncio
    async def test_the_promotion_tags_count_toward_the_limit(self):
        data, graph = await self._promote(
            tags=[f"t{i}" for i in range(MAX_TAGS - 1)]
        )
        assert data["error_code"] == "INVALID_PARAM"
        assert f"tags has {MAX_TAGS + 1} entries" in data["error"]
        assert "which promotion adds" in data["error"]
        graph.add_discovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversized_related_files_are_refused(self):
        data, graph = await self._promote(related_files=["f" * 40_000])
        assert data["error_code"] == "INVALID_PARAM"
        assert "related_files" in data["error"]
        graph.add_discovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_bounded_promotion_is_stored(self):
        data, graph = await self._promote(tags=["verified"], related_files=["src/a.py"])
        assert data["success"] is True
        graph.add_discovery.assert_awaited_once()
