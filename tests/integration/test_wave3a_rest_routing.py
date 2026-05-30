"""
Wave 3a REST dispatch-path routing integration tests.

Spec: ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §5
(Wave 3a cutover sequence) + architect FIND-A4 (dispatch-path question).

Cutover-discovered gap: PR #539 wired the per-tool routing table into the
MCP-protocol wrapper at ``src/mcp_server.py::get_tool_wrapper``, but the
REST entry point ``src/services/http_tool_service.py::execute_http_tool``
short-circuits five core tools through ``_DIRECT_HTTP_TOOL_HANDLERS`` before
MCP dispatch ever runs. Without the routing check inside ``execute_http_tool``,
``WAVE_3A_HEALTH_CHECK_ON_BEAM=true`` only routes MCP-protocol calls; REST
callers (curl, loadgen scripts, simple HTTP clients) still hit the Python
handler. This test suite closes that gap.

Coverage (3 cases — mirroring the wrapper-level tests in
``test_wave_3a_routing_table.py`` with REST as the call surface):

    1. REST routing-table-hit success — REST call to a routed tool → BEAM
       response returned (unwrapped envelope payload), direct handler NOT
       touched, no fallback event.
    2. REST routing-table-hit fallback to Python — BEAM unreachable → REST
       falls back to direct handler, ``coordination_failure.wave_3a.fallback``
       event emitted.
    3. REST routing-table-miss passthrough — non-routed tool → direct
       handler fires unchanged, BEAM proxy NOT invoked.

Test surface (mirrors ``test_wave_3a_routing_table.py``): a uvicorn-driven
Starlette stub on a free port plays the BEAM listener role. The REST surface
is the real ``execute_http_tool``. Event emissions are observed via a mock
of ``src.wave3a_beam_proxy._spawn_emit`` so the tests do not require a live
audit database.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import pytest_asyncio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# BEAM stub fixture — pins the §2.2 envelope shape (mirrors test_wave_3a_routing_table.py)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _beam_stub_handler(request: Request) -> Response:
    """Return a valid §2.2 success envelope echoing the arguments.

    The handler payload is flat (top-level keys) per the §2.2 invariant —
    no ``data`` nesting. ``served_by`` distinguishes BEAM hits from the
    Python direct-handler path in test assertions.
    """
    body = await request.body()
    try:
        parsed = json.loads(body) if body else {}
    except Exception:
        parsed = {}
    return JSONResponse(
        {
            "ok": True,
            "protocol_version": "wave3a.v1",
            "served_by": "beam_stub",
            "status": "healthy",
            "echo": parsed,
        },
        status_code=200,
    )


class _StubServer:
    """uvicorn-driven Starlette stub server, bound to 127.0.0.1:<port>."""

    def __init__(self, port: int) -> None:
        self.port = port
        app = Starlette(routes=[Route("/", _beam_stub_handler, methods=["POST"])])
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", lifespan="off"
        )
        self.server = uvicorn.Server(config)
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    async def start(self) -> None:
        self._task = asyncio.create_task(self.server.serve())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("stub server failed to start within 5s")

    async def stop(self) -> None:
        self.server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except BaseException:
                    pass


@pytest_asyncio.fixture
async def stub_server():
    port = _free_port()
    server = _StubServer(port)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Event-capture fixture (mirrors test_wave_3a_routing_table.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Dict[str, Any]]]:
    """Capture ``_spawn_emit`` calls from the proxy module."""
    captured: List[Tuple[str, Dict[str, Any]]] = []

    from src import wave3a_beam_proxy

    def _capture(event_type: str, payload: Dict[str, Any]) -> None:
        captured.append((event_type, dict(payload)))

    monkeypatch.setattr(wave3a_beam_proxy, "_spawn_emit", _capture)
    return captured


# ---------------------------------------------------------------------------
# Suppress the proxy's success-measurement write (no live DB in unit env)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def suppress_measurement_writes(monkeypatch: pytest.MonkeyPatch):
    """The proxy's FIND-A5 success-row write touches the audit DB. These
    tests don't need it (the wrapper-level suite already covers it); stub
    the spawn so test runs don't depend on ``governance_test``."""
    from src import wave3a_beam_proxy

    def _noop(**_kwargs):
        pass

    monkeypatch.setattr(
        wave3a_beam_proxy, "_spawn_success_measurement", _noop
    )


# ---------------------------------------------------------------------------
# Clean routing-table fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_routing_table():
    from src import wave3a_routing

    wave3a_routing.clear_routes()
    try:
        yield
    finally:
        wave3a_routing.clear_routes()


# ---------------------------------------------------------------------------
# Tool-usage recorder stub — keeps tests independent of telemetry plumbing
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_tool_usage(monkeypatch: pytest.MonkeyPatch):
    """``record_tool_usage`` writes JSONL + audit row. The REST path always
    calls it; tests only care about routing behavior, so stub it out."""
    from src.services import http_tool_service

    def _noop(**_kwargs):
        pass

    monkeypatch.setattr(http_tool_service, "record_tool_usage", _noop)


