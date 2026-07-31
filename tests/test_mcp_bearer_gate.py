"""Bearer gate for the /mcp endpoint (src/mcp_listen_config).

This is the one primitive a hosted deployment needs and that was absent: a way
to require an Authorization: Bearer token on every /mcp request, while staying
byte-identical to current behavior when no token is configured (localhost dev /
self-host). These tests pin both halves — the off-by-default posture and the
on-path accept/reject rules — plus the deliberate no-trusted-bypass design.
"""

from __future__ import annotations

import importlib
import time

import pytest
from mcp.server.auth.provider import AccessToken

import src.mcp_listen_config as cfg


@pytest.fixture(autouse=True)
def _clear_bearer_env(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    importlib.reload(cfg)
    yield


def test_gate_off_by_default():
    # No env configured -> gate OFF, everything allowed, even no header.
    assert cfg.mcp_bearer_required() is False
    assert cfg.check_mcp_bearer(None) is True
    assert cfg.check_mcp_bearer("anything") is True


def test_gate_on_when_tokens_configured(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert cfg.mcp_bearer_required() is True
    assert cfg.mcp_bearer_tokens() == ["s3cret"]


def test_valid_bearer_accepted(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert cfg.check_mcp_bearer("Bearer s3cret") is True


@pytest.mark.parametrize(
    "header",
    [
        None,                       # missing header
        "",                         # empty header
        "s3cret",                   # raw token, no "Bearer " scheme
        "Bearer ",                  # scheme but empty token
        "Bearer wrong",             # wrong token
        "Basic s3cret",             # wrong scheme
    ],
)
def test_invalid_bearer_rejected_when_gate_on(monkeypatch, header):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert cfg.check_mcp_bearer(header) is False


@pytest.mark.parametrize("header", ["Bearer s3cret", "bearer s3cret", "BEARER s3cret"])
def test_bearer_scheme_is_case_insensitive(monkeypatch, header):
    # RFC 7235: the auth scheme is case-insensitive. Real clients send "bearer".
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert cfg.check_mcp_bearer(header) is True


class _OAuthProvider:
    def __init__(self, tokens):
        self.tokens = tokens

    async def load_access_token(self, token):
        return self.tokens.get(token)


@pytest.mark.asyncio
async def test_oauth_bearer_accepts_provider_token():
    provider = _OAuthProvider({
        "oauth-token": AccessToken(
            token="oauth-token",
            client_id="client-1",
            scopes=["mcp:tools"],
            expires_at=int(time.time()) + 60,
        )
    })

    ok, client_id = await cfg.check_oauth_bearer(
        "Bearer oauth-token",
        provider,
        required_scopes=["mcp:tools"],
    )

    assert ok is True
    assert client_id == "client-1"


@pytest.mark.asyncio
async def test_oauth_bearer_rejects_unknown_token():
    ok, client_id = await cfg.check_oauth_bearer(
        "Bearer missing",
        _OAuthProvider({}),
        required_scopes=["mcp:tools"],
    )

    assert ok is False
    assert client_id is None


@pytest.mark.asyncio
async def test_oauth_bearer_rejects_missing_required_scope():
    provider = _OAuthProvider({
        "oauth-token": AccessToken(
            token="oauth-token",
            client_id="client-1",
            scopes=["profile"],
            expires_at=int(time.time()) + 60,
        )
    })

    ok, client_id = await cfg.check_oauth_bearer(
        "Bearer oauth-token",
        provider,
        required_scopes=["mcp:tools"],
    )

    assert ok is False
    assert client_id is None


@pytest.mark.asyncio
async def test_oauth_bearer_rejects_expired_token():
    provider = _OAuthProvider({
        "oauth-token": AccessToken(
            token="oauth-token",
            client_id="client-1",
            scopes=["mcp:tools"],
            expires_at=int(time.time()) - 1,
        )
    })

    ok, client_id = await cfg.check_oauth_bearer(
        "Bearer oauth-token",
        provider,
        required_scopes=["mcp:tools"],
    )

    assert ok is False
    assert client_id is None


def test_token_rotation_accepts_any_listed(monkeypatch):
    # CSV allowlist lets an operator rotate without dropping the old token mid-flight.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "old-token, new-token")
    assert cfg.mcp_bearer_tokens() == ["old-token", "new-token"]
    assert cfg.check_mcp_bearer("Bearer old-token") is True
    assert cfg.check_mcp_bearer("Bearer new-token") is True
    assert cfg.check_mcp_bearer("Bearer retired-token") is False


def test_whitespace_only_env_is_gate_off(monkeypatch):
    # "   ,  ," parses to zero tokens -> still OFF (no accidental empty-token allow).
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "   ,  ,")
    assert cfg.mcp_bearer_required() is False
    assert cfg.check_mcp_bearer(None) is True


def test_read_fresh_each_call_supports_runtime_rotation(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "first")
    assert cfg.check_mcp_bearer("Bearer first") is True
    # Rotate in-process; no restart, no reload — read-fresh semantics.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "second")
    assert cfg.check_mcp_bearer("Bearer first") is False
    assert cfg.check_mcp_bearer("Bearer second") is True


def test_explicit_allow_overrides_env(monkeypatch):
    # The ASGI gate fetches the allowlist once and passes it through, so the
    # decision uses one snapshot (no add/remove TOCTOU vs a second env read).
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "env-token")
    assert cfg.check_mcp_bearer("Bearer snapshot", allow=["snapshot"]) is True
    assert cfg.check_mcp_bearer("Bearer env-token", allow=["snapshot"]) is False
    # Empty explicit allow == gate off for this decision.
    assert cfg.check_mcp_bearer(None, allow=[]) is True


def _request(host: str, origin: str | None = None):
    """Minimal Starlette request carrying just the headers the guard inspects."""
    from starlette.requests import Request

    headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "POST", "headers": headers})


