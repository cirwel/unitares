"""A synthesis comes only from a caller bound as the identity it submits for.

A synthesis can converge a dialectic session and trigger resolution.
``handle_submit_synthesis`` checks that the named agent is a participant (the
paused agent or the reviewer); that allow-list says which identity is named,
not who is calling. Synthesis also requires the resolver-stamped caller to
equal the resolved agent.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

AUTH = "src.mcp_handlers.dialectic.auth"
DIALECTIC = "src.mcp_handlers.dialectic.handlers"
PAUSED = "22222222-2222-4222-8222-222222222222"
REVIEWER = "33333333-3333-4333-8333-333333333333"


def _server():
    server = MagicMock()
    meta = MagicMock()
    meta.public_agent_id = None
    meta.structured_id = None
    meta.label = None
    meta.status = "active"
    server.agent_metadata = {PAUSED: meta, REVIEWER: meta}
    return server


def _session():
    session = MagicMock()
    session.session_id = "sess-bound"
    session.paused_agent_id = PAUSED
    session.reviewer_agent_id = REVIEWER
    return session


def _run_args(run_method, arguments):
    """Call FastMCP's internal Tool.run on both supported SDK majors."""
    params = inspect.signature(run_method).parameters
    if "context" in params and params["context"].default is inspect.Parameter.empty:
        return (arguments, None)
    return (arguments,)


def _transport_arguments(action: str, identity_case: str):
    caller = PAUSED if action == "thesis" else REVIEWER
    other = REVIEWER if caller == PAUSED else PAUSED
    arguments = {
        "action": action,
        "session_id": "sess-bound",
        "client_session_id": "caller-owned-session",
        "root_cause": "rc",
        "proposed_conditions": ["c"],
        "reasoning": "r",
        "observed_metrics": {},
        "concerns": ["x"],
        "agrees": True,
    }
    if identity_case == "self":
        arguments["agent_id"] = caller
    elif identity_case == "other":
        arguments["agent_id"] = other
    return caller, arguments


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/tools/call",
        "headers": [(b"user-agent", b"dialectic-boundary-test")],
        "client": ("127.0.0.1", 41000),
    })


def _install_transport_seams(monkeypatch, caller: str, load: AsyncMock):
    """Keep the real transport/middleware/router/auth chain, stub its stores."""
    server = _server()
    identity_result = {
        "agent_uuid": caller,
        "created": False,
        "persisted": False,
        "source": "client_session_id",
        "core_agent_row_status": "active",
    }
    resolve = AsyncMock(return_value=identity_result)

    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "false")
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")
    monkeypatch.setattr(f"{AUTH}.mcp_server", server)
    monkeypatch.setattr("src.mcp_handlers.shared.get_mcp_server", lambda: server)
    monkeypatch.setattr(f"{DIALECTIC}.load_session", load)
    monkeypatch.setattr(
        f"{DIALECTIC}.get_session_lock",
        AsyncMock(return_value=asyncio.Lock()),
    )
    monkeypatch.setattr(
        "src.mcp_handlers.identity.handlers.derive_session_key",
        AsyncMock(return_value="resolved-session-key"),
    )
    monkeypatch.setattr(
        "src.mcp_handlers.identity.handlers.resolve_session_identity",
        resolve,
    )
    monkeypatch.setattr(
        "src.mcp_handlers.identity.operator.resolve_operator_identity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.http_routes.access._touch_http_session_activity",
        AsyncMock(),
    )
    monkeypatch.setattr("src.tool_registration._wave3a_get_route", lambda _name: None)
    monkeypatch.setattr("src.tool_registration.record_tool_usage", lambda **_kwargs: None)
    monkeypatch.setattr(
        "src.services.http_tool_service.wave3a_get_route", lambda _name: None
    )
    monkeypatch.setattr(
        "src.services.http_tool_service.record_tool_usage", lambda **_kwargs: None
    )

    limiter = MagicMock()
    limiter.check_rate_limit.return_value = (True, None)
    limiter.get_stats.return_value = {}
    monkeypatch.setattr(
        "src.mcp_handlers.middleware.rate_limit_step.get_rate_limiter",
        lambda: limiter,
    )
    monkeypatch.setattr(
        "src.pattern_tracker.get_pattern_tracker", lambda: MagicMock()
    )
    return resolve


