"""WebSocket auth alignment with the REST bearer gate.

``/ws/eisv`` was the only route on the server with no auth check at all:
``websocket_eisv_stream`` called the broadcaster directly, while every REST
handler went through ``_check_http_auth`` and there is no auth middleware.
Over the tunnel that meant ``GET /v1/residents`` answered 401 while the
WebSocket handshake answered 101 and streamed the full governance feed — agent
ids, EISV, risk, verdicts, and Lumen's raw sensor payload — to any
unauthenticated caller.

These tests pin the aligned posture. ``_check_ws_auth`` mirrors
``_check_http_auth`` exactly, with one difference: a browser cannot set headers
on a ``WebSocket``, so the credential may ride in the query string. Both
sources are accepted; neither weakens the gate.
"""

from __future__ import annotations

import pytest

from src.http_api import _check_ws_auth


class _WS:
    """Minimal stand-in for a Starlette WebSocket: .query_params + .headers + .client."""

    def __init__(
        self,
        ip: str = "10.1.2.3",
        auth: str | None = None,
        qs_token: str | None = None,
        dashboard_session: dict | None = None,
        origin: str | None = None,
    ):
        self.headers = {"authorization": auth} if auth is not None else {}
        if origin is not None:
            self.headers["origin"] = origin
        self.query_params = {"token": qs_token} if qs_token is not None else {}
        self.client = type("C", (), {"host": ip})()
        self.state = type("State", (), {})()
        if dashboard_session is not None:
            self.state.dashboard_session = dashboard_session


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    # Cleared explicitly: every "local posture" assertion below depends on
    # it being absent, and it is about to exist in this machine's env.
    monkeypatch.delenv("UNITARES_REST_STRICT", raising=False)
    yield


# ---- The regression this closes: untrusted peer, no credential ----

def test_untrusted_peer_without_token_is_rejected():
    # The tunnel case. cloudflared + uvicorn proxy-headers rewrite client.host
    # to the real caller IP, so a phone/public client is untrusted -> must
    # present the token. Before the fix this path had no check and returned a
    # live stream.
    assert _check_ws_auth(_WS(ip="8.8.8.8"), http_api_token="s3cret") is False


def test_untrusted_peer_with_query_token_is_accepted():
    assert _check_ws_auth(_WS(ip="8.8.8.8", qs_token="s3cret"), http_api_token="s3cret") is True


def test_untrusted_peer_with_wrong_query_token_is_rejected():
    assert _check_ws_auth(_WS(ip="8.8.8.8", qs_token="nope"), http_api_token="s3cret") is False


def test_untrusted_peer_with_bearer_header_is_accepted():
    # Non-browser clients (wscat, a resident agent) can still use the header.
    assert _check_ws_auth(_WS(ip="8.8.8.8", auth="Bearer s3cret"), http_api_token="s3cret") is True


def test_lowercase_bearer_header_accepted():
    assert _check_ws_auth(_WS(ip="8.8.8.8", auth="bearer s3cret"), http_api_token="s3cret") is True


def test_malformed_authorization_header_is_rejected():
    assert _check_ws_auth(_WS(ip="8.8.8.8", auth="s3cret"), http_api_token="s3cret") is False


# ---- Local posture preserved: loopback/Tailscale keep streaming unauthenticated ----

def test_loopback_bypasses_without_token():
    # The dashboard on localhost and the phase view must not start 403-ing.
    assert _check_ws_auth(_WS(ip="127.0.0.1"), http_api_token="s3cret") is True


def test_tailscale_bypasses_without_token():
    assert _check_ws_auth(_WS(ip="100.101.102.103"), http_api_token="s3cret") is True


def test_no_token_configured_fails_closed_for_untrusted_peer():
    assert _check_ws_auth(_WS(ip="8.8.8.8"), http_api_token=None) is False


def test_cookie_session_accepts_exact_origin():
    ws = _WS(
        ip="8.8.8.8",
        dashboard_session={"operator_label": "operator"},
        origin="https://gov.cirwel.org",
    )
    assert _check_ws_auth(ws, http_api_token=None) is True


@pytest.mark.parametrize("origin", [None, "https://lumen.cirwel.org", "https://evil.example"])
def test_cookie_session_rejects_missing_or_foreign_origin(origin):
    ws = _WS(
        ip="8.8.8.8",
        dashboard_session={"operator_label": "operator"},
        origin=origin,
    )
    assert _check_ws_auth(ws, http_api_token=None) is False


def test_strict_posture_accepts_a_session_from_the_exact_rp_origin(monkeypatch):
    """Reversed by operator decision, 2026-08-28 — see the REST twin.

    The Origin pin the local branch applies is kept here rather than dropped:
    WebSockets receive no CORS protection and sibling subdomains are
    same-site, so a cookie alone is not sufficient evidence on this transport.
    """
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-tok")
    ws = _WS(
        ip="8.8.8.8",
        dashboard_session={"operator_label": "operator"},
        origin="https://gov.cirwel.org",
    )
    assert _check_ws_auth(ws, http_api_token=None) is True


def test_strict_posture_session_from_a_foreign_origin_is_refused(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-tok")
    for origin in ("https://evil.example", None):
        ws = _WS(
            ip="8.8.8.8",
            dashboard_session={"operator_label": "operator"},
            origin=origin,
        )
        assert _check_ws_auth(ws, http_api_token=None) is False


# ---- Hosted mode: MCP bearer configured -> strict, no IP bypass ----

def test_hosted_loopback_still_needs_bearer(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-tok")
    assert _check_ws_auth(_WS(ip="127.0.0.1"), http_api_token=None) is False


def test_hosted_query_token_is_accepted(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-tok")
    assert _check_ws_auth(_WS(ip="10.1.2.3", qs_token="hosted-tok"), http_api_token=None) is True


def test_hosted_wrong_token_is_rejected(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-tok")
    assert _check_ws_auth(_WS(ip="10.1.2.3", qs_token="nope"), http_api_token=None) is False


def test_hosted_ignores_local_http_api_token(monkeypatch):
    # In hosted posture the MCP bearer is the only credential; the legacy local
    # token must not open a side door.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-tok")
    assert _check_ws_auth(_WS(ip="10.1.2.3", qs_token="local-tok"), http_api_token="local-tok") is False


# ---- The gate is actually wired into the handler ----

@pytest.mark.asyncio
async def test_handler_closes_unauthorized_before_connecting(monkeypatch):
    """An unauthorized handshake must close without ever reaching the broadcaster."""
    from src import http_api

    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "s3cret")
    connected: list = []
    monkeypatch.setattr(
        http_api.broadcaster_instance,
        "connect",
        lambda ws: connected.append(ws),
    )

    closed: list = []

    class _RejectWS(_WS):
        async def close(self, code=1000):
            closed.append(code)

    await http_api.websocket_eisv_stream(_RejectWS(ip="8.8.8.8"))

    assert closed == [1008], "unauthorized handshake should close with policy-violation"
    assert connected == [], "broadcaster must never see an unauthorized socket"
