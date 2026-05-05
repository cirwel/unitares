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

PR #342's `audit.coordination_events` table + async emitter remain on a
separate open PR for FUTURE Wave 0 step 3 work if a separate replay surface
becomes necessary. For Wave 0 step 2A — wire the smallest meaningful
chokepoint (the @mcp_tool decorator's TimeoutError handler) — `audit.events`
with a namespaced `event_type` is enough.

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
