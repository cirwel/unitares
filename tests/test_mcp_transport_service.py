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
    bind_public_socket,
    build_transport_runtime,
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


def test_transport_runtime_uses_supported_sansio_websocket_backend(monkeypatch):
    app = object()
    session_manager = object()

    monkeypatch.setattr(
        "src.background_tasks.start_all_background_tasks", lambda **_kwargs: None
    )
    monkeypatch.setattr("src.mcp_compat.lowlevel_server", lambda _mcp: object())
    monkeypatch.setattr(
        "src.mcp_listen_config.build_streamable_session_manager",
        lambda _server: session_manager,
    )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._log_transport_security",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._create_base_application",
        lambda _mcp: app,
    )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._configure_middleware",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._register_application_routes",
        lambda *_args, **_kwargs: None,
    )

    runtime = build_transport_runtime(
        object(),
        auth_config=McpAuthConfig(),
        host="127.0.0.1",
        port=8767,
        reload=False,
        server_ready_fn=lambda: True,
        set_server_ready=lambda: None,
        server_start_time=0.0,
        server_version="test",
        server_build_sha="test",
    )

    assert runtime.session_manager is session_manager
    assert runtime.server.config.ws == "websockets-sansio"
    assert runtime.public_server is None


def test_transport_runtime_adds_a_loopback_public_listener(monkeypatch):
    """The public OAuth listener binds loopback only and trusts forwarded
    headers exactly as the main listener does, so REST gates behind it keep
    seeing the caller's address."""
    monkeypatch.setattr(
        "src.background_tasks.start_all_background_tasks", lambda **_kwargs: None
    )
    monkeypatch.setattr("src.mcp_compat.lowlevel_server", lambda _mcp: object())
    monkeypatch.setattr(
        "src.mcp_listen_config.build_streamable_session_manager",
        lambda _server: object(),
    )
    for name in ("_log_transport_security", "_configure_middleware", "_register_application_routes"):
        monkeypatch.setattr(
            f"src.services.mcp_transport_service.{name}", lambda *_a, **_k: None
        )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._create_base_application",
        lambda _mcp: object(),
    )

    runtime = build_transport_runtime(
        object(),
        auth_config=McpAuthConfig(oauth_public_listener_only=True),
        host="0.0.0.0",
        port=8767,
        reload=False,
        server_ready_fn=lambda: True,
        set_server_ready=lambda: None,
        server_start_time=0.0,
        server_version="test",
        server_build_sha="test",
        # Ephemeral: never collide with a running deployment.
        public_socket=bind_public_socket(0, main_port=8767),
    )

    try:
        cfg = runtime.public_server.config
        assert cfg.host == "127.0.0.1"
        assert cfg.port == runtime.public_socket.getsockname()[1] != 0
        assert cfg.proxy_headers is True
        assert cfg.forwarded_allow_ips == runtime.server.config.forwarded_allow_ips
    finally:
        runtime.public_socket.close()


def test_an_unbound_public_listener_falls_back_to_gating_every_request(monkeypatch):
    """UNITARES_OAUTH_PUBLIC_PORT equal to the main port must not leave the
    only entry point ungated."""
    seen = {}
    monkeypatch.setattr(
        "src.background_tasks.start_all_background_tasks", lambda **_kwargs: None
    )
    monkeypatch.setattr("src.mcp_compat.lowlevel_server", lambda _mcp: object())
    monkeypatch.setattr(
        "src.mcp_listen_config.build_streamable_session_manager",
        lambda _server: object(),
    )
    for name in ("_log_transport_security", "_configure_middleware"):
        monkeypatch.setattr(
            f"src.services.mcp_transport_service.{name}", lambda *_a, **_k: None
        )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._register_application_routes",
        lambda *_a, auth_config, **_k: seen.setdefault("auth", auth_config),
    )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._create_base_application",
        lambda _mcp: object(),
    )

    runtime = build_transport_runtime(
        object(),
        auth_config=McpAuthConfig(oauth_public_listener_only=True),
        host="127.0.0.1",
        port=8767,
        reload=False,
        server_ready_fn=lambda: True,
        set_server_ready=lambda: None,
        server_start_time=0.0,
        server_version="test",
        server_build_sha="test",
        public_socket=bind_public_socket(8767, main_port=8767),  # refused: None
    )

    assert runtime.public_server is None
    assert seen["auth"].oauth_public_listener_only is False


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


