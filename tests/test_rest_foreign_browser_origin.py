"""A foreign browser page must not inherit the trusted-network bypass.

The local posture trusts loopback and RFC 1918 source addresses. A browser on
the operator's machine sends from loopback whatever page asked, so without an
Origin/Host check any web page could POST a CORS-simple body to
``/v1/tools/call`` (cross-site request forgery), open ``/ws/eisv`` (no CORS on
WebSockets), or — after DNS rebinding — read the dashboard, which embeds the
API token. Non-browser clients send neither Origin nor Sec-Fetch-*, so they
keep the bypass unchanged.
"""

from __future__ import annotations

import pytest

from src.http_api import _check_http_auth
from src.http_routes.access import _check_ws_auth, _foreign_browser_request


class _Req:
    def __init__(self, ip: str = "127.0.0.1", headers: dict | None = None):
        self.headers = dict(headers or {})
        self.client = type("C", (), {"host": ip})()
        self.state = type("State", (), {})()
        self.scope = {}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "UNITARES_MCP_BEARER_TOKENS",
        "UNITARES_HTTP_API_TOKEN",
        "UNITARES_REST_STRICT",
        "UNITARES_MCP_ALLOWED_ORIGINS",
        "UNITARES_MCP_ALLOWED_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)


def _auth(req) -> bool:
    return _check_http_auth(req, http_api_token=None)


# ---- Non-browser clients: unchanged ----

def test_non_browser_loopback_keeps_bypass():
    assert _auth(_Req(headers={"host": "127.0.0.1:8767"})) is True


@pytest.mark.parametrize("host", [
    "governance-mcp:8767",  # Compose service name
    "localhost",
    "192.168.1.151:8767",
    "100.96.201.46:8767",
    "[fd7a:115c:a1e0::d201:c996]:8767",
    "",
])
def test_non_browser_non_rebindable_host_keeps_bypass(host):
    headers = {"host": host} if host else {}
    assert _auth(_Req(ip="172.18.0.1", headers=headers)) is True


def test_dotted_hostname_must_be_listed(monkeypatch):
    req = _Req(headers={"host": "unitares.tail76aee6.ts.net:8767"})
    assert _auth(req) is False
    monkeypatch.setenv("UNITARES_MCP_ALLOWED_HOSTS", "unitares.tail76aee6.ts.net:*")
    assert _auth(req) is True


# ---- Cross-site request forgery ----

def test_cross_site_post_origin_loses_bypass():
    req = _Req(headers={
        "host": "127.0.0.1:8767",
        "origin": "https://evil.example",
        "sec-fetch-site": "cross-site",
    })
    assert _auth(req) is False


def test_origin_without_fetch_metadata_still_checked():
    # Older browsers send Origin on POST but no Sec-Fetch-*.
    req = _Req(headers={"host": "127.0.0.1:8767", "origin": "https://evil.example"})
    assert _auth(req) is False


def test_null_origin_loses_bypass():
    # Sandboxed iframes on any site send the opaque origin.
    req = _Req(headers={"host": "127.0.0.1:8767", "origin": "null"})
    assert _auth(req) is False


def test_cross_site_without_origin_loses_bypass():
    req = _Req(headers={"host": "localhost:8767", "sec-fetch-site": "cross-site"})
    assert _auth(req) is False


def test_foreign_page_can_still_present_a_token(monkeypatch):
    req = _Req(headers={
        "host": "127.0.0.1:8767",
        "origin": "https://evil.example",
        "authorization": "Bearer local-tok",
    })
    assert _check_http_auth(req, http_api_token="local-tok") is True


# ---- DNS rebinding ----

@pytest.mark.parametrize("ip", ["127.0.0.1", "172.18.0.1"])
def test_rebound_same_origin_get_loses_bypass(ip):
    # Plain-http same-origin GET: no Origin, no Sec-Fetch-*; only the Host
    # names the attacker. 172.18.0.1 is the Compose bridge gateway.
    req = _Req(ip=ip, headers={"host": "rebind.evil.example:8767"})
    assert _auth(req) is False


def test_rebound_trailing_dot_host_loses_bypass():
    assert _auth(_Req(headers={"host": "localhost.:8767"})) is False


def test_rebound_post_loses_bypass():
    req = _Req(headers={
        "host": "rebind.evil.example:8767",
        "origin": "http://rebind.evil.example:8767",
        "sec-fetch-site": "same-origin",
    })
    assert _auth(req) is False


# ---- The operator's own browser keeps working ----

@pytest.mark.parametrize("host,origin", [
    ("127.0.0.1:8767", "http://127.0.0.1:8767"),
    ("localhost:8767", "http://localhost:8767"),
    ("[::1]:8767", "http://[::1]:8767"),
    ("localhost", "http://localhost"),
])
def test_localhost_dashboard_keeps_bypass(host, origin):
    req = _Req(headers={"host": host, "origin": origin, "sec-fetch-site": "same-origin"})
    assert _auth(req) is True


def test_localhost_navigation_keeps_bypass():
    req = _Req(headers={"host": "localhost:8767", "sec-fetch-site": "none"})
    assert _auth(req) is True


def test_listed_lan_origin_and_host_keep_bypass(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_ALLOWED_HOSTS", "192.168.1.151:*")
    monkeypatch.setenv("UNITARES_MCP_ALLOWED_ORIGINS", "http://192.168.1.151:*")
    req = _Req(ip="192.168.1.20", headers={
        "host": "192.168.1.151:8767",
        "origin": "http://192.168.1.151:8767",
        "sec-fetch-site": "same-origin",
    })
    assert _auth(req) is True


def test_unlisted_lan_origin_from_browser_loses_bypass():
    req = _Req(ip="192.168.1.20", headers={
        "host": "192.168.1.151:8767",
        "origin": "http://192.168.1.151:8767",
        "sec-fetch-site": "same-origin",
    })
    assert _auth(req) is False


def test_port_wildcard_requires_digits():
    req = _Req(headers={"host": "127.0.0.1:8767", "origin": "http://localhost:8767.evil.example"})
    assert _foreign_browser_request(req) is True


# ---- WebSocket feed ----

def test_ws_cross_site_origin_loses_bypass():
    ws = _Req(headers={
        "host": "127.0.0.1:8767",
        "Origin": "https://evil.example",
        "sec-fetch-site": "cross-site",
    })
    assert _check_ws_auth(ws, http_api_token=None) is False


def test_ws_localhost_origin_keeps_bypass():
    ws = _Req(headers={
        "host": "127.0.0.1:8767",
        "origin": "http://127.0.0.1:8767",
        "sec-fetch-site": "same-origin",
    })
    assert _check_ws_auth(ws, http_api_token=None) is True
