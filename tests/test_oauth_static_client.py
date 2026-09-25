"""Pre-registered OAuth client and the public-listener /mcp gate.

Some hosted connectors cannot self-register: Google's custom MCP connector asks
the operator for a client ID and secret. These tests pin the static client (it
authenticates at the token endpoint whichever standard way the connector sends
its secret) and the dedicated public listener that carries the OAuth gate, so
local callers on the main port are unaffected when OAuth is switched on.

The listener, not Host / peer address / forwarding headers, decides: every one
of those is set by the caller or by whatever proxy sits in front, and review of
the host-scoped version found a proxy configuration that defeated each.
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
from src.services.mcp_transport_service import (
    PUBLIC_LISTENER_SCOPE_KEY,
    McpAuthConfig,
    mark_public_listener,
)

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
# Public listener gate
# --------------------------------------------------------------------------- #


def _scope(authorization: str | None = None, public: bool = False, host: str = "localhost:8767"):
    headers = [(b"host", host.encode())]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
    }
    if public:
        scope[PUBLIC_LISTENER_SCOPE_KEY] = True
    return scope


class _Provider:
    async def load_access_token(self, token):
        if token == "good":
            return AccessToken(token=token, client_id="c", scopes=["mcp:tools"])
        return None


_LISTENER = McpAuthConfig(oauth_provider=_Provider(), oauth_public_listener_only=True)


@pytest.mark.asyncio
async def test_public_listener_requires_oauth(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope(public=True), _LISTENER)
    assert decision.allowed is False
    assert decision.response.status_code == 401


@pytest.mark.asyncio
async def test_public_listener_accepts_an_oauth_token(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope("Bearer good", public=True), _LISTENER)
    assert decision.allowed is True
    assert decision.oauth_client_id == "oauth:c"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_header",
    [(b"x-forwarded-for", b"203.0.113.9"), (b"host", b"gov.example.org"), (b"cf-connecting-ip", b"1.2.3.4")],
)
async def test_nothing_in_the_request_makes_the_public_listener_local(monkeypatch, extra_header):
    """Every header the host-scoped design keyed on is irrelevant now."""
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    scope = _scope(public=True)
    scope["headers"].append(extra_header)
    decision = await svc.authorize_mcp_request(scope, _LISTENER)
    assert decision.allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost:8767", "gov.example.org", "127.0.0.1:8767"])
async def test_main_listener_is_not_gated(monkeypatch, host):
    """A default nginx / Host-rewriting tunnel pointed at the main port is the
    operator's own exposure, exactly as before this change; the public tunnel
    belongs on the public listener."""
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope(host=host), _LISTENER)
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_main_listener_still_attributes_a_presented_token(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope("Bearer good"), _LISTENER)
    assert decision.allowed is True
    assert decision.oauth_client_id == "oauth:c"


@pytest.mark.asyncio
async def test_main_listener_ignores_a_bad_token(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(_scope("Bearer nope"), _LISTENER)
    assert decision.allowed is True
    assert decision.oauth_client_id is None


@pytest.mark.asyncio
async def test_without_a_public_listener_oauth_gates_every_request(monkeypatch):
    """Unset keeps the historical posture."""
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    decision = await svc.authorize_mcp_request(
        _scope(), McpAuthConfig(oauth_provider=_Provider())
    )
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_a_bearer_allowlist_stays_global(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: ["tok"])
    decision = await svc.authorize_mcp_request(_scope(), _LISTENER)
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_failed_gate_closes_only_the_public_listener(monkeypatch):
    monkeypatch.setattr(svc, "mcp_bearer_tokens", lambda: [])
    cfg = McpAuthConfig(gate_unavailable=True, oauth_public_listener_only=True)
    public = await svc.authorize_mcp_request(_scope(public=True), cfg)
    main = await svc.authorize_mcp_request(_scope(), cfg)
    assert public.response.status_code == 503
    assert main.allowed is True


@pytest.mark.asyncio
async def test_mark_public_listener_stamps_every_request():
    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    await mark_public_listener(inner)({"type": "http", "headers": []}, None, None)
    assert seen[PUBLIC_LISTENER_SCOPE_KEY] is True


@pytest.mark.parametrize(
    "raw,expected",
    [("8772", 8772), ("", None), ("nope", None), ("0", None), ("70000", None)],
)
def test_public_port_parsing_fails_closed(monkeypatch, raw, expected):
    """An invalid value leaves OAuth on every request rather than none."""
    from src.mcp_listen_config import oauth_public_port

    monkeypatch.setenv("UNITARES_OAUTH_PUBLIC_PORT", raw)
    assert oauth_public_port() == expected


def test_rest_never_trusts_the_public_listener():
    from starlette.requests import Request

    from src.http_routes.access import _is_trusted_network

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 1)}
    assert _is_trusted_network(Request(scope)) is True
    assert _is_trusted_network(Request({**scope, PUBLIC_LISTENER_SCOPE_KEY: True})) is False

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


@pytest.mark.parametrize(
    "env",
    [
        {"UNITARES_OAUTH_STATIC_CLIENT_SECRET": "s", "UNITARES_OAUTH_STATIC_REDIRECT_URIS": REDIRECT},
        {"UNITARES_OAUTH_STATIC_CLIENT_ID": "c", "UNITARES_OAUTH_STATIC_REDIRECT_URIS": REDIRECT},
        {"UNITARES_OAUTH_STATIC_CLIENT_ID": "c", "UNITARES_OAUTH_STATIC_CLIENT_SECRET": "s"},
    ],
)
def test_partial_static_client_env_fails_loudly(monkeypatch, env):
    """A misspelled or missing variable must not silently register nothing."""
    from src.oauth_provider import static_clients_from_env

    for key in (
        "UNITARES_OAUTH_STATIC_CLIENT_ID",
        "UNITARES_OAUTH_STATIC_CLIENT_SECRET",
        "UNITARES_OAUTH_STATIC_REDIRECT_URIS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        static_clients_from_env()


def test_static_client_env_unset_registers_nothing(monkeypatch):
    from src.oauth_provider import static_clients_from_env

    for key in (
        "UNITARES_OAUTH_STATIC_CLIENT_ID",
        "UNITARES_OAUTH_STATIC_CLIENT_SECRET",
        "UNITARES_OAUTH_STATIC_REDIRECT_URIS",
    ):
        monkeypatch.delenv(key, raising=False)
    assert static_clients_from_env() == []