# --- a configured gate that failed to build closes the route ---------------
#
# Serving /mcp unauthenticated because OAuth construction raised answers a
# different question than the operator asked. The closure is deliberately
# route-scoped: every other surface on this process has its own gate, so
# refusing to start would turn one route's misconfiguration into a fleet
# outage. 503 rather than 401 because the state is the server's, not the
# caller's, and because a diagnostic can then read it off the wire instead of
# inferring the gate from an environment variable.

@pytest.mark.asyncio
async def test_unavailable_gate_closes_the_route_instead_of_serving_it_open(monkeypatch):
    from src.services import mcp_transport_service as svc

    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(
        _scope(), McpAuthConfig(gate_unavailable=True)
    )
    assert decision.allowed is False
    assert decision.response is not None
    assert decision.response.status_code == 503


@pytest.mark.asyncio
async def test_a_bearer_allowlist_reopens_a_route_whose_oauth_gate_failed(monkeypatch):
    """The requirement is a gate, not OAuth — the second credential still works."""
    from src.services import mcp_transport_service as svc

    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: ["tok"])
    decision = await svc.authorize_mcp_request(
        _scope((b"authorization", b"Bearer tok")),
        McpAuthConfig(gate_unavailable=True),
    )
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_a_wrong_token_against_a_failed_gate_is_401_not_503(monkeypatch):
    """Once a bearer allowlist exists the caller's credential is the question."""
    from src.services import mcp_transport_service as svc

    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: ["tok"])
    decision = await svc.authorize_mcp_request(
        _scope((b"authorization", b"Bearer wrong")),
        McpAuthConfig(gate_unavailable=True),
    )
    assert decision.allowed is False
    assert decision.response.status_code == 401


@pytest.mark.asyncio
async def test_no_gate_configured_still_serves_open(monkeypatch):
    """The free/self-hosted default never sets an issuer and is untouched."""
    from src.services import mcp_transport_service as svc

    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope(), McpAuthConfig())
    assert decision.allowed is True


@pytest.mark.parametrize(
    "provider,client_id,installed",
    [(object(), "static", True), (None, "static", False), (object(), None, False)],
)
def test_runtime_installs_the_basic_auth_shim_for_a_static_client(
    monkeypatch, provider, client_id, installed
):
    from src.oauth_provider import StaticClientBasicAuthShim

    class _App:
        def __init__(self):
            self.middleware = []

        def add_middleware(self, cls, **kwargs):
            self.middleware.append((cls, kwargs))

    app = _App()
    monkeypatch.setattr(
        "src.background_tasks.start_all_background_tasks", lambda **_kwargs: None
    )
    monkeypatch.setattr("src.mcp_compat.lowlevel_server", lambda _mcp: object())
    monkeypatch.setattr(
        "src.mcp_listen_config.build_streamable_session_manager",
        lambda _server: object(),
    )
    for name in ("_log_transport_security", "_configure_middleware", "_register_application_routes"):
        monkeypatch.setattr(
            f"src.services.mcp_transport_service.{name}", lambda *_a, **_k: None
        )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._create_base_application",
        lambda _mcp: app,
    )

    build_transport_runtime(
        object(),
        auth_config=McpAuthConfig(oauth_provider=provider, static_client_id=client_id),
        host="127.0.0.1",
        port=8767,
        reload=False,
        server_ready_fn=lambda: True,
        set_server_ready=lambda: None,
        server_start_time=0.0,
        server_version="test",
        server_build_sha="test",
    )

    shims = [kw for cls, kw in app.middleware if cls is StaticClientBasicAuthShim]
    assert shims == ([{"client_id": client_id}] if installed else [])


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _get(port: int) -> bytes:
    import asyncio

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