async def _submit(resolved_caller, agent_id=REVIEWER):
    from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

    load = AsyncMock(return_value=_session())
    with patch(f"{AUTH}.mcp_server", _server()), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=resolved_caller), \
         patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=resolved_caller), \
         patch(f"{DIALECTIC}.load_session", load), \
         patch(f"{DIALECTIC}.get_session_lock", new_callable=AsyncMock, return_value=asyncio.Lock()):
        result = await handle_submit_synthesis({
            "session_id": "sess-bound",
            "agent_id": agent_id,
            "agrees": True,
            "proposed_conditions": ["c"],
            "root_cause": "rc",
            "reasoning": "r",
        })
    return json.loads(result[0].text), load


@pytest.mark.asyncio
async def test_an_unbound_caller_naming_the_reviewer_is_refused_before_the_session_loads():
    payload, load = await _submit(resolved_caller=None)
    assert payload["success"] is False
    assert payload.get("error_code") == "AUTH_REQUIRED"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_caller_bound_as_one_participant_cannot_submit_as_the_other():
    payload, load = await _submit(resolved_caller=PAUSED, agent_id=REVIEWER)
    assert payload["success"] is False
    assert payload.get("error_code") == "AUTH_REQUIRED"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_caller_bound_as_the_reviewer_passes_the_caller_check():
    """Past the check the handler loads the session; what happens next is not this test's."""
    payload, load = await _submit(resolved_caller=REVIEWER, agent_id=REVIEWER)
    assert payload.get("error_code") != "AUTH_REQUIRED"
    load.assert_awaited()


def test_caller_is_bound_as_reads_the_resolver_stamped_slot():
    from src.mcp_handlers.dialectic.auth import caller_is_bound_as

    with patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=REVIEWER):
        assert caller_is_bound_as(REVIEWER) is False
    with patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=REVIEWER):
        assert caller_is_bound_as(REVIEWER) is True
        assert caller_is_bound_as(PAUSED) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["thesis", "antithesis"])
async def test_thesis_and_antithesis_require_the_same_bound_caller(action):
    """The session-ownership check only runs when a binding exists."""
    from src.mcp_handlers.dialectic import handlers

    handler = {
        "thesis": handlers.handle_submit_thesis,
        "antithesis": handlers.handle_submit_antithesis,
    }[action]
    load = AsyncMock(return_value=_session())
    named = PAUSED if action == "thesis" else REVIEWER
    with patch(f"{AUTH}.mcp_server", _server()), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=None), \
         patch(f"{DIALECTIC}.load_session", load):
        result = await handler({
            "session_id": "sess-bound",
            "agent_id": named,
            "root_cause": "rc",
            "proposed_conditions": ["c"],
            "reasoning": "r",
            "concerns": ["x"],
        })
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert payload.get("error_code") == "AUTH_REQUIRED"
    load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"agent_id": None}, {"agent_id": ""}])
async def test_omitted_agent_id_still_requires_a_resolver_stamped_caller(arguments):
    from src.mcp_handlers.context import reset_session_context, set_session_context
    from src.mcp_handlers.dialectic.auth import (
        caller_is_bound_as,
        resolve_dialectic_agent_id,
    )

    token = set_session_context(agent_id=REVIEWER)
    try:
        with patch("src.mcp_handlers.shared.get_mcp_server", return_value=_server()):
            # Registration can find this identity, but the generic transport
            # context is not proof that identity resolution bound the caller.
            assert caller_is_bound_as(REVIEWER) is False
            agent_id, error = await resolve_dialectic_agent_id(
                dict(arguments), require_bound_caller=True,
            )
    finally:
        reset_session_context(token)

    assert agent_id is None
    assert json.loads(error[0].text)["error_code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["thesis", "antithesis", "synthesis"])
@pytest.mark.parametrize("bound", [False, True])
async def test_omitted_agent_id_checks_binding_before_loading_a_session(action, bound):
    from src.mcp_handlers.context import (
        reset_session_context,
        set_session_context,
        update_context_agent_id,
    )
    from src.mcp_handlers.dialectic import handlers

    handler = getattr(handlers, f"handle_submit_{action}")
    caller = PAUSED if action == "thesis" else REVIEWER
    load = AsyncMock(return_value=None)
    token = set_session_context(agent_id=caller)
    try:
        if bound:
            update_context_agent_id(caller)
        with patch("src.mcp_handlers.shared.get_mcp_server", return_value=_server()), \
             patch(f"{DIALECTIC}.load_session", load):
            result = await handler({
                "session_id": "sess-bound",
                "agrees": True,
                "root_cause": "rc",
                "proposed_conditions": ["c"],
                "reasoning": "r",
                "concerns": ["x"],
            })
    finally:
        reset_session_context(token)

    payload = json.loads(result[0].text)
    if bound:
        assert payload.get("error_code") != "AUTH_REQUIRED"
        load.assert_awaited_once_with("sess-bound")
    else:
        assert payload["success"] is False
        assert payload.get("error_code") == "AUTH_REQUIRED"
        load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["thesis", "antithesis", "synthesis"])
