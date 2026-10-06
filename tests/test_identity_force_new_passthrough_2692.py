"""#2692: identity(force_new=true) skipped the session-key resume but then
resolved the same key without force_new, so the resolver could answer a
request for a fresh identity with the key's existing agent, or (since keyed
session ids) with a refusal the handler then indexed as a result.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.identity import handlers, resolution
from src.mcp_handlers.identity.shared import make_client_session_id

BOUND = "5e728ecb-1234-4abc-8def-0123456789ab"


@pytest.mark.asyncio
@pytest.mark.parametrize("sent, expected", [
    (True, True), (False, False), ("true", True), ("false", False), (None, False),
])
async def test_identity_v2_passes_force_new_to_the_resolver(sent, expected):
    """MCP clients may send the flag as a string; "false" must stay false."""
    resolver = AsyncMock(return_value={"agent_uuid": BOUND, "agent_id": BOUND})
    with patch.object(handlers, "resolve_session_identity", resolver):
        await handlers.handle_identity_v2({"force_new": sent}, "some-session-key")

    assert resolver.await_args.kwargs["force_new"] is expected


@pytest.mark.asyncio
async def test_force_new_with_a_refused_session_id_mints_instead_of_failing(monkeypatch):
    """A forged keyed id under force_new: the resolver's session-id checks
    belong to resumes, so the fresh mint proceeds and returns a new agent."""
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "force-new-2692")
    forged = make_client_session_id(BOUND)[:-1] + "a"
    with patch.object(resolution, "_get_redis", return_value=None):
        result = await handlers.handle_identity_v2({"force_new": True}, forged)

    # Without the fix the refusal is indexed as an identity and raises KeyError.
    assert result["agent_uuid"] != BOUND