@pytest.mark.asyncio
async def test_runtime_serves_both_listeners_stamps_only_public_and_stops_together(monkeypatch):
    """End to end over real sockets: the stamp that carries the OAuth gate is
    present on the public listener and absent on the main one, and stopping
    the main server stops the public one without it owning signals."""
    import asyncio

    import uvicorn

    from src.services.mcp_transport_service import (
        PUBLIC_LISTENER_SCOPE_KEY,
        McpTransportRuntime,
        _follower_server_class,
        mark_public_listener,
    )

    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)
    seen: list[bool] = []

    async def app(scope, receive, send):
        seen.append(bool(scope.get(PUBLIC_LISTENER_SCOPE_KEY)))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    class _SessionManager:
        def run(self):
            import contextlib

            @contextlib.asynccontextmanager
            async def _cm():
                yield

            return _cm()

    main_port = _free_port()
    main = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=main_port, lifespan="off", log_level="warning")
    )
    sock = bind_public_socket(0, main_port=main_port)
    public = _follower_server_class()(
        main,
        uvicorn.Config(mark_public_listener(app), lifespan="off", log_level="warning"),
    )
    runtime = McpTransportRuntime(
        app=app, session_manager=_SessionManager(), server=main,
        public_server=public, public_socket=sock,
    )
    task = asyncio.create_task(runtime.serve())
    try:
        for _ in range(200):
            if main.started and public.started:
                break
            await asyncio.sleep(0.02)
        assert main.started and public.started

        assert b"200" in (await _get(main_port)).split(b"\r\n", 1)[0]
        assert b"200" in (await _get(sock.getsockname()[1])).split(b"\r\n", 1)[0]
        assert seen == [False, True]

        main.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        assert public.should_exit is True
    finally:
        if not task.done():
            main.should_exit = True
            await asyncio.wait_for(task, timeout=10)


def test_follower_never_installs_signal_handlers():
    import signal

    import uvicorn

    from src.services.mcp_transport_service import _follower_server_class

    before = signal.getsignal(signal.SIGTERM)
    follower = _follower_server_class()(
        uvicorn.Server(uvicorn.Config(lambda *a: None)),
        uvicorn.Config(lambda *a: None),
    )
    with follower.capture_signals():
        assert signal.getsignal(signal.SIGTERM) is before


def test_a_port_held_on_all_interfaces_counts_as_in_use(monkeypatch):
    """SO_REUSEADDR lets macOS bind 127.0.0.1:P over another 0.0.0.0:P, so
    the bind alone cannot detect it; Linux refuses that bind, which would hide
    a missing probe in CI. Simulate the macOS case directly: the bind would
    succeed, but something answers on the port, so it must be refused."""
    import socket

    from src.services import mcp_transport_service as svc

    bound = []

    class _Sock:
        def __init__(self, *_a, **_k):
            pass

        def settimeout(self, *_a):
            pass

        def connect_ex(self, _addr):
            return 0  # something already answers

        def setsockopt(self, *_a):
            pass

        def bind(self, addr):
            bound.append(addr)  # would succeed, as on macOS

        def listen(self, *_a):
            pass

        def setblocking(self, *_a):
            pass

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _Sock)
    assert svc.bind_public_socket(8772, main_port=8767) is None
    assert bound == []


@pytest.mark.asyncio
async def test_build_transport_runtime_stamps_the_public_listener(monkeypatch):
    """The one line that carries the OAuth gate: if the builder handed the
    public server the bare app, the tunnel would serve /mcp ungated."""
    from src.services.mcp_transport_service import PUBLIC_LISTENER_SCOPE_KEY

    seen = {}

    class _App:
        def add_middleware(self, *_a, **_k):
            pass

        async def __call__(self, scope, receive, send):
            seen.update(scope)

    app = _App()
    monkeypatch.setattr(
        "src.background_tasks.start_all_background_tasks", lambda **_kwargs: None
    )
    monkeypatch.setattr("src.mcp_compat.lowlevel_server", lambda _mcp: object())
    monkeypatch.setattr(
        "src.mcp_listen_config.build_streamable_session_manager",
        lambda _server: object(),
    )
    for name in ("_log_transport_security", "_configure_middleware", "_register_application_routes"):
        monkeypatch.setattr(
            f"src.services.mcp_transport_service.{name}", lambda *_a, **_k: None
        )
    monkeypatch.setattr(
        "src.services.mcp_transport_service._create_base_application",
        lambda _mcp: app,
    )
    runtime = build_transport_runtime(
        object(),
        auth_config=McpAuthConfig(oauth_public_listener_only=True),
        host="127.0.0.1",
        port=8767,
        reload=False,
        server_ready_fn=lambda: True,
        set_server_ready=lambda: None,
        server_start_time=0.0,
        server_version="test",
        server_build_sha="test",
        public_socket=bind_public_socket(0, main_port=8767),
    )
    try:
        await runtime.public_server.config.app({"type": "http", "headers": []}, None, None)
        assert seen.get(PUBLIC_LISTENER_SCOPE_KEY) is True
        assert runtime.server.config.app is app
    finally:
        runtime.public_socket.close()


