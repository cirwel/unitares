"""Dependency canary for the `mcp` SDK's client transport contract.

CI resolves `mcp` fresh from its allowed range, so the tested version can
change without any commit — which is exactly how mcp 2.0.0 entered CI
unnoticed in August 2026 while every client-side connect test verified a
hand-built mock instead of the library. Two contract changes shipped in
2.0.0 that mocks structurally cannot catch:

1. `streamable_http_client` yields (read, write) instead of
   (read, write, get_session_id) — a loud ValueError at real connect.
2. The transport was rewritten against `httpx2` (`client.sse(...)`,
   `httpx2.StreamError`), so injecting the `httpx` 0.x client we pass at
   five call sites kills the server-push GET stream SILENTLY — the
   AttributeError is swallowed by the transport's retry loop while POST
   round-trips keep working, so a naive connect test passes.

This file is the tripwire for both classes: a mock-free connect through
the real transport against a real in-process server, plus an import-surface
check on the seam the silent failure hides behind. If a resolved `mcp`
version fails here, the fix is a port (see mcp_compat.py and the SDK's
pin rationale in agents/sdk/pyproject.toml), never a mock adjustment.
"""

import asyncio
import inspect
import socket
import threading
import time

import httpx
import pytest


# --- seam contract (no server needed) ---------------------------------------


def test_transport_accepts_injected_http_client():
    """The five real call sites all pass http_client=; losing the parameter
    breaks the UDS substrate-attestation path, which has no other way to
    route MCP traffic over a Unix socket."""
    from mcp.client.streamable_http import streamable_http_client

    sig = inspect.signature(streamable_http_client)
    assert "http_client" in sig.parameters, (
        "streamable_http_client no longer accepts http_client=. The SDK's "
        "UDS attestation path (unitares_sdk/client.py) and the stdio proxy "
        "depend on client injection; this mcp version needs a port, not a "
        "pin bump."
    )


def test_transport_is_written_against_the_injected_client_library():
    """Heuristic on the transport module's import surface: we inject
    `httpx` clients, so the transport must be written against `httpx` —
    not a successor library. mcp 2.0.0 imports `httpx2` and calls
    `client.sse(...)` on whatever is injected; with an httpx 0.x client
    that AttributeError is swallowed by the transport's retry loop and
    the server-push stream dies silently, so no end-to-end connect test
    can catch it. This import check is the loud version of that failure.
    """
    import mcp.client.streamable_http as transport

    assert not hasattr(transport, "httpx2"), (
        "The installed mcp transport is written against httpx2, but the "
        "repo injects httpx 0.x AsyncClient at five call sites (SDK "
        "connect(), mcp_server_std.py stdio proxy, dialectic_canary, "
        "mcp_agent, calibration harness). Under this mcp version the "
        "server-push GET stream fails SILENTLY. Port the client injection "
        "to httpx2 (including the UDS transport equivalent) before "
        "allowing this mcp version; do not widen any pin past it."
    )
    assert hasattr(transport, "httpx"), (
        "The mcp transport module no longer imports httpx at module level; "
        "the import-surface heuristic in this canary needs re-basing "
        "against the new transport implementation before trusting green."
    )


# --- real-transport end-to-end (mock-free) -----------------------------------


@pytest.fixture(scope="module")
def canary_server_url():
    """A real MCP server (minimal FastMCP app, real uvicorn, real TCP) so the
    connect below exercises the installed transport, not a mock of it."""
    import uvicorn

    # mcp_compat resolves the high-level server class across majors
    # (1.x FastMCP / 2.x MCPServer); both keep .tool() and
    # .streamable_http_app().
    from src.mcp_compat import FastMCP, server_supports_kwarg

    kwargs = {"stateless_http": True} if server_supports_kwarg("stateless_http") else {}
    canary = FastMCP("dependency-canary", **kwargs)

    @canary.tool()
    def echo(text: str) -> str:
        return text

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = uvicorn.Config(
        canary.streamable_http_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("canary uvicorn did not start within 15s")
        time.sleep(0.05)

    # FastMCP mounts the endpoint at exactly /mcp; the trailing-slash form
    # 307-redirects, which the transport's POST does not follow.
    yield f"http://127.0.0.1:{port}/mcp"

    server.should_exit = True
    thread.join(timeout=10)


def test_real_connect_and_tool_call_through_installed_transport(canary_server_url):
    """Real streamable_http_client + injected httpx client, exactly as the
    five production call sites do. Catches yield-arity and signature
    changes that mocked transports hide."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run() -> str:
        async with httpx.AsyncClient(http2=False, timeout=15) as http_client:
            async with streamable_http_client(
                canary_server_url, http_client=http_client
            ) as streams:
                # mcp 1.x yields (read, write, get_session_id); 2.x drops the third.
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert [t.name for t in tools.tools] == ["echo"]
                    res = await session.call_tool("echo", {"text": "canary"})
                    return res.content[0].text

    assert asyncio.run(run()) == "canary"


def test_sdk_client_real_connect(canary_server_url):
    """The resident-critical path: GovernanceClient.connect() with no mocks.
    Vigil and Chronicler run this exact code on the production interpreter;
    a transport contract change that breaks it takes residents dark."""
    from unitares_sdk.client import GovernanceClient

    async def run() -> None:
        client = GovernanceClient(mcp_url=canary_server_url, connect_retries=0)
        await client.connect()
        try:
            assert client._session is not None
        finally:
            await client.disconnect()

    asyncio.run(run())
