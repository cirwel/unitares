"""Both pre-onboard read gates judge a transport header the same way.

A pre-onboard read (check_working_state, get_governance_metrics, a knowledge
search) resolves the caller's identity only when the caller transmitted proof
of its own process session. For a header, that is X-Session-ID: a client sets
it per process. /mcp/ has counted only X-Session-ID since #2475. The REST
prebind took any caller-asserted derivation, which on REST also admitted an
X-Client-Id (or X-MCP-Client-Id) header. That header names a client, not a
process, so two processes of one client could read each other's state.

Both gates now ask ``identity/session.transport_session_is_read_proof``. On
REST the header arrives as a client_session_id that
``_inject_http_client_session`` derived from it, so the injection records which
header produced it and the gate judges that header.

Each case runs the real session-key derivation on both transports. Only
storage (session lookup, onboard pin, Redis, operator lookup) is stubbed.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from src.mcp_handlers.context import SessionSignals
from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")

AGENT_UUID = "1856bb5c-2553-4523-809b-a5d26bbd58d1"
HEADER = "agent-1856bb5c-255"
BODY_SESSION = "agent-5a0e3c1d-77b"
USER_AGENT = "curl/8.7.1"


def _hit():
    return {"agent_uuid": AGENT_UUID, "created": False, "source": "session_binding"}


def _storage_stubs(stack: ExitStack, resolve) -> None:
    db = MagicMock()
    db.update_session_activity = AsyncMock(return_value=True)
    stack.enter_context(patch("src.cache.redis_client.get_redis", AsyncMock(return_value=None)))
    stack.enter_context(patch(
        "src.mcp_handlers.identity.session.lookup_onboard_pin", AsyncMock(return_value=None),
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.middleware.identity_step._load_binding_from_redis",
        AsyncMock(return_value=None),
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.identity.handlers.resolve_session_identity", resolve,
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.identity.operator.resolve_operator_identity",
        AsyncMock(return_value=None),
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.middleware.identity_step._anchor_resolved_identity",
    ))
    stack.enter_context(patch("src.db.get_db", MagicMock(return_value=db)))


def _request(headers: dict) -> Request:
    raw = [(b"user-agent", USER_AGENT.encode())]
    raw += [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/tools/call",
        "headers": raw,
        "client": ("127.0.0.1", 43210),
    })


async def _rest_read(tool_name: str, headers: dict, body: dict | None = None):
    """The REST route's own order (http_routes/tools.http_call_tool): inject
    the session, open an unbound request context, then prebind."""
    from src.http_routes import access
    from src.http_routes.tools import _inject_http_client_session
    from src.mcp_handlers.context import (
        get_context_resolved_agent_id,
        reset_session_context,
        set_session_context,
    )

    resolve = AsyncMock(return_value=_hit())
    request = _request(headers)
    arguments = dict(body or {})
    with ExitStack() as stack:
        _storage_stubs(stack, resolve)
        session_id = await _inject_http_client_session(request, arguments)
        context_token = set_session_context(
            session_key=session_id, client_session_id=session_id,
        )
        try:
            signals = access._build_http_session_signals(request)
            bound = await access._resolve_http_bound_agent(tool_name, arguments, signals)
            context_bound = get_context_resolved_agent_id()
        finally:
            reset_session_context(context_token)
    return bound, context_bound, resolve


async def _mcp_read(tool_name: str, signal_fields: dict, body: dict | None = None):
    from src.mcp_handlers.context import reset_session_signals, set_session_signals
    from src.mcp_handlers.middleware import DispatchContext, resolve_identity
    from src.mcp_handlers.middleware import identity_step

    resolve = AsyncMock(return_value=_hit())
    signals = SessionSignals(
        ip_ua_fingerprint="127.0.0.1:abc123",
        user_agent=USER_AGENT,
        transport="mcp",
        **signal_fields,
    )
    token = set_session_signals(signals)
    try:
        with ExitStack() as stack, patch.object(
            identity_step, "_transport_identity_cache", {}
        ):
            _storage_stubs(stack, resolve)
            ctx = DispatchContext()
            await resolve_identity(tool_name, dict(body or {}), ctx)
    finally:
        reset_session_signals(token)
    return ctx.bound_agent_id, resolve


# (id, REST headers, the same signal on /mcp/, whether it proves a read)
HEADER_CASES = [
    pytest.param({"X-Session-ID": HEADER}, {"x_session_id": HEADER}, True, id="x-session-id"),
    pytest.param({"X-Client-Id": "client-1"}, {"x_client_id": "client-1"}, False, id="x-client-id"),
    pytest.param(
        {"X-MCP-Client-Id": "client-1"}, {"x_client_id": "client-1"}, False, id="x-mcp-client-id"
    ),
    pytest.param({}, {}, False, id="no-header"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["check_working_state", "get_governance_metrics"])
@pytest.mark.parametrize("rest_headers, mcp_signals, proves", HEADER_CASES)
async def test_rest_and_mcp_read_gates_agree_on_each_header(
    read, rest_headers, mcp_signals, proves
):
    rest_bound, rest_context, rest_resolve = await _rest_read(read, rest_headers)
    mcp_bound, mcp_resolve = await _mcp_read(read, mcp_signals)

    expected = AGENT_UUID if proves else None
    assert rest_bound == expected
    assert rest_context == expected
    assert mcp_bound == expected
    # An unproven read never even looks the session up.
    assert rest_resolve.await_count == mcp_resolve.await_count == (1 if proves else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["X-Client-Id", "X-MCP-Client-Id"])
async def test_a_client_session_id_the_caller_sent_still_proves_the_read(header):
    """The narrowing is about the header only: a caller that sends its own
    client_session_id in the body is judged on that, whatever else it sends."""
    bound, _context, resolve = await _rest_read(
        "check_working_state",
        {header: "client-1"},
        body={"client_session_id": BODY_SESSION},
    )

    assert bound == AGENT_UUID
    assert resolve.await_args.args[0] == BODY_SESSION


def test_only_x_session_id_is_transport_read_proof():
    from src.mcp_handlers.identity.session import transport_session_is_read_proof

    assert transport_session_is_read_proof("x_session_id")
    for source in (
        "x_client_id",
        "oauth_client_id",
        "mcp_session_id",
        "pinned_onboard_session",
        "ip_ua_fingerprint",
        "explicit_client_session_id",
        None,
    ):
        assert not transport_session_is_read_proof(source), source


async def _nested_rest_read(headers: dict, nested_arguments: dict):
    """use_tool over REST: the outer route injects the session and sets the
    request context, then the gateway re-enters the prebind for the target."""
    from src.http_routes import access
    from src.http_routes.tools import _inject_http_client_session
    from src.mcp_handlers.context import (
        get_context_resolved_agent_id,
        reset_session_context,
        reset_session_signals,
        set_session_context,
        set_session_signals,
    )
    from src.services import http_tool_service

    resolve = AsyncMock(return_value=_hit())
    request = _request(headers)
    outer = {"tool_name": "check_working_state", "arguments": nested_arguments}
    seen: dict = {}

    async def target(tool_name, arguments):
        seen["bound"] = get_context_resolved_agent_id()
        return {"success": True}

    async def no_rate_limit(name, arguments, ctx):
        return name, arguments, ctx

    with ExitStack() as stack:
        _storage_stubs(stack, resolve)
        stack.enter_context(patch.object(http_tool_service, "execute_http_tool", target))
        stack.enter_context(patch(
            "src.mcp_handlers.middleware.check_rate_limit", no_rate_limit,
        ))
        session_id = await _inject_http_client_session(request, outer)
        signals_token = set_session_signals(access._build_http_session_signals(request))
        context_token = set_session_context(
            session_key=session_id, client_session_id=session_id,
        )
        try:
            await http_tool_service.execute_nested_http_tool(
                "check_working_state", nested_arguments,
            )
        finally:
            reset_session_context(context_token)
            reset_session_signals(signals_token)
    return seen.get("bound"), resolve


@pytest.mark.asyncio
async def test_nested_rest_read_inherits_the_outer_header_judgement():
    bound, resolve = await _nested_rest_read({"X-Client-Id": "client-1"}, {})

    assert bound is None
    assert resolve.await_count == 0


@pytest.mark.asyncio
async def test_nested_rest_read_with_its_own_session_is_judged_on_that_session():
    bound, resolve = await _nested_rest_read(
        {"X-Client-Id": "client-1"}, {"client_session_id": BODY_SESSION},
    )

    assert bound == AGENT_UUID
    assert resolve.await_args.args[0] == BODY_SESSION


@pytest.mark.asyncio
async def test_nested_rest_read_inherits_an_x_session_id_header_as_proof():
    bound, resolve = await _nested_rest_read({"X-Session-ID": HEADER}, {})

    assert bound == AGENT_UUID
    assert resolve.await_args.args[0] == HEADER