def test_follower_exit_tracks_the_leader_without_being_told():
    """The runtime's finally also sets the follower's flag, so an end-to-end
    stop cannot show the link; this does. Without it the public listener keeps
    taking connections through the main server's whole drain."""
    import uvicorn

    from src.services.mcp_transport_service import _follower_server_class

    leader = uvicorn.Server(uvicorn.Config(lambda *a: None))
    follower = _follower_server_class()(leader, uvicorn.Config(lambda *a: None))
    assert follower.should_exit is False
    leader.should_exit = True
    assert follower.should_exit is True
    leader.should_exit = False
    follower.should_exit = True
    assert follower.should_exit is True
    assert leader.should_exit is False


_SIGTERM_CHILD = r'''
import asyncio, sys
import uvicorn
from src.services.mcp_transport_service import (
    McpTransportRuntime, _follower_server_class, _leader_server_class,
    bind_public_socket, mark_public_listener,
)

async def app(scope, receive, send):
    await asyncio.sleep(2)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"drained"})

class SM:
    def run(self):
        import contextlib
        @contextlib.asynccontextmanager
        async def cm():
            yield
        return cm()

async def main():
    main_port = int(sys.argv[1])
    sock = bind_public_socket(0, main_port=main_port)
    leader = _leader_server_class()(uvicorn.Config(
        app, host="127.0.0.1", port=main_port, lifespan="off", log_level="warning",
        timeout_graceful_shutdown=10))
    follower = _follower_server_class()(leader, uvicorn.Config(
        mark_public_listener(app), lifespan="off", log_level="warning",
        timeout_graceful_shutdown=10))
    print(sock.getsockname()[1], flush=True)
    await McpTransportRuntime(app=app, session_manager=SM(), server=leader,
                              public_server=follower, public_socket=sock).serve()

asyncio.run(main())
'''


def test_sigterm_lets_an_in_flight_public_request_finish(tmp_path):
    """launchd stops the server with SIGTERM. uvicorn re-raises it once the
    main listener's shutdown returns, so a request still running on the public
    listener (where the tunnel's traffic lives) must be drained before then."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = tmp_path / "child.py"
    script.write_text(_SIGTERM_CHILD)
    env = {**os.environ, "PYTHONPATH": repo}
    env.pop("UNITARES_UDS_SOCKET", None)
    child = subprocess.Popen(
        [sys.executable, str(script), str(_free_port())],
        cwd=repo, env=env, stdout=subprocess.PIPE, text=True,
    )
    try:
        public_port = int(child.stdout.readline())
        deadline = time.time() + 10
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", public_port), timeout=1)
                break
            except OSError:
                assert time.time() < deadline, "public listener never came up"
                time.sleep(0.05)
        client.settimeout(15)
        client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        time.sleep(0.5)  # request is now in flight (the app sleeps 2s)
        child.send_signal(signal.SIGTERM)
        data = b""
        while chunk := client.recv(4096):
            data += chunk
        client.close()
        assert data.startswith(b"HTTP/1.1 200"), data
        assert b"drained" in data
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=15)


@pytest.mark.asyncio
async def test_a_public_listener_crash_after_startup_is_logged(caplog):
    """Nothing awaits the task until shutdown; a silent crash would leave the
    tunnel refused with no trace."""
    import logging

    from src.services.mcp_transport_service import _serve_public_listener

    class _Crashes:
        async def serve(self, sockets):
            raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        await _serve_public_listener(_Crashes(), object())
    assert any("Public OAuth listener STOPPED" in r.getMessage() for r in caplog.records)
