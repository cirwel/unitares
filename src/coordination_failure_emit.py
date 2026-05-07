"""Wave 0 step 2A — sync coordination-failure emit (RFC roadmap §86, post-council pivot).

Council on PR #342's scoping doc (3-agent ack-pass v0.3) BLOCKED on the
direct-asyncpg-await-from-decorator path: that's exactly the anyio task-
group deadlock CLAUDE.md "Known Issue" exists to prevent. v0.3's prescription
for `await get_pool().acquire()` from inside the @mcp_tool wrapper's
`except asyncio.TimeoutError` was unsafe.

This module pivots: instead of writing to the new `audit.coordination_events`
table via async asyncpg, write to the EXISTING `audit.events` table via
the EXISTING `audit_logger._write_entry` sync path. That path is the same
one `_AuditEmitter` in src/background_tasks.py:464 uses — it's been in
production for the entire `audit.events` infrastructure, sidesteps the
anyio task group entirely, and provides the durability guarantees we
already trust (JSONL append + fire-and-forget Postgres).

PR #342's `audit.coordination_events` table + async emitter remain the
dedicated replay surface. As of Wave 2 §"audit.coordination_events
routing fix" (RFC roadmap), this module dual-writes: the existing
audit.events path stays as the failure-safe truth (Wave 1 exit criterion
queries depend on it), and the dedicated table is populated in parallel
via a fire-and-forget `loop.create_task` after the sync write — same
anyio-deadlock-avoiding pattern as audit_log._write_entry's Postgres tail.
A failure on the dedicated-table side is logged at WARNING and dropped;
audit.events remains durable.

`event_type` namespace:
  coordination_failure.<class>.<subtype>   (e.g. mcp_handler_timeout.tool_decorator)

The dotted-namespace discipline matches PR #342's regex contract; future
sweepers / dashboards that want to project these into a dedicated table
filter on `event_type LIKE 'coordination_failure.%'`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Service identifiers — kept in sync with PR #342's coordination_events service
# enum so when/if we promote to the dedicated table, the values port unchanged.
SERVICES: frozenset[str] = frozenset({
    "sentinel",
    "governance_mcp",
    "lease_plane",
    "vigil",
    "chronicler",
    "watcher",
})


def emit_coordination_failure_sync(
    *,
    service: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Emit a coordination-failure event via the sync `audit_logger._write_entry` path.

    This function is FAILURE-SAFE by contract: it MUST NOT raise. Wave 0 step 2A
    wires it INSIDE existing `except` clauses (decorator timeout, etc.); a raising
    emitter would replace the original exception with the emit-failure traceback
    (council BLOCK-2 from v0.1 review). All exception paths are caught and
    swallowed with WARNING-level logging.

    Args:
        service: emitter identity, MUST be in SERVICES. Unknown service silently
                 falls back to 'governance_mcp' with a WARNING (we log AND emit;
                 missing the event would be worse than emitting with a fallback).
        event_type: dotted namespace, MUST start with 'coordination_failure.'
                    per the v0.3 council convergence on event_type discipline.
                    Validated by prefix-match only — caller is trusted to use
                    documented sub-namespaces.
        payload: event-specific structure. Stored under details.payload.
        agent_id: optional UNITARES UUID when agent-attributable.
        session_id: optional session_key for cross-event correlation. Persists to
                    audit.events.session_id column (text, indexed via session
                    pattern). Decorator passes get_context_session_key().
    """
    if not isinstance(event_type, str) or not event_type.startswith("coordination_failure."):
        logger.warning(
            "[coord-failure-emit] event_type %r does not start with 'coordination_failure.' — skipping emit",
            event_type,
        )
        return

    effective_service = service if service in SERVICES else "governance_mcp"
    if effective_service != service:
        logger.warning(
            "[coord-failure-emit] unknown service %r, falling back to 'governance_mcp'", service
        )

    try:
        from src.audit_log import AuditEntry, audit_logger

        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type=event_type,
            confidence=1.0,
            details={
                "service": effective_service,
                "payload": payload or {},
            },
            session_id=session_id,
        )
        audit_logger._write_entry(entry)
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
        logger.warning(
            "[coord-failure-emit] write failed for %s/%s: %r — original exception preserved",
            effective_service,
            event_type,
            exc,
        )

    # Wave 2 §"audit.coordination_events routing fix" (RFC roadmap):
    # Dual-write to the dedicated replay surface in addition to audit.events.
    # The audit.events row above remains the failure-safe truth (Wave 1 exit
    # criterion queries depend on it); this populates the dedicated table
    # the v0 envelope specified, so a future Chronicler/dashboard projection
    # has a single-purpose surface to read instead of LIKE-filtering
    # audit.events. Fire-and-forget by design — same anyio-deadlock-avoiding
    # pattern as audit_log._write_entry's Postgres tail.
    _schedule_coordination_events_dual_write(
        service=effective_service,
        event_type=event_type,
        payload=payload or {},
        agent_id=agent_id,
    )


