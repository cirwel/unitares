from __future__ import annotations

import pytest
from starlette.requests import Request

from src.http_api import _build_http_session_signals, _execute_http_tool_in_context
from src.mcp_handlers.context import get_session_signals


def _request(*, peer_pid=None, headers=()) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/tools/call",
        "headers": list(headers),
        "client": ("127.0.0.1", 43210),
    }
    if peer_pid is not None:
        scope["unitares_peer_pid"] = peer_pid
    return Request(scope)


def test_http_session_signals_preserve_server_injected_uds_peer_pid() -> None:
    signals = _build_http_session_signals(_request(peer_pid=4321))

    assert signals.transport == "uds"
    assert signals.peer_pid == 4321


def test_http_session_signals_leave_plain_http_unattested() -> None:
    signals = _build_http_session_signals(
        _request(headers=[(b"x-unitares-peer-pid", b"4321")])
    )

    assert signals.transport == "rest"
    assert signals.peer_pid is None


@pytest.mark.parametrize("invalid_peer_pid", [True, 0, -1, "4321"])
def test_http_session_signals_reject_invalid_scope_peer_pid(invalid_peer_pid) -> None:
    signals = _build_http_session_signals(_request(peer_pid=invalid_peer_pid))

    assert signals.transport == "rest"
    assert signals.peer_pid is None


@pytest.mark.asyncio
async def test_rest_tool_context_exposes_uds_attestation_to_handlers(monkeypatch) -> None:
    observed = []

    async def fake_resolve(_tool_name, _arguments, _signals):
        observed.append(get_session_signals())

    async def fake_execute(_tool_name, _arguments):
        observed.append(get_session_signals())
        return {"ok": True}

    monkeypatch.setattr("src.http_routes.access._resolve_http_bound_agent", fake_resolve)
    monkeypatch.setattr("src.http_routes.tools.execute_http_tool", fake_execute)

    result = await _execute_http_tool_in_context(
        _request(peer_pid=9876),
        "identity",
        {"agent_uuid": "00000000-0000-0000-0000-000000000000"},
        "session-1",
    )

    assert result == {"ok": True}
    assert [signals.peer_pid for signals in observed] == [9876, 9876]
    assert [signals.transport for signals in observed] == ["uds", "uds"]
