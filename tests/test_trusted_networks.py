"""The REST trusted-network bypass trusts only what every install shares.

Loopback and the RFC1918 private ranges are built in. Overlay and VPN ranges
are not: 100.64.0.0/10, which Tailscale assigns from and some ISPs use for
carrier-grade NAT, used to be, so every install trusted one operator's tailnet
layout. An operator on such a network now lists it in UNITARES_TRUSTED_NETWORKS.
"""

from __future__ import annotations

import ipaddress

import pytest
from starlette.requests import Request

from src.http_routes.access import _is_trusted_network


def _request(host: str, **scope_extra) -> Request:
    return Request({"type": "http", "headers": [], "client": (host, 1), **scope_extra})


@pytest.fixture(autouse=True)
def _no_extra_networks(monkeypatch):
    monkeypatch.delenv("UNITARES_TRUSTED_NETWORKS", raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.168.1.5", "10.2.3.4", "172.17.0.1"])
def test_loopback_and_private_ranges_are_trusted_by_default(host):
    assert _is_trusted_network(_request(host)) is True


@pytest.mark.parametrize("host", ["100.64.0.1", "100.100.100.100", "100.127.255.254"])
def test_the_cgnat_tailscale_range_is_not_trusted_by_default(host):
    assert _is_trusted_network(_request(host)) is False


def test_a_public_address_is_not_trusted():
    assert _is_trusted_network(_request("8.8.8.8")) is False


def test_an_operator_adds_their_tailnet(monkeypatch):
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "100.64.0.0/10")
    assert _is_trusted_network(_request("100.100.100.100")) is True
    assert _is_trusted_network(_request("8.8.8.8")) is False


def test_several_entries_and_a_bare_address(monkeypatch):
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", " 100.64.0.0/10 , 203.0.113.7 ")
    assert _is_trusted_network(_request("100.64.0.9")) is True
    assert _is_trusted_network(_request("203.0.113.7")) is True
    assert _is_trusted_network(_request("203.0.113.8")) is False


def test_a_bad_entry_is_skipped_not_widened(monkeypatch):
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "not-a-network, 0.0.0.0/garbage, 100.64.0.0/10")
    assert _is_trusted_network(_request("100.64.0.9")) is True
    assert _is_trusted_network(_request("8.8.8.8")) is False


def test_the_setting_is_reread_when_it_changes(monkeypatch):
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "100.64.0.0/10")
    assert _is_trusted_network(_request("100.64.0.9")) is True
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "")
    assert _is_trusted_network(_request("100.64.0.9")) is False


def test_the_public_listener_is_never_trusted_even_when_listed(monkeypatch):
    from src.services.mcp_transport_service import PUBLIC_LISTENER_SCOPE_KEY

    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "100.64.0.0/10")
    assert _is_trusted_network(_request("100.64.0.9", **{PUBLIC_LISTENER_SCOPE_KEY: True})) is False
    assert _is_trusted_network(_request("127.0.0.1", **{PUBLIC_LISTENER_SCOPE_KEY: True})) is False


def test_a_cidr_with_host_bits_is_refused_not_masked_wider(monkeypatch):
    # 203.0.113.7/8 is a likely typo for one host; masking it would trust a /8.
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "203.0.113.7/8")
    assert _is_trusted_network(_request("203.0.113.7")) is False
    assert _is_trusted_network(_request("203.1.2.3")) is False


@pytest.mark.parametrize("listed,caller", [
    ("0.0.0.0/0", "8.8.8.8"),
    ("::/0", "2001:db8::1"),
    # Together these cover all of IPv4; neither entry is a /0 on its own.
    ("0.0.0.0/1,128.0.0.0/1", "8.8.8.8"),
    ("::/1,8000::/1", "2001:db8::1"),
    # A dual-stack bind reports every IPv4 caller from this range.
    ("::ffff:0:0/96", "::ffff:8.8.8.8"),
    # ...and these two halves collapse to it.
    ("::ffff:0:0/97,::ffff:8000:0/97", "::ffff:8.8.8.8"),
    # One half as IPv4-mapped IPv6, the other as IPv4: every IPv4 caller.
    ("::ffff:0:0/97,128.0.0.0/1", "::ffff:200.1.1.1"),
    ("0.0.0.0/1,::ffff:128.0.0.0/97", "::ffff:200.1.1.1"),
    # Everything outside the built-in 10.0.0.0/8: with it, every IPv4 caller.
    (",".join(str(n) for n in ipaddress.ip_network("0.0.0.0/0")
              .address_exclude(ipaddress.ip_network("10.0.0.0/8"))), "8.8.8.8"),
])
def test_a_catch_all_is_honoured_but_logged(monkeypatch, caplog, listed, caller):
    import logging

    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", listed)
    with caplog.at_level(logging.WARNING):
        assert _is_trusted_network(_request(caller)) is True
    assert "trusts every caller" in caplog.text


def test_an_ordinary_range_is_not_logged_as_a_catch_all(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "100.64.0.0/10,fd7a:115c:a1e0::/48")
    with caplog.at_level(logging.WARNING):
        assert _is_trusted_network(_request("100.101.102.103")) is True
    assert "trusts every caller" not in caplog.text


@pytest.mark.parametrize("listed,caller,trusted", [
    # A dual-stack bind reports an IPv4 peer in its IPv4-mapped form.
    ("100.64.0.0/10", "::ffff:100.101.102.103", True),
    ("", "::ffff:127.0.0.1", True),
    ("", "::ffff:192.168.1.5", True),
    ("", "::ffff:8.8.8.8", False),
    ("100.64.0.0/10", "::ffff:8.8.8.8", False),
    # An IPv6 range still matches the IPv6 address itself.
    ("fd7a:115c:a1e0::/48", "fd7a:115c:a1e0::1", True),
])
def test_an_ipv4_mapped_peer_matches_the_ipv4_networks(monkeypatch, listed, caller, trusted):
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", listed)
    assert _is_trusted_network(_request(caller)) is trusted


def test_the_setting_is_parsed_and_logged_once_per_value(monkeypatch, caplog):
    # Every REST and WebSocket request checks trust; a catch-all must not log
    # a warning on each one.
    import logging

    from src.http_routes import access

    monkeypatch.setattr(access, "_extra_networks_cache", ("", ()))
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "0.0.0.0/0")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert _is_trusted_network(_request("8.8.8.8")) is True
    assert sum("trusts every caller" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("proxy_peer", [
    "127.0.0.1", "127.0.0.2", "127.255.255.254", "::1", "::1%lo",
    "::ffff:127.0.0.1", "::ffff:127.0.0.2", "::ffff:127.255.255.254"])
def test_a_same_host_proxy_passes_on_its_callers_address_not_loopback(proxy_peer):
    # A reverse proxy on this host connects from a loopback address, in
    # whichever form the bind reports it. uvicorn must apply its
    # X-Forwarded-For for each form the trusted-network check treats as
    # loopback, or the proxy's public callers would ride its loopback trust.
    import asyncio

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from src.services.mcp_transport_service import FORWARDED_ALLOW_IPS

    seen = {}

    async def app(scope, receive, send):
        seen["trusted"] = _is_trusted_network(Request(scope))

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=FORWARDED_ALLOW_IPS)
    scope = {"type": "http", "scheme": "http", "client": (proxy_peer, 1),
             "headers": [(b"x-forwarded-for", b"8.8.8.8")]}
    asyncio.run(middleware(scope, None, None))
    assert seen["trusted"] is False
