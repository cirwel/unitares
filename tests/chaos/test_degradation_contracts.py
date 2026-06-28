"""Chaos / failure-injection suite: prove the documented degradation paths degrade.

The "testing gaps" review asked for tests that exercise the failure modes the
code *claims* to handle gracefully, instead of trusting the docstrings. Each test
here injects a single, bounded, deterministic failure (monkeypatch / a fake slow
coroutine / a forced exception — never a real DoS or a real outage) and asserts
the degradation contract the source documents.

Unlike the full-stack ``integration`` suite, these need **no live services**: the
failure is injected in-process, so the suite runs in the default gate and keeps
the contracts continuously verified. The ``chaos`` marker lets you select just
this suite (``pytest -m chaos``).

Contracts under test (each cites the source that documents it):

1. **BEAM proxy fallback** (``src/wave3a_beam_proxy.py`` §3.2). A slow / dead /
   malformed BEAM endpoint must fall back to Python and fire the matching
   ``coordination_failure.wave_3a.*`` event — never silently skip.
2. **Redis degradation** (``src/mcp_handlers/middleware/identity_step.py``). The
   ``asyncio.wait_for(0.5)`` guards must degrade a slow / hung Redis to a cold
   miss (return ``None``) promptly, not hang the dispatch path.
3. **Assessment fail-open** (``src/mcp_handlers/core.py``, PR #1211). When the
   assessment pipeline itself errors, the check-in returns an error response with
   NO verdict/action — the agent proceeds. Governance must not synthesize a pause
   out of its own internal failure.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

pytestmark = pytest.mark.chaos


# ---------------------------------------------------------------------------
# 1. BEAM proxy — timeout / dead-endpoint / bad-envelope → Python fallback
# ---------------------------------------------------------------------------


def _capture_emits(monkeypatch):
    """Replace the proxy's fire-and-forget emit with a synchronous capture.

    ``proxy_to_beam`` emits coordination events via ``_spawn_emit`` (which
    create_task's a DB write). Patching it to append to a list lets us assert the
    *emission decision* deterministically without a DB or an event loop race.
    """
    import src.wave3a_beam_proxy as proxy

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(proxy, "_spawn_emit", lambda event_type, payload: emitted.append((event_type, payload)))
    # Metrics + success-measurement also fire-and-forget; neutralize to keep the
    # injection hermetic (they swallow errors anyway, but this avoids touching the
    # prometheus registry / DB during a unit test).
    monkeypatch.setattr(proxy, "_record_proxy_metric", lambda *a, **k: None)
    monkeypatch.setattr(proxy, "_spawn_success_measurement", lambda *a, **k: None)
    return proxy, emitted


@pytest.mark.asyncio
async def test_beam_timeout_falls_back_and_emits(monkeypatch):
    """A BEAM endpoint slower than the 500ms budget → fallback_reason='timeout'
    and BOTH the timeout and fallback coordination events fire (§3.2)."""
    proxy, emitted = _capture_emits(monkeypatch)

    # Tighten the budget so the test is fast, and make the call exceed it.
    monkeypatch.setattr(proxy, "BEAM_TIMEOUT_SECONDS", 0.05)

    async def _slow(*_args, **_kwargs):
        await asyncio.sleep(1.0)
        return {"ok": True, "protocol_version": proxy.PROTOCOL_VERSION}

    monkeypatch.setattr(proxy, "_call_beam", _slow)

    start = time.monotonic()
    result = await proxy.proxy_to_beam(tool_name="some_tool", beam_url="http://beam.invalid", kwargs={})
    elapsed = time.monotonic() - start

    assert result.ok is False, "timeout must force a Python fallback, not a BEAM success"
    assert result.fallback_reason == "timeout"
    assert elapsed < 0.9, f"timeout guard must bound the wait near the budget, took {elapsed:.3f}s"

    event_types = [et for et, _ in emitted]
    assert proxy.COORDINATION_FAILURE_WAVE_3A_TIMEOUT in event_types, "timeout event must fire"
    assert proxy.COORDINATION_FAILURE_WAVE_3A_FALLBACK in event_types, "fallback event must fire"
    # §4.2 dedup contract: the timeout and its fallback share one incident_id.
    payloads = {et: p for et, p in emitted}
    assert (
        payloads[proxy.COORDINATION_FAILURE_WAVE_3A_TIMEOUT]["incident_id"]
        == payloads[proxy.COORDINATION_FAILURE_WAVE_3A_FALLBACK]["incident_id"]
    ), "timeout + fallback describe one incident; they must share incident_id"


@pytest.mark.asyncio
async def test_beam_dead_endpoint_falls_back_and_emits(monkeypatch):
    """A dead BEAM endpoint (connect error) → fallback_reason='connect_error'
    and the fallback coordination event fires (§3.2)."""
    proxy, emitted = _capture_emits(monkeypatch)

    async def _refused(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(proxy, "_call_beam", _refused)

    result = await proxy.proxy_to_beam(tool_name="some_tool", beam_url="http://beam.invalid", kwargs={})

    assert result.ok is False
    assert result.fallback_reason == "connect_error"
    assert proxy.COORDINATION_FAILURE_WAVE_3A_FALLBACK in [et for et, _ in emitted], (
        "a dead endpoint must fire the fallback event, never silently skip"
    )


@pytest.mark.asyncio
async def test_beam_bad_envelope_falls_back_and_emits(monkeypatch):
    """A 200 response with a malformed envelope → fallback_reason='envelope_invalid'
    and the envelope_invalid coordination event fires (§3.2 / §2.2)."""
    proxy, emitted = _capture_emits(monkeypatch)

    async def _bad_shape(*_args, **_kwargs):
        # Valid JSON, wrong protocol_version → §2.2 violation.
        return {"ok": True, "protocol_version": "not-wave3a"}

    monkeypatch.setattr(proxy, "_call_beam", _bad_shape)

    result = await proxy.proxy_to_beam(tool_name="some_tool", beam_url="http://beam.invalid", kwargs={})

    assert result.ok is False
    assert result.fallback_reason == "envelope_invalid"
    assert proxy.COORDINATION_FAILURE_WAVE_3A_ENVELOPE_INVALID in [et for et, _ in emitted]


@pytest.mark.asyncio
async def test_beam_success_does_not_fall_back(monkeypatch):
    """Control: a well-formed BEAM success returns ok=True and emits no failure
    event — proves the failure assertions above aren't trivially always-true."""
    proxy, emitted = _capture_emits(monkeypatch)

    async def _ok(*_args, **_kwargs):
        return {"ok": True, "protocol_version": proxy.PROTOCOL_VERSION, "result": 42}

    monkeypatch.setattr(proxy, "_call_beam", _ok)

    result = await proxy.proxy_to_beam(tool_name="some_tool", beam_url="http://beam.invalid", kwargs={})

    assert result.ok is True
    assert result.response == {"ok": True, "protocol_version": proxy.PROTOCOL_VERSION, "result": 42}
    assert emitted == [], f"a clean success must emit no coordination_failure events, got {emitted}"


# ---------------------------------------------------------------------------
# 2. Redis degradation — a hung Redis must degrade to a cold miss, not hang
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_binding_recovery_degrades_on_hang(monkeypatch):
    """A hung Redis recovery must time out via asyncio.wait_for and return None
    (cold path) promptly, instead of blocking every subsequent MCP call."""
    import src.mcp_handlers.middleware.identity_step as idstep

    # Tight budget so the test is fast; production value is 0.5s.
    monkeypatch.setattr(idstep, "_REDIS_RECOVERY_TIMEOUT", 0.05)

    async def _hung(_key):
        await asyncio.sleep(10.0)  # simulate a Redis client wedged by the anyio/asyncpg stall

    monkeypatch.setattr(idstep, "_load_binding_from_redis_inner", _hung)

    start = time.monotonic()
    result = await idstep._load_binding_from_redis("some-session-key")
    elapsed = time.monotonic() - start

    assert result is None, "a hung Redis must degrade to a cold miss (None), not a binding"
    assert elapsed < 1.0, f"the wait_for guard must bound the hang near the budget, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_redis_status_lookup_degrades_on_hang(monkeypatch):
    """The core-agent-row status lookup has the same wait_for guard: a hung
    lookup degrades to None rather than hanging the dispatch path."""
    import src.mcp_handlers.middleware.identity_step as idstep
    from src.mcp_handlers.identity import handlers as id_handlers

    monkeypatch.setattr(idstep, "_REDIS_RECOVERY_TIMEOUT", 0.05)

    async def _hung(_uuid):
        await asyncio.sleep(10.0)

    monkeypatch.setattr(id_handlers, "_get_agent_status", _hung)

    start = time.monotonic()
    result = await idstep._lookup_core_agent_row_status("a" * 36, source="chaos-test")
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 1.0, f"status lookup must bound the hang, took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# 3. Assessment fail-open — an internal error must NOT synthesize a pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_error_fails_open_no_verdict(monkeypatch):
    """When the assessment pipeline raises, process_agent_update returns an error
    response with NO verdict/action/decision — the agent proceeds (PR #1211)."""
    import src.mcp_handlers.core as core
    from tests.helpers import parse_result

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("forced assessment pipeline failure")

    # The fail-open branch wraps the whole assessment workflow.
    monkeypatch.setattr(core, "run_process_update_workflow", _boom)

    result = await core.handle_process_agent_update({"response_text": "did some work", "agent_id": "chaos-agent"})
    data = parse_result(result)

    # It is an error response...
    assert data.get("success") is False, f"expected success=false error response, got {data}"
    assert data.get("error_type") == "unexpected_error", (
        f"fail-open branch should tag error_type=unexpected_error, got {data}"
    )

    # ...and crucially it does NOT synthesize a pause/verdict. Fail-OPEN means the
    # agent proceeds; governance must not turn its own internal failure into a stop.
    for forbidden in ("verdict", "action", "decision", "sub_action"):
        assert forbidden not in data, (
            f"fail-open response must carry no '{forbidden}' (no synthesized pause); got {data}"
        )
