"""Read-only keys (UNITARES_READ_ONLY_TOKENS).

Operator decision 2026-10-27 (KG 2026-10-27T19:26:49.785412): the operator
hub reads the timeline with a key of its own that can only read. Only
GET /v1/timeline and the /ws/eisv stream accept one; every other route
refuses it as an unknown token. Unset, nothing changes.
"""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_routes import access
from src.http_routes.access import WS_BEARER_SUBPROTOCOL

READ_KEY = "r" * 40
API_TOKEN = "a" * 40


def _digest(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("UNITARES_MCP_BEARER_TOKENS", "UNITARES_REST_STRICT", "UNITARES_TRUSTED_NETWORKS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(access.READ_ONLY_TOKENS_ENV, _digest(READ_KEY))


def _request(token=None, ip="8.8.8.8"):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=ip),
        scope={"headers": [], "client": (ip, 1)},
        state=SimpleNamespace(),
    )


def _websocket(token=None, via="header", ip="8.8.8.8"):
    headers, subprotocols = {}, []
    if token and via == "header":
        headers["authorization"] = f"Bearer {token}"
    if token and via == "subprotocol":
        b64 = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
        subprotocols = [WS_BEARER_SUBPROTOCOL, f"{WS_BEARER_SUBPROTOCOL}.{b64}"]
    return SimpleNamespace(
        headers=headers,
        query_params={},
        client=SimpleNamespace(host=ip),
        scope={"subprotocols": subprotocols, "headers": [], "client": (ip, 1)},
        state=SimpleNamespace(),
    )


def test_a_read_only_key_reads_but_is_refused_by_the_general_gate():
    assert access._check_read_auth(_request(READ_KEY), http_api_token=API_TOKEN) is True
    assert access._check_http_auth(_request(READ_KEY), http_api_token=API_TOKEN) is False


@pytest.mark.parametrize("via", ["header", "subprotocol"])
def test_the_stream_accepts_a_read_only_key_and_the_general_gate_does_not(via):
    assert access._check_ws_read_auth(_websocket(READ_KEY, via), http_api_token=API_TOKEN) is True
    assert access._check_ws_auth(_websocket(READ_KEY, via), http_api_token=API_TOKEN) is False


def test_strict_posture_accepts_it_on_read_routes_only(monkeypatch):
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "m" * 40)
    assert access._check_read_auth(_request(READ_KEY), http_api_token=API_TOKEN) is True
    assert access._check_http_auth(_request(READ_KEY), http_api_token=API_TOKEN) is False
    assert access._check_ws_read_auth(_websocket(READ_KEY), http_api_token=API_TOKEN) is True


def test_unset_means_no_read_only_keys(monkeypatch):
    monkeypatch.delenv(access.READ_ONLY_TOKENS_ENV)
    assert access._check_read_auth(_request(READ_KEY), http_api_token=API_TOKEN) is False
    assert access._check_ws_read_auth(_websocket(READ_KEY), http_api_token=API_TOKEN) is False


def test_several_digests_work(monkeypatch):
    other = "o" * 32
    monkeypatch.setenv(access.READ_ONLY_TOKENS_ENV, f" {_digest(READ_KEY)} ,{_digest(other)}")
    assert len(access.read_only_token_digests()) == 2
    assert access._check_read_auth(_request(other), http_api_token=API_TOKEN) is True


@pytest.mark.parametrize("entry", [
    READ_KEY,                                  # a raw key: the server must hold no secret
    "sha256:" + "A" * 64,                      # upper-case hex
    "sha256:" + "a" * 63,                      # short digest
    "md5:" + "a" * 32,
])
def test_a_malformed_entry_disables_every_read_only_key(monkeypatch, entry, caplog):
    monkeypatch.setenv(access.READ_ONLY_TOKENS_ENV, f"{_digest(READ_KEY)},{entry}")
    assert access.read_only_token_digests() == []
    assert access._check_read_auth(_request(READ_KEY), http_api_token=API_TOKEN) is False
    assert "every read-only key is disabled" in caplog.text


def test_a_short_presented_key_is_refused_even_if_its_digest_is_listed(monkeypatch):
    short = "s" * 31
    monkeypatch.setenv(access.READ_ONLY_TOKENS_ENV, _digest(short))
    assert access._check_read_auth(_request(short), http_api_token=API_TOKEN) is False


@pytest.mark.parametrize("token", [None, "r" * 39, "x" * 40])
def test_anything_else_is_refused(token):
    assert access._check_read_auth(_request(token), http_api_token=API_TOKEN) is False


def test_read_routes_still_accept_what_they_accepted_before():
    assert access._check_read_auth(_request(API_TOKEN), http_api_token=API_TOKEN) is True
    assert access._check_read_auth(_request(None, ip="127.0.0.1"), http_api_token=API_TOKEN) is True


def test_the_timeline_route_reads_with_it_and_another_route_refuses_it():
    from src.http_api import http_residents
    from src.http_routes.timeline import http_timeline
    from tests.test_http_timeline import _mock_db

    app = Starlette(routes=[
        Route("/v1/timeline", http_timeline, methods=["GET"]),
        Route("/v1/residents", http_residents, methods=["GET"]),
    ])
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {READ_KEY}"}
    with patch("src.db.get_db", return_value=_mock_db()[0]):
        assert client.get("/v1/timeline", headers=headers).status_code == 200
    assert client.get("/v1/residents", headers=headers).status_code == 401
    assert client.get("/v1/timeline").status_code == 401


def test_the_stream_handler_uses_the_read_gate():
    import inspect

    from src.http_routes.telemetry import websocket_eisv_stream

    source = inspect.getsource(websocket_eisv_stream)
    assert "_check_ws_read_auth" in source
