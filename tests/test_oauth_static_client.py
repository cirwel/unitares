"""Pre-registered OAuth client and the host-scoped /mcp gate.

Some hosted connectors cannot self-register: Google's custom MCP connector asks
the operator for a client ID and secret. These tests pin the static client (it
authenticates at the token endpoint whichever standard way the connector sends
its secret) and the host scoping that keeps local callers off the gate once
OAuth is switched on for the public hostname.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import AnyHttpUrl
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.oauth_provider import (
    GovernanceOAuthProvider,
    StaticClientBasicAuthShim,
    build_static_client,
)
from src.services import mcp_transport_service as svc
from src.services.mcp_transport_service import McpAuthConfig, request_host

CLIENT_ID = "google-connector"
SECRET = "s3cret:with/odd chars"
REDIRECT = "https://oauth-redirect.googleusercontent.com/r/example"
VERIFIER = "v" * 64


def _challenge() -> str:
    import hashlib

    digest = hashlib.sha256(VERIFIER.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _app(provider: GovernanceOAuthProvider) -> Starlette:
    app = Starlette(
        routes=create_auth_routes(
            provider,
            issuer_url=AnyHttpUrl("https://gov.example.org"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["mcp:tools"], default_scopes=["mcp:tools"]
            ),
        )
    )
    app.add_middleware(StaticClientBasicAuthShim, client_id=CLIENT_ID)
    return app


def _provider() -> GovernanceOAuthProvider:
    return GovernanceOAuthProvider(
        static_clients=[build_static_client(CLIENT_ID, SECRET, [REDIRECT])]
    )


def _code(client: TestClient) -> str:
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
            "state": "st",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith(REDIRECT)
    return parse_qs(urlparse(location).query)["code"][0]


def _basic(client_id: str, secret: str) -> str:
    from urllib.parse import quote

    raw = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def test_static_client_redeems_a_code_with_secret_in_the_form():
    client = TestClient(_app(_provider()))
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": _code(client),
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": CLIENT_ID,
            "client_secret": SECRET,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"].startswith("at_")


def test_static_client_redeems_a_code_with_basic_auth_and_no_form_client_id():
    """RFC 6749 §2.3.1 clients send Basic and omit client_id from the body."""
    client = TestClient(_app(_provider()))
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": _code(client),
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
        },
        headers={"Authorization": _basic(CLIENT_ID, SECRET)},
    )
    assert resp.status_code == 200, resp.text


def test_basic_auth_with_the_wrong_secret_is_refused():
    client = TestClient(_app(_provider()))
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": _code(client),
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
        },
        headers={"Authorization": _basic(CLIENT_ID, "wrong")},
    )
    assert resp.status_code == 401


def test_static_client_rejects_an_unregistered_redirect():
    client = TestClient(_app(_provider()))
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": "https://attacker.example/cb",
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_static_client_survives_alongside_dcr_registrations():
    provider = _provider()
    client = TestClient(_app(provider))
    resp = client.post(
        "/register",
        json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["client_id"] != CLIENT_ID
    assert CLIENT_ID in provider._clients


@pytest.mark.parametrize(
    "kwargs",
    [
        {"client_id": "", "client_secret": "x", "redirect_uris": [REDIRECT]},
        {"client_id": "x", "client_secret": "", "redirect_uris": [REDIRECT]},
        {"client_id": "x", "client_secret": "y", "redirect_uris": []},
    ],
)
def test_incomplete_static_client_config_raises(kwargs):
    with pytest.raises(ValueError):
        build_static_client(**kwargs)


# --------------------------------------------------------------------------- #
# Host scoping
# --------------------------------------------------------------------------- #


def _scope(
    host: str | None,
    authorization: str | None = None,
    peer: str | None = "127.0.0.1",
    peer_pid: int | None = None,
    extra: tuple[tuple[bytes, bytes], ...] = (),
):
    headers = list(extra)
    if host is not None:
        headers.append((b"host", host.encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    if peer is not None:
        scope["client"] = (peer, 50000)
    if peer_pid is not None:
        scope["unitares_peer_pid"] = peer_pid
    return scope


@pytest.mark.parametrize(
    "host,expected",
    [
        ("gov.example.org", "gov.example.org"),
        ("GOV.example.org:443", "gov.example.org"),
        ("127.0.0.1:8767", "127.0.0.1"),
        ("[::1]:8767", "[::1]"),
        (None, ""),
    ],
)
def test_request_host_normalizes(host, expected):
    assert request_host(_scope(host)) == expected


class _Provider:
    async def load_access_token(self, token):
        if token == "good":
            return AccessToken(token=token, client_id="c", scopes=["mcp:tools"])
        return None


_SCOPED = McpAuthConfig(
    oauth_provider=_Provider(), oauth_enforce_hosts=("gov.example.org",)
)


@pytest.mark.asyncio
async def test_enforced_host_requires_oauth(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope("gov.example.org"), _SCOPED)
    assert decision.allowed is False
    assert decision.response.status_code == 401


@pytest.mark.asyncio
async def test_enforced_host_accepts_an_oauth_token(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(
        _scope("gov.example.org", "Bearer good"), _SCOPED
    )
    assert decision.allowed is True
    assert decision.oauth_client_id == "oauth:c"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost:8767", "127.0.0.1:8767", "192.168.1.151:8767", None])
async def test_other_hosts_stay_ungated(monkeypatch, host):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope(host), _SCOPED)
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_unscoped_oauth_still_gates_every_host(monkeypatch):
    """No enforce list keeps the historical everywhere-gated posture."""
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(
        _scope("localhost:8767"), McpAuthConfig(oauth_provider=_Provider())
    )
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_a_bearer_allowlist_stays_global_under_host_scoping(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: ["tok"])
    decision = await svc.authorize_mcp_request(_scope("localhost:8767"), _SCOPED)
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_failed_gate_closes_only_the_enforced_host(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    cfg = McpAuthConfig(gate_unavailable=True, oauth_enforce_hosts=("gov.example.org",))
    public = await svc.authorize_mcp_request(_scope("gov.example.org"), cfg)
    local = await svc.authorize_mcp_request(_scope("localhost:8767"), cfg)
    assert public.response.status_code == 503
    assert local.allowed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost:8767", "gov.example.org"])
async def test_a_public_peer_is_gated_whatever_host_it_sends(monkeypatch, host):
    """A forged Host, or a tunnel that rewrites Host, must not open the route."""
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope(host, peer="203.0.113.9"), _SCOPED)
    assert decision.allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["10.1.2.3", "100.96.201.46", "::1", "::ffff:127.0.0.1"])
async def test_lan_and_tailnet_peers_are_exempt_on_other_hosts(monkeypatch, peer):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope("localhost:8767", peer=peer), _SCOPED)
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_uds_peer_is_exempt_on_other_hosts(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(
        _scope("localhost", peer=None, peer_pid=4242), _SCOPED
    )
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_a_peerless_request_is_gated(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope("localhost", peer=None), _SCOPED)
    assert decision.allowed is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gov.example.org:*", ("gov.example.org",)),
        ("GOV.example.org:443, gov.example.org", ("gov.example.org",)),
        ("[fd7a::1]:8767", ("[fd7a::1]",)),
        ("", ()),
    ],
)
def test_enforce_hosts_accepts_the_allowed_hosts_forms(monkeypatch, raw, expected):
    """The host:* form taught for UNITARES_MCP_ALLOWED_HOSTS must not ungate."""
    from src.mcp_listen_config import oauth_enforce_hosts

    monkeypatch.setenv("UNITARES_OAUTH_ENFORCE_HOSTS", raw)
    assert oauth_enforce_hosts() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [b"x-forwarded-for", b"X-Forwarded-For", b"forwarded", b"cf-connecting-ip"],
)
async def test_a_relayed_request_from_a_private_peer_is_gated(monkeypatch, header):
    """Docker port forwarding delivers tunnel traffic from a 172.x bridge address
    uvicorn does not trust, so the address alone would read it as local."""
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(
        _scope("localhost", peer="172.17.0.1", extra=((header, b"203.0.113.9"),)),
        _SCOPED,
    )
    assert decision.allowed is False


def test_an_oversized_basic_token_request_is_refused_before_buffering():
    """The client_id is public; an anonymous Basic caller must not make the
    shim buffer an unbounded body ahead of the SDK's own limit."""
    client = TestClient(_app(_provider()))
    resp = client.post(
        "/token",
        content=b"grant_type=authorization_code&code=" + b"x" * (128 * 1024),
        headers={
            "Authorization": _basic(CLIENT_ID, "wrong"),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert resp.status_code == 413
