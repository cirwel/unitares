"""Every JSON REST route counts the streamed body against a byte cap.

A chunked request carries no Content-Length, so the only boundary that holds
is one that counts the chunks as they arrive. These tests feed each route a
body larger than its cap and assert a 413 before the stream is drained.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import dashboard_auth
from src.http_routes import access, effects, findings, lease_identity, metrics_api
from src.http_routes import substrate
from src.mcp_handlers import wave3a_admin
from src.services import http_request_parser as parser

_CHUNK = b"x" * 16 * 1024


class _ChunkedRequest:
    """A request whose body streams without a Content-Length."""

    def __init__(self, total_chunks: int):
        self._total = total_chunks
        self.chunks_yielded = 0
        self.headers = {"origin": dashboard_auth.DASHBOARD_EXPECTED_ORIGIN}
        self.cookies = {}
        self.query_params = {}
        self.method = "POST"
        self.client = SimpleNamespace(host="127.0.0.1")
        self.state = SimpleNamespace()

    async def stream(self):
        for _ in range(self._total):
            self.chunks_yielded += 1
            yield _CHUNK

    async def json(self):  # pragma: no cover - must never be reached
        raise AssertionError("route buffered the whole body with request.json()")


def _chunks_over(limit: int) -> int:
    return limit // len(_CHUNK) + 8


@pytest.fixture
def open_gates(monkeypatch):
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    monkeypatch.setenv("UNITARES_GOVERNED_EFFECT_BINDING", "1")
    monkeypatch.setattr(effects, "_binding_mint_enabled", lambda: True, raising=False)
    monkeypatch.setattr(wave3a_admin, "_is_operator", lambda request: True)
    monkeypatch.setattr(
        substrate, "dashboard_session_write_authorized", lambda request: True
    )
    monkeypatch.setattr(dashboard_auth, "_passkeys_unconfigured", lambda: None)
    monkeypatch.setattr(
        dashboard_auth, "_consume_challenge", AsyncMock(return_value=b"c" * 32)
    )
    monkeypatch.setattr(
        dashboard_auth,
        "_registration_authorized",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(dashboard_auth, "_session_gate", lambda request: None)
    monkeypatch.setattr(
        dashboard_auth, "dashboard_session_is_fresh", lambda request: True
    )
    monkeypatch.setattr(
        dashboard_auth, "hub_config", lambda: ({"origin": "https://hub.test"}, None)
    )
    monkeypatch.setattr(
        dashboard_auth, "_hub_unconfigured", lambda config, problem: None
    )
    monkeypatch.setattr(
        dashboard_auth, "_hub_internal_gate", lambda request, config: None
    )


_SMALL = parser.MAX_HTTP_SMALL_JSON_BODY
_INGEST = parser.MAX_HTTP_INGEST_JSON_BODY

_ROUTES = [
    (dashboard_auth.http_webauthn_verify, _SMALL),
    (dashboard_auth.http_webauthn_register_verify, _SMALL),
    (dashboard_auth.http_hub_authorize, _SMALL),
    (dashboard_auth.http_hub_exchange, _SMALL),
    (effects.http_effect_grant, _SMALL),
    (effects.http_effect_veto, _SMALL),
    (lease_identity.http_verify_lease_holder, _SMALL),
    (lease_identity.http_attest_lease_holder, _SMALL),
    (wave3a_admin._set_route, _SMALL),
    (findings.http_record_finding, _INGEST),
    (metrics_api.http_post_metric, _INGEST),
    (substrate.http_record_bridge_event, _INGEST),
    (substrate.http_substrate_observe, _INGEST),
    (substrate.http_runtime_observe, _INGEST),
    (substrate.http_harness_outcome, _INGEST),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, limit", _ROUTES, ids=[h.__name__ for h, _ in _ROUTES]
)
async def test_chunked_body_over_cap_is_refused_before_full_read(
    open_gates, handler, limit
):
    total = _chunks_over(limit)
    request = _ChunkedRequest(total)

    response = await handler(request)

    assert response.status_code == 413
    assert request.chunks_yielded < total
    assert request.chunks_yielded * len(_CHUNK) <= limit + len(_CHUNK)


@pytest.mark.asyncio
async def test_explicit_cap_overrides_tool_default():
    request = _ChunkedRequest(3)

    with pytest.raises(parser.HttpToolRequestError) as exc_info:
        await parser.read_bounded_json_body(request, 2 * len(_CHUNK))

    assert exc_info.value.status_code == 413
    assert exc_info.value.payload["max_bytes"] == 2 * len(_CHUNK)
    assert "max_size_mb" not in exc_info.value.payload
    assert request.chunks_yielded == 3


def test_declared_length_checked_against_explicit_cap():
    with pytest.raises(parser.HttpToolRequestError) as exc_info:
        parser.validate_http_tool_content_length("1025", 1024)
    assert exc_info.value.status_code == 413
    assert parser.validate_http_tool_content_length("1024", 1024) == 1024


def test_tool_route_payload_keeps_megabyte_field():
    with pytest.raises(parser.HttpToolRequestError) as exc_info:
        parser.validate_http_tool_content_length(
            str(parser.MAX_HTTP_TOOL_REQUEST_SIZE + 1)
        )
    assert exc_info.value.payload["max_size_mb"] == 10


# --- raw ASGI bodies are held and replayed in time linear in their bytes -----


def _asgi_receive(messages):
    queue = list(messages)
    reads = {"n": 0}

    async def receive():
        reads["n"] += 1
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    return receive, reads


@pytest.mark.asyncio
async def test_asgi_body_in_many_messages_replays_as_one():
    count = 5000
    messages = [
        {"type": "http.request", "body": b"x", "more_body": i < count - 1}
        for i in range(count)
    ]
    receive, _ = _asgi_receive(messages)

    replay = await parser.read_bounded_asgi_body({"headers": []}, receive)

    first = await replay()
    assert first == {"type": "http.request", "body": b"x" * count, "more_body": False}
    # Anything after the body comes from the live receive.
    assert (await replay())["type"] == "http.disconnect"


@pytest.mark.asyncio
async def test_asgi_disconnect_mid_body_replays_the_partial_body_then_disconnect():
    messages = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": True},
        {"type": "http.disconnect"},
    ]
    receive, reads = _asgi_receive(messages)

    replay = await parser.read_bounded_asgi_body({"headers": []}, receive)

    assert await replay() == {"type": "http.request", "body": b"abcd", "more_body": True}
    assert await replay() == {"type": "http.disconnect"}
    assert reads["n"] == 3


@pytest.mark.asyncio
async def test_asgi_disconnect_before_any_body_is_replayed_alone():
    receive, _ = _asgi_receive([{"type": "http.disconnect"}])
    replay = await parser.read_bounded_asgi_body({"headers": []}, receive)
    assert await replay() == {"type": "http.disconnect"}
