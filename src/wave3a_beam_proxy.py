"""Wave 3a BEAM-outbound proxy — PR #3 of v0.2 sequencing.

Spec: ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §2.2
(envelope shape), §3.2 (timeout discipline + Python-fallback semantics),
§4.2 (stop-sign event taxonomy: ``coordination_failure.wave_3a.{timeout,
fallback,envelope_invalid}``).

This module owns the outbound HTTP call to the BEAM listener. It does NOT
own the routing table (``src/wave3a_routing.py``) or the dispatch hook
(``src/mcp_server.py::get_tool_wrapper``). The call sequence is:

  1. The wrapper checks ``is_routed(tool_name)``.
  2. If routed, it calls ``proxy_to_beam(tool_name, kwargs)``.
  3. ``proxy_to_beam`` returns ``ProxyResult(ok=True, response=...)`` on
     success or ``ProxyResult(ok=False, fallback_reason=...)`` on any
     failure mode that requires falling back to Python.
  4. The wrapper then either returns the BEAM response (success) or calls
     ``dispatch_tool`` exactly once (fallback).

Hard contract from §3.2:

- 500ms hard timeout on the BEAM outbound HTTP call. We use
  ``asyncio.wait_for`` per CLAUDE.md "Substrate Tax" pattern #3 (tight
  timeout fallback). This is correct for HTTP — CLAUDE.md's prohibition on
  ``asyncio.wait_for`` guards applies to asyncpg/Redis (the ExecutorPool
  fix replaces those guards), not to outbound HTTP.
- On timeout → emit ``coordination_failure.wave_3a.timeout`` AND
  ``coordination_failure.wave_3a.fallback`` → fall back to Python.
- On connect/HTTP/decode failure → emit
  ``coordination_failure.wave_3a.fallback`` → fall back to Python.
- On envelope-shape mismatch → emit
  ``coordination_failure.wave_3a.envelope_invalid`` → fall back to Python.
- Silent skip on any failure is the worst possible outcome (§3.2 hard
  constraint: "must always fire if BEAM fails"). The fallback path always
  runs Python — there is no path that returns nothing.

Per RFC §2.5 the BEAM listener authenticates the inbound HTTP via
``WAVE_3A_BEAM_TOKEN``. PR #3 (this module) writes the bearer header on
the outbound request; PR #4 verifies it on the BEAM side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx

from src.coordination_events import (
    COORDINATION_FAILURE_WAVE_3A_ENVELOPE_INVALID,
    COORDINATION_FAILURE_WAVE_3A_FALLBACK,
    COORDINATION_FAILURE_WAVE_3A_TIMEOUT,
)

# §4.3 measurement channel: the §4.2 rate-of-failure stop sign needs both
# numerator (failure events emitted above) and denominator (per-routed-call
# success rows). ``wave3a_probe.py`` writes measurement rows for HTTP probe
# calls; tool-dispatch calls through ``proxy_to_beam`` go through a
# different surface and need their own success-row write site.
MEASUREMENT_TYPE_WAVE_3A_REQUEST = "measurement.wave_3a.request"

logger = logging.getLogger(__name__)


# §3.2: 500ms hard timeout. Exposed as a module-level constant so tests
# can monkeypatch it; the production value is fixed by the RFC.
BEAM_TIMEOUT_SECONDS = 0.5
BEAM_TIMEOUT_MS = 500

# §2.5: outbound bearer token to BEAM. Reading from env each call (not
# cached) so an operator can rotate the secret without restarting the MCP.
BEAM_TOKEN_ENV = "WAVE_3A_BEAM_TOKEN"

PROTOCOL_VERSION = "wave3a.v1"


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyResult:
    """Outcome of a BEAM proxy attempt.

    ``ok=True`` → BEAM succeeded; ``response`` carries the parsed JSON
    body and the caller returns it directly to the MCP client. ``ok=False``
    → BEAM failed at some stage; ``fallback_reason`` is the
    short machine-readable label (also used as the trigger in the
    ``coordination_failure.wave_3a.fallback`` payload) and the caller MUST
    fall back to the Python in-process dispatch.
    """

    ok: bool
    response: Optional[Dict[str, Any]] = None
    fallback_reason: Optional[str] = None
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Envelope shape validation — §2.2
# ---------------------------------------------------------------------------


def _validate_success_envelope(body: Any) -> Optional[str]:
    """Return ``None`` if the body is a valid §2.2 success envelope, else
    a short string naming the first violation.

    §2.2 success shape (top-level keys only):

        {"ok": true, "protocol_version": "wave3a.v1", ...}

    The shape rule is intentionally loose: any additional handler-specific
    keys are allowed under ``ok=true``. The strict requirement is that
    ``ok`` is the boolean True and ``protocol_version`` equals the wave's
    pinned literal.
    """
    if not isinstance(body, dict):
        return f"body_not_object:{type(body).__name__}"
    if "ok" not in body:
        return "missing_ok"
    if body["ok"] is not True:
        # ok=false is an error envelope, not a success envelope. Caller
        # decides whether to treat that as fallback or as a typed error;
        # this validator only certifies the success path.
        return "ok_not_true"
    if "protocol_version" not in body:
        return "missing_protocol_version"
    if body["protocol_version"] != PROTOCOL_VERSION:
        return f"protocol_version_mismatch:{body['protocol_version']!r}"
    return None


# ---------------------------------------------------------------------------
# Event emission — §4.2
# ---------------------------------------------------------------------------


async def _emit_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Emit a Wave 3a coordination event, swallowing all failures.

    The event channel is observability infrastructure for stop signs; a
    failure here MUST NOT propagate into the dispatch path. We log at
    debug and move on (mirrors ``src/mcp_handlers/wave3a_probe.py``'s
    measurement-write discipline).

    Routes through ``db._pool`` — which post-PR #218 is an ``ExecutorPool``
    wrapper, NOT a raw ``asyncpg.Pool`` — passed straight to ``emit_event``.
    ``emit_event`` only calls ``pool.acquire()``, which both the wrapper
    and a raw asyncpg pool expose, so the call works against either. An
    earlier ``isinstance(pool, asyncpg.Pool)`` guard here silently dropped
    every emit in production (FIND-R1, review fold). The probe module
    (``wave3a_probe.py::_write_measurement``) demonstrates the same
    pattern using ``db.acquire()`` directly.
    """
    try:
        from src.coordination_events import emit_event
        from src.db import get_db

        db = get_db()
        pool = getattr(db, "_pool", None)
        if pool is None:
            # Lazy-init the pool if the backend hasn't been touched yet
            # (e.g., a tool dispatch fired before any other handler did).
            try:
                await db.init()
            except Exception:  # noqa: BLE001
                return
            pool = getattr(db, "_pool", None)
        if pool is None:
            logger.debug("[wave3a-proxy] event emit skipped: no pool available")
            return
        await emit_event(
            pool,
            service="governance_mcp",
            event_type=event_type,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask
        logger.debug(
            "[wave3a-proxy] event emit failed: %s (event_type=%s payload=%s)",
            exc,
            event_type,
            payload,
        )


def _spawn_emit(event_type: str, payload: Dict[str, Any]) -> None:
    """Fire-and-forget the event emit, keeping a strong task ref.

    Same pattern as ``src/mcp_handlers/wave3a_probe.py::_PENDING_WRITES``:
    ``asyncio.create_task`` returns a weakly-held task and GC can collect
    it before the write completes. The set-and-discard pattern pins it.
    """
    try:
        task = asyncio.create_task(_emit_event(event_type, payload))
        _PENDING_EMITS.add(task)
        task.add_done_callback(_PENDING_EMITS.discard)
    except Exception as exc:  # noqa: BLE001
        # No running loop, etc. Drop silently — emit is observability infra.
        logger.debug("[wave3a-proxy] create_task for emit failed: %r", exc)


_PENDING_EMITS: "set[asyncio.Task[None]]" = set()

# Strong references for in-flight measurement writes. Same set+discard
# pattern as ``wave3a_probe.py::_PENDING_WRITES`` — Watcher P001 fix.
_PENDING_WRITES: "set[asyncio.Task[None]]" = set()


async def _write_success_measurement(
    *,
    endpoint: str,
    elapsed_ms: int,
    payload_bytes: int,
    beam_url: str,
) -> None:
    """Insert one ``measurement.wave_3a.request`` row marking a routed-call
    success. Failures swallowed — measurement is observability infra for
    the §4.2 denominator and a write failure here MUST NOT propagate into
    the dispatch path. Mirrors ``wave3a_probe.py::_write_measurement``.
    """
    try:
        from src.db import get_db

        db = get_db()
        meta_json = json.dumps(
            {"source": "proxy_to_beam", "beam_url": beam_url}
        )
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit.coordination_measurements
                    (measurement_type, endpoint, elapsed_ms, status,
                     payload_bytes, meta)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                MEASUREMENT_TYPE_WAVE_3A_REQUEST,
                endpoint,
                elapsed_ms,
                "200",
                payload_bytes,
                meta_json,
            )
    except Exception as exc:  # noqa: BLE001 — observability infra; swallow
        logger.debug(
            "[wave3a-proxy] measurement write failed: %r (endpoint=%s "
            "elapsed_ms=%s)",
            exc,
            endpoint,
            elapsed_ms,
        )


