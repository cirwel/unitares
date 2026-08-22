"""Dependency canary for the `mcp` SDK's client transport contract.

CI resolves `mcp` fresh from its allowed range, so the tested version can
change without any commit — which is exactly how mcp 2.0.0 entered CI
unnoticed in August 2026 while every client-side connect test verified a
hand-built mock instead of the library. Two contract changes shipped in
2.0.0 that mocks structurally cannot catch:

1. `streamable_http_client` yields (read, write) instead of
   (read, write, get_session_id) — a loud ValueError at real connect.
2. The transport was rewritten against `httpx2` (`client.sse(...)`,
   `httpx2.StreamError`), so an `httpx` 0.x client injected via
   `http_client=` kills the server-push GET stream SILENTLY — the
   AttributeError is swallowed by the transport's retry loop while POST
   round-trips keep working, so a naive connect test passes.

(2) is resolved by the `mcp_httpx()` resolvers — `src/mcp_compat.py` for the
server tree, `unitares_sdk/_mcp_httpx.py` for the SDK — which read the
library off the installed transport's own import surface and hand call sites
exactly that.

The silent failure IS observable end-to-end, contrary to what this file used
to claim: the server-push stream is an HTTP GET, so counting the methods a
real server receives separates a working stream from a dead one even though
both spell identical tool results.
`test_server_push_stream_opens_with_the_injected_client` is that check and is
the primary coverage. The structural checks around it cover what one
connect cannot: that both distributions' resolvers agree with the transport,
and that no OTHER call site builds its client outside them.

If a resolved `mcp` version fails here, the fix is a port (see mcp_compat.py
and the bump procedure in constraints.txt), never a mock adjustment.
"""

import asyncio
import inspect
import pathlib
import socket
import threading
import time

import pytest


# --- seam contract (no server needed) ---------------------------------------


def test_transport_accepts_injected_http_client():
    """The production client call sites pass http_client=; losing the
    parameter breaks the UDS substrate-attestation path, which has no other
    way to route MCP traffic over a Unix socket."""
    from mcp.client.streamable_http import streamable_http_client

    sig = inspect.signature(streamable_http_client)
    assert "http_client" in sig.parameters, (
        "streamable_http_client no longer accepts http_client=. The SDK's "
        "UDS attestation path (unitares_sdk/client.py) and the stdio proxy "
        "depend on client injection; this mcp version needs a port, not a "
        "pin bump."
    )


def test_resolver_agrees_with_the_transports_client_library():
    """The seam: whatever library the transport is written against is the one
    our resolvers hand to call sites.

    mcp 2.x rewrote the transport against ``httpx2`` — it calls
    ``client.sse(...)`` and catches ``httpx2.StreamError``. An ``httpx`` 0.x
    client has no ``.sse()``, and the AttributeError is swallowed by the
    transport's retry loop: POST round-trips keep working while the
    server-push GET stream dies SILENTLY. The end-to-end tests below pass
    under that failure, which is why this structural check exists.

    Both resolvers are checked because they are separate distributions with
    duplicated logic (``unitares-sdk`` cannot import from the server tree),
    and a drift between them is invisible at runtime until a resident goes
    dark.
    """
    import mcp.client.streamable_http as transport

    from src.mcp_compat import mcp_httpx as server_resolver
    from unitares_sdk._mcp_httpx import mcp_httpx as sdk_resolver

    exposed = [
        name
        for name in ("httpx2", "httpx")
        if getattr(getattr(transport, name, None), "__name__", None) == name
    ]
    assert exposed, (
        "mcp.client.streamable_http exposes neither 'httpx2' nor 'httpx' at "
        "module level under its own name, so which client library it expects "
        "cannot be read off it. Both resolvers raise ImportError in this "
        "state rather than guess. Re-base them and this check against the "
        "new transport implementation before trusting green."
    )
    expected = exposed[0]

    for label, resolver in (("server", server_resolver), ("sdk", sdk_resolver)):
        assert resolver().__name__ == expected, (
            f"The {label} resolver hands call sites "
            f"'{resolver().__name__}' but the installed mcp transport is "
            f"written against '{expected}'. Injecting the wrong library kills "
            "the server-push GET stream silently."
        )


# Production call sites that inject an HTTP client into mcp. Every one of them
# must build that client from the resolver, never from a bare ``import httpx``
# — that bare import is precisely the pre-port defect, and it fails silently.
_INJECTION_CALL_SITES = (
    "agents/sdk/src/unitares_sdk/client.py",
    "src/mcp_server_std.py",
    "scripts/ops/mcp_agent.py",
    "scripts/ops/dialectic_canary.py",
    "scripts/dev/calibration_harness/client_mcp.py",
)


