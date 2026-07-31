"""Tests for `src/mcp_handlers/dialectic/auth.py` — the dialectic identity resolver.

#1414: `resolve_dialectic_agent_id` must ALWAYS return an agent UUID. Every
consumer compares the result against `session.paused_agent_id` /
`session.reviewer_agent_id` (UUIDs) and writes it into
`core.dialectic_messages.agent_id` (UUIDs). Returning the public handle — which
is what `require_registered_agent` leaves in `arguments["agent_id"]` — makes
every submit path fail with "not registered".
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

AUTH = "src.mcp_handlers.dialectic.auth"

PAUSED_UUID = "3b531b97-a39d-4b95-aeb3-91a1003c9685"
PAUSED_HANDLE = "Claude_Opus_5_20260730"
OTHER_UUID = "1f0c9e2a-7b44-4d6e-9c31-5aa8e0d21bb7"


def _meta(public_agent_id=None, structured_id=None, label=None):
    meta = MagicMock()
    meta.public_agent_id = public_agent_id
    meta.structured_id = structured_id
    meta.label = label
    meta.status = "active"
    return meta


def _server(metadata):
    server = MagicMock()
    server.agent_metadata = metadata
    server.ensure_metadata_loaded = MagicMock()
    return server


def _payload(error_seq):
    return json.loads(error_seq[0].text)


class TestCanonicalization:
    @pytest.mark.asyncio
    async def test_public_handle_resolves_to_uuid(self):
        """#1414 regression lock: the public handle must map to its UUID.

        This is the exact shape the one-call-review path produced —
        `require_registered_agent` rewrites arguments["agent_id"] to the
        public handle, and the nested submit_thesis then re-resolves it.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        with patch(f"{AUTH}.mcp_server", _server(metadata)):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_HANDLE}
            )

        assert error is None
        assert agent_id == PAUSED_UUID

    @pytest.mark.asyncio
    async def test_structured_id_resolves_to_uuid(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(structured_id="claude_opus_5:20260730")}
        with patch(f"{AUTH}.mcp_server", _server(metadata)):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "claude_opus_5:20260730"}
            )

        assert error is None
        assert agent_id == PAUSED_UUID

    @pytest.mark.asyncio
    async def test_label_does_not_resolve(self):
        """No-lookup-by-label invariant: labels are self-claimed, never verified.

        `require_registered_agent` will match on label; the dialectic resolver
        deliberately will not.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {
            PAUSED_UUID: _meta(
                public_agent_id=PAUSED_HANDLE,
                label="Claude trajectory-identity audit",
            )
        }
        with patch(f"{AUTH}.mcp_server", _server(metadata)):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "Claude trajectory-identity audit"}
            )

        assert agent_id is None
        assert error is not None
        assert _payload(error)["success"] is False

    @pytest.mark.asyncio
    async def test_uuid_passes_through_without_touching_postgres(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        pg = AsyncMock(return_value=True)
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.identity.handlers._agent_exists_in_postgres", pg
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_UUID}
            )

        assert error is None
        assert agent_id == PAUSED_UUID
        pg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cold_cache_uuid_falls_back_to_postgres(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        pg = AsyncMock(return_value=True)
        with patch(f"{AUTH}.mcp_server", _server({})), \
             patch(
                 "src.mcp_handlers.identity.handlers._agent_exists_in_postgres", pg
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_UUID}
            )

        assert error is None
        assert agent_id == PAUSED_UUID
        pg.assert_awaited_once_with(PAUSED_UUID)

    @pytest.mark.asyncio
    async def test_unknown_ref_errors_and_does_not_advise_onboard(self):
        """The old recovery told an already-registered caller to onboard(),
        which mints a NEW identity and orphans the session further."""
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        pg = AsyncMock(return_value=False)
        with patch(f"{AUTH}.mcp_server", _server({})), \
             patch(
                 "src.mcp_handlers.identity.handlers._agent_exists_in_postgres", pg
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "Nobody_In_Particular"}
            )

        assert agent_id is None
        data = _payload(error)
        assert data["success"] is False
        recovery = data.get("recovery") or {}
        # onboard() must not be offered as a route out: it mints a NEW identity,
        # which cannot help a caller who is already registered.
        assert "onboard" not in recovery.get("related_tools", [])
        assert "do not call onboard" in recovery.get("action", "").lower()

    @pytest.mark.asyncio
    async def test_no_agent_id_uses_require_registered_agent(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        with patch(f"{AUTH}.mcp_server", _server({})), \
             patch(
                 f"{AUTH}.require_registered_agent",
                 return_value=(PAUSED_UUID, None),
             ) as rra:
            agent_id, error = await resolve_dialectic_agent_id({})

        assert error is None
        assert agent_id == PAUSED_UUID
        rra.assert_called_once()


class TestForgeryResistance:
    @pytest.mark.asyncio
    async def test_forged_agent_uuid_is_ignored(self):
        """`_agent_uuid` is caller-reachable (params_step preserves unknown keys
        and it is not scrubbed), so the resolver must never read it. Preferring
        it would let `agent_id=<mine> _agent_uuid=<victim>` impersonate."""
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {
            OTHER_UUID: _meta(public_agent_id="Attacker_Handle"),
            PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE),
        }
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=OTHER_UUID,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "Attacker_Handle", "_agent_uuid": PAUSED_UUID}
            )

        assert error is None
        assert agent_id == OTHER_UUID, "resolver must not honour a wire _agent_uuid"


class TestSessionOwnership:
    @pytest.mark.asyncio
    async def test_ownership_check_receives_the_uuid_not_the_handle(self):
        """The ownership gate compares against `get_context_agent_id()`, which is
        a UUID. Handing it a public handle made it fail closed for the rightful
        owner — the second half of the #1414 break."""
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=PAUSED_UUID,
             ), \
             patch(
                 "src.mcp_handlers.utils.verify_agent_ownership",
                 return_value=True,
             ) as vao:
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_HANDLE}, enforce_session_ownership=True
            )

        assert error is None
        assert agent_id == PAUSED_UUID
        assert vao.call_args[0][0] == PAUSED_UUID

    @pytest.mark.asyncio
    async def test_ownership_check_rejects_another_agent(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=OTHER_UUID,
             ), \
             patch(
                 "src.mcp_handlers.utils.verify_agent_ownership",
                 return_value=False,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_HANDLE}, enforce_session_ownership=True
            )

        assert agent_id is None
        assert _payload(error)["error_code"] == "AUTH_REQUIRED"
