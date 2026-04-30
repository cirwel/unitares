"""
Tests for ephemeral identity marking in dispatch middleware.

Feb 2026 fix: Identities created via dispatch (not onboard) should be marked
ephemeral=True when created=True and persisted=False. This prevents ghost
agent proliferation (96% ghost rate before the fix).

Key behavior:
- Dispatch creates new identity -> created=True, persisted=False -> ephemeral=True
- Dispatch finds existing identity -> created=False -> no ephemeral flag
- Persisted identities get TTL refresh via update_session_activity
- Ephemeral identities do NOT get TTL refresh
"""

import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from mcp.types import TextContent

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.middleware import resolve_identity, DispatchContext


@pytest.fixture
def mock_db():
    """Mock database for TTL refresh tracking."""
    db = AsyncMock()
    db.update_session_activity = AsyncMock(return_value=True)
    return db


def _identity_patches(identity_result, mock_db):
    """Stack of patches needed for resolve_identity tests.

    Mocks get_session_signals to prevent contextvar leakage from prior tests
    (the real get_session_signals reads a contextvar that may not be cleaned up).
    """
    return [
        patch("src.mcp_handlers.context.get_session_signals", return_value=None),
        patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="test-session"),
        patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=identity_result),
        patch("src.mcp_handlers.context.set_session_context", return_value=MagicMock()),
        patch("src.db.get_db", return_value=mock_db),
    ]


