"""Unit tests for the extracted streamable MCP transport boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.mcp_handlers.context import (
    get_context_client_session_id,
    get_context_session_key,
    get_mcp_session_id,
    get_session_signals,
)
from src.services.mcp_transport_service import (
    McpAuthConfig,
    capture_transport_context,
    make_streamable_mcp_asgi,
    reset_transport_context,
)


def _scope(*headers: tuple[bytes, bytes], peer_pid: int | None = None):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": list(headers),
        "client": ("127.0.0.1", 50000),
    }
    if peer_pid is not None:
        scope["unitares_peer_pid"] = peer_pid
    return scope


def test_capture_transport_context_sets_and_resets_all_compatibility_contexts():
    scope = _scope(
        (b"mcp-session-id", b"mcp-123"),
        (b"x-session-id", b"client-123"),
        (b"user-agent", b"Codex/CLI"),
        (b"x-unitares-model", b"gpt-5.6-sol"),
        (b"x-unitares-model-provider", b"openai"),
        (b"x-unitares-model-source", b"provider_reported"),
        (b"x-unitares-harness-type", b"codex-cli"),
        (b"x-unitares-harness-version", b"0.115.0"),
        (b"x-agent-name", b"Refactor Agent"),
        (b"x-unitares-operator", b"operator-token"),
        peer_pid=4321,
    )

    tokens = capture_transport_context(scope, oauth_client_id="oauth:client")
    signals = get_session_signals()

    assert signals is not None
    assert signals.mcp_session_id == "mcp-123"
    assert signals.x_session_id == "client-123"
    assert signals.oauth_client_id == "oauth:client"
    assert signals.transport == "uds"
    assert signals.peer_pid == 4321
    assert signals.unitares_operator_token == "operator-token"
    assert signals.reported_model == "gpt-5.6-sol"
    assert signals.model_provider == "openai"
    assert signals.model_provenance_source == "provider_reported"
    assert signals.reported_harness_type == "codex-cli"
    assert signals.harness_version == "0.115.0"
    assert get_mcp_session_id() == "mcp-123"
    assert get_context_client_session_id() == "client-123"
    assert get_context_session_key().startswith("127.0.0.1:")
    assert scope["state"]["governance_client_id"] == "client-123"

    reset_transport_context(tokens)

    assert get_session_signals() is None
    assert get_mcp_session_id() is None
    assert get_context_client_session_id() is None


@pytest.mark.asyncio
async def test_streamable_asgi_rejects_before_dispatch_when_bearer_is_invalid(
    monkeypatch,
):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "expected-token")
    manager = AsyncMock()
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    app = make_streamable_mcp_asgi(manager, auth_config=McpAuthConfig())
    await app(_scope((b"authorization", b"Bearer wrong-token")), receive, send)

    manager.handle_request.assert_not_awaited()
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_streamable_asgi_delegates_and_resets_context(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    captured = {}

    class SessionManager:
        async def handle_request(self, scope, _receive, send):
            captured["signals"] = get_session_signals()
            captured["session_id"] = get_mcp_session_id()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    app = make_streamable_mcp_asgi(
        SessionManager(),
        auth_config=McpAuthConfig(),
    )
    await app(
        _scope(
            (b"mcp-session-id", b"mcp-456"),
            (b"user-agent", b"Codex/CLI"),
        ),
        receive,
        send,
    )

    assert captured["signals"].mcp_session_id == "mcp-456"
    assert captured["signals"].transport == "mcp"
    assert captured["session_id"] == "mcp-456"
    assert sent[0]["status"] == 200
    assert get_session_signals() is None
    assert get_mcp_session_id() is None


@pytest.mark.asyncio
async def test_streamable_asgi_translates_manager_error_and_resets_context(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)

    class SessionManager:
        async def handle_request(self, _scope, _receive, _send):
            assert get_session_signals() is not None
            raise RuntimeError("transport exploded")

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    app = make_streamable_mcp_asgi(
        SessionManager(),
        auth_config=McpAuthConfig(),
    )
    await app(
        _scope((b"user-agent", b"Codex/CLI")),
        receive,
        send,
    )

    assert sent[0]["status"] == 500
    assert b"transport exploded" in sent[1]["body"]
    assert get_session_signals() is None
