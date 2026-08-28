"""REST posture is configurable independently of the /mcp bearer gate.

One predicate used to govern both surfaces, which made securing a publicly
reachable /mcp an all-or-nothing act: configuring the bearer to close it also
revoked the trusted-network branch for REST, so every resident, CLI and LAN
caller that never left the machine began answering 401 — and the browser lost
its only way in, because a navigation cannot set an Authorization header.

Two changes, pinned here:
  * UNITARES_REST_STRICT decides REST posture, defaulting to the MCP gate so
    an existing deployment keeps exactly the posture it has today.
  * Strict posture admits a DB-validated passkey session alongside the bearer
    (operator decision, 2026-08-28) — a revocable credential that is stronger
    evidence than the source-IP check local posture already accepts.
"""

from __future__ import annotations

import pytest

from src.http_routes.access import _check_http_auth, _check_ws_auth
from src.mcp_listen_config import rest_strict_required

BEARER = "hosted-secret"  # noqa: S105 - test fixture
LOCAL_TOKEN = "local-token"  # noqa: S105 - test fixture
RP_ORIGIN = "https://gov.cirwel.org"


class _Req:
    """Stand-in for a Starlette request: headers, client.host, session state."""

    def __init__(self, ip="10.1.2.3", auth=None, session=None):
        self.headers = {"authorization": auth} if auth else {}
        self.client = type("C", (), {"host": ip})()
        self.state = type("S", (), {})()
        if session is not None:
            self.state.dashboard_session = session


class _WS(_Req):
    def __init__(self, ip="10.1.2.3", auth=None, session=None, origin=None, token=None):
        super().__init__(ip=ip, auth=auth, session=session)
        if origin:
            self.headers["origin"] = origin
        self.query_params = {"token": token} if token else {}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    monkeypatch.delenv("UNITARES_REST_STRICT", raising=False)


# --- the default must not move ------------------------------------------------

def test_unset_follows_the_mcp_gate_off(monkeypatch):
    assert rest_strict_required() is False


def test_unset_follows_the_mcp_gate_on(monkeypatch):
    """The compatibility guarantee: a deployment that set only the MCP bearer
    keeps the strict REST posture it has today. Loosening is explicit."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    assert rest_strict_required() is True


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_explicit_on(monkeypatch, raw):
    monkeypatch.setenv("UNITARES_REST_STRICT", raw)
    assert rest_strict_required() is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off"])
def test_explicit_off(monkeypatch, raw):
    monkeypatch.setenv("UNITARES_REST_STRICT", raw)
    assert rest_strict_required() is False


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_counts_as_unset(monkeypatch, blank):
    """os.getenv returns "" for a present-but-empty var — an ordinary state
    from a compose template line. Treating it as unparseable would force
    strict posture on a deployment that configured nothing, which is exactly
    the lockout this predicate exists to prevent."""
    monkeypatch.setenv("UNITARES_REST_STRICT", blank)
    assert rest_strict_required() is False
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    assert rest_strict_required() is True


def test_unparseable_value_fails_strict(monkeypatch):
    """An unreadable setting must not read as 'off' and widen the surface."""
    monkeypatch.setenv("UNITARES_REST_STRICT", "maybe")
    assert rest_strict_required() is True


# --- the decoupling this change exists for -----------------------------------

def test_mcp_can_be_closed_without_locking_out_loopback(monkeypatch):
    """The operator's actual goal: /mcp requires a bearer, while the residents
    and CLI on this machine keep working exactly as before."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    monkeypatch.setenv("UNITARES_REST_STRICT", "0")
    assert _check_http_auth(_Req(ip="127.0.0.1"), http_api_token=LOCAL_TOKEN) is True
    assert _check_http_auth(_Req(ip="100.96.201.46"), http_api_token=LOCAL_TOKEN) is True


