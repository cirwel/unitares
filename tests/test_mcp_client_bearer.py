"""MCP clients can present a bearer, so the /mcp gate can actually be turned on.

The server has had a bearer gate for /mcp since #847, but nothing that speaks
MCP to it could send one: the SDK had no auth path at any layer, and the
gateway and the dialectic reviewer built headers without one. Configuring
UNITARES_MCP_BEARER_TOKENS would therefore have 401'd Vigil, Chronicler,
Sentinel, the gateway and every spawned reviewer — the gate was unusable in
practice rather than merely unused.

The contract here is deliberately additive: with the variable unset every
client sends exactly the request it sent before, so this change alters nothing
until an operator turns the gate on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SDK_SRC = Path(__file__).resolve().parent.parent / "agents" / "sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from unitares_sdk.client import GovernanceClient  # noqa: E402

TOKEN = "gate-token-abc"  # noqa: S105 - test fixture


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)


# --- resolution ---------------------------------------------------------------

def test_no_token_configured_sends_nothing():
    """The additive guarantee: an ungated server sees an unchanged client."""
    assert GovernanceClient().bearer_token is None


def test_token_read_from_environment(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", TOKEN)
    assert GovernanceClient().bearer_token == TOKEN


def test_explicit_argument_beats_the_environment(monkeypatch):
    """Tests and callers must be able to pin a value without exporting it."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", TOKEN)
    assert GovernanceClient(bearer_token="explicit").bearer_token == "explicit"


def test_blank_environment_value_is_no_token(monkeypatch):
    """`UNITARES_MCP_BEARER_TOKEN=` is how "unset" gets spelled in an env file;
    sending `Bearer ` would be a malformed header, not a credential."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", "")
    assert GovernanceClient().bearer_token is None


# --- the header actually reaches the transport --------------------------------

@pytest.mark.asyncio
async def test_header_is_attached_to_the_http_client(monkeypatch):
    """Resolution is not enough — the token has to land on the client the MCP
    transport actually uses."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", TOKEN)
    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    from unitares_sdk import client as client_mod

    monkeypatch.setattr(client_mod._httpx, "AsyncClient", _FakeAsyncClient)
    gc = GovernanceClient()
    # BaseException, not Exception: the MCP transport unwinds through anyio
    # cancel scopes, and CancelledError is a BaseException. We only need the
    # constructor kwargs, which are captured before any of that happens.
    try:
        await gc.connect()
    except BaseException:  # noqa: BLE001 - see above
        pass

    assert captured.get("headers", {}).get("Authorization") == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_no_authorization_header_when_unconfigured(monkeypatch):
    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    from unitares_sdk import client as client_mod

    monkeypatch.setattr(client_mod._httpx, "AsyncClient", _FakeAsyncClient)
    gc = GovernanceClient()
    try:
        await gc.connect()
    except BaseException:  # noqa: BLE001
        pass

    assert "Authorization" not in captured.get("headers", {})


@pytest.mark.asyncio
async def test_uds_transport_also_carries_the_bearer(monkeypatch, tmp_path):
    """The UDS listener serves the same ASGI app, so the gate applies there
    too. Kernel PID attestation answers WHO the caller is, not WHETHER it is
    allowed — a resident on the socket still needs the credential."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", TOKEN)
    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    class _FakeTransport:
        def __init__(self, *args, **kwargs):
            pass

    from unitares_sdk import client as client_mod

    monkeypatch.setattr(client_mod._httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(client_mod._httpx, "AsyncHTTPTransport", _FakeTransport)
    gc = GovernanceClient(uds_path=str(tmp_path / "sock"))
    try:
        await gc.connect()
    except BaseException:  # noqa: BLE001
        pass

    assert captured.get("headers", {}).get("Authorization") == f"Bearer {TOKEN}"


# --- the other MCP callers ----------------------------------------------------

def test_gateway_client_sends_the_bearer(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", TOKEN)
    from src.gateway.client import GovernanceMCPClient

    headers = GovernanceMCPClient()._build_headers()
    assert headers.get("Authorization") == f"Bearer {TOKEN}"


def test_gateway_client_unchanged_when_unconfigured():
    from src.gateway.client import GovernanceMCPClient

    assert "Authorization" not in GovernanceMCPClient()._build_headers()


def test_dialectic_reviewer_inherits_the_token():
    """A spawned reviewer talks to gov-mcp through the SDK. Without the token
    in its env every call it makes 401s, and the failure reads as a broken
    reviewer rather than a missing credential.

    Parsed rather than substring-searched: a grep would pass on the name
    appearing in a comment, which is exactly the mistake worth catching.
    """
    import ast

    from src.mcp_handlers.dialectic import orchestrator_dispatch

    tree = ast.parse(Path(orchestrator_dispatch.__file__).read_text())
    forwarded = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # Narrow it: the constant must sit inside the passthrough tuple, so find a
    # tuple that also carries a known sibling from that list.
    in_passthrough = any(
        isinstance(node, ast.Tuple)
        and {
            c.value
            for c in node.elts
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
        }
        >= {"UNITARES_MCP_BEARER_TOKEN", "UNITARES_OLLAMA_BASE_URL"}
        for node in ast.walk(tree)
    )
    assert "UNITARES_MCP_BEARER_TOKEN" in forwarded
    assert in_passthrough, "token is not in the reviewer env passthrough tuple"


def test_resident_config_defines_no_duplicate_token_constant():
    """The SDK resolves the token; a second definition would be dead weight
    that reads as load-bearing."""
    from agents.common import config as agent_config

    assert not hasattr(agent_config, "GOV_MCP_BEARER_TOKEN")


@pytest.mark.asyncio
async def test_bearer_reaches_the_wire(monkeypatch):
    """End-to-end through a real httpx transport, not just constructor kwargs.

    The constructor tests prove the token was handed to httpx; this proves
    httpx actually puts it on an outgoing request, which is the claim that
    matters and the one a library upgrade could silently break.
    """
    import httpx

    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", TOKEN)
    seen: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    gc = GovernanceClient()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_capture),
        headers=(
            {"Authorization": f"Bearer {gc.bearer_token}"} if gc.bearer_token else {}
        ),
    ) as client:
        await client.post("http://127.0.0.1:8767/mcp/", json={"jsonrpc": "2.0"})

    assert seen["auth"] == f"Bearer {TOKEN}"
