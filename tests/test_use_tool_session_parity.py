"""A target named through use_tool resolves exactly as it does named directly.

``tool_registration._invoke_mcp_nested_tool`` used to copy the transport
session (an X-Session-ID header, an Mcp-Session-Id, the OAuth or X-Client-Id
client, or the bare IP:UA fingerprint) into a session-injected target's
``client_session_id`` and flag it transport-injected (b79f737e). A direct
/mcp/ call never does that: FastMCP None-fills the declared argument before the
typed wrapper sees it, so ``derive_session_key`` resolves an omitted id from
the transport signals themselves (tests/test_mcp_x_session_id_read_parity.py).
The same credentials therefore resolved differently through use_tool:

- a header-only ``sync_state`` derived ``x_session_id`` / caller_asserted
  directly, but through use_tool the injected header read as a
  server-inferred explicit id, and ``STRICT_IDENTITY_REQUIRED`` refused it;
- a proof-less ``check_working_state`` short-circuited to unbound directly,
  but through use_tool the injected id counted as proof, the read resolved
  a server-inferred binding and served that agent's state;
- a fingerprint-only write was keyed on the raw fingerprint instead of
  reaching the step-7 onboard pin.

Each test sends one set of credentials twice, once to the target's registered
FastMCP tool and once to the registered ``use_tool``, through the real typed
wrappers, the real tool wrapper and nested invoker, and the real dispatch
runner with the real identity, alias and inject steps. Only storage (session
lookup, onboard pin, Redis sticky recovery, anchor, session TTL) is stubbed.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

from src.mcp_handlers.context import SessionSignals
from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")

AGENT_UUID = "1856bb5c-2553-4523-809b-a5d26bbd58d1"
# Not in the agent-<uuid> form and sent with a Claude user agent, so an id
# copied into client_session_id would also be model-scoped (":claude") and
# land on a different session key from the header's own.
HEADER = "proc-7f3a-session"
USER_AGENT = "claude-code/2.1.0 (cli)"
FINGERPRINT = "127.0.0.1:abc123"
PINNED_SESSION = "agent-1856bb5c-255"
EXPLICIT = "agent-5a0e3c1d-77b"


def _signals(**overrides) -> SessionSignals:
    fields = {
        "x_session_id": HEADER,
        "ip_ua_fingerprint": FINGERPRINT,
        "user_agent": USER_AGENT,
        "transport": "mcp",
    }
    fields.update(overrides)
    return SessionSignals(**fields)


def _hit():
    return {
        "agent_uuid": AGENT_UUID,
        "created": False,
        "persisted": False,
        "source": "session_binding",
    }


async def _pipeline(name, arguments):
    """The real dispatch runner and identity steps, without the steps that
    need live state (trajectory, validation, rate limit, patterns, envelope)."""
    from src.mcp_handlers.middleware import (
        inject_identity,
        resolve_alias,
        resolve_identity,
        strip_untrusted_dispatch_metadata,
        unwrap_kwargs,
    )
    from src.services.tool_dispatch_service import run_tool_dispatch_pipeline

    return await run_tool_dispatch_pipeline(
        name=name,
        arguments=arguments,
        pre_steps=[
            unwrap_kwargs,
            strip_untrusted_dispatch_metadata,
            resolve_identity,
            resolve_alias,
            inject_identity,
        ],
        post_steps=(),
    )


async def _strict_write_gate_outcome() -> str:
    """Run the real process_agent_update identity phase in the live request
    context and report whether the strict write gate refused.

    The bound agent is registered as paused, so a call the gate lets through
    stops at the next guard (AGENT_PAUSED) instead of reaching persistence.
    """
    from src.mcp_handlers.identity_bootstrap import identity_refusal_status
    from src.mcp_handlers.updates.context import UpdateContext
    from src.mcp_handlers.updates.phases import resolve_identity_and_guards

    ctx = UpdateContext(
        arguments={},
        mcp_server=SimpleNamespace(
            agent_metadata={AGENT_UUID: SimpleNamespace(status="paused", paused_at="t")}
        ),
    )
    with patch(
        "src.mcp_handlers.support.pause_ttl.maybe_auto_expire_pause_async",
        AsyncMock(return_value=False),
    ), patch(
        "src.mcp_handlers.support.pause_ttl.paused_refusal_recovery",
        return_value={},
    ), patch(
        "src.identity.substrate.verify_substrate_earned",
        AsyncMock(return_value={"conditions": {}}),
    ):
        result = await resolve_identity_and_guards(ctx)
    payload = json.loads(result[0].text)
    if identity_refusal_status(payload):
        return "refused_strict"
    return payload.get("error_code") or "passed"


async def _call(
    monkeypatch,
    route: str,
    target: str,
    signals: SessionSignals,
    *,
    target_arguments: dict | None = None,
    outer_arguments: dict | None = None,
    pinned: str | None = None,
    strict: bool = False,
) -> dict:
    """Send one call through the registered FastMCP tool for ``route``."""
    import src.mcp_handlers as handlers
    import src.mcp_handlers.core as core_mod
    import src.tool_registration as registration
    from src import mcp_server
    from src.mcp_handlers.context import (
        get_context_agent_id,
        get_context_session_key,
        get_csid_transport_injected,
        get_session_proof_origin,
        get_session_resolution_source,
        reset_session_signals,
        set_session_signals,
    )
    from src.mcp_handlers.middleware import identity_step
    from src.mcp_handlers.updates.phases import _compute_identity_assurance

    seen: dict = {}

    async def write_handler(arguments):
        source = get_session_resolution_source()
        proof = get_session_proof_origin()
        seen.update(
            arguments=dict(arguments),
            session_key=get_context_session_key(),
            source=source,
            proof_origin=proof,
            bound=get_context_agent_id(),
            transport_injected=get_csid_transport_injected(),
            caller_proven=_compute_identity_assurance(source, None, proof)["caller_proven"],
        )
        if strict:
            seen["strict_gate"] = await _strict_write_gate_outcome()
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    resolve = AsyncMock(return_value=_hit())
    pin_lookup = AsyncMock(return_value=pinned)
    metrics = AsyncMock(return_value={"agent_id": AGENT_UUID, "state": "real"})
    db = MagicMock()
    db.update_session_activity = AsyncMock(return_value=True)
    db.is_substrate_earned = AsyncMock(return_value=False)

    # The target's own resolution calls. Through use_tool the outer use_tool
    # dispatch runs the identity step too (and resolves when the caller sent
    # read proof), so the totals differ by that one outer call.
    target_resolves: list = []
    target_pins: list = []

    async def dispatch(name, arguments):
        resolves_before = len(resolve.await_args_list)
        pins_before = len(pin_lookup.await_args_list)
        result = await _pipeline(name, arguments)
        if name != "use_tool":
            target_resolves.extend(
                call.args[0] for call in resolve.await_args_list[resolves_before:]
            )
            target_pins.extend(
                call.args[0] for call in pin_lookup.await_args_list[pins_before:]
            )
        return result

    monkeypatch.setattr(registration, "dispatch_tool", dispatch)
    monkeypatch.setattr(registration, "record_tool_usage", lambda **_: None)
    monkeypatch.setattr(registration, "_wave3a_get_route", lambda _name: None)
    monkeypatch.setitem(handlers.TOOL_HANDLERS, "process_agent_update", write_handler)
    monkeypatch.setattr(identity_step, "_transport_identity_cache", {})
    if strict:
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    else:
        monkeypatch.delenv("STRICT_IDENTITY_REQUIRED", raising=False)
    registration._tool_wrappers_cache.clear()

    if route == "direct":
        tool = mcp_server.mcp._tool_manager.get_tool(target)
        payload = dict(target_arguments or {})
    else:
        tool = mcp_server.mcp._tool_manager.get_tool("use_tool")
        payload = {"tool_name": target, "arguments": dict(target_arguments or {})}
        payload.update(outer_arguments or {})
    assert tool is not None, route

    token = set_session_signals(signals)
    try:
        with ExitStack() as stack:
            # No live Redis: the shadow pin observation in derive_session_key
            # and the sticky-binding recovery both reach for it.
            stack.enter_context(patch(
                "src.cache.redis_client.get_redis", AsyncMock(return_value=None),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._load_binding_from_redis",
                AsyncMock(return_value=None),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.session.lookup_onboard_pin", pin_lookup,
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.handlers.resolve_session_identity", resolve,
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._anchor_resolved_identity",
            ))
            stack.enter_context(patch("src.db.get_db", MagicMock(return_value=db)))
            stack.enter_context(patch.object(
                core_mod.mcp_server,
                "agent_metadata",
                {AGENT_UUID: SimpleNamespace(label="a", status="active")},
            ))
            stack.enter_context(patch(
                "src.services.runtime_queries.get_governance_metrics_data", metrics,
            ))
            result = await tool.run(payload, context=SimpleNamespace(), convert_result=False)
    finally:
        reset_session_signals(token)
        registration._tool_wrappers_cache.clear()

    seen["result"] = result
    seen["resolve_calls"] = target_resolves
    seen["pin_candidates"] = target_pins
    seen["metrics_served"] = metrics.await_count > 0
    return seen


async def _both(monkeypatch, target, signals, **kwargs):
    direct = await _call(monkeypatch, "direct", target, signals, **kwargs)
    nested = await _call(monkeypatch, "use_tool", target, signals, **kwargs)
    return direct, nested


@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["sync_state", "process_agent_update"])
async def test_header_only_nested_write_derives_the_header_like_a_direct_write(
    monkeypatch, write
):
    direct, nested = await _both(monkeypatch, write, _signals())

    for seen in (direct, nested):
        assert "client_session_id" not in seen["arguments"]
        assert seen["transport_injected"] is False
        assert seen["source"] == "x_session_id"
        assert seen["proof_origin"] == "caller_asserted"
        assert seen["caller_proven"] is True
        assert seen["session_key"] == HEADER
        assert seen["bound"] == AGENT_UUID
    assert nested["resolve_calls"] == direct["resolve_calls"] == [HEADER]


@pytest.mark.asyncio
async def test_header_only_nested_write_passes_the_strict_gate_like_a_direct_write(
    monkeypatch,
):
    """The user-visible symptom: under STRICT_IDENTITY_REQUIRED a header-only
    sync_state passed directly and was refused through use_tool."""
    direct, nested = await _both(monkeypatch, "sync_state", _signals(), strict=True)

    # AGENT_PAUSED is the guard after the strict gate: the gate let it through.
    assert direct["strict_gate"] == nested["strict_gate"] == "AGENT_PAUSED"


@pytest.mark.asyncio
async def test_fingerprint_only_nested_write_reaches_the_onboard_pin_like_a_direct_write(
    monkeypatch,
):
    signals = _signals(x_session_id=None, user_agent="curl/8.7.1")
    direct, nested = await _both(
        monkeypatch, "sync_state", signals, pinned=PINNED_SESSION
    )

    for seen in (direct, nested):
        assert "client_session_id" not in seen["arguments"]
        assert seen["source"] == "pinned_onboard_session"
        assert seen["proof_origin"] == "server_inferred"
        assert seen["caller_proven"] is False
        assert seen["session_key"] == PINNED_SESSION
    from src.mcp_handlers.identity.session import _extract_base_fingerprint

    # The step-7 lookup probed the fingerprint's pin, not a copied-in id.
    assert direct["pin_candidates"] == nested["pin_candidates"] == [
        _extract_base_fingerprint(FINGERPRINT)
    ]
    assert nested["resolve_calls"] == direct["resolve_calls"] == [PINNED_SESSION]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signals",
    [
        pytest.param(_signals(x_session_id=None), id="fingerprint-only"),
        pytest.param(
            _signals(x_session_id=None, mcp_session_id="conn-1"), id="mcp-session-id"
        ),
        pytest.param(_signals(x_session_id=None, x_client_id="client-1"), id="x-client-id"),
        pytest.param(
            _signals(x_session_id=None, oauth_client_id="oauth-1"), id="oauth-client-id"
        ),
    ],
)
async def test_proofless_nested_read_short_circuits_to_unbound_like_a_direct_read(
    monkeypatch, signals
):
    direct, nested = await _both(monkeypatch, "check_working_state", signals)

    for seen in (direct, nested):
        payload = seen["result"]
        assert payload["status"] == "⚪ unbound", payload
        assert seen["resolve_calls"] == []
        assert seen["metrics_served"] is False
    def stable(payload):
        return {k: v for k, v in payload.items() if k != "server_time"}

    assert stable(nested["result"]) == stable(direct["result"])


@pytest.mark.asyncio
async def test_explicit_outer_session_still_reaches_the_nested_target(monkeypatch):
    """An outer client_session_id on use_tool is caller input: the target
    resolves on it, caller-proven, exactly as the same id sent directly."""
    direct = await _call(
        monkeypatch,
        "direct",
        "sync_state",
        _signals(),
        target_arguments={"client_session_id": EXPLICIT},
    )
    nested = await _call(
        monkeypatch,
        "use_tool",
        "sync_state",
        _signals(),
        outer_arguments={"client_session_id": EXPLICIT},
    )

    for seen in (direct, nested):
        assert seen["arguments"]["client_session_id"] == EXPLICIT
        assert seen["transport_injected"] is False
        assert seen["source"] == "explicit_client_session_id"
        assert seen["proof_origin"] == "caller_asserted"
        assert seen["session_key"] == EXPLICIT
    assert nested["resolve_calls"] == direct["resolve_calls"] == [EXPLICIT]