def test_streamable_manager_carries_dns_rebinding_protection(monkeypatch):
    # /mcp is served by the manager mcp_server builds, NOT by the SDK's own
    # streamable_http_app(), so the transport_security= passed to the high-level
    # server object never reaches this transport. With security_settings=None the
    # SDK's TransportSecurityMiddleware disables protection outright — i.e. the
    # allowlists below would be configured but never enforced. Pin that the
    # factory attaches them.
    monkeypatch.setenv("UNITARES_MCP_ALLOWED_HOSTS", "gov.example.org")
    monkeypatch.setenv("UNITARES_MCP_ALLOWED_ORIGINS", "https://gov.example.org")

    manager = cfg.build_streamable_session_manager(object())
    settings = manager.security_settings

    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert "gov.example.org" in settings.allowed_hosts
    assert "127.0.0.1:*" in settings.allowed_hosts


def test_dns_rebinding_protection_has_an_operator_kill_switch(monkeypatch):
    # Flipping enforcement on for a live deployment needs a no-redeploy way out
    # if a client turns up under a Host nobody enumerated. Default stays ON.
    assert cfg.dns_rebinding_protection_enabled() is True
    assert cfg.build_transport_security_settings().enable_dns_rebinding_protection is True

    monkeypatch.setenv("UNITARES_MCP_DNS_REBIND_PROTECTION", "off")
    assert cfg.dns_rebinding_protection_enabled() is False
    assert cfg.build_transport_security_settings().enable_dns_rebinding_protection is False

    # Only an explicit falsy value disables it — an unrecognized value stays safe.
    monkeypatch.setenv("UNITARES_MCP_DNS_REBIND_PROTECTION", "maybe")
    assert cfg.dns_rebinding_protection_enabled() is True


@pytest.mark.asyncio
async def test_configured_allowlists_reject_forged_host_and_origin(monkeypatch):
    # Behavioral half: the settings this repo builds actually accept the hosts an
    # operator allowlists and reject everything else (a forged Host is the DNS-
    # rebinding vector; a foreign Origin is the browser-driven one).
    from mcp.server.transport_security import TransportSecurityMiddleware

    monkeypatch.setenv("UNITARES_MCP_ALLOWED_HOSTS", "gov.example.org")
    monkeypatch.setenv("UNITARES_MCP_ALLOWED_ORIGINS", "https://gov.example.org")
    guard = TransportSecurityMiddleware(cfg.build_transport_security_settings())

    # Allowed: loopback with a port (base allowlist) and the configured host.
    assert await guard.validate_request(_request("127.0.0.1:8767"), is_post=True) is None
    assert await guard.validate_request(_request("gov.example.org"), is_post=True) is None

    # Rejected: forged Host, and an allowed Host with a foreign Origin.
    assert await guard.validate_request(_request("evil.example.com"), is_post=True) is not None
    assert await guard.validate_request(
        _request("127.0.0.1:8767", origin="https://evil.example.com"), is_post=True
    ) is not None


def test_sse_routes_are_prunable_to_close_gate_bypass():
    # The /mcp bearer gate would be bypassable if /sse + /messages/ (which the
    # SDK wires to the SAME tool registry, unauthenticated when OAuth is off)
    # stayed mounted. mcp_server prunes them; assert that prune fully removes
    # the SSE surface so no ungated route reaches the tools.
    # Use the compat shim's FastMCP (MCPServer on mcp 2.x) — resolved to the
    # real class at import time, so it is unaffected by tests that stub
    # `mcp.server.fastmcp` in sys.modules. Mirror mcp_server.py's version-
    # agnostic path lookup: 1.x exposes the mount paths on settings, 2.x uses
    # the fixed sse_app() defaults (/sse, /messages).
    from src.mcp_compat import FastMCP

    m = FastMCP("probe")
    app = m.sse_app()
    before = {getattr(r, "path", None) for r in app.routes}
    _sse = getattr(m.settings, "sse_path", "/sse")
    _msg = getattr(m.settings, "message_path", "/messages/").rstrip("/")
    assert _sse in before  # /sse present pre-prune

    app.routes[:] = [r for r in app.routes if getattr(r, "path", None) not in (_sse, _msg)]

    after = {getattr(r, "path", None) for r in app.routes}
    assert _sse not in after
    assert _msg not in after
