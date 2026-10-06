"""#2692: identity(force_new=true) skipped the session-key resume but then
resolved the same key without force_new, so the resolver ran resume checks
for a fresh mint; since keyed session ids, a refusal from those checks was
indexed as a result. Through the public tool the identity middleware
refuses a forged session id first (pinned below), so the handler change
hardens direct callers; the flag is also coerced from MCP strings.
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


@pytest.mark.asyncio
async def test_dispatch_refuses_a_forged_session_id_even_with_force_new(monkeypatch):
    """Through the public tool path the identity middleware validates the
    presented id first and refuses a forged one, force_new or not: a tampered
    credential is refused loudly rather than swapped for a fresh identity.
    The handler-level pass-through above covers callers that reach
    handle_identity_v2 without that middleware."""
    import json

    from src.mcp_handlers.context import SessionSignals
    from src.mcp_handlers.identity import stable_session
    from src.mcp_handlers.middleware.identity_step import resolve_identity
    from src.services.tool_dispatch_service import run_tool_dispatch_pipeline

    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "force-new-2692")
    stable_session.forget_verified()
    forged = make_client_session_id(BOUND)[:-1] + "a"
    handler = AsyncMock(return_value=["handler-ran"])
    signals = SessionSignals(transport="rest", ip_ua_fingerprint="127.0.0.1:test-agent")
    with patch("src.mcp_handlers.context.get_session_signals", return_value=signals), \
         patch.object(stable_session, "_candidates", AsyncMock(return_value=[
             {"agent_id": BOUND, "status": "active", "disabled_at": None},
         ])), \
         patch.object(resolution, "_get_redis", return_value=None), \
         patch("src.mcp_handlers.TOOL_HANDLERS", {"identity": handler}):
        result = await run_tool_dispatch_pipeline(
            name="identity",
            arguments={"force_new": True, "client_session_id": forged},
            pre_steps=[resolve_identity],
            post_steps=[],
        )

    handler.assert_not_awaited()
    payload = json.loads(result[0].text)
    assert payload["surface_context"]["resume_rejected_reason"] == "stable_session_id_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [False, "false", None, True, "true"])
async def test_the_adapter_reads_the_flag_once_before_the_s13_default(sent):
    """With no proof signal, the S13 default mints whatever form false takes,
    and the coerced value is what handle_identity_v2 sees."""
    seen = {}

    async def v2(arguments, session_key, model_type=None):
        seen["force_new"] = arguments["force_new"]
        raise RuntimeError("stop after the handler is reached")

    args = {} if sent is None else {"force_new": sent}
    with patch.object(handlers, "handle_identity_v2", v2), \
         patch.object(handlers, "derive_session_key", AsyncMock(return_value="fp-key")), \
         patch.object(handlers, "_try_resume_by_agent_uuid_direct", AsyncMock(return_value=None)), \
         patch.object(handlers, "_try_resume_by_session_key", AsyncMock(return_value=(None, None))):
        await handlers.handle_identity_adapter(args)  # the tool wrapper reports the stop

    assert seen["force_new"] is True