def test_decoupled_rest_still_denies_the_public_internet(monkeypatch):
    """Loosening REST must not mean opening it: an untrusted peer with no
    credential is still refused."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    monkeypatch.setenv("UNITARES_REST_STRICT", "0")
    assert _check_http_auth(_Req(ip="203.0.113.7"), http_api_token=LOCAL_TOKEN) is False


# --- strict posture keeps its teeth, and gains a browser ---------------------

def test_strict_still_refuses_the_rfc1918_proxy_bypass(monkeypatch):
    """The gap strict posture exists to close: a hosted server behind a cloud
    proxy sees a 10.x source IP, which must not authenticate anything."""
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    assert _check_http_auth(_Req(ip="10.1.2.3"), http_api_token=LOCAL_TOKEN) is False
    assert _check_http_auth(_Req(ip="127.0.0.1"), http_api_token=LOCAL_TOKEN) is False


def test_strict_rejects_the_local_token(monkeypatch):
    """UNITARES_HTTP_API_TOKEN is not a strict-posture credential."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    req = _Req(ip="203.0.113.7", auth=f"Bearer {LOCAL_TOKEN}")
    assert _check_http_auth(req, http_api_token=LOCAL_TOKEN) is False


def test_strict_accepts_the_mcp_bearer(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    req = _Req(ip="203.0.113.7", auth=f"Bearer {BEARER}")
    assert _check_http_auth(req, http_api_token=None) is True


def test_strict_accepts_a_validated_passkey_session(monkeypatch):
    """Operator decision 2026-08-28: without this, strict posture has no
    browser path at all — a navigation cannot send an Authorization header."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    req = _Req(ip="203.0.113.7", session={"operator_label": "kenny"})
    assert _check_http_auth(req, http_api_token=None) is True


def test_strict_without_session_or_bearer_is_refused(monkeypatch):
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    assert _check_http_auth(_Req(ip="203.0.113.7"), http_api_token=None) is False


# --- the WebSocket keeps its Origin pin --------------------------------------

def test_ws_strict_session_requires_the_exact_rp_origin(monkeypatch):
    """WebSockets get no CORS protection and sibling subdomains are same-site,
    so a cookie alone is not sufficient evidence on this transport."""
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    good = _WS(ip="203.0.113.7", session={"o": 1}, origin=RP_ORIGIN)
    bad = _WS(ip="203.0.113.7", session={"o": 1}, origin="https://evil.example")
    none = _WS(ip="203.0.113.7", session={"o": 1})
    from src.http_routes import access

    monkeypatch.setattr(access, "DASHBOARD_EXPECTED_ORIGIN", RP_ORIGIN, raising=False)
    assert access._check_ws_auth(good, http_api_token=None) is True
    assert access._check_ws_auth(bad, http_api_token=None) is False
    assert access._check_ws_auth(none, http_api_token=None) is False


def test_ws_strict_accepts_the_bearer_by_query_param(monkeypatch):
    """Browsers cannot set headers on a WebSocket handshake, so the bearer
    rides in the query string — unchanged by this refactor."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    assert _check_ws_auth(_WS(ip="203.0.113.7", token=BEARER), http_api_token=None) is True


# --- the dashboard token-injection guard follows REST posture, not /mcp -----

def _shell(peer, monkeypatch_env=None):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from src.http_routes.dashboard import http_dashboard_redesign

    app = Starlette(routes=[Route("/", http_dashboard_redesign, methods=["GET"])])
    return TestClient(app, client=peer)


def test_token_injected_when_mcp_closed_but_rest_loosened(monkeypatch):
    """The headline configuration: /mcp requires a bearer while REST keeps
    local posture. UNITARES_HTTP_API_TOKEN is a valid REST credential there,
    so the browser should receive it — keying this on the /mcp gate instead
    of REST posture withheld it."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", BEARER)
    monkeypatch.setenv("UNITARES_REST_STRICT", "0")
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", LOCAL_TOKEN)
    r = _shell(("127.0.0.1", 50000)).get("/")
    assert r.status_code == 200
    assert LOCAL_TOKEN in r.text


def test_token_withheld_in_strict_posture_without_the_mcp_gate(monkeypatch):
    """UNITARES_REST_STRICT=1 with no bearer configured: the local token is
    not a strict-posture credential, so injecting it would hand the browser
    something every gated route rejects."""
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", LOCAL_TOKEN)
    r = _shell(("127.0.0.1", 50000)).get("/")
    assert r.status_code == 200
    assert LOCAL_TOKEN not in r.text
