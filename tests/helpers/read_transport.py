"""Drive a metrics read, or a write, through the real transport path.

``mcp_call`` sends one call through the registered FastMCP tool (the target,
or ``use_tool`` wrapping it): the real typed wrapper, tool wrapper and nested
invoker, and the real dispatch runner with the identity, alias, inject and
validation steps and the experience envelope, into the real handler.
``rest_call`` follows the REST route's own order
(http_routes/tools.http_call_tool): session injection, then the prebind and
``execute_http_tool`` in the route's request context.

Only storage is stubbed: the session lookup (``resolve_session_identity``,
whose result each test chooses), the onboard pin, Redis, the metrics
producer, the agent registry, and usage telemetry. Callers must also use the
``no_live_redis`` fixture.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.types import TextContent
from starlette.requests import Request

from src.mcp_handlers.context import SessionSignals

AGENT_UUID = "1856bb5c-2553-4523-809b-a5d26bbd58d1"
OTHER_UUID = "5a0e3c1d-77b2-4f10-9c3e-0d9a1b2c3d4e"
AGENT_SESSION = "agent-1856bb5c-255"
FINGERPRINT = "127.0.0.1:abc123"
USER_AGENT = "claude-code/2.1.0 (cli)"
CONNECTION = "conn-7f3a"
REST_UA = "curl/8.7.1"
REST_FINGERPRINT = f"127.0.0.1:{hashlib.md5(REST_UA.encode()).hexdigest()[:6]}"


def signals(**overrides) -> SessionSignals:
    fields = {
        "ip_ua_fingerprint": FINGERPRINT,
        "user_agent": USER_AGENT,
        "transport": "mcp",
    }
    fields.update(overrides)
    return SessionSignals(**fields)


def hit(agent_uuid: str = AGENT_UUID) -> dict:
    return {
        "agent_uuid": agent_uuid,
        "created": False,
        "persisted": False,
        "source": "session_binding",
    }


def sticky_binding():
    from src.mcp_handlers.middleware.identity_step import TransportBinding

    return TransportBinding(
        agent_uuid=AGENT_UUID,
        session_key=AGENT_SESSION,
        bound_at=time.monotonic(),
        source="post_handler:onboard",
        original_session_source="explicit_client_session_id",
    )


def registry() -> dict:
    return {
        AGENT_UUID: SimpleNamespace(label="a", status="active"),
        OTHER_UUID: SimpleNamespace(label="b", status="active"),
    }


def is_unbound(payload: dict) -> bool:
    """The raw unbound payload, or check_working_state's envelope of it."""
    if payload.get("status") == "⚪ unbound":
        return True
    summary = payload.get("action_summary")
    return isinstance(summary, dict) and summary.get("verdict") == "unbound"


def _resolver(resolve_result):
    if isinstance(resolve_result, BaseException):
        return AsyncMock(side_effect=resolve_result)
    return AsyncMock(return_value=resolve_result)


async def _pipeline(name, arguments):
    """The real dispatch runner, without the steps that need live state
    (trajectory, rate limit, patterns). The envelope runs, so
    check_working_state returns what a caller receives."""
    from src.mcp_handlers.middleware import (
        apply_experience_envelope,
        inject_identity,
        resolve_alias,
        resolve_identity,
        strip_untrusted_dispatch_metadata,
        unwrap_kwargs,
        validate_params,
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
            validate_params,
        ],
        post_steps=(),
        post_execution_steps=[apply_experience_envelope],
    )