# ---------------------------------------------------------------------------
# Test 1 — REST routing-table-hit success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_routing_hit_returns_unwrapped_beam_response(
    stub_server, captured_events, monkeypatch: pytest.MonkeyPatch
):
    """Routed tool over REST → BEAM response (unwrapped), direct handler untouched.

    Asserts:
    - Result carries ``served_by=beam_stub`` (BEAM was hit).
    - Envelope transport keys (``ok``, ``protocol_version``) are stripped
      so REST callers see handler-shape output (parity with the Python
      direct handler).
    - The direct handler for ``health_check`` was NOT invoked.
    - No §4.2 fallback events emitted on the success path.
    """
    from src import wave3a_routing
    from src.services import http_tool_service

    direct_handler_calls: List[Dict[str, Any]] = []

    async def fake_direct(arguments: Dict[str, Any]) -> Any:  # pragma: no cover
        direct_handler_calls.append(dict(arguments or {}))
        raise AssertionError(
            "direct handler invoked on routing-table-hit success path"
        )

    monkeypatch.setitem(
        http_tool_service._DIRECT_HTTP_TOOL_HANDLERS,
        "health_check",
        fake_direct,
    )
    wave3a_routing.set_route("health_check", stub_server.url)

    result = await http_tool_service.execute_http_tool(
        "health_check", {"lite": True}
    )

    assert direct_handler_calls == []
    assert isinstance(result, dict)
    # Envelope transport keys stripped by _unwrap_wave3a_envelope_for_http.
    assert "ok" not in result
    assert "protocol_version" not in result
    # Handler payload preserved.
    assert result["served_by"] == "beam_stub"
    assert result["status"] == "healthy"
    assert result["echo"] == {
        "tool_name": "health_check",
        "arguments": {"lite": True},
    }
    # Success path emits no events.
    assert captured_events == []


# ---------------------------------------------------------------------------
# Test 2 — REST routing-table-hit fallback to Python on BEAM failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_routing_hit_falls_back_to_direct_handler_on_beam_unreachable(
    captured_events, monkeypatch: pytest.MonkeyPatch
):
    """BEAM stub unreachable → REST falls back to the direct handler.

    Routing-table row points at a port nothing is listening on; the proxy
    returns ``ok=False`` with ``fallback_reason='connect_error'`` and emits
    ``coordination_failure.wave_3a.fallback``. ``execute_http_tool`` MUST
    then run the direct handler — same fallback semantics as the MCP wrapper.
    """
    from src import wave3a_routing
    from src.services import http_tool_service

    direct_handler_calls: List[Dict[str, Any]] = []

    async def fake_direct(arguments: Dict[str, Any]) -> Any:
        direct_handler_calls.append(dict(arguments or {}))
        return {"served_by": "python_direct", "status": "healthy"}

    monkeypatch.setitem(
        http_tool_service._DIRECT_HTTP_TOOL_HANDLERS,
        "health_check",
        fake_direct,
    )

    # Point at a closed port. ``_free_port`` finds an unbound port; we
    # don't start a stub there, so the connect will fail immediately.
    unreachable_port = _free_port()
    wave3a_routing.set_route(
        "health_check", f"http://127.0.0.1:{unreachable_port}/"
    )

    result = await http_tool_service.execute_http_tool(
        "health_check", {"lite": True}
    )

    # Direct handler ran exactly once with the original arguments.
    assert direct_handler_calls == [{"lite": True}]
    assert isinstance(result, dict)
    assert result["served_by"] == "python_direct"
    assert result["status"] == "healthy"

    # The proxy emitted a fallback event with trigger=connect_error.
    event_types = [et for et, _ in captured_events]
    assert "coordination_failure.wave_3a.fallback" in event_types
    fallback_payload = next(
        p for et, p in captured_events
        if et == "coordination_failure.wave_3a.fallback"
    )
    assert fallback_payload["tool_name"] == "health_check"
    assert fallback_payload["trigger"] == "connect_error"


# ---------------------------------------------------------------------------
# Test 3 — REST routing-table-miss passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_routing_miss_uses_direct_handler_unchanged(
    captured_events, monkeypatch: pytest.MonkeyPatch
):
    """Non-routed tool → direct handler fires unchanged; BEAM proxy NOT invoked.

    Confirms the fix preserves the existing performance optimization for
    the four direct-handler tools that are NOT in the routing table.
    """
    from src import wave3a_routing
    from src.services import http_tool_service

    assert wave3a_routing.get_route("health_check") is None
    assert wave3a_routing.route_count() == 0

    direct_handler_calls: List[Dict[str, Any]] = []
    proxy_calls: List[str] = []

    async def fake_direct(arguments: Dict[str, Any]) -> Any:
        direct_handler_calls.append(dict(arguments or {}))
        return {"served_by": "python_direct", "status": "healthy"}

    async def fake_proxy(**kwargs):  # pragma: no cover — must not fire
        proxy_calls.append(kwargs.get("tool_name", ""))
        raise AssertionError(
            "proxy_to_beam invoked on routing-table-miss path"
        )

    monkeypatch.setitem(
        http_tool_service._DIRECT_HTTP_TOOL_HANDLERS,
        "health_check",
        fake_direct,
    )
    monkeypatch.setattr(http_tool_service, "proxy_to_beam", fake_proxy)

    result = await http_tool_service.execute_http_tool(
        "health_check", {"lite": True}
    )

    # Direct handler fired exactly once; BEAM proxy did NOT fire; no events.
    assert direct_handler_calls == [{"lite": True}]
    assert proxy_calls == []
    assert captured_events == []
    assert result == {"served_by": "python_direct", "status": "healthy"}
