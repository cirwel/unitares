"""The REST trusted-network bypass trusts only what every install shares.

Loopback and the RFC1918 private ranges are built in. Overlay and VPN ranges
are not: 100.64.0.0/10, which Tailscale assigns from and some ISPs use for
carrier-grade NAT, used to be, so every install trusted one operator's tailnet
layout. An operator on such a network now lists it in UNITARES_TRUSTED_NETWORKS.
"""

from __future__ import annotations

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


def test_a_catch_all_is_honoured_but_logged(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "0.0.0.0/0")
    with caplog.at_level(logging.WARNING):
        assert _is_trusted_network(_request("8.8.8.8")) is True
    assert "trusts every caller" in caplog.text