async def mcp_call(
    monkeypatch,
    route: str,
    target: str,
    call_signals: SessionSignals,
    *,
    target_arguments: dict | None = None,
    sticky: dict | None = None,
    pinned: str | None = None,
    resolve_result=None,
    strict: bool = False,
) -> dict:
    """One call through the registered FastMCP tool for ``route``
    ("direct" names the target, "use_tool" wraps it)."""
    import src.mcp_handlers as handlers
    import src.mcp_handlers.core as core_mod
    import src.tool_registration as registration
    from src import mcp_server
    from src.mcp_handlers.context import (
        get_context_agent_id,
        get_session_proof_origin,
        get_session_resolution_source,
        reset_session_signals,
        set_session_signals,
    )
    from src.mcp_handlers.middleware import identity_step

    seen: dict = {}

    async def write_handler(arguments):
        seen.update(
            bound=get_context_agent_id(),
            source=get_session_resolution_source(),
            proof_origin=get_session_proof_origin(),
        )
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    resolve = _resolver(hit() if resolve_result is None else resolve_result)
    metrics = AsyncMock(return_value={"agent_id": AGENT_UUID, "state": "real"})
    db = MagicMock()
    db.update_session_activity = AsyncMock(return_value=True)

    target_resolves: list = []

    async def dispatch(name, arguments):
        before = len(resolve.await_args_list)
        result = await _pipeline(name, arguments)
        if name != "use_tool":
            target_resolves.extend(
                call.args[0] for call in resolve.await_args_list[before:]
            )
        return result

    monkeypatch.setattr(registration, "dispatch_tool", dispatch)
    monkeypatch.setattr(registration, "record_tool_usage", lambda **_: None)
    monkeypatch.setattr(registration, "_wave3a_get_route", lambda _name: None)
    monkeypatch.setitem(handlers.TOOL_HANDLERS, "process_agent_update", write_handler)
    monkeypatch.setattr(identity_step, "_transport_identity_cache", dict(sticky or {}))
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
    assert tool is not None, route

    token = set_session_signals(call_signals)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._load_binding_from_redis",
                AsyncMock(return_value=None),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._persist_binding_to_redis",
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.session.lookup_onboard_pin",
                AsyncMock(return_value=pinned),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.handlers.resolve_session_identity", resolve,
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._lookup_core_agent_row_status",
                AsyncMock(return_value="active"),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._anchor_resolved_identity",
            ))
            stack.enter_context(patch("src.db.get_db", MagicMock(return_value=db)))
            stack.enter_context(patch.object(
                core_mod.mcp_server, "agent_metadata", registry(),
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
    seen["metrics_served"] = metrics.await_count > 0
    seen["metrics_for"] = [call.args[0] for call in metrics.await_args_list]
    return seen


def rest_request(headers: dict | None = None) -> Request:
    raw = [(b"user-agent", REST_UA.encode())]
    raw += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/tools/call",
        "headers": raw,
        "client": ("127.0.0.1", 43210),
    })


async def rest_call(
    monkeypatch,
    tool_name: str,
    arguments: dict,
    *,
    headers: dict | None = None,
    sticky: dict | None = None,
    resolve_result=None,
    strict: bool = False,
):
    """``(payload, metrics_mock, resolve_mock)`` for one REST call."""
    import src.mcp_handlers.core as core_mod
    import src.services.http_tool_service as hts
    from src.http_routes.tools import (
        _execute_http_tool_in_context,
        _inject_http_client_session,
    )

    if strict:
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    else:
        monkeypatch.delenv("STRICT_IDENTITY_REQUIRED", raising=False)
    resolve = _resolver(hit() if resolve_result is None else resolve_result)
    metrics = AsyncMock(return_value={"agent_id": AGENT_UUID, "state": "real"})
    db = MagicMock()
    db.update_session_activity = AsyncMock(return_value=True)
    request = rest_request(headers)
    arguments = dict(arguments)
    with ExitStack() as stack:
        stack.enter_context(patch(
            "src.mcp_handlers.middleware.identity_step._transport_identity_cache",
            dict(sticky or {}),
        ))
        stack.enter_context(patch(
            "src.mcp_handlers.middleware.identity_step._persist_binding_to_redis",
        ))
        stack.enter_context(patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            AsyncMock(return_value=None),
        ))
        stack.enter_context(patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity", resolve,
        ))
        stack.enter_context(patch(
            "src.mcp_handlers.identity.operator.resolve_operator_identity",
            AsyncMock(return_value=None),
        ))
        stack.enter_context(patch("src.db.get_db", MagicMock(return_value=db)))
        stack.enter_context(patch.object(hts, "record_tool_usage", lambda **_: None))
        stack.enter_context(patch.object(hts, "wave3a_get_route", lambda _name: None))
        stack.enter_context(patch.object(hts, "get_governance_metrics_data", metrics))
        stack.enter_context(patch(
            "src.services.runtime_queries.get_governance_metrics_data", metrics,
        ))
        stack.enter_context(patch.object(
            core_mod.mcp_server, "agent_metadata", registry(),
        ))
        session_id = await _inject_http_client_session(request, arguments)
        result = await _execute_http_tool_in_context(
            request, tool_name, arguments, session_id,
        )
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        result = json.loads(result[0].text)
    return result, metrics, resolve
