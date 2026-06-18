"""REST auth alignment with the /mcp bearer gate.

The /mcp transport (bearer gate, #847) and the REST tool-call surface reach the
same tools. Before, REST bypassed auth for any trusted network — which includes
every RFC1918 range, so a hosted server behind a cloud proxy (source IP ~10.x)
would bypass the write path. These tests pin the aligned posture: when the
hosted bearer (UNITARES_MCP_BEARER_TOKENS) is set, REST requires it with NO
trusted-network bypass; otherwise the legacy local posture is unchanged.
"""

from __future__ import annotations

import pytest

from src.http_api import _check_http_auth


class _Req:
    """Minimal stand-in for a Starlette request: .headers.get + .client.host."""

    def __init__(self, ip: str = "10.1.2.3", auth: str | None = None):
        self.headers = {"authorization": auth} if auth is not None else {}
        self.client = type("C", (), {"host": ip})()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    yield


# ---- Hosted mode: MCP bearer configured -> strict, no IP bypass ----

def test_hosted_trusted_ip_without_bearer_is_rejected(monkeypatch):
    # The core fix: a 10.x cloud-internal peer must NOT bypass auth when hosted.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert _check_http_auth(_Req(ip="10.1.2.3", auth=None), http_api_token=None) is False


def test_hosted_valid_bearer_is_accepted(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert _check_http_auth(_Req(ip="10.1.2.3", auth="Bearer s3cret"), http_api_token=None) is True


def test_hosted_lowercase_bearer_accepted(monkeypatch):
    # REST historically accepted case-insensitive "bearer "; the aligned gate
    # must not regress that (RFC 7235 — scheme is case-insensitive).
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert _check_http_auth(_Req(ip="10.1.2.3", auth="bearer s3cret"), http_api_token=None) is True


def test_hosted_wrong_bearer_is_rejected(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert _check_http_auth(_Req(ip="127.0.0.1", auth="Bearer nope"), http_api_token=None) is False


def test_hosted_localhost_still_needs_bearer(monkeypatch):
    # Even localhost gets no free pass in hosted mode.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert _check_http_auth(_Req(ip="127.0.0.1", auth=None), http_api_token=None) is False


# ---- Local default: no MCP bearer -> legacy posture unchanged ----

def test_local_trusted_network_bypasses(monkeypatch):
    assert _check_http_auth(_Req(ip="192.168.1.5", auth=None), http_api_token=None) is True


def test_local_untrusted_no_token_allows(monkeypatch):
    # Default-off: no token configured -> open (unchanged legacy behavior).
    assert _check_http_auth(_Req(ip="8.8.8.8", auth=None), http_api_token=None) is True


def test_local_untrusted_with_token_enforced(monkeypatch):
    req_ok = _Req(ip="8.8.8.8", auth="Bearer localtok")
    req_bad = _Req(ip="8.8.8.8", auth="Bearer wrong")
    assert _check_http_auth(req_ok, http_api_token="localtok") is True
    assert _check_http_auth(req_bad, http_api_token="localtok") is False