def test_no_call_site_builds_its_injected_client_outside_the_resolver():
    """Source guard: the resolver is only worth having if it is not bypassed.

    A new call site that reaches for ``httpx.AsyncClient`` directly re-opens
    the silent failure at that one site while every other test stays green, so
    the bypass is caught here rather than in production. Keep
    ``_INJECTION_CALL_SITES`` in step with ``grep -rn 'http_client=' `` and
    ``grep -rn 'httpx_client_factory='``.
    """
    import ast

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []

    for rel in _INJECTION_CALL_SITES:
        path = repo_root / rel
        assert path.exists(), f"{rel} moved; update _INJECTION_CALL_SITES"
        tree = ast.parse(path.read_text(), filename=str(path))

        resolver_used = False
        for node in ast.walk(tree):
            # `import httpx` / `from httpx import ...`, at any scope.
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in ("httpx", "httpx2"):
                        offenders.append(f"{rel}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in ("httpx", "httpx2"):
                    offenders.append(f"{rel}:{node.lineno} from {node.module} import ...")
                elif "mcp_httpx" in (node.module or "") or any(
                    a.name == "mcp_httpx" for a in node.names
                ):
                    resolver_used = True

        assert resolver_used, (
            f"{rel} injects an HTTP client into mcp but never imports the "
            "mcp_httpx resolver. It must build that client from the library "
            "the installed transport expects."
        )

    assert not offenders, (
        "These files inject an HTTP client into mcp and also import a client "
        "library directly; build the client from mcp_httpx() instead, so the "
        "injected type follows the installed transport:\n  "
        + "\n  ".join(offenders)
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
    production call sites do. Catches signature and handshake changes that
    mocked transports hide. The unpack here is deliberately index-based
    (the post-#1800 production form), so yield-arity enforcement lives in
    the SDK connect test below, whose code is what actually ships."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from src.mcp_compat import mcp_httpx

    async def run() -> str:
        async with mcp_httpx().AsyncClient(http2=False, timeout=15) as http_client:
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


def test_sdk_client_real_connect(canary_server_url, monkeypatch):
    """The resident-critical path: GovernanceClient.connect() with no mocks.
    Vigil and Chronicler run this exact code on the production interpreter;
    a transport contract change that breaks it takes residents dark."""
    from unitares_sdk.client import GovernanceClient

    # Without this, a host that exports UNITARES_UDS_SOCKET (resident hosts,
    # the operator Mac) would route this connect to the PRODUCTION governance
    # socket instead of the canary server.
    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)

    async def run() -> None:
        client = GovernanceClient(mcp_url=canary_server_url, connect_retries=0)
        await client.connect()
        try:
            assert client._session is not None
        finally:
            await client.disconnect()

    asyncio.run(run())


# --- the silent failure, caught behaviorally ---------------------------------


@pytest.fixture(scope="module")
def push_stream_server():
    """A STATEFUL MCP server plus a record of the HTTP methods it received.

    Stateful on purpose: with ``stateless_http=True`` there is no server-push
    stream to open, so the fixture above cannot observe this. Yields
    ``(url, methods)`` where ``methods`` accumulates live.
    """
    import uvicorn

    from src.mcp_compat import FastMCP

    canary = FastMCP("push-stream-canary")

    @canary.tool()
    def echo(text: str) -> str:
        return text

    app = canary.streamable_http_app()
    methods: list[str] = []

    async def recording_app(scope, receive, send):
        if scope["type"] == "http":
            methods.append(scope["method"])
        await app(scope, receive, send)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(
            recording_app, host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("push-stream canary uvicorn did not start within 15s")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}/mcp", methods

    server.should_exit = True
    thread.join(timeout=10)


def test_server_push_stream_opens_with_the_injected_client(push_stream_server):
    """The failure this whole file exists for, observed directly.

    mcp 2.x's transport opens the server-push stream by calling
    ``client.sse(...)``. With the wrong client library that call raises
    AttributeError, the transport's retry loop swallows it, and the GET is
    never issued — while POSTs keep succeeding. So a tool call completing
    proves nothing; the server receiving a GET is the observable that
    separates a working stream from a dead one.

    Measured against mcp 2.0.0, identical tool results either way::

        httpx2 (resolved)  tools=['echo'] echo='x'  GET=1
        httpx  0.x         tools=['echo'] echo='x'  GET=0

    and mcp 1.29.0 + httpx gives GET=1, so the assertion holds on both lines
    whenever the injected client matches the transport.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from src.mcp_compat import mcp_httpx

    url, methods = push_stream_server
    methods.clear()

    async def run() -> str:
        async with mcp_httpx().AsyncClient(http2=False, timeout=15) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    res = await session.call_tool("echo", {"text": "canary"})
                    # The GET is opened by a background task after the
                    # handshake, so poll rather than sleeping a fixed amount.
                    deadline = time.monotonic() + 10
                    while "GET" not in methods and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    return res.content[0].text

    echoed = asyncio.run(run())

    assert echoed == "canary"  # POST path works -- it works under the bug too
    assert "GET" in methods, (
        "The server never received the server-push GET stream, though tool "
        "calls succeeded over POST. That is exactly the silent failure this "
        "file guards: the injected client is not the library the installed "
        "transport calls .sse() on, so the stream died without raising. "
        f"Methods the server saw: {methods}"
    )