class TestEphemeralIdentityMarking:
    """Test that resolve_identity() correctly marks ephemeral identities."""

    @pytest.mark.asyncio
    async def test_new_identity_marked_ephemeral(self, mock_db):
        """When resolve_identity creates a new identity (created=True, persisted=False), it should be ephemeral."""
        identity_result = {
            "agent_uuid": "new-uuid-1111-2222-3333",
            "agent_name": None,
            "created": True,
            "persisted": False,
        }

        patches = _identity_patches(identity_result, mock_db)
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity("status", {}, ctx)
        finally:
            for p in reversed(patches):
                p.stop()

        # identity_result should have been mutated
        assert identity_result.get("ephemeral") is True
        assert identity_result.get("created_via") == "dispatch"
        # ctx should store the result
        assert ctx.identity_result is identity_result

    @pytest.mark.asyncio
    async def test_existing_identity_not_ephemeral(self, mock_db):
        """When resolve_identity finds an existing identity (created=False), it should NOT be ephemeral."""
        identity_result = {
            "agent_uuid": "existing-uuid-4444-5555",
            "agent_name": "ExistingAgent",
            "created": False,
            "persisted": True,
        }

        patches = _identity_patches(identity_result, mock_db)
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity("status", {}, ctx)
        finally:
            for p in reversed(patches):
                p.stop()

        assert "ephemeral" not in identity_result
        assert "created_via" not in identity_result

    @pytest.mark.asyncio
    async def test_persisted_identity_gets_ttl_refresh(self, mock_db):
        """Persisted identities should have their session TTL refreshed."""
        identity_result = {
            "agent_uuid": "persisted-uuid-6666-7777",
            "agent_name": "PersistedAgent",
            "created": False,
            "persisted": True,
        }

        patches = _identity_patches(identity_result, mock_db)
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity("status", {}, ctx)
        finally:
            for p in reversed(patches):
                p.stop()

        # TTL refresh must be called with whatever session key was derived
        assert ctx.session_key is not None
        mock_db.update_session_activity.assert_called_once_with(ctx.session_key)

    @pytest.mark.asyncio
    async def test_ephemeral_identity_no_ttl_refresh(self, mock_db):
        """Ephemeral (not persisted) identities should NOT get TTL refresh."""
        identity_result = {
            "agent_uuid": "ephemeral-uuid-8888-9999",
            "agent_name": None,
            "created": True,
            "persisted": False,
        }

        patches = _identity_patches(identity_result, mock_db)
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity("status", {}, ctx)
        finally:
            for p in reversed(patches):
                p.stop()

        # No TTL refresh for ephemeral identities
        mock_db.update_session_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_only_session_miss_does_not_auto_mint(self, mock_db):
        """Diagnostics should not mint identities when a client_session_id misses.

        Regression for mixed adapter signatures: read-only calls with a stale or
        unbound ``client_session_id`` used to retry ``resolve_session_identity``
        with ``force_new=True``, producing a different UUID for each diagnostic.
        """
        identity_result = {
            "resume_failed": True,
            "error": "session_resolve_miss",
            "session_key": "agent-missing",
        }

        resolve_mock = AsyncMock(return_value=identity_result)
        patches = [
            patch("src.mcp_handlers.context.get_session_signals", return_value=None),
            patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="agent-missing"),
            patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolve_mock),
            patch("src.mcp_handlers.context.set_session_context", return_value=MagicMock()),
            patch("src.db.get_db", return_value=mock_db),
        ]
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            await resolve_identity("get_governance_metrics", {"client_session_id": "agent-missing"}, ctx)
        finally:
            for p in reversed(patches):
                p.stop()

        assert resolve_mock.await_count == 1
        assert ctx.bound_agent_id is None
        assert ctx.identity_result is identity_result
        mock_db.update_session_activity.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["identity", "onboard"])
    async def test_identity_lifecycle_session_miss_does_not_auto_mint_in_middleware(
        self, tool_name, mock_db,
    ):
        """Lifecycle handlers own mint/persist; middleware only threads the miss."""
        identity_result = {
            "resume_failed": True,
            "error": "session_resolve_miss",
            "session_key": "agent-lifecycle-miss",
        }

        resolve_mock = AsyncMock(return_value=identity_result)
        patches = [
            patch("src.mcp_handlers.context.get_session_signals", return_value=None),
            patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="agent-lifecycle-miss"),
            patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolve_mock),
            patch("src.mcp_handlers.context.set_session_context", return_value=MagicMock()),
            patch("src.db.get_db", return_value=mock_db),
        ]
        for p in patches:
            p.start()
        try:
            args = {"client_session_id": "agent-lifecycle-miss"}
            ctx = DispatchContext()
            await resolve_identity(tool_name, args, ctx)
        finally:
            for p in reversed(patches):
                p.stop()

        assert resolve_mock.await_count == 1
        assert ctx.bound_agent_id is None
        assert args["_middleware_identity_result"] is not identity_result
        assert args["_middleware_identity_result"]["error"] == "session_resolve_miss"
        assert args["_middleware_identity_session_key"] == "agent-lifecycle-miss"

    @pytest.mark.asyncio
    async def test_caller_internal_identity_fields_are_scrubbed_on_resolution_error(
        self, mock_db,
    ):
        """Caller-supplied middleware handoff fields must never survive dispatch."""
        resolve_mock = AsyncMock(side_effect=RuntimeError("resolver unavailable"))
        patches = [
            patch("src.mcp_handlers.context.get_session_signals", return_value=None),
            patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="agent-error"),
            patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolve_mock),
            patch("src.mcp_handlers.context.set_session_context", return_value=MagicMock()),
            patch("src.db.get_db", return_value=mock_db),
        ]
        for p in patches:
            p.start()
        try:
            args = {
                "agent_id": "spoofed-agent",
                "_middleware_identity_session_key": "spoofed-session",
                "_middleware_identity_result": {
                    "agent_uuid": "spoofed-agent",
                    "core_agent_row_status": "active",
                },
                "_core_agent_row_status": "active",
            }
            ctx = DispatchContext()
            await resolve_identity("process_agent_update", args, ctx)
        finally:
            for p in reversed(patches):
                p.stop()

        assert "_middleware_identity_session_key" not in args
        assert "_middleware_identity_result" not in args
        assert "_core_agent_row_status" not in args
        assert ctx.identity_result is None

    @pytest.mark.asyncio
    async def test_kwargs_wrapped_knowledge_store_uses_continuity_token(self, mock_db, monkeypatch):
        """kwargs-wrapped calls must unwrap before identity/alias logic."""
        monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "test-secret")
        from src.mcp_handlers.identity.session import create_continuity_token
        from src.mcp_handlers.middleware.identity_step import resolve_identity
        from src.mcp_handlers.middleware.params_step import unwrap_kwargs
        from src.services.tool_dispatch_service import run_tool_dispatch_pipeline

        agent_uuid = "11111111-1111-4111-8111-111111111111"
        token = create_continuity_token(agent_uuid, "agent-stable-session")
        resolve_mock = AsyncMock(return_value={
            "agent_uuid": agent_uuid,
            "persisted": True,
            "source": "token_test",
        })
        alias_probe_args = {}
        handler_args = {}

        async def alias_probe(name, arguments, ctx):
            alias_probe_args.update(arguments)
            return name, arguments, ctx

        async def fake_handler(arguments):
            handler_args.update(arguments)
            return [TextContent(type="text", text="ok")]

        patches = [
            patch("src.mcp_handlers.context.get_session_signals", return_value=None),
            patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="agent-stable-session"),
            patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolve_mock),
            patch("src.db.get_db", return_value=mock_db),
            patch.dict("src.mcp_handlers.TOOL_HANDLERS", {"knowledge": fake_handler}),
        ]
        for p in patches:
            p.start()
        try:
            result = await run_tool_dispatch_pipeline(
                name="knowledge",
                arguments={
                    "kwargs": {
                        "action": "store",
                        "summary": "Token should survive kwargs wrapping",
                        "continuity_token": token,
                    }
                },
                # Put unwrap after identity/alias probes on purpose: the
                # pipeline runner must pre-normalize kwargs before any
                # continuity-sensitive step can inspect the request.
                pre_steps=[resolve_identity, alias_probe, unwrap_kwargs],
                post_steps=[],
            )
        finally:
            for p in reversed(patches):
                p.stop()

        assert result[0].text == "ok"
        assert resolve_mock.await_args.kwargs["token_agent_uuid"] == agent_uuid
        assert alias_probe_args["continuity_token"] == token
        assert alias_probe_args["action"] == "store"
        assert "kwargs" not in handler_args
        assert handler_args["continuity_token"] == token