def _schedule_coordination_events_dual_write(
    *,
    service: str,
    event_type: str,
    payload: dict[str, Any],
    agent_id: str | None,
) -> None:
    """Fire-and-forget schedule of an audit.coordination_events insert.

    Failure-safe by contract: never raises, never blocks. If no event loop
    is reachable, the dedicated-table write is dropped silently — the
    audit.events row is still durable. Mirrors audit_log._write_entry's
    fire-and-forget Postgres pattern so this stays sync-safe under the
    council BLOCK on direct-asyncpg-await from decorator except clauses.
    """
    try:
        import asyncio

        def _coro_factory():
            return _emit_to_coordination_events_async(
                service=service,
                event_type=event_type,
                payload=payload,
                agent_id=agent_id,
            )

        try:
            loop = asyncio.get_running_loop()
            _spawn_dedicated_write_task(loop, _coro_factory())
            return
        except RuntimeError:
            pass

        # No running loop in this thread — try the audit-logger's captured
        # main loop, which is set up at server boot for executor-thread
        # writes. If that's also unavailable (CLI / pure unit tests), drop
        # the dedicated-table write rather than block the caller.
        captured_loop = None
        try:
            from src.audit_log import AuditLogger

            captured_loop = getattr(AuditLogger, "_event_loop", None)
        except Exception:  # noqa: BLE001 — defensive on import failure
            captured_loop = None

        if captured_loop is not None and captured_loop.is_running():
            def _spawn_on_main():
                _spawn_dedicated_write_task(captured_loop, _coro_factory())

            captured_loop.call_soon_threadsafe(_spawn_on_main)
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
        logger.debug(
            "[coord-failure-emit] dedicated-table schedule failed: %r — "
            "audit.events row remains durable",
            exc,
        )


# Module-local strong-ref set for in-flight dedicated-table coroutines.
# Watcher P001: bare `loop.create_task(coro)` returns a Task that the GC
# can collect mid-flight if no one holds a reference. We can't use
# `background_tasks.create_tracked_task` because the supervisor's
# cancellation done-callback recursively calls back into
# `emit_coordination_failure_sync` (Wave 0 step 2C-1 cancellation emit),
# which would re-register a fresh task on the *same* `_supervised_tasks`
# list and break `test_stop_all_background_tasks_cancels_supervised_tasks`'s
# emptiness invariant. A private ref set isolated to this module gives the
# same GC protection without sharing fate with the supervisor.
_inflight_dedicated_writes: "set[asyncio.Task]" = set()


def _spawn_dedicated_write_task(loop, coro):
    """Spawn coro on `loop` and pin a strong ref until it completes.

    Mirrors the canonical asyncio "save the task to a set; remove on done"
    pattern. The `name=` kwarg is preserved so a stray crash log still
    identifies the call site as `coord_failure_dedicated_table_write`.
    """
    task = loop.create_task(coro, name="coord_failure_dedicated_table_write")
    _inflight_dedicated_writes.add(task)
    task.add_done_callback(_inflight_dedicated_writes.discard)


async def _emit_to_coordination_events_async(
    *,
    service: str,
    event_type: str,
    payload: dict[str, Any],
    agent_id: str | None,
) -> None:
    """Write a coordination event into audit.coordination_events.

    Failure-safe: WARNING-level log on failure, never raises. The audit.events
    row was already committed via the sync path before this coroutine ran;
    losing the dedicated-table write costs only the replay surface, not
    durability.
    """
    try:
        import asyncpg

        from src.coordination_events import emit_event
        from src.db import get_db

        db = get_db()
        # Match audit_db.append_audit_event_async's lazy-init dance. Without
        # this, the very first emit after process boot races the pool warmup.
        if not hasattr(db, "_pool") or getattr(db, "_pool", None) is None:
            try:
                await db.init()
            except Exception:  # noqa: BLE001
                # init() failures land on the outer handler below; the
                # explicit catch here just prevents partial init from
                # masking the original error class.
                raise
        pool = getattr(db, "_pool", None)
        # `isinstance(pool, asyncpg.Pool)` is the right gate. Without it,
        # the autouse `_isolate_db_backend` fixture leaves `_pool` as an
        # AsyncMock auto-attribute, and `async with pool.acquire()` inside
        # emit_event falls through into AsyncMock's coroutine-returning
        # call protocol, leaving an unawaited AsyncMockMixin coroutine that
        # the pytest_warning_recorded hook (per memory
        # feedback_pytest-unawaited-coroutine) flags as a leak in any test
        # that incidentally triggers an emit. The leak fires SILENTLY in
        # production absent this check too — async with would raise
        # TypeError on a non-real pool — so the isinstance gate is correct
        # production posture, not just test ergonomics.
        if not isinstance(pool, asyncpg.Pool):
            logger.debug(
                "[coord-failure-emit] dedicated-table write skipped: "
                "pool is not asyncpg.Pool (got %s)",
                type(pool).__name__,
            )
            return

        await emit_event(
            pool,
            service=service,  # type: ignore[arg-type]  # validated against SERVICES upstream
            event_type=event_type,
            payload=payload,
            agent_id=agent_id,
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
        logger.warning(
            "[coord-failure-emit] dedicated-table write failed (non-fatal): %r",
            exc,
        )