@pytest.mark.parametrize("identity_case", ["omitted", "self", "other"])
async def test_mcp_dialectic_submit_preserves_resolved_caller(
    monkeypatch, action, identity_case,
):
    """The registered MCP tool carries resolver proof to every submit handler."""
    from src.mcp_handlers.context import get_context_resolved_agent_id
    import src.mcp_server as mcp_server

    caller, arguments = _transport_arguments(action, identity_case)
    observed = []

    async def _load(session_id):
        observed.append(get_context_resolved_agent_id())
        return None

    load = AsyncMock(side_effect=_load)
    _install_transport_seams(monkeypatch, caller, load)

    tool = mcp_server.mcp._tool_manager.get_tool("dialectic")
    await tool.run(
        *_run_args(tool.run, arguments),
        convert_result=False,
    )

    if identity_case == "other":
        load.assert_not_awaited()
        assert observed == []
    else:
        load.assert_awaited_once_with("sess-bound")
        assert observed == [caller]
    assert get_context_resolved_agent_id() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["thesis", "antithesis", "synthesis"])
@pytest.mark.parametrize("identity_case", ["omitted", "self", "other"])
async def test_rest_dialectic_submit_preserves_resolved_caller(
    monkeypatch, action, identity_case,
):
    """REST prebinding and fallback preserve the same submit authorization."""
    from src.http_routes.tools import _execute_http_tool_in_context
    from src.mcp_handlers.context import get_context_resolved_agent_id

    caller, arguments = _transport_arguments(action, identity_case)
    observed = []

    async def _load(session_id):
        observed.append(get_context_resolved_agent_id())
        return None

    load = AsyncMock(side_effect=_load)
    _install_transport_seams(monkeypatch, caller, load)

    await _execute_http_tool_in_context(
        _request(),
        "dialectic",
        arguments,
        arguments["client_session_id"],
    )

    if identity_case == "other":
        load.assert_not_awaited()
        assert observed == []
    else:
        load.assert_awaited_once_with("sess-bound")
        assert observed == [caller]
    assert get_context_resolved_agent_id() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    [
        {
            "agent_uuid": REVIEWER,
            "resume_failed": True,
            "error": "substrate_http_reject",
            "created": False,
        },
        {
            "agent_uuid": REVIEWER,
            "error": "future_terminal_refusal",
            "created": False,
        },
    ],
)
async def test_rest_rejected_resolution_cannot_stamp_dialectic_caller(
    monkeypatch, rejection,
):
    """A refusal-carried UUID is diagnostic context, never caller proof."""
    from src.http_routes import tools as http_tools
    from src.mcp_handlers.context import get_context_resolved_agent_id

    caller, arguments = _transport_arguments("synthesis", "omitted")
    observed = []
    load = AsyncMock(return_value=None)
    resolver = _install_transport_seams(monkeypatch, caller, load)
    resolver.return_value = rejection

    real_execute = http_tools.execute_http_tool

    async def _observe_execute(tool_name, current_arguments):
        observed.append(get_context_resolved_agent_id())
        return await real_execute(tool_name, current_arguments)

    monkeypatch.setattr(http_tools, "execute_http_tool", _observe_execute)
    await http_tools._execute_http_tool_in_context(
        _request(),
        "dialectic",
        arguments,
        arguments["client_session_id"],
    )

    assert observed == [None]
    load.assert_not_awaited()
    assert "agent_id" not in arguments
    assert get_context_resolved_agent_id() is None
