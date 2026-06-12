"""Wave 3 §3.2 transport-side 503 machinery — §14 prereq PR #10.

Spec: ``docs/proposals/beam-wave-3-handler-dispatch.md`` §3.2 step 3 (503
circuit-breaker for the cutover gap) and §14 row 10. This module is the
RFC-named single source for:

1. **The typed-unavailable response contract.** ``make_unavailable_body()``
   builds the pinned §3.2 body ``{"ok": false, "error":
   "governance_temporarily_unavailable", "reason": ..., "retry_after_seconds":
   5}``. The Wave 3 cutover proxy and the §4(β) Python-writer fail-fast path
   both build their 503 responses from here; SDK consumers
   (``agents/sdk/src/unitares_sdk/errors.py::extract_retry_after_seconds``)
   detect the same shape. Field names are pinned — clients key on them.

2. **The numerator emission point.** ``Transport503EmissionMiddleware`` writes
   one ``measurement.governance_mcp.503_emission`` row per 503 the transport
   returns, payload ``{request_path, error_reason}``. The middleware is the
   ONLY numerator emitter — the cutover proxy must NOT emit its own row for a
   503 it returns, or the §3.2 rate double-counts.

3. **The denominator emission helper.** ``spawn_request_accepted_measurement``
   writes one ``measurement.governance_mcp.request`` row per request accepted
   for proxying. No caller exists pre-cutover (nothing proxies handler
   dispatch yet); the Wave 3 implementation wires it at the proxy entry.

4. **The §3.2 sliding-window aggregator.** ``cutover_503_aggregator_task``
   reads both event counts over the last 60s once per 15s and emits
   ``coordination_failure.governance_mcp.cutover_503_rate_breach`` when the
   rate exceeds 1%. Inert by default — gated on the
   ``WAVE3_CUTOVER_503_AGGREGATOR`` env flag (same shipped-inert posture as
   the REST strict-identity gate); the cutover runbook flips it.

Numerator and denominator share one source (``audit.coordination_measurements``,
migration 041), so the rate is restart-recoverable from PG history within the
60s window — no process-memory counter (§0 item 11).

Constants for both measurement event types live in
``src/coordination_events.py`` (landed with §14 prereq PR #1); this module
emits without re-extending them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from src.coordination_events import (
    COORDINATION_FAILURE_GOVERNANCE_MCP_CUTOVER_503_RATE_BREACH,
    MEASUREMENT_GOVERNANCE_MCP_503_EMISSION,
    MEASUREMENT_GOVERNANCE_MCP_REQUEST,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §3.2 typed-unavailable contract
# ---------------------------------------------------------------------------

# Pinned by §3.2 step 3 and consumed by SDK retry logic. Do not rename.
UNAVAILABLE_ERROR = "governance_temporarily_unavailable"
UNAVAILABLE_REASON_HANDLER_DISPATCH = "handler_dispatch_unavailable"
UNAVAILABLE_RETRY_AFTER_SECONDS = 5

# §3.2 aggregator parameters (also pinned in the breach-event payload contract
# in src/coordination_events.py — keep in sync).
AGGREGATOR_WINDOW_SECONDS = 60
AGGREGATOR_THRESHOLD = 0.01
AGGREGATOR_POLL_SECONDS = 15

# Default-off: the aggregator only runs during the Wave 3 cutover/rollback
# window. The runbook sets this in the server environment and restarts.
AGGREGATOR_ENV_FLAG = "WAVE3_CUTOVER_503_AGGREGATOR"


def make_unavailable_body(
    reason: str = UNAVAILABLE_REASON_HANDLER_DISPATCH,
    retry_after_seconds: int = UNAVAILABLE_RETRY_AFTER_SECONDS,
) -> Dict[str, Any]:
    """Build the pinned §3.2 503 response body.

    Callers must also set the HTTP ``Retry-After`` header to the same value —
    §3.2 names both, and clients are allowed to honor either.
    """
    return {
        "ok": False,
        "error": UNAVAILABLE_ERROR,
        "reason": reason,
        "retry_after_seconds": retry_after_seconds,
    }


# ---------------------------------------------------------------------------
# Measurement-row writes (fire-and-forget, strong task refs)
# ---------------------------------------------------------------------------

# Same set+discard pattern as ``src/wave3a_beam_proxy.py::_PENDING_WRITES`` —
# asyncio.create_task returns a weakly-held task and GC can collect it before
# the write completes (Watcher P001).
_PENDING_WRITES: "set[asyncio.Task[None]]" = set()


async def _write_measurement_row(
    *,
    measurement_type: str,
    request_path: str,
    status: str,
    meta: Dict[str, Any],
) -> None:
    """Insert one numerator/denominator row into
    ``audit.coordination_measurements``. Failures swallowed — this channel is
    observability infrastructure for the §3.2 halt aggregator and a write
    failure MUST NOT propagate into the response path (mirrors
    ``wave3a_beam_proxy.py::_write_success_measurement``).

    ``elapsed_ms`` is 0 by contract: these are counter events, not latency
    samples; the §0(B) latency series lives in ``measurement.lease_plane.*``.
    """
    try:
        from src.db import get_db

        db = get_db()
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit.coordination_measurements
                    (measurement_type, endpoint, elapsed_ms, status, meta)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                measurement_type,
                request_path,
                0,
                status,
                json.dumps(meta),
            )
    except Exception as exc:  # noqa: BLE001 — observability infra; swallow
        logger.debug(
            "[mcp-transport] measurement write failed: %r (type=%s path=%s)",
            exc,
            measurement_type,
            request_path,
        )


def _spawn_write(coro) -> None:
    """Fire-and-forget a measurement write, keeping a strong task ref."""
    try:
        task = asyncio.create_task(coro)
        _PENDING_WRITES.add(task)
        task.add_done_callback(_PENDING_WRITES.discard)
    except Exception as exc:  # noqa: BLE001 — no running loop, etc.
        coro.close()
        logger.debug("[mcp-transport] create_task for write failed: %r", exc)


def spawn_503_measurement(request_path: str, error_reason: str) -> None:
    """Emit the §3.2 numerator: one ``measurement.governance_mcp.503_emission``
    row, payload ``{request_path, error_reason}`` (pinned in
    src/coordination_events.py)."""
    _spawn_write(
        _write_measurement_row(
            measurement_type=MEASUREMENT_GOVERNANCE_MCP_503_EMISSION,
            request_path=request_path,
            status="503",
            meta={"request_path": request_path, "error_reason": error_reason},
        )
    )


def spawn_request_accepted_measurement(request_path: str) -> None:
    """Emit the §3.2 denominator: one ``measurement.governance_mcp.request``
    row per request accepted for proxying, payload ``{request_path}``.

    Pre-cutover this has no caller — the Wave 3 cutover proxy wires it at its
    entry point. It ships here so the proxy lands against a tested emitter.
    """
    _spawn_write(
        _write_measurement_row(
            measurement_type=MEASUREMENT_GOVERNANCE_MCP_REQUEST,
            request_path=request_path,
            status="accepted",
            meta={"request_path": request_path},
        )
    )


# ---------------------------------------------------------------------------
# Numerator capture — ASGI middleware
# ---------------------------------------------------------------------------


class Transport503EmissionMiddleware:
    """Pure-ASGI middleware: emit one numerator row per HTTP 503 returned.

    Watches ``http.response.start`` for status 503, then sniffs the FIRST
    ``http.response.body`` chunk for a JSON object carrying ``reason`` /
    ``error`` / ``status`` to attribute the 503 (e.g.
    ``handler_dispatch_unavailable`` during cutover, ``warming_up`` from the
    readiness probe). Non-JSON or streaming bodies fall back to
    ``error_reason="unknown"``. The sniff only runs on 503s, so the hot path
    is two dict lookups per send event.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_path = scope.get("path", "")
        state = {"is_503": False, "emitted": False}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                state["is_503"] = message.get("status") == 503
            elif (
                message.get("type") == "http.response.body"
                and state["is_503"]
                and not state["emitted"]
            ):
                state["emitted"] = True
                spawn_503_measurement(
                    request_path, _sniff_error_reason(message.get("body"))
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _sniff_error_reason(body: Optional[bytes]) -> str:
    """Best-effort error_reason from the first 503 body chunk."""
    if not body:
        return "unknown"
    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001 — non-JSON body
        return "unknown"
    if not isinstance(parsed, dict):
        return "unknown"
    for key in ("reason", "error", "status"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


# ---------------------------------------------------------------------------
# §3.2 sliding-window aggregator
# ---------------------------------------------------------------------------


async def read_503_window_counts(db) -> tuple[int, int]:
    """Return ``(count_503, count_request)`` over the trailing
    ``AGGREGATOR_WINDOW_SECONDS`` from ``audit.coordination_measurements``."""
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE measurement_type = $1) AS count_503,
                COUNT(*) FILTER (WHERE measurement_type = $2) AS count_request
            FROM audit.coordination_measurements
            WHERE measurement_type IN ($1, $2)
              AND recorded_at > NOW() - make_interval(secs => $3)
            """,
            MEASUREMENT_GOVERNANCE_MCP_503_EMISSION,
            MEASUREMENT_GOVERNANCE_MCP_REQUEST,
            float(AGGREGATOR_WINDOW_SECONDS),
        )
    return int(row["count_503"]), int(row["count_request"])


async def check_503_breach_once(db) -> Optional[Dict[str, Any]]:
    """One aggregator tick. Returns the breach payload when the §3.2 rate
    exceeds the threshold (after emitting the breach event), else ``None``.

    A zero denominator means nothing is being proxied in the window — no rate
    exists, never a breach (§3.2's rate is specifically
    ``count(503_emission) / count(request)``).
    """
    count_503, count_request = await read_503_window_counts(db)
    if count_request <= 0:
        return None
    rate = count_503 / count_request
    if rate <= AGGREGATOR_THRESHOLD:
        return None

    # Payload pinned in src/coordination_events.py (§8.4 docstring).
    payload = {
        "window_seconds": AGGREGATOR_WINDOW_SECONDS,
        "rate": rate,
        "threshold": AGGREGATOR_THRESHOLD,
        "count_503": count_503,
        "count_request": count_request,
    }
    try:
        from src.coordination_events import emit_event

        pool = getattr(db, "_pool", None)
        if pool is None:
            logger.warning(
                "[mcp-transport] 503 breach detected but no pool for emit: %s",
                payload,
            )
            return payload
        await emit_event(
            pool,
            service="governance_mcp",
            event_type=COORDINATION_FAILURE_GOVERNANCE_MCP_CUTOVER_503_RATE_BREACH,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 — breach detection must outlive emit
        logger.warning(
            "[mcp-transport] 503 breach emit failed: %r (payload=%s)", exc, payload
        )
    logger.error(
        "[mcp-transport] §3.2 cutover 503-rate breach: %.4f > %.2f "
        "(%d/%d in %ds window) — halt direction: restore Python writers "
        "before stopping (stop sign #7)",
        rate,
        AGGREGATOR_THRESHOLD,
        count_503,
        count_request,
        AGGREGATOR_WINDOW_SECONDS,
    )
    return payload


def aggregator_enabled() -> bool:
    """True when the cutover runbook has armed the aggregator."""
    return os.environ.get(AGGREGATOR_ENV_FLAG, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def cutover_503_aggregator_task(
    interval_seconds: float = AGGREGATOR_POLL_SECONDS,
) -> None:
    """§3.2 halt aggregator loop. Inert unless ``WAVE3_CUTOVER_503_AGGREGATOR``
    is set — registered unconditionally at server start so the cutover runbook
    only has to set the flag and restart, with no code change at cutover time.
    """
    if not aggregator_enabled():
        logger.info(
            "[mcp-transport] cutover 503 aggregator inert (%s not set)",
            AGGREGATOR_ENV_FLAG,
        )
        return

    logger.info(
        "[mcp-transport] cutover 503 aggregator armed: window=%ds poll=%.0fs "
        "threshold=%.2f",
        AGGREGATOR_WINDOW_SECONDS,
        interval_seconds,
        AGGREGATOR_THRESHOLD,
    )
    while True:
        started = time.monotonic()
        try:
            from src.db import get_db

            await check_503_breach_once(get_db())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the watchdog alive
            logger.warning("[mcp-transport] aggregator tick failed: %r", exc)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(0.0, interval_seconds - elapsed))


__all__ = [
    "AGGREGATOR_ENV_FLAG",
    "AGGREGATOR_POLL_SECONDS",
    "AGGREGATOR_THRESHOLD",
    "AGGREGATOR_WINDOW_SECONDS",
    "Transport503EmissionMiddleware",
    "UNAVAILABLE_ERROR",
    "UNAVAILABLE_REASON_HANDLER_DISPATCH",
    "UNAVAILABLE_RETRY_AFTER_SECONDS",
    "check_503_breach_once",
    "cutover_503_aggregator_task",
    "aggregator_enabled",
    "make_unavailable_body",
    "read_503_window_counts",
    "spawn_503_measurement",
    "spawn_request_accepted_measurement",
]
