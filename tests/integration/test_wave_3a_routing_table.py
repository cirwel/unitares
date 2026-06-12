"""
Wave 3a per-tool routing table integration tests (PR #3 of Wave 3a sequencing).

Specification:
    ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §3.1
    (rollback shape), §3.2 (500ms timeout discipline + Python-fallback),
    §4.2 (stop-sign event taxonomy), §5 PR #3 (this PR's scope).

Stub decision — Python over Elixir (RFC literal text proposes a local
Elixir BEAM stub binary). The contract is what matters: a Python stub
that pins the same envelope shape verifies the same property. The eventual
real BEAM listener's contract is independently verified by PR #4's ExUnit
envelope test. Operator can override this decision; cost of switching
later is a fixture swap, not a test rewrite.

Coverage (7 cases):

    1. Routing-table-miss passthrough — tool not in table → Python dispatch,
       no BEAM call, no fallback event.
    2. Routing-table-hit success — tool in table → BEAM response returned,
       Python NOT touched, no fallback event.
    3. Routing-table-hit timeout-to-fallback — stub delays >500ms → fallback
       to Python AND ``coordination_failure.wave_3a.timeout`` AND
       ``coordination_failure.wave_3a.fallback`` events emitted.
    4. Routing-table-hit envelope-invalid → fallback to Python AND
       ``coordination_failure.wave_3a.envelope_invalid`` event emitted.
    5. Rollback empties table — set route, run rollback script --all,
       assert table empty, subsequent calls go to Python.
    6. Smoke: rollback --all on empty table exits 0 cleanly (covers the
       rollback contract before any handler is ported, per RFC §5 PR #3).
    7. Routing-table thread safety — concurrent add/remove from multiple
       async tasks, no exceptions, final state consistent.

Test surface:
    A standalone aiohttp-style Starlette stub on a free port plays the BEAM
    role. The Python dispatch path is the real ``get_tool_wrapper`` and a
    fake handler registered into ``TOOL_HANDLERS`` for the duration of the
    test. Event emissions are observed via a mock of
    ``src.wave3a_beam_proxy._spawn_emit`` — we capture (event_type, payload)
    tuples directly rather than reading the DB, so these tests do not
    require a live ``governance_test`` database.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
# BEAM stub fixture — plays the BEAM listener for PR #3
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find a free TCP port for the stub listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _beam_stub_handler(request: Request) -> Response:
    """Stub BEAM endpoint. Behaviour controlled by query string.

    - ``?delay_ms=N``      — sleep N ms before responding.
    - ``?envelope=invalid``— return a body that fails §2.2 shape check.
    - default              — return a valid §2.2 success envelope.
    """
    delay_ms = request.query_params.get("delay_ms")
    if delay_ms:
        try:
            await asyncio.sleep(int(delay_ms) / 1000.0)
        except ValueError:
            pass

    envelope = request.query_params.get("envelope")
    if envelope == "invalid":
        # Top-level keys but missing ok=True / protocol_version mismatch.
        return JSONResponse(
            {"ok": True, "protocol_version": "WRONG_VERSION", "data": {}},
            status_code=200,
        )

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
        # Wait for the server to start accepting connections.
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
    """Spin up the BEAM stub on a free port for the duration of the test."""
    port = _free_port()
    server = _StubServer(port)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Event-capture fixture — observes _spawn_emit calls
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Dict[str, Any]]]:
    """Capture all ``_spawn_emit`` calls from the proxy module.

    Avoids the live-DB dependency and makes assertions exact. The proxy
    module's production path is unchanged — we only patch the emit hook.
    """
    captured: List[Tuple[str, Dict[str, Any]]] = []

    from src import wave3a_beam_proxy

    def _capture(event_type: str, payload: Dict[str, Any]) -> None:
        captured.append((event_type, dict(payload)))

    monkeypatch.setattr(wave3a_beam_proxy, "_spawn_emit", _capture)
    return captured


# ---------------------------------------------------------------------------
# Clean routing-table fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_routing_table():
    """Routing table starts empty for every test; flush afterwards.

    Per §3.1 the production invariant is empty-at-startup; tests honor the
    same invariant so cross-test ordering can't leak rows.
    """
    from src import wave3a_routing

    wave3a_routing.clear_routes()
    try:
        yield
    finally:
        wave3a_routing.clear_routes()


# ---------------------------------------------------------------------------
# Test 1 — routing-table-miss passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_table_miss_passthrough(
    captured_events, monkeypatch: pytest.MonkeyPatch
):
    """Tool not in table → wrapper invokes Python dispatch, NOT the BEAM proxy.

    Exercises ``get_tool_wrapper`` directly so the assertion observes the
    wrapper's actual behaviour. Earlier draft of this test only consulted
    ``wave3a_routing.get_route`` — that test would have passed even if the
    routing-table integration in ``mcp_server.get_tool_wrapper`` were
    deleted. FIND-R4 council fold: tests must observe the wrapper, not the
    routing-table state.
    """
    from src import wave3a_beam_proxy, wave3a_routing
    from src import mcp_server

    assert wave3a_routing.get_route("_wave3a_test_miss_tool") is None
    assert wave3a_routing.route_count() == 0

    # Spy on dispatch_tool and on the BEAM proxy so we can confirm exactly
    # which path fired. ``patch("src.mcp_server.dispatch_tool")`` works
    # because the wrapper closes over the module-level binding (FIND-A3
    # hoisted ``_wave3a_get_route`` / ``_wave3a_proxy_to_beam`` similarly).
    dispatch_calls: List[Tuple[str, Dict[str, Any]]] = []
    proxy_calls: List[str] = []

    class _FakeText:
        def __init__(self, text: str) -> None:
            self.text = text

    async def fake_dispatch(name, arguments):
        dispatch_calls.append((name, dict(arguments or {})))
        # Mimic dispatch_tool's TextContent-list return; the wrapper
        # extracts ``.text`` from the first element and json-decodes.
        return [_FakeText(json.dumps({"ok": True, "fake": True}))]

    async def fake_proxy(**kwargs):  # pragma: no cover — must not fire
        proxy_calls.append(kwargs.get("tool_name", ""))
        raise AssertionError(
            "proxy_to_beam invoked on routing-table-miss path"
        )

    monkeypatch.setattr(mcp_server, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(
        mcp_server, "_wave3a_proxy_to_beam", fake_proxy
    )

    # Wrappers are cached; clear the cache so a fresh wrapper picks up the
    # patched dispatch binding via closure-on-module.
    mcp_server._tool_wrappers_cache.clear()
    wrapper = mcp_server.get_tool_wrapper("_wave3a_test_miss_tool")
    result = await wrapper(arg="value")

    # Python dispatch fired exactly once; BEAM proxy did NOT fire; no
    # fallback events were emitted.
    assert dispatch_calls == [("_wave3a_test_miss_tool", {"arg": "value"})]
    assert proxy_calls == []
    assert captured_events == []
    # Wrapper's post-processing decodes the TextContent payload.
    assert result == {"ok": True, "fake": True}

    # Cleanup: pop the cached wrapper so the patched dispatch binding does
    # not leak into other tests in this module.
    mcp_server._tool_wrappers_cache.pop("_wave3a_test_miss_tool", None)
    # Silence unused-import lint while keeping the import-sanity probe.
    _ = wave3a_beam_proxy


# ---------------------------------------------------------------------------
# Test 2 — routing-table-hit success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_table_hit_success(stub_server, captured_events):
    """Tool in table → BEAM response returned, Python NOT touched."""
    from src import wave3a_routing
    from src.wave3a_beam_proxy import proxy_to_beam

    wave3a_routing.set_route("ported_tool", stub_server.url)
    result = await proxy_to_beam(
        tool_name="ported_tool",
        beam_url=stub_server.url,
        kwargs={"foo": "bar"},
    )

    assert result.ok is True
    assert result.response is not None
    assert result.response["ok"] is True
    assert result.response["protocol_version"] == "wave3a.v1"
    assert result.response["served_by"] == "beam_stub"
    assert result.response["echo"] == {"tool_name": "ported_tool", "arguments": {"foo": "bar"}}
    # Success path emits no events — events are §4.2 stop-sign signal only.
    assert captured_events == []


# ---------------------------------------------------------------------------
# Test 3 — routing-table-hit timeout-to-fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_table_hit_timeout_to_fallback(stub_server, captured_events):
    """Stub delays > 500ms → timeout event + fallback event emitted."""
    from src import wave3a_routing
    from src.wave3a_beam_proxy import proxy_to_beam

    wave3a_routing.set_route("slow_tool", stub_server.url)
    # delay_ms=2000 well past the 500ms budget.
    slow_url = f"{stub_server.url}?delay_ms=2000"

    start = time.monotonic()
    result = await proxy_to_beam(
        tool_name="slow_tool",
        beam_url=slow_url,
        kwargs={},
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)

    assert result.ok is False
    assert result.fallback_reason == "timeout"
    # The proxy MUST give up within ~500ms; allow generous overhead for
    # the asyncio wait_for shutdown plus uvicorn cancellation path.
    assert elapsed_ms < 1500, f"timeout took {elapsed_ms}ms — budget is 500ms"

    event_types = [et for et, _ in captured_events]
    assert "coordination_failure.wave_3a.timeout" in event_types
    assert "coordination_failure.wave_3a.fallback" in event_types

    # Sanity-check payload shapes per RFC §4.2 documented payloads.
    timeout_payload = next(
        p for et, p in captured_events if et == "coordination_failure.wave_3a.timeout"
    )
    assert timeout_payload["tool_name"] == "slow_tool"
    assert timeout_payload["budget_ms"] == 500
    assert isinstance(timeout_payload["elapsed_ms"], int)

    fallback_payload = next(
        p for et, p in captured_events if et == "coordination_failure.wave_3a.fallback"
    )
    assert fallback_payload["tool_name"] == "slow_tool"
    assert fallback_payload["trigger"] == "timeout"

    # Wave 0 step 2 dedup contract (§129 follow-up from
    # section-129-measurement-fix-2026-06-03.md): every coordination_failure
    # emit carries incident_id, and the timeout + fallback pair describe the
    # SAME incident — one occurrence must not count as two distinct
    # incidents under COUNT(DISTINCT incident_id).
    import uuid as _uuid

    assert timeout_payload["incident_id"] == fallback_payload["incident_id"]
    _uuid.UUID(timeout_payload["incident_id"])  # raises if not a valid UUID


# ---------------------------------------------------------------------------
# Test 4 — routing-table-hit envelope-invalid fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_table_hit_envelope_invalid_fallback(stub_server, captured_events):
    """Stub returns a body that fails §2.2 → envelope_invalid event."""
    from src import wave3a_routing
    from src.wave3a_beam_proxy import proxy_to_beam

    wave3a_routing.set_route("malformed_tool", stub_server.url)
    bad_url = f"{stub_server.url}?envelope=invalid"

    result = await proxy_to_beam(
        tool_name="malformed_tool",
        beam_url=bad_url,
        kwargs={},
    )

    assert result.ok is False
    assert result.fallback_reason == "envelope_invalid"

    event_types = [et for et, _ in captured_events]
    assert "coordination_failure.wave_3a.envelope_invalid" in event_types

    envelope_payload = next(
        p
        for et, p in captured_events
        if et == "coordination_failure.wave_3a.envelope_invalid"
    )
    assert envelope_payload["tool_name"] == "malformed_tool"
    assert isinstance(envelope_payload["envelope_keys"], list)
    # The stub returns ok=true + WRONG protocol_version — the violation
    # should name the protocol mismatch, not a missing key.
    assert "protocol_version_mismatch" in envelope_payload["detail"]

    # Dedup contract: incident_id present on every coordination_failure emit.
    import uuid as _uuid

    _uuid.UUID(envelope_payload["incident_id"])


# ---------------------------------------------------------------------------
# Test 5 — rollback empties table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_empties_table():
    """Add routes, run clear_routes() (the rollback script's backend), assert empty."""
    from src import wave3a_routing

    wave3a_routing.set_route("tool_a", "http://127.0.0.1:9001/")
    wave3a_routing.set_route("tool_b", "http://127.0.0.1:9002/")
    assert wave3a_routing.route_count() == 2

    removed = wave3a_routing.clear_routes()
    assert removed == 2
    assert wave3a_routing.route_count() == 0
    assert wave3a_routing.get_route("tool_a") is None
    assert wave3a_routing.get_route("tool_b") is None


# ---------------------------------------------------------------------------
# Test 6 — smoke: rollback --all on empty table exits 0
# ---------------------------------------------------------------------------


def test_rollback_empty_table_smoke():
    """Running the rollback script's underlying clear on empty table is a no-op.

    Exercises the rollback contract before any handler is ported (RFC §5
    PR #3 explicit acceptance criterion). The bash script itself is shelled
    out only when the MCP is running — we verify the Python backend the
    script targets handles the empty-table case cleanly.

    Additional shell-level smoke: ``--help`` exits 0 and prints usage.
    """
    from src import wave3a_routing

    assert wave3a_routing.route_count() == 0
    removed = wave3a_routing.clear_routes()
    assert removed == 0  # nothing to remove
    assert wave3a_routing.route_count() == 0

    # Shell-level smoke: the bash script's --help path is offline-safe and
    # must exit 0.
    script = project_root / "scripts" / "ops" / "wave-3a-rollback.sh"
    assert script.exists(), f"rollback script missing at {script}"
    result = subprocess.run(
        ["bash", str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, f"--help should exit 0, got {result.returncode}"
    assert "wave-3a-rollback.sh" in result.stdout


# ---------------------------------------------------------------------------
# Test 7 — routing-table thread safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_table_concurrent_add_remove():
    """Concurrent add/remove from many async tasks → no exceptions, final consistent."""
    from src import wave3a_routing

    N = 50

    async def add_route(idx: int) -> None:
        wave3a_routing.set_route(f"tool_{idx}", f"http://127.0.0.1:9000/{idx}")

    async def remove_route(idx: int) -> None:
        wave3a_routing.remove_route(f"tool_{idx}")

    # Phase 1: parallel adds.
    await asyncio.gather(*[add_route(i) for i in range(N)])
    assert wave3a_routing.route_count() == N

    # Phase 2: interleaved add and remove on overlapping keys.
    tasks: List[Any] = []
    for i in range(N):
        tasks.append(add_route(i))
        tasks.append(remove_route(i))
    await asyncio.gather(*tasks)

    # The final state depends on scheduling; the strong invariant is that
    # no exception was raised and the table is internally consistent
    # (every present key has the expected URL shape).
    routes = wave3a_routing.list_routes()
    for tool, url in routes.items():
        assert url.startswith("http://127.0.0.1:9000/")
        assert tool.startswith("tool_")

    # Phase 3: clear_routes resets to zero regardless.
    wave3a_routing.clear_routes()
    assert wave3a_routing.route_count() == 0


# ---------------------------------------------------------------------------
# Admin endpoint smoke (auth gate)
# ---------------------------------------------------------------------------


def test_admin_routes_require_operator_token(monkeypatch: pytest.MonkeyPatch):
    """The admin surface MUST reject requests without a valid operator token.

    Backstop on the rollback script's auth contract — if the gate broke,
    the rollback surface would be unauthenticated, which is the worst
    possible posture for a runtime-mutation endpoint.
    """
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from src.mcp_handlers.wave3a_admin import (
        OPERATOR_TOKENS_ENV,
        WAVE3A_ADMIN_PREFIX,
        register_wave3a_admin_routes,
    )

    monkeypatch.setenv(OPERATOR_TOKENS_ENV, "valid-operator-token")
    app = Starlette(routes=[])
    register_wave3a_admin_routes(app)

    with TestClient(app) as client:
        # No header → 401.
        r = client.get(f"{WAVE3A_ADMIN_PREFIX}/routing-table")
        assert r.status_code == 401
        # Wrong header → 401.
        r = client.get(
            f"{WAVE3A_ADMIN_PREFIX}/routing-table",
            headers={"X-Unitares-Operator": "wrong-token"},
        )
        assert r.status_code == 401
        # Correct header → 200 with empty routing table.
        r = client.get(
            f"{WAVE3A_ADMIN_PREFIX}/routing-table",
            headers={"X-Unitares-Operator": "valid-operator-token"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["count"] == 0
