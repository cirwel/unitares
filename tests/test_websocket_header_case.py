"""WebSocket header names reach the app lowercased, as ASGI requires.

uvicorn's websockets-sansio protocol keeps the client's casing in the scope,
and Starlette's Headers assumes lowercase, so a title-cased Authorization,
Origin or Cookie was invisible on /ws/eisv. These tests drive the middleware
with the casing that protocol actually produces.
"""

from __future__ import annotations

import asyncio

from starlette.websockets import WebSocket

from src.http_routes.access import _check_ws_auth
from src.services.mcp_transport_service import LowercaseWebSocketHeaders


def _scope(kind: str, headers: list[tuple[str, str]], ip: str = "203.0.113.9") -> dict:
    return {
        "type": kind,
        "path": "/ws/eisv",
        "client": (ip, 50000),
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
    }


def _seen(scope: dict) -> dict:
    captured = {}

    async def inner(scope, receive, send):
        captured.update(scope)

    asyncio.run(LowercaseWebSocketHeaders(inner)(scope, None, None))
    return captured


def test_websocket_header_names_are_lowercased():
    seen = _seen(_scope("websocket", [("Origin", "https://gov.example"), ("Cookie", "a=b")]))
    assert seen["headers"] == [(b"origin", b"https://gov.example"), (b"cookie", b"a=b")]
    ws = WebSocket(seen, receive=None, send=None)
    assert ws.headers.get("origin") == "https://gov.example"
    assert ws.cookies == {"a": "b"}


def test_http_scope_passes_through_unchanged():
    # A title-cased name proves the middleware left the scope alone; a
    # lowercase one would compare equal even if it had been rewritten.
    scope = _scope("http", [("Host", "127.0.0.1:8767")])
    seen = _seen(scope)
    assert seen == scope
    assert seen["headers"] == [(b"Host", b"127.0.0.1:8767")]


def test_title_cased_bearer_authenticates_untrusted_websocket(monkeypatch):
    for name in ("UNITARES_MCP_BEARER_TOKENS", "UNITARES_REST_STRICT"):
        monkeypatch.delenv(name, raising=False)
    raw = _scope("websocket", [("Host", "gov.example"), ("Authorization", "Bearer tok")])
    assert _check_ws_auth(WebSocket(raw, receive=None, send=None), http_api_token="tok") is False
    ws = WebSocket(_seen(raw), receive=None, send=None)
    assert _check_ws_auth(ws, http_api_token="tok") is True