def _spawn_success_measurement(
    *, endpoint: str, elapsed_ms: int, payload_bytes: int, beam_url: str,
) -> None:
    """Fire-and-forget the success-measurement write, keeping a strong ref."""
    try:
        task = asyncio.create_task(
            _write_success_measurement(
                endpoint=endpoint,
                elapsed_ms=elapsed_ms,
                payload_bytes=payload_bytes,
                beam_url=beam_url,
            )
        )
        _PENDING_WRITES.add(task)
        task.add_done_callback(_PENDING_WRITES.discard)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[wave3a-proxy] create_task for measurement write failed: %r", exc
        )


# ---------------------------------------------------------------------------
# Outbound HTTP — §3.2 timeout + fallback
# ---------------------------------------------------------------------------


async def _call_beam(
    *, beam_url: str, tool_name: str, kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Single outbound POST to the BEAM listener.

    Pulled into its own coroutine so ``asyncio.wait_for`` wraps a clean
    awaitable. No retries — §3.2 is "single attempt, hard timeout,
    fallback to Python."
    """
    headers = {"content-type": "application/json"}
    token = os.environ.get(BEAM_TOKEN_ENV)
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = {"tool_name": tool_name, "arguments": kwargs}
    # Per-call client: Wave 3a calls are HTTP-over-localhost (no TLS), so
    # connection setup cost is negligible against the 500ms budget. A shared
    # client is also viable — httpx does not have asyncpg's loop-binding
    # constraint. Per-call is chosen for simplicity at Wave 3a scale (≤4
    # handlers). Revisit for Wave 3b if p99 pressure appears.
    async with httpx.AsyncClient(timeout=BEAM_TIMEOUT_SECONDS) as client:
        response = await client.post(beam_url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()


async def proxy_to_beam(
    *, tool_name: str, beam_url: str, kwargs: Dict[str, Any]
) -> ProxyResult:
    """Attempt to dispatch ``tool_name`` through BEAM.

    Returns ``ProxyResult(ok=True, response=...)`` on a clean BEAM response
    that matches the §2.2 success envelope; in every other case returns
    ``ProxyResult(ok=False, fallback_reason=...)`` and emits the appropriate
    §4.2 coordination event. The caller (the MCP transport wrapper) MUST
    fall back to Python on any ``ok=False`` outcome.

    Failure taxonomy (mapped to ``fallback_reason``):

    - ``"timeout"`` — outbound HTTP exceeded the 500ms budget. Emits
      ``coordination_failure.wave_3a.timeout`` AND
      ``coordination_failure.wave_3a.fallback`` (the timeout IS a fallback
      trigger; the fallback event preserves the §4.2 denominator math).
    - ``"connect_error"`` — TCP connect / DNS failure / connection reset.
      Emits ``coordination_failure.wave_3a.fallback``.
    - ``"non_200"`` — BEAM returned a non-2xx status. Emits
      ``coordination_failure.wave_3a.fallback``.
    - ``"decode_error"`` — body was not valid JSON. Emits
      ``coordination_failure.wave_3a.fallback``.
    - ``"envelope_invalid"`` — JSON parsed but failed §2.2 shape check.
      Emits ``coordination_failure.wave_3a.envelope_invalid``.

    Per RFC §3.2 hard invariant: this function NEVER returns a ProxyResult
    that the caller could misread as "skip Python silently". The two
    return shapes are mutually exclusive: ok=True → use BEAM response,
    ok=False → fall back to Python. No third state.
    """
    start = time.monotonic()
    try:
        body = await asyncio.wait_for(
            _call_beam(beam_url=beam_url, tool_name=tool_name, kwargs=kwargs),
            timeout=BEAM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        # Wave 0 step 2 dedup contract: every coordination_failure emit
        # carries an incident_id (§129 counts DISTINCT incident_id). One
        # failure occurrence = one id — the timeout and fallback events
        # below describe the SAME incident, so they share it rather than
        # inflating the distinct-incident count to two.
        incident_id = str(uuid4())
        _spawn_emit(
            COORDINATION_FAILURE_WAVE_3A_TIMEOUT,
            {
                "tool_name": tool_name,
                "elapsed_ms": elapsed_ms,
                "budget_ms": BEAM_TIMEOUT_MS,
                "incident_id": incident_id,
            },
        )
        _spawn_emit(
            COORDINATION_FAILURE_WAVE_3A_FALLBACK,
            {
                "tool_name": tool_name,
                "trigger": "timeout",
                "elapsed_ms": elapsed_ms,
                "incident_id": incident_id,
            },
        )
        logger.info(
            "[wave3a-proxy] %s: BEAM timeout at %dms → fallback to Python",
            tool_name,
            elapsed_ms,
        )
        return ProxyResult(
            ok=False, fallback_reason="timeout", elapsed_ms=elapsed_ms
        )
    except httpx.HTTPStatusError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _spawn_emit(
            COORDINATION_FAILURE_WAVE_3A_FALLBACK,
            {
                "tool_name": tool_name,
                "trigger": "non_200",
                "elapsed_ms": elapsed_ms,
                "status_code": exc.response.status_code if exc.response else None,
                "incident_id": str(uuid4()),
            },
        )
        logger.info(
            "[wave3a-proxy] %s: BEAM returned non-2xx (%s) at %dms → fallback",
            tool_name,
            exc.response.status_code if exc.response else "?",
            elapsed_ms,
        )
        return ProxyResult(
            ok=False, fallback_reason="non_200", elapsed_ms=elapsed_ms
        )
    except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _spawn_emit(
            COORDINATION_FAILURE_WAVE_3A_FALLBACK,
            {
                "tool_name": tool_name,
                "trigger": "connect_error",
                "elapsed_ms": elapsed_ms,
                "incident_id": str(uuid4()),
            },
        )
        logger.info(
            "[wave3a-proxy] %s: BEAM connect error (%s) at %dms → fallback",
            tool_name,
            type(exc).__name__,
            elapsed_ms,
        )
        return ProxyResult(
            ok=False, fallback_reason="connect_error", elapsed_ms=elapsed_ms
        )
    except Exception as exc:  # noqa: BLE001 — broad on purpose
        # Includes JSON decode errors. We default to fallback rather than
        # bubbling — silent skip is the worst failure mode per §3.2.
        elapsed_ms = int((time.monotonic() - start) * 1000)
        trigger = "decode_error" if "json" in str(exc).lower() else "other"
        _spawn_emit(
            COORDINATION_FAILURE_WAVE_3A_FALLBACK,
            {
                "tool_name": tool_name,
                "trigger": trigger,
                "elapsed_ms": elapsed_ms,
                "incident_id": str(uuid4()),
            },
        )
        logger.info(
            "[wave3a-proxy] %s: BEAM unexpected error (%s: %s) at %dms → fallback",
            tool_name,
            type(exc).__name__,
            exc,
            elapsed_ms,
        )
        return ProxyResult(
            ok=False, fallback_reason=trigger, elapsed_ms=elapsed_ms
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    violation = _validate_success_envelope(body)
    if violation is not None:
        envelope_keys = sorted(body.keys()) if isinstance(body, dict) else []
        _spawn_emit(
            COORDINATION_FAILURE_WAVE_3A_ENVELOPE_INVALID,
            {
                "tool_name": tool_name,
                "detail": violation,
                "envelope_keys": envelope_keys,
                "incident_id": str(uuid4()),
            },
        )
        logger.info(
            "[wave3a-proxy] %s: BEAM envelope invalid (%s) at %dms → fallback",
            tool_name,
            violation,
            elapsed_ms,
        )
        return ProxyResult(
            ok=False,
            fallback_reason="envelope_invalid",
            elapsed_ms=elapsed_ms,
        )

    logger.debug(
        "[wave3a-proxy] %s: BEAM ok at %dms", tool_name, elapsed_ms
    )
    # FIND-A5 fold: write one ``measurement.wave_3a.request`` row marking
    # this routed-call success so §4.2's rate-of-failure denominator has a
    # non-failure baseline to divide against. Without this, the denominator
    # counts only emitted failure events and the stop sign is structurally
    # mis-scaled. Fire-and-forget so the write does not contribute to the
    # latency it itself records.
    try:
        payload_bytes = len(json.dumps(body)) if body is not None else 0
    except Exception:  # noqa: BLE001
        payload_bytes = 0
    _spawn_success_measurement(
        endpoint=tool_name,
        elapsed_ms=elapsed_ms,
        payload_bytes=payload_bytes,
        beam_url=beam_url,
    )
    return ProxyResult(ok=True, response=body, elapsed_ms=elapsed_ms)


__all__ = [
    "BEAM_TIMEOUT_MS",
    "BEAM_TIMEOUT_SECONDS",
    "BEAM_TOKEN_ENV",
    "PROTOCOL_VERSION",
    "ProxyResult",
    "proxy_to_beam",
]
