"""Regression tests: a knowledge write naming an unproven agent_id is refused.

Under strict identity (the suite default), a low or medium knowledge write
(store, batch store, note, leave_note, update) whose arguments carry an
``agent_id`` while this call has no RESOLVED binding
(``get_context_resolved_agent_id()`` is None) is the caller's word alone.
Before ``_unproven_agent_id_refusal`` such a write landed under the invented
name, each invented name got its own hourly store budget, and an update
could edit or close another agent's finding. It is now refused with the #425
typed payload (success-shaped, ``refused: True``).

Unchanged and pinned here as companions: bound callers writing as
themselves, anonymous writes with no agent_id, the module's own ``anonkg_``
pseudonym written back into the same arguments, and the legacy posture
(``STRICT_IDENTITY_REQUIRED=0``).

Fixture patterns follow tests/test_kg_store.py. The mock graph charges a
stand-in store budget from ``add_discovery`` the way the real backends do
(``src.storage.kg_write_budget.check_store_budget``), so a refused write can
be shown not to have touched the budget.
"""

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.knowledge_graph import DiscoveryNode
from tests.helpers import parse_result


INVENTED_AGENT_ID = "invented-writer-not-bound"
FOREIGN_PSEUDONYM = "anonkg_other_0123456789ab"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def budget():
    """Stand-in for the per-agent store budget the backends charge."""
    with patch(
        "src.storage.kg_write_budget.check_store_budget", new_callable=AsyncMock
    ) as charge:
        yield charge


@pytest.fixture
def mock_mcp_server():
    server = MagicMock()
    server.agent_metadata = {}
    server.monitors = {}
    return server


@pytest.fixture
def mock_graph(budget):
    """Mock KG backend whose add_discovery charges the store budget first."""
    import src.storage.kg_write_budget as kg_write_budget

    async def add_discovery(discovery):
        await kg_write_budget.check_store_budget(discovery.agent_id, db=None)
        return True

    graph = AsyncMock()
    graph.add_discovery = AsyncMock(side_effect=add_discovery)
    graph.find_similar = AsyncMock(return_value=[])
    graph.query = AsyncMock(return_value=[])
    graph.get_discovery = AsyncMock(return_value=None)
    graph.get_agent_discoveries = AsyncMock(return_value=[])
    graph.update_discovery = AsyncMock(return_value=True)
    graph.full_text_search = AsyncMock(return_value=[])
    graph._get_db = AsyncMock()
    return graph


@pytest.fixture
def patch_common(mock_mcp_server, mock_graph):
    """Same dependency patches as tests/test_kg_store.py: no bound context."""
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_mcp_server), \
         patch("src.mcp_handlers.knowledge.handlers.mcp_server", mock_mcp_server), \
         patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
               new_callable=AsyncMock, return_value=mock_graph), \
         patch("src.mcp_handlers.knowledge.handlers.record_ms"), \
         patch("src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
               new_callable=AsyncMock):
        yield mock_mcp_server, mock_graph


@pytest.fixture
def unbound():
    """This call has NO resolved binding (explicit, not left to defaults)."""
    with patch(
        "src.mcp_handlers.context.get_context_resolved_agent_id", return_value=None
    ):
        yield


def _register(mock_mcp_server) -> str:
    agent_uuid = str(uuid.uuid4())
    meta = MagicMock()
    meta.status = "active"
    meta.health_status = "healthy"
    meta.total_updates = 5
    meta.label = "TestAgent"
    meta.display_name = "TestAgent"
    meta.structured_id = "test_agent_opus"
    meta.parent_agent_id = None
    meta.spawn_reason = None
    meta.created_at = "2026-01-01T00:00:00"
    meta.paused_at = None
    mock_mcp_server.agent_metadata[agent_uuid] = meta
    return agent_uuid


@pytest.fixture
def bound_agent(mock_mcp_server):
    """A registered agent that IS this call's resolved binding."""
    agent_uuid = _register(mock_mcp_server)
    with patch(
        "src.mcp_handlers.context.get_context_resolved_agent_id",
        return_value=agent_uuid,
    ):
        yield agent_uuid


