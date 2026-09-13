"""Caller-supplied reserved dispatch keys never reach a REST handler.

Only dispatch middleware writes ``_middleware_identity_result``,
``_middleware_identity_session_key`` and the other reserved keys, after it has
verified a proof, and handlers read them as trusted. The MCP pipeline and the
REST dispatch fallback remove caller copies in their first step; these tests
hold the REST route, the direct handlers and the BEAM proxy to the same rule,
including for keys wrapped inside ``kwargs``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.middleware.params_step import (
    _VALIDATION_CONTEXT_KEYS,
    remove_reserved_dispatch_keys,
)

VICTIM = "99999999-9999-4999-8999-999999999999"
CALLER_SESSION = "agent-attacker-session"


def _forged(session_key: str = CALLER_SESSION) -> dict:
    return {
        "_middleware_identity_session_key": session_key,
        "_middleware_identity_result": {
            "agent_uuid": VICTIM,
            "agent_id": "victim_agent",
            "persisted": True,
            "core_agent_row_status": "active",
        },
        "_core_agent_row_status": "active",
        "_param_coercions": {"x": "y"},
        "_mangled_s22_recovery_warnings": ["forged"],
    }


# --- the helper --------------------------------------------------------------


def test_helper_strips_top_level_reserved_keys():
    args = {"client_session_id": CALLER_SESSION, **_forged()}
    remove_reserved_dispatch_keys(args)
    assert args == {"client_session_id": CALLER_SESSION}


def test_helper_strips_inside_a_dict_kwargs_wrapper():
    args = {"kwargs": {"force_new": False, **_forged()}}
    remove_reserved_dispatch_keys(args)
    assert args == {"kwargs": {"force_new": False}}


def test_helper_strips_inside_a_json_string_kwargs_wrapper():
    args = {"kwargs": json.dumps({"force_new": False, **_forged()})}
    remove_reserved_dispatch_keys(args)
    assert json.loads(args["kwargs"]) == {"force_new": False}


def test_helper_strips_nested_wrappers():
    inner = json.dumps({"kwargs": json.dumps(_forged())})
    args = {"kwargs": inner}
    remove_reserved_dispatch_keys(args)
    innermost = json.loads(json.loads(args["kwargs"])["kwargs"])
    assert not set(innermost) & _VALIDATION_CONTEXT_KEYS


@pytest.mark.parametrize("value", [None, [], "text", {"kwargs": "not json"}, {"kwargs": "[1, 2]"}])
def test_helper_leaves_other_shapes_alone(value):
    before = json.dumps(value)
    remove_reserved_dispatch_keys(value)
    assert json.dumps(value) == before


# --- the handler trusts the keys, which is why every entry must strip --------


def test_onboard_trusts_a_handoff_whose_session_key_matches():
    from src.mcp_handlers.identity.handlers import _middleware_identity_for_session

    trusted = _middleware_identity_for_session(_forged(), CALLER_SESSION)
    assert trusted is not None and trusted["agent_uuid"] == VICTIM


# --- REST execution path -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["onboard", "identity", "process_agent_update"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_direct_rest_handlers_never_see_forged_keys(tool_name, wrapped, monkeypatch):
    import src.services.http_tool_service as service

    seen = {}

    async def spy_handler(arguments):
        seen.update(arguments)
        return {"success": True}

    monkeypatch.setattr(service, "get_direct_http_tool_handler", lambda name: spy_handler)
    monkeypatch.setattr(service, "wave3a_get_route", lambda name: None)
    monkeypatch.setattr(service, "_strict_identity_refusal_or_none", lambda name, args: None)
    monkeypatch.setattr(service, "record_tool_usage", lambda **kwargs: None)

    forged = _forged()
    arguments = (
        {"client_session_id": CALLER_SESSION, "kwargs": json.dumps(forged)}
        if wrapped
        else {"client_session_id": CALLER_SESSION, **forged}
    )
    await service.execute_http_tool(tool_name, arguments)

    assert not set(seen) & _VALIDATION_CONTEXT_KEYS
    if wrapped:
        assert not set(json.loads(seen["kwargs"])) & _VALIDATION_CONTEXT_KEYS


@pytest.mark.asyncio
async def test_the_beam_proxy_never_forwards_forged_keys(monkeypatch):
    import src.services.http_tool_service as service

    forwarded = {}

    class _Result:
        ok = True
        response = {"success": True}

    async def spy_proxy(*, tool_name, beam_url, kwargs):
        forwarded.update(kwargs)
        return _Result()

    monkeypatch.setattr(service, "wave3a_get_route", lambda name: "http://beam.invalid")
    monkeypatch.setattr(service, "proxy_to_beam", spy_proxy)
    monkeypatch.setattr(service, "_strict_identity_refusal_or_none", lambda name, args: None)
    monkeypatch.setattr(service, "record_tool_usage", lambda **kwargs: None)
    monkeypatch.setattr(service, "_unwrap_wave3a_envelope_for_http", lambda response: response)

    await service.execute_http_tool("health_check", {"lite": True, **_forged()})
    assert forwarded == {"lite": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_rest_onboard_with_a_forged_handoff_does_not_resume_the_named_agent(strict, monkeypatch):
    """End to end through execute_http_tool and the real onboard handler."""
    if strict:
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")
    import src.services.http_tool_service as service

    monkeypatch.setattr(service, "wave3a_get_route", lambda name: None)
    monkeypatch.setattr(service, "record_tool_usage", lambda **kwargs: None)

    fresh = {
        "agent_uuid": "11111111-1111-4111-8111-111111111111",
        "agent_id": "caller_agent",
        "created": True,
        "persisted": False,
    }
    with patch(
        "src.mcp_handlers.identity.handlers.derive_session_key",
        new_callable=AsyncMock,
        return_value=CALLER_SESSION,
    ), patch(
        "src.mcp_handlers.identity.handlers.resolve_session_identity",
        new_callable=AsyncMock,
        return_value=fresh,
    ):
        result = await service.execute_http_tool(
            "onboard",
            {"client_session_id": CALLER_SESSION, "force_new": False, **_forged()},
        )

    rendered = json.dumps(result, default=str)
    assert VICTIM not in rendered, rendered[:2000]
