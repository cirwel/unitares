"""Wave 3 §3.2 transport-side 503 machinery — §14 prereq PR #10.

Covers src/mcp_transport.py: the pinned typed-unavailable body, the
numerator-emission middleware, the sliding-window aggregator math, and the
default-inert flag gating. Spec: docs/proposals/beam-wave-3-handler-dispatch.md
§3.2 step 3.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.coordination_events import (
    COORDINATION_FAILURE_GOVERNANCE_MCP_CUTOVER_503_RATE_BREACH,
    MEASUREMENT_GOVERNANCE_MCP_503_EMISSION,
    MEASUREMENT_GOVERNANCE_MCP_REQUEST,
)
from src.mcp_transport import (
    AGGREGATOR_THRESHOLD,
    AGGREGATOR_WINDOW_SECONDS,
    Transport503EmissionMiddleware,
    UNAVAILABLE_ERROR,
    UNAVAILABLE_REASON_HANDLER_DISPATCH,
    UNAVAILABLE_RETRY_AFTER_SECONDS,
    _sniff_error_reason,
    aggregator_enabled,
    check_503_breach_once,
    cutover_503_aggregator_task,
    make_unavailable_body,
)


# --- §3.2 typed-unavailable body contract ---


class TestUnavailableBody:
    def test_pinned_shape(self):
        body = make_unavailable_body()
        assert body == {
            "ok": False,
            "error": "governance_temporarily_unavailable",
            "reason": "handler_dispatch_unavailable",
            "retry_after_seconds": 5,
        }

    def test_pinned_constants(self):
        # Clients key on these literals — renaming is a breaking contract change.
        assert UNAVAILABLE_ERROR == "governance_temporarily_unavailable"
        assert UNAVAILABLE_REASON_HANDLER_DISPATCH == "handler_dispatch_unavailable"
        assert UNAVAILABLE_RETRY_AFTER_SECONDS == 5

    def test_custom_reason(self):
        body = make_unavailable_body(reason="writer_lock_timeout", retry_after_seconds=2)
        assert body["reason"] == "writer_lock_timeout"
        assert body["retry_after_seconds"] == 2
        assert body["error"] == UNAVAILABLE_ERROR


# --- error_reason sniffing ---


class TestSniffErrorReason:
    def test_typed_unavailable_body(self):
        raw = json.dumps(make_unavailable_body()).encode()
        assert _sniff_error_reason(raw) == "handler_dispatch_unavailable"

    def test_warming_up_body(self):
        # The readiness probe's pre-existing 503 shape.
        assert _sniff_error_reason(b'{"status": "warming_up"}') == "warming_up"

    def test_error_key_fallback(self):
        assert _sniff_error_reason(b'{"error": "tracemalloc not enabled"}') == (
            "tracemalloc not enabled"
        )

    @pytest.mark.parametrize("body", [None, b"", b"not json", b"[1,2]", b'{"x": 1}'])
    def test_unknown_fallbacks(self, body):
        assert _sniff_error_reason(body) == "unknown"


# --- Numerator middleware ---


def _make_asgi_app(status: int, body: bytes):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": body})

    return app


def _http_scope(path: str = "/v1/tools/call") -> dict:
    return {"type": "http", "path": path, "method": "POST", "headers": []}


async def _drive(middleware, scope) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


class TestTransport503EmissionMiddleware:
    @pytest.mark.asyncio
    async def test_emits_on_503(self):
        app = _make_asgi_app(503, json.dumps(make_unavailable_body()).encode())
        middleware = Transport503EmissionMiddleware(app)
        with patch("src.mcp_transport.spawn_503_measurement") as spawn:
            sent = await _drive(middleware, _http_scope())
        spawn.assert_called_once_with(
            "/v1/tools/call", "handler_dispatch_unavailable"
        )
        # Response passes through untouched.
        assert sent[0]["status"] == 503

    @pytest.mark.asyncio
    async def test_no_emit_on_200(self):
        app = _make_asgi_app(200, b'{"ok": true}')
        middleware = Transport503EmissionMiddleware(app)
        with patch("src.mcp_transport.spawn_503_measurement") as spawn:
            await _drive(middleware, _http_scope())
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_once_for_multi_chunk_body(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send(
                {"type": "http.response.body", "body": b'{"reason": "x"}', "more_body": True}
            )
            await send({"type": "http.response.body", "body": b"tail"})

        middleware = Transport503EmissionMiddleware(app)
        with patch("src.mcp_transport.spawn_503_measurement") as spawn:
            await _drive(middleware, _http_scope())
        assert spawn.call_count == 1

    @pytest.mark.asyncio
    async def test_non_http_scope_passthrough(self):
        called = {}

        async def app(scope, receive, send):
            called["yes"] = True

        middleware = Transport503EmissionMiddleware(app)
        with patch("src.mcp_transport.spawn_503_measurement") as spawn:
            await middleware({"type": "websocket"}, None, None)
        assert called == {"yes": True}
        spawn.assert_not_called()


# --- Aggregator ---


def _fake_db(count_503: int, count_request: int):
    """DB stub whose acquire() yields a conn returning the given counts."""

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "count_503": count_503,
        "count_request": count_request,
    }

    class FakeDB:
        _pool = object()

        @asynccontextmanager
        async def acquire(self):
            yield conn

    return FakeDB()


class TestCheck503BreachOnce:
    @pytest.mark.asyncio
    async def test_zero_denominator_never_breaches(self):
        with patch("src.coordination_events.emit_event", new=AsyncMock()) as emit:
            result = await check_503_breach_once(_fake_db(50, 0))
        assert result is None
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rate_at_threshold_does_not_breach(self):
        # Exactly 1% — §3.2 says "exceeds 1%", so equality stays quiet.
        with patch("src.coordination_events.emit_event", new=AsyncMock()) as emit:
            result = await check_503_breach_once(_fake_db(1, 100))
        assert result is None
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_breach_emits_pinned_payload(self):
        emit = AsyncMock()
        with patch("src.coordination_events.emit_event", new=emit):
            result = await check_503_breach_once(_fake_db(3, 100))
        assert result == {
            "window_seconds": AGGREGATOR_WINDOW_SECONDS,
            "rate": 0.03,
            "threshold": AGGREGATOR_THRESHOLD,
            "count_503": 3,
            "count_request": 100,
        }
        emit.assert_awaited_once()
        kwargs = emit.await_args.kwargs
        assert kwargs["service"] == "governance_mcp"
        assert (
            kwargs["event_type"]
            == COORDINATION_FAILURE_GOVERNANCE_MCP_CUTOVER_503_RATE_BREACH
        )
        assert kwargs["payload"] == result

    @pytest.mark.asyncio
    async def test_breach_survives_emit_failure(self):
        # Breach detection must outlive a broken emit channel.
        emit = AsyncMock(side_effect=RuntimeError("pg down"))
        with patch("src.coordination_events.emit_event", new=emit):
            result = await check_503_breach_once(_fake_db(10, 100))
        assert result is not None
        assert result["rate"] == 0.1

    @pytest.mark.asyncio
    async def test_window_query_uses_both_types(self):
        db = _fake_db(0, 10)
        async with db.acquire() as conn:
            pass  # grab the shared conn mock
        with patch("src.coordination_events.emit_event", new=AsyncMock()):
            await check_503_breach_once(db)
        args = conn.fetchrow.await_args.args
        assert MEASUREMENT_GOVERNANCE_MCP_503_EMISSION in args
        assert MEASUREMENT_GOVERNANCE_MCP_REQUEST in args
        assert float(AGGREGATOR_WINDOW_SECONDS) in args


class TestAggregatorGating:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("WAVE3_CUTOVER_503_AGGREGATOR", raising=False)
        assert aggregator_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_enabled_values(self, monkeypatch, value):
        monkeypatch.setenv("WAVE3_CUTOVER_503_AGGREGATOR", value)
        assert aggregator_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "", "off"])
    def test_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv("WAVE3_CUTOVER_503_AGGREGATOR", value)
        assert aggregator_enabled() is False

    @pytest.mark.asyncio
    async def test_task_is_inert_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("WAVE3_CUTOVER_503_AGGREGATOR", raising=False)
        # Returns immediately — no DB access, no loop.
        await asyncio.wait_for(cutover_503_aggregator_task(), timeout=1.0)