def _existing_finding(agent_id: str, severity: str) -> DiscoveryNode:
    return DiscoveryNode(
        id="victim-finding-1",
        agent_id=agent_id,
        type="bug_found",
        summary="Original summary written by its owner",
        details="Original details",
        tags=["owner-tag"],
        severity=severity,
        status="open",
    )


def _assert_unproven_refusal(data: dict, reason: str = "unproven_agent_id") -> None:
    """The #425 typed refusal, success-shaped, naming why the call is unproven."""
    from src.mcp_handlers.identity_bootstrap import identity_refusal_status

    assert identity_refusal_status(data) == "identity_required", data
    assert data["refused"] is True
    assert data["rollout_flag"] == "STRICT_IDENTITY_REQUIRED"
    assert data["status"] == "identity_required"
    assert data.get("success") is not False
    assert "error" not in data
    assert data["tool"] == "knowledge"
    assert data["surface_context"]["reason"] == reason
    assert "discovery_id" not in data


# ============================================================================
# (1) Unbound store at low / medium with an invented agent_id
# ============================================================================


class TestUnboundStoreRefused:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severity", ["low", "medium"])
    async def test_invented_agent_id_store_is_refused(
        self, patch_common, unbound, budget, severity
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        data = parse_result(await handle_store_knowledge_graph({
            "agent_id": INVENTED_AGENT_ID,
            "summary": "Claimed finding under someone else's name",
            "discovery_type": "bug_found",
            "severity": severity,
        }))

        _assert_unproven_refusal(data)
        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()

    @pytest.mark.asyncio
    async def test_invented_agent_id_store_via_knowledge_router_is_refused(
        self, patch_common, unbound, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.consolidated import handle_knowledge

        data = parse_result(await handle_knowledge({
            "action": "store",
            "agent_id": INVENTED_AGENT_ID,
            "summary": "Claimed finding via the router",
        }))

        _assert_unproven_refusal(data)
        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()


# ============================================================================
# (2) Budget multiplication: 25 distinct invented names
# ============================================================================


class TestBudgetMultiplication:

    @pytest.mark.asyncio
    async def test_twenty_five_invented_agent_ids_store_nothing(
        self, patch_common, unbound, budget
    ):
        """One caller minting a new name per write gets no budget per name."""
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        names = [f"sybil-writer-{i:02d}" for i in range(25)]
        assert len(set(names)) == 25
        for index, name in enumerate(names):
            data = parse_result(await handle_store_knowledge_graph({
                "agent_id": name,
                "summary": f"Flood entry {index}",
                "severity": "low" if index % 2 else "medium",
            }))
            _assert_unproven_refusal(data)

        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()


# ============================================================================
# (3) Batch store with an unbound invented agent_id
# ============================================================================


class TestUnboundBatchRefused:

    @pytest.mark.asyncio
    async def test_batch_store_with_invented_agent_id_stores_nothing(
        self, patch_common, unbound, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        data = parse_result(await handle_store_knowledge_graph({
            "agent_id": INVENTED_AGENT_ID,
            "discoveries": [
                {"discovery_type": "note", "summary": "Batch item 1"},
                {"discovery_type": "insight", "summary": "Batch item 2"},
                {"discovery_type": "note", "summary": "Batch item 3"},
            ],
        }))

        _assert_unproven_refusal(data)
        assert "success_count" not in data
        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()


# ============================================================================
# (4) Notes: knowledge(action='note') and leave_note
# ============================================================================


class TestUnboundNoteRefused:

    @pytest.mark.asyncio
    async def test_knowledge_note_with_invented_agent_id_is_refused(
        self, patch_common, unbound, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.consolidated import handle_knowledge

        data = parse_result(await handle_knowledge({
            "action": "note",
            "agent_id": INVENTED_AGENT_ID,
            "content": "Note claimed under an invented name",
        }))

        _assert_unproven_refusal(data)
        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_knowledge_note_with_invented_agent_id_is_refused(
        self, patch_common, unbound, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_knowledge_note

        data = parse_result(await handle_knowledge_note({
            "agent_id": INVENTED_AGENT_ID,
            "summary": "Note claimed under an invented name",
        }))

        _assert_unproven_refusal(data)
        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()

    @pytest.mark.asyncio
    async def test_leave_note_with_invented_agent_id_is_refused(
        self, patch_common, unbound, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        data = parse_result(await handle_leave_note({
            "agent_id": INVENTED_AGENT_ID,
            "summary": "leave_note claimed under an invented name",
            "tags": ["claimed"],
        }))

        _assert_unproven_refusal(data)
        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()

    @pytest.mark.asyncio
    async def test_knowledge_note_without_agent_id_still_writes_anonymously(
        self, patch_common, unbound, budget
    ):
        """Companion: no agent_id is the anonymous low-friction path, unchanged."""
        _, mock_graph = patch_common
        from src.mcp_handlers.consolidated import handle_knowledge

        data = parse_result(await handle_knowledge({
            "action": "note",
            "content": "Anonymous note, no name claimed",
        }))

        assert data["success"] is True
        assert data.get("refused") is not True
        assert data["agent_mode"] == "anonymous"
        mock_graph.add_discovery.assert_awaited_once()
        written = mock_graph.add_discovery.await_args.args[0]
        assert written.agent_id.startswith("anonkg_")
        budget.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_leave_note_without_agent_id_still_writes_anonymously(
        self, patch_common, unbound, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        data = parse_result(await handle_leave_note({
            "summary": "Anonymous leave_note, no name claimed",
        }))

        assert data["success"] is True
        assert data.get("refused") is not True
        assert data["agent_mode"] == "anonymous"
        mock_graph.add_discovery.assert_awaited_once()
        written = mock_graph.add_discovery.await_args.args[0]
        assert written.agent_id.startswith("anonkg_")
        budget.assert_awaited_once()


# ============================================================================
# (5) Updates on an existing finding by an unbound named caller
# ============================================================================


class TestUnboundUpdateRefused:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severity", ["low", "medium"])
    @pytest.mark.parametrize("via", ["update_handler", "knowledge_update"])
    async def test_invented_agent_id_update_is_refused_and_finding_unchanged(
        self, patch_common, mock_mcp_server, unbound, severity, via
    ):
        _, mock_graph = patch_common
        owner = _register(mock_mcp_server)
        finding = _existing_finding(owner, severity)
        mock_graph.get_discovery = AsyncMock(return_value=finding)

        arguments = {
            "agent_id": INVENTED_AGENT_ID,
            "discovery_id": finding.id,
            "status": "resolved",
            "summary": "Rewritten by an unproven caller",
            "details": "Rewritten details",
        }
        if via == "update_handler":
            from src.mcp_handlers.knowledge.handlers import (
                handle_update_discovery_status_graph,
            )
            result = await handle_update_discovery_status_graph(arguments)
        else:
            from src.mcp_handlers.consolidated import handle_knowledge
            result = await handle_knowledge({"action": "update", **arguments})

        data = parse_result(result)
        _assert_unproven_refusal(data)
        mock_graph.update_discovery.assert_not_called()
        assert finding.status == "open"
        assert finding.summary == "Original summary written by its owner"
        assert finding.details == "Original details"
        assert finding.agent_id == owner
        assert finding.severity == severity


# ============================================================================
# (6) Bound companions: the resolved binding writing as itself
# ============================================================================


class TestBoundCompanions:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severity", ["low", "medium"])
    async def test_bound_agent_store_as_itself_succeeds(
        self, patch_common, bound_agent, budget, severity
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        data = parse_result(await handle_store_knowledge_graph({
            "agent_id": bound_agent,
            "summary": "Bound agent's own finding",
            "discovery_type": "bug_found",
            "severity": severity,
        }))

        assert data["success"] is True
        assert data.get("refused") is not True
        assert "discovery_id" in data
        assert data.get("agent_mode") != "anonymous"
        mock_graph.add_discovery.assert_awaited_once()
        assert mock_graph.add_discovery.await_args.args[0].agent_id == bound_agent
        budget.assert_awaited_once()
        assert budget.await_args.args[0] == bound_agent

    @pytest.mark.asyncio
    async def test_bound_agent_batch_store_as_itself_succeeds(
        self, patch_common, bound_agent, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        data = parse_result(await handle_store_knowledge_graph({
            "agent_id": bound_agent,
            "discoveries": [
                {"discovery_type": "note", "summary": "Bound batch 1"},
                {"discovery_type": "insight", "summary": "Bound batch 2"},
            ],
        }))

        assert data["success"] is True
        assert data["success_count"] == 2
        assert mock_graph.add_discovery.await_count == 2
        assert budget.await_count == 2

    @pytest.mark.asyncio
    async def test_bound_agent_leave_note_as_itself_succeeds(
        self, patch_common, bound_agent, budget
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        data = parse_result(await handle_leave_note({
            "agent_id": bound_agent,
            "summary": "Bound agent's own note",
        }))

        assert data["success"] is True
        assert data.get("refused") is not True
        assert data.get("agent_mode") != "anonymous"
        mock_graph.add_discovery.assert_awaited_once()
        assert mock_graph.add_discovery.await_args.args[0].agent_id == bound_agent

    @pytest.mark.asyncio
    async def test_bound_agent_low_update_of_own_finding_succeeds(
        self, patch_common, bound_agent
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import (
            handle_update_discovery_status_graph,
        )

        finding = _existing_finding(bound_agent, "low")
        mock_graph.get_discovery = AsyncMock(return_value=finding)

        data = parse_result(await handle_update_discovery_status_graph({
            "agent_id": bound_agent,
            "discovery_id": finding.id,
            "status": "resolved",
        }))

        assert data["success"] is True
        assert data.get("refused") is not True
        mock_graph.update_discovery.assert_awaited()


# ============================================================================
# (7) The module's own anonkg_ pseudonym vs a foreign one
# ============================================================================


class TestAnonymousPseudonymWriteBack:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("client_session_id", [None, "csid-pseudonym-test-1"])
    async def test_own_derived_pseudonym_passes(
        self, patch_common, unbound, budget, client_session_id
    ):
        """The pseudonym derived from these same arguments is let through."""
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import (
            _derive_anonymous_writer_id,
            handle_store_knowledge_graph,
        )

        arguments = {"summary": "Anonymous write with its pseudonym written back"}
        if client_session_id:
            arguments["client_session_id"] = client_session_id
        pseudonym = _derive_anonymous_writer_id(dict(arguments))
        assert pseudonym.startswith("anonkg_")
        assert pseudonym != FOREIGN_PSEUDONYM

        data = parse_result(await handle_store_knowledge_graph(
            {**arguments, "agent_id": pseudonym}
        ))

        assert data["success"] is True
        assert data.get("refused") is not True
        mock_graph.add_discovery.assert_awaited_once()
        assert mock_graph.add_discovery.await_args.args[0].agent_id == pseudonym
        budget.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_own_derived_pseudonym_passes_on_note(
        self, patch_common, unbound
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import (
            _derive_anonymous_writer_id,
            handle_knowledge_note,
        )

        arguments = {"summary": "Anonymous note with its pseudonym written back"}
        pseudonym = _derive_anonymous_writer_id(dict(arguments))

        data = parse_result(await handle_knowledge_note(
            {**arguments, "agent_id": pseudonym}
        ))

        assert data["success"] is True
        assert data.get("refused") is not True
        mock_graph.add_discovery.assert_awaited_once()
        assert mock_graph.add_discovery.await_args.args[0].agent_id == pseudonym

    @pytest.mark.asyncio
    async def test_foreign_pseudonym_is_refused(self, patch_common, unbound, budget):
        """An anonkg_-shaped name this call did not derive is just another claim."""
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import (
            _derive_anonymous_writer_id,
            handle_store_knowledge_graph,
        )

        arguments = {"summary": "Write claiming someone else's pseudonym"}
        assert _derive_anonymous_writer_id(dict(arguments)) != FOREIGN_PSEUDONYM

        data = parse_result(await handle_store_knowledge_graph(
            {**arguments, "agent_id": FOREIGN_PSEUDONYM}
        ))

        _assert_unproven_refusal(data)
        mock_graph.add_discovery.assert_not_called()
        budget.assert_not_called()

    @pytest.mark.asyncio
    async def test_foreign_pseudonym_update_is_refused(
        self, patch_common, mock_mcp_server, unbound
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import (
            handle_update_discovery_status_graph,
        )

        finding = _existing_finding(FOREIGN_PSEUDONYM, "low")
        mock_graph.get_discovery = AsyncMock(return_value=finding)

        data = parse_result(await handle_update_discovery_status_graph({
            "agent_id": FOREIGN_PSEUDONYM,
            "discovery_id": finding.id,
            "status": "resolved",
        }))

        _assert_unproven_refusal(data)
        mock_graph.update_discovery.assert_not_called()
        assert finding.status == "open"


# ============================================================================
# (8) Legacy posture: STRICT_IDENTITY_REQUIRED=0 keeps the named writer
# ============================================================================


class TestLegacyPostureUnchanged:

    @pytest.mark.asyncio
    @pytest.mark.legacy_identity_defaults
    @pytest.mark.parametrize("severity", ["low", "medium"])
    async def test_unbound_invented_agent_id_writes_as_named_author(
        self, patch_common, unbound, budget, severity
    ):
        import os

        from src.mcp_handlers.identity_bootstrap import is_strict_identity_required
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        assert os.environ.get("STRICT_IDENTITY_REQUIRED") == "false"
        assert is_strict_identity_required() is False
        _, mock_graph = patch_common

        data = parse_result(await handle_store_knowledge_graph({
            "agent_id": INVENTED_AGENT_ID,
            "summary": "Legacy named write",
            "severity": severity,
        }))

        assert data["success"] is True
        assert data.get("refused") is not True
        assert data.get("agent_mode") != "anonymous"
        mock_graph.add_discovery.assert_awaited_once()
        assert mock_graph.add_discovery.await_args.args[0].agent_id == INVENTED_AGENT_ID
        budget.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_explicit_zero_disables_the_refusal(
        self, patch_common, unbound, monkeypatch
    ):
        """STRICT_IDENTITY_REQUIRED=0 (the literal the task names) behaves the same."""
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "0")
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        data = parse_result(await handle_leave_note({
            "agent_id": INVENTED_AGENT_ID,
            "summary": "Legacy named note",
        }))

        assert data["success"] is True
        assert data.get("refused") is not True
        mock_graph.add_discovery.assert_awaited_once()
        assert mock_graph.add_discovery.await_args.args[0].agent_id == INVENTED_AGENT_ID


# ---------------------------------------------------------------------------
# Every identity-gated knowledge handler, not only the writer resolver
# ---------------------------------------------------------------------------
#
# Supersede without notes, cleanup, synthesize, promote and audit reached their
# writes without the writer resolver, each as a router action and a standalone
# tool. The handler-entry decorator gates them all.

GATED_HANDLERS = [
    ("handle_supersede_discovery", {"discovery_id": "new-1", "supersedes_id": "victim-finding-1"}),
    ("handle_cleanup_knowledge_graph", {"dry_run": False}),
    ("handle_synthesize_knowledge_graph", {"dry_run": False}),
    ("handle_promote_memory_claim", {"discovery_id": "victim-finding-1", "evidence_ids": ["e-1"],
                                     "summary": "s", "verification_basis": "b", "decision_standard": "d"}),
    ("handle_audit_knowledge_graph", {"scope": "all"}),
]

GATED_ACTIONS = {
    "handle_supersede_discovery": "supersede",
    "handle_cleanup_knowledge_graph": "cleanup",
    "handle_synthesize_knowledge_graph": "synthesize",
    "handle_promote_memory_claim": "promote",
    "handle_audit_knowledge_graph": "audit",
}


class TestEveryGatedHandlerRefuses:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,args", GATED_HANDLERS, ids=[h for h, _ in GATED_HANDLERS])
    async def test_invented_agent_id_is_refused_before_any_write(self, patch_common, unbound, handler_name, args):
        _, mock_graph = patch_common
        import src.mcp_handlers.knowledge.handlers as handlers

        result = await getattr(handlers, handler_name)({**args, "agent_id": "invented-writer-7"})

        _assert_unproven_refusal(parse_result(result))
        mock_graph.supersede_discovery.assert_not_called()
        mock_graph.update_discovery.assert_not_called()
        mock_graph.add_discovery.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,args", GATED_HANDLERS, ids=[h for h, _ in GATED_HANDLERS])
    async def test_own_derived_pseudonym_is_refused(self, patch_common, unbound, handler_name, args):
        # The pseudonym an anonymous store would be written under, re-derived
        # from these same arguments, is a label and not an identity: it must
        # not authorize an administrative write.
        _, mock_graph = patch_common
        import src.mcp_handlers.knowledge.handlers as handlers

        pseudonym = handlers._derive_anonymous_writer_id(dict(args))
        assert pseudonym.startswith(handlers._ANONYMOUS_WRITER_PREFIX)
        result = await getattr(handlers, handler_name)({**args, "agent_id": pseudonym})

        _assert_unproven_refusal(parse_result(result))
        mock_graph.supersede_discovery.assert_not_called()
        mock_graph.update_discovery.assert_not_called()
        mock_graph.add_discovery.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,args", GATED_HANDLERS, ids=[h for h, _ in GATED_HANDLERS])
    @pytest.mark.parametrize("agent_id", ["absent", None, ""])
    async def test_unbound_call_without_agent_id_is_refused(
        self, patch_common, unbound, handler_name, args, agent_id
    ):
        # A missing agent_id does not show the gate saw none: REST dispatch
        # unwraps kwargs after the gate, which can null the name it passed.
        _, mock_graph = patch_common
        import src.mcp_handlers.knowledge.handlers as handlers

        arguments = dict(args) if agent_id == "absent" else {**args, "agent_id": agent_id}
        result = await getattr(handlers, handler_name)(arguments)

        _assert_unproven_refusal(parse_result(result), reason="unbound")
        mock_graph.supersede_discovery.assert_not_called()
        mock_graph.update_discovery.assert_not_called()
        mock_graph.add_discovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_supersede_without_notes_through_the_router_is_refused(self, patch_common, unbound):
        _, mock_graph = patch_common
        from src.mcp_handlers.consolidated import handle_knowledge

        result = await handle_knowledge({
            "action": "supersede", "agent_id": "invented-writer-7",
            "discovery_id": "new-1", "supersedes_id": "victim-finding-1",
        })

        _assert_unproven_refusal(parse_result(result))
        mock_graph.supersede_discovery.assert_not_called()
        mock_graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_bound_supersede_still_reaches_the_graph(self, patch_common, bound_agent):
        _, mock_graph = patch_common
        mock_graph.get_discovery = AsyncMock(side_effect=lambda did: _existing_finding(bound_agent, "low"))
        mock_graph.supersede_discovery = AsyncMock(return_value=True)
        import src.mcp_handlers.knowledge.handlers as handlers

        result = await handlers.handle_supersede_discovery({
            "agent_id": bound_agent, "discovery_id": "new-1", "supersedes_id": "victim-finding-1",
        })

        data = parse_result(result)
        assert data.get("refused") is not True, data
        mock_graph.supersede_discovery.assert_awaited()


class TestRestEndToEnd:
    """Through the REST tool path, which passes a non-UUID agent_id at its gate
    and leaves it to the handlers (tests/test_rest_strict_identity_gate.py)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [
        {"action": "store", "summary": "rest probe", "discovery_type": "note", "severity": "low"},
        {"action": "note", "summary": "rest probe note"},
        {"action": "supersede", "discovery_id": "new-1", "supersedes_id": "victim-finding-1"},
    ], ids=["store", "note", "supersede"])
    async def test_rest_call_with_invented_agent_id_is_refused(self, patch_common, unbound, monkeypatch, arguments):
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        _, mock_graph = patch_common
        from src.services.http_tool_service import execute_http_tool

        result = await execute_http_tool("knowledge", {**arguments, "agent_id": "invented-rest-writer"})

        data = result if isinstance(result, dict) else parse_result(result)
        data = data.get("result", data) if isinstance(data, dict) else data
        _assert_unproven_refusal(data)
        mock_graph.add_discovery.assert_not_called()
        mock_graph.supersede_discovery.assert_not_called()
        mock_graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [
        {"action": "cleanup", "dry_run": False},
        {"action": "supersede", "discovery_id": "new-1", "supersedes_id": "victim-finding-1"},
    ], ids=["cleanup", "supersede"])
    async def test_kwargs_unwrap_nulling_the_gate_name_is_refused(self, patch_common, unbound, monkeypatch, arguments):
        # The gate passes the outer non-UUID name; dispatch then unwraps
        # kwargs, which replaces it with null before the handler runs.
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        _, mock_graph = patch_common
        from src.services.http_tool_service import execute_http_tool

        with patch("src.knowledge_graph_lifecycle.run_kg_lifecycle_cleanup", new_callable=AsyncMock) as cleanup:
            result = await execute_http_tool("knowledge", {
                **arguments, "agent_id": "invented-rest-writer", "kwargs": {"agent_id": None},
            })

        from src.mcp_handlers.identity_bootstrap import identity_refusal_status

        data = result if isinstance(result, dict) else parse_result(result)
        data = data.get("result", data) if isinstance(data, dict) else data
        assert identity_refusal_status(data) == "identity_required", data
        cleanup.assert_not_called()
        mock_graph.supersede_discovery.assert_not_called()
        mock_graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [
        {"action": "store", "summary": "kw", "discovery_type": "note", "severity": "low",
         "agent_id": "invented-rest-writer", "kwargs": {"agent_id": None}},
        {"action": "note", "summary": "kw", "agent_id": "invented-rest-writer",
         "kwargs": {"agent_id": None}},
        {"action": "search", "kwargs": {"action": "store", "summary": "kw",
                                        "discovery_type": "note", "severity": "low"}},
        {"action": "search", "kwargs": {"kwargs": {"action": "note", "summary": "kw"}}},
        {"action": "store", "summary": "kw", "discovery_type": "note", "severity": "low",
         "agent_id": "invented-rest-writer", "kwargs": json.dumps({"agent_id": ""})},
    ], ids=["store-null-name", "note-null-name", "search-wraps-store",
            "search-wraps-note-two-deep", "string-kwargs-blank-name"])
    async def test_the_gate_judges_the_call_dispatch_unwraps(self, patch_common, unbound, monkeypatch, arguments):
        # Each of these got past the gate and wrote anonymously: the gate read
        # the outer dict, and dispatch then unwrapped kwargs over it.
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        _, mock_graph = patch_common
        from src.mcp_handlers.identity_bootstrap import identity_refusal_status
        from src.services.http_tool_service import execute_http_tool

        result = await execute_http_tool("knowledge", arguments)

        data = result if isinstance(result, dict) else parse_result(result)
        data = data.get("result", data) if isinstance(data, dict) else data
        assert identity_refusal_status(data) == "identity_required", data
        mock_graph.add_discovery.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [
        {"kwargs": {"action": "search", "query": "x"}},
        {"action": "search", "kwargs": {"query": "x"}},
    ], ids=["wrapped-search", "search-with-wrapped-params"])
    async def test_a_wrapped_read_still_serves_unbound(self, patch_common, unbound, monkeypatch, arguments):
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        from src.services.http_tool_service import _strict_identity_refusal_or_none

        assert _strict_identity_refusal_or_none("knowledge", arguments) is None

    @pytest.mark.asyncio
    async def test_a_name_at_every_depth_still_passes_the_gate(self, patch_common, unbound, monkeypatch):
        # The legacy name reference is left to the handlers, which refuse it.
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        from src.services.http_tool_service import _strict_identity_refusal_or_none

        assert _strict_identity_refusal_or_none("knowledge", {
            "action": "store", "agent_id": "outer", "kwargs": {"agent_id": "inner"},
        }) is None


class TestServerInferredBindingOnUngatedHandlers:
    """A server-inferred binding (same address and User-Agent) is not proof.

    The five handlers without a writer resolver refuse it under strict
    identity unless the resolved agent is exempt from caller proof, the rule
    high and critical writes already follow."""

    @pytest.fixture
    def inferred(self, bound_agent):
        def _set(exempt: bool):
            # The session's binding is the inferred agent; the exemption is
            # asked of that agent, never of an agent_id the call passes.
            return (
                patch("src.mcp_handlers.context.get_session_proof_origin", return_value="server_inferred"),
                patch("src.mcp_handlers.context.get_context_agent_id", return_value=bound_agent),
                patch("src.mcp_handlers.identity_bootstrap.is_exempt_from_caller_proof",
                      new_callable=AsyncMock, return_value=exempt),
            )
        return bound_agent, _set

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,args", GATED_HANDLERS, ids=[h for h, _ in GATED_HANDLERS])
    async def test_non_exempt_inferred_binding_is_refused(self, patch_common, inferred, handler_name, args):
        _, mock_graph = patch_common
        agent, set_inferred = inferred
        origin, ctx_agent, exempt = set_inferred(False)
        import src.mcp_handlers.knowledge.handlers as handlers

        with origin, ctx_agent, exempt as predicate:
            result = await getattr(handlers, handler_name)({**args, "agent_id": agent})
        predicate.assert_awaited_with(agent)

        data = parse_result(result)
        assert data.get("refused") is True, data
        mock_graph.supersede_discovery.assert_not_called()
        mock_graph.update_discovery.assert_not_called()
        # The retry repeats the refused action, not a store or an update.
        action = GATED_ACTIONS[handler_name]
        assert f"knowledge(action='{action}')" in data["next_step"], data["next_step"]
        calls = " ".join(option.get("call", "") for option in data["safe_options"])
        assert "update_finding" not in calls and "store_finding" not in calls, calls
        retries = [
            option["call"] for option in data["safe_options"]
            if option["action"] in ("retry_with_client_session_id", "rebind_then_retry")
        ]
        assert len(retries) == 2, data["safe_options"]
        for call in retries:
            assert f"knowledge(action='{action}', ..., client_session_id=" in call, call

    @pytest.mark.asyncio
    async def test_exempt_resident_inferred_binding_still_supersedes(self, patch_common, inferred):
        _, mock_graph = patch_common
        agent, set_inferred = inferred
        mock_graph.get_discovery = AsyncMock(side_effect=lambda did: _existing_finding(agent, "low"))
        mock_graph.supersede_discovery = AsyncMock(return_value=True)
        origin, ctx_agent, exempt = set_inferred(True)
        import src.mcp_handlers.knowledge.handlers as handlers

        with origin, ctx_agent, exempt as predicate:
            result = await handlers.handle_supersede_discovery(
                {"agent_id": agent, "discovery_id": "new-1", "supersedes_id": "victim-finding-1"}
            )

        assert parse_result(result).get("refused") is not True
        mock_graph.supersede_discovery.assert_awaited()
        predicate.assert_awaited_with(agent)

    @pytest.mark.asyncio
    async def test_exempt_resident_supersede_with_notes_passes_both_checks(self, patch_common, inferred):
        """With notes, the writer resolver runs after the entry guard: both pass."""
        _, mock_graph = patch_common
        agent, set_inferred = inferred
        mock_graph.get_discovery = AsyncMock(side_effect=lambda did: _existing_finding(agent, "low"))
        mock_graph.supersede_discovery = AsyncMock(return_value=True)
        origin, ctx_agent, exempt = set_inferred(True)
        import src.mcp_handlers.knowledge.handlers as handlers

        with origin, ctx_agent, exempt as predicate:
            result = await handlers.handle_supersede_discovery({
                "agent_id": agent, "discovery_id": "new-1", "supersedes_id": "victim-finding-1",
                "resolution_notes": "superseded by new-1 after re-checking",
            })

        assert parse_result(result).get("refused") is not True, parse_result(result)
        mock_graph.supersede_discovery.assert_awaited()
        assert all(c.args == (agent,) for c in predicate.await_args_list)

    @pytest.mark.asyncio
    async def test_inferred_low_store_still_falls_back_to_the_anonymous_writer(self, patch_common, inferred):
        """Unchanged: store keeps its anonymous fallback for an inferred binding."""
        _, mock_graph = patch_common
        agent, set_inferred = inferred
        origin, ctx_agent, exempt = set_inferred(False)
        import src.mcp_handlers.knowledge.handlers as handlers

        with origin, ctx_agent, exempt:
            result = await handlers.handle_store_knowledge_graph(
                {"agent_id": agent, "summary": "inferred low store", "discovery_type": "note", "severity": "low"}
            )

        assert parse_result(result).get("refused") is not True
        stored = mock_graph.add_discovery.await_args.args[0]
        assert stored.agent_id.startswith("anonkg_")
