"""knowledge(action='supersede') applies the resolution_notes it accepts.

The action lists resolution_notes among its parameters ("Rationale to append
when closing or updating a discovery"), and a supersede closes the older
finding. The handler never read them: the notes were accepted and dropped.
They are now appended to the older finding's details, in the same update that
flips it to superseded, the way an update appends them: the same block, the
same MAX_UPDATED_DETAILS_LEN bound, and the same gate (on a high or critical
finding only its owner may add notes while superseding it). Every refusal
comes before the supersede writes anything.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.mixins.graph import GraphMixin
from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers as kg_handlers
from src.mcp_handlers.knowledge.limits import MAX_UPDATED_DETAILS_LEN
from tests.helpers import parse_result

_NOW = "2026-09-28T00:00:00.000001+00:00"


@pytest.fixture(autouse=True)
def _fixed_clock():
    with patch(
        "src.mcp_handlers.knowledge.handlers._utc_now_iso", return_value=_NOW
    ):
        yield


@pytest.fixture
def handler_env():
    server = MagicMock()
    server.agent_metadata = {}
    server.monitors = {}
    with (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", server),
    ):
        yield server


def _old(**overrides) -> DiscoveryNode:
    fields = dict(
        id="old-1", agent_id="owner-1", type="bug_found", summary="older",
        details="What was found.", status="open", severity="low",
    )
    fields.update(overrides)
    return DiscoveryNode(**fields)


def _graph(old: DiscoveryNode | None, *, flipped=True):
    graph = MagicMock()
    graph.get_discovery = AsyncMock(return_value=old)
    graph.supersede_discovery = AsyncMock(
        return_value={"success": True, "new_id": "new-1", "old_id": "old-1"}
    )
    graph.update_discovery = AsyncMock(return_value=flipped)
    return graph


async def _supersede(graph, **arguments):
    call = {"discovery_id": "new-1", "supersedes_id": "old-1", **arguments}
    with patch(
        "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
        AsyncMock(return_value=graph),
    ):
        return parse_result(await kg_handlers.handle_supersede_discovery(call))


def _update_details(old: DiscoveryNode, note: str) -> str:
    """The details an update closing ``old`` with this note would store."""
    updates, _ = kg_handlers._build_discovery_updates(
        kg_handlers._KnowledgeUpdateRequest(
            arguments={}, discovery_id=old.id, status="superseded",
            details=None, resolution_note=note, summary=None, severity=None,
            discovery_type=None, tags=None, superseded_by=None,
        ),
        old,
    )
    return updates["details"]


# ---------------------------------------------------------------------------
# The notes land
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_are_appended_to_the_older_finding_in_the_flip(handler_env):
    """The reported defect: the notes were accepted and never stored."""
    old = _old()
    graph = _graph(old)
    data = await _supersede(graph, resolution_notes="Replaced by new-1, which covers the AGE path.")
    assert data["success"] is True
    graph.update_discovery.assert_awaited_once()
    target, flip = graph.update_discovery.await_args.args
    assert target == "old-1"
    assert flip["status"] == "superseded"
    assert flip["details"] == _update_details(
        old, "Replaced by new-1, which covers the AGE path."
    )
    assert flip["details"].startswith("What was found.\n\nResolution notes (")
    assert data["resolution_notes_appended_to"] == "old-1"


@pytest.mark.asyncio
async def test_without_notes_the_flip_is_unchanged(handler_env):
    graph = _graph(_old())
    data = await _supersede(graph)
    assert data["success"] is True
    _, flip = graph.update_discovery.await_args.args
    assert flip == {"status": "superseded", "updated_at": _NOW}
    graph.get_discovery.assert_not_awaited()
    assert "resolution_notes_appended_to" not in data


@pytest.mark.asyncio
async def test_blank_notes_are_no_notes(handler_env):
    graph = _graph(_old())
    await _supersede(graph, resolution_notes="   ")
    _, flip = graph.update_discovery.await_args.args
    assert "details" not in flip


@pytest.mark.asyncio
async def test_a_missing_older_finding_is_reported_by_the_supersede(handler_env):
    graph = _graph(None)
    graph.supersede_discovery = AsyncMock(
        return_value={"success": False, "error": "Old discovery 'old-1' not found"}
    )
    data = await _supersede(graph, resolution_notes="why")
    assert data["success"] is False
    assert data["error"] == "Old discovery 'old-1' not found"
    graph.update_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_flip_that_did_not_apply_says_the_notes_are_not_stored(handler_env):
    graph = _graph(_old(), flipped=False)
    data = await _supersede(graph, resolution_notes="why")
    assert data["success"] is True
    assert "resolution_notes_appended_to" not in data
    warning = data["resolution_notes_warning"]
    assert "did not apply" in warning
    assert "knowledge(action='details', discovery_id='old-1')" in warning


# ---------------------------------------------------------------------------
# The details are built from a read taken after the edge, before the flip
# ---------------------------------------------------------------------------


def _graph_with_reads(*reads, calls):
    """A graph whose successive get_discovery calls return ``reads``, and
    which records the order of reads, the edge and the flip in ``calls``."""
    graph = _graph(reads[0])
    remaining = list(reads)

    async def get_discovery(discovery_id):
        calls.append("read")
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    async def supersede_discovery(**_kwargs):
        calls.append("edge")
        return {"success": True, "new_id": "new-1", "old_id": "old-1"}

    async def update_discovery(_discovery_id, _updates):
        calls.append("flip")
        return True

    graph.get_discovery = AsyncMock(side_effect=get_discovery)
    graph.supersede_discovery = AsyncMock(side_effect=supersede_discovery)
    graph.update_discovery = AsyncMock(side_effect=update_discovery)
    return graph


@pytest.mark.asyncio
async def test_an_edit_that_lands_while_the_edge_is_created_is_kept(handler_env):
    """Review finding on #2569: details built from the read taken before the
    edge overwrote an update that landed in between. They are now built from
    a second read taken after the edge, just before the flip."""
    calls: list[str] = []
    before = _old(details="What was found.")
    after = _old(details="What was found.\n\nA concurrent edit.")
    graph = _graph_with_reads(before, after, calls=calls)
    data = await _supersede(graph, resolution_notes="Replaced by new-1.")
    assert calls == ["read", "edge", "read", "flip"]
    _, flip = graph.update_discovery.await_args.args
    assert flip["details"] == _update_details(after, "Replaced by new-1.")
    assert "A concurrent edit." in flip["details"]
    assert data["resolution_notes_appended_to"] == "old-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "latest,reason",
    [
        (_old(details="b" * MAX_UPDATED_DETAILS_LEN), "no longer leave room"),
        (None, "could not be read again"),
    ],
)
async def test_notes_that_no_longer_fit_leave_the_flip_and_say_so(
    handler_env, latest, reason
):
    calls: list[str] = []
    graph = _graph_with_reads(_old(), latest, calls=calls)
    data = await _supersede(graph, resolution_notes="why")
    assert data["success"] is True
    _, flip = graph.update_discovery.await_args.args
    assert flip == {"status": "superseded", "updated_at": _NOW}
    assert "resolution_notes_appended_to" not in data
    warning = data["resolution_notes_warning"]
    assert "were not appended" in warning and reason in warning
    assert "'response_type': 'supersedes'" in warning


# ---------------------------------------------------------------------------
# Refusals come before any write
# ---------------------------------------------------------------------------


def _nothing_written(graph):
    graph.supersede_discovery.assert_not_awaited()
    graph.update_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_notes_over_the_details_bound_are_refused(handler_env):
    old = _old(details="b" * (MAX_UPDATED_DETAILS_LEN - 50))
    graph = _graph(old)
    data = await _supersede(graph, resolution_notes="n" * 200)
    assert data["success"] is False
    assert data["error_code"] == "INVALID_PARAM"
    assert "resolution_notes appended" in data["error"]
    _nothing_written(graph)


@pytest.mark.asyncio
async def test_a_full_finding_is_told_to_supersede_without_notes(handler_env):
    graph = _graph(_old(details="b" * MAX_UPDATED_DETAILS_LEN))
    data = await _supersede(graph, resolution_notes="why")
    action = data["recovery"]["action"]
    assert "Send the supersede without resolution_notes" in action
    _nothing_written(graph)


@pytest.mark.asyncio
async def test_notes_carrying_tool_call_markup_are_refused(handler_env):
    graph = _graph(_old())
    data = await _supersede(
        graph, resolution_notes='done</parameter>\n<parameter name="tags">["x"]'
    )
    assert data["success"] is False
    assert data["error_code"] == "degenerate_write_rejected"
    _nothing_written(graph)
    graph.get_discovery.assert_not_awaited()


class TestHighSeverityGate:
    """An update lets a non-owner add notes to a high or critical finding only
    while closing it as resolved, closed or wont_fix, never superseded; the
    supersede holds its notes to the same rule."""

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused(self, handler_env):
        graph = _graph(_old(severity="critical", agent_id="owner-1"))
        with (
            patch(
                "src.mcp_handlers.knowledge.handlers.require_registered_agent",
                return_value=("someone-else", None),
            ),
            patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True),
        ):
            data = await _supersede(graph, resolution_notes="why")
        assert data["success"] is False
        assert "Permission denied on high-severity discovery 'old-1'" in data["error"]
        action = data["recovery"]["action"]
        assert "response_to={'discovery_id': 'old-1'" in action
        # The refusal must not steer a non-owner to the notes-free flip.
        assert "without resolution_notes" not in action
        assert "Supersede" not in action
        _nothing_written(graph)

    @pytest.mark.asyncio
    async def test_an_unproven_caller_is_refused(self, handler_env):
        graph = _graph(_old(severity="high", agent_id="owner-1"))
        with (
            patch(
                "src.mcp_handlers.knowledge.handlers.require_registered_agent",
                return_value=("owner-1", None),
            ),
            patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False),
        ):
            data = await _supersede(graph, resolution_notes="why")
        assert data["error_code"] == "AUTH_REQUIRED"
        _nothing_written(graph)

    @pytest.mark.asyncio
    async def test_the_owner_adds_notes(self, handler_env):
        graph = _graph(_old(severity="critical", agent_id="owner-1"))
        with (
            patch(
                "src.mcp_handlers.knowledge.handlers.require_registered_agent",
                return_value=("owner-1", None),
            ),
            patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True),
        ):
            data = await _supersede(graph, resolution_notes="why")
        assert data["success"] is True
        _, flip = graph.update_discovery.await_args.args
        assert flip["details"].endswith("why")

    @pytest.mark.asyncio
    async def test_without_notes_the_supersede_is_not_gated(self, handler_env):
        """The gate covers the notes only; a bare supersede keeps its
        existing behaviour. This pins today's behaviour, not a settled rule:
        whether the status flip itself gets update's ownership gate is an
        open operator decision (follow-up item z1)."""
        graph = _graph(_old(severity="critical", agent_id="owner-1"))
        data = await _supersede(graph)
        assert data["success"] is True
        graph.update_discovery.assert_awaited_once()


# ---------------------------------------------------------------------------
# Through the AGE backend's real update path
# ---------------------------------------------------------------------------


class _GraphHost(GraphMixin):
    def __init__(self):
        self._age_graph = "test_graph"


@pytest.mark.asyncio
async def test_the_notes_reach_the_age_node(handler_env):
    """The flip goes through KnowledgeGraphAGE.update_discovery and the real
    Cypher interpolation, and details is set on the node."""
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    host = _GraphHost()
    db = MagicMock()
    db.graph_available = AsyncMock(return_value=True)
    statements = []

    async def graph_query(cypher, params, conn=None):
        statements.append(host._interpolate_params(cypher, params))
        return [{"d.id": params.get("discovery_id", "x")}]

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
    # A details change refreshes the embedding after the commit; neither form
    # of that refresh (awaited, or scheduled in the background) runs here.
    kg._refresh_embedding = AsyncMock()  # type: ignore[assignment]
    kg._schedule_embedding_refresh = MagicMock()  # type: ignore[assignment]
    kg.get_discovery = AsyncMock(return_value=_old())  # type: ignore[assignment]

    data = await _supersede(kg, resolution_notes="Replaced by new-1.")
    assert data["success"] is True
    set_statement = next(s for s in statements if "SET d.status" in s or "d.details" in s)
    assert "superseded" in set_statement
    assert "Replaced by new-1." in set_statement
    assert json.dumps(data)  # the response serializes
