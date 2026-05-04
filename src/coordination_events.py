"""Wave 0 coordination-event emitter (RFC docs/proposals/beam-footprint-roadmap-v0.md).

Single-surface replay log for coordination-class failures across all services
(sentinel, governance_mcp, lease_plane, vigil, chronicler, watcher). Wave 1's
exit criterion (incident-rate trends, the "stop or proceed to BEAM port"
signal) reads from `audit.coordination_events`.

Wave 0 lands the foundation:
  - Migration 035 (the table)
  - This module (the emitter)
  - Tests pinning the envelope contract

Wave 0 step 2 wires actual call sites (asyncpg connect errors, anyio task-
group cancellations, executor pool exhaustion, MCP handler timeouts). Wave 0
step 3 lands the Chronicler projection. Wave 0 step 4 lands the dashboard panel.

Stability discipline: `event_type` extends ONLY by adding new dotted
namespaces (coordination_recovery.*, coordination_lifecycle.*, ...). Never
reuse or rename an existing event_type. The migration's CHECK constraint
enforces the regex `^(coordination_failure)\\.[a-z_]+$` today; new families
extend the alternation in a follow-up migration, never silently in caller code.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# Service enum mirrors migration 035's coordination_events_service_check.
# Drift caught by test_emit_rejects_unknown_service.
Service = Literal[
    "sentinel",
    "governance_mcp",
    "lease_plane",
    "vigil",
    "chronicler",
    "watcher",
]

# Wave 0 event_type values. Future families extend the regex in a follow-up
# migration AND add their values here in the same PR. Drift caught by
# test_event_type_constants_match_documented_set.
COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR = "coordination_failure.asyncpg_connect_error"
COORDINATION_FAILURE_ANYIO_CANCELLATION = "coordination_failure.anyio_cancellation"
COORDINATION_FAILURE_EXECUTOR_POOL_EXHAUSTION = "coordination_failure.executor_pool_exhaustion"
COORDINATION_FAILURE_MCP_HANDLER_TIMEOUT = "coordination_failure.mcp_handler_timeout"

WAVE_0_EVENT_TYPES: frozenset[str] = frozenset({
    COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR,
    COORDINATION_FAILURE_ANYIO_CANCELLATION,
    COORDINATION_FAILURE_EXECUTOR_POOL_EXHAUSTION,
    COORDINATION_FAILURE_MCP_HANDLER_TIMEOUT,
})

# Cached emitter context — git_commit and host don't change at runtime.
# service_pid and running_since are per-process; the rest are per-host.
_CONTEXT_CACHE: dict[str, Any] | None = None


def _git_commit_short() -> str:
    """Return the short git SHA of the running deploy, or 'unknown' on failure.

    Captured once at first emit and cached. The captured value is the SHA at
    process startup — long-running services that survive across deploys will
    still report their original startup SHA, which is the correct attribution
    semantics (the running binary IS that SHA, regardless of what's checked
    out on disk now). Per memory `feedback_running-process-vs-master-commit`.
    """
    try:
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[coord-events] git_commit lookup failed: %r", exc)
    return "unknown"


def _build_context() -> dict[str, Any]:
    """Build the emitter context envelope. Cached after first call.

    Per the roadmap envelope spec: `{git_commit, service_pid, running_since, host}`.
    These are facts about the emitter, not the event — callers don't pass this.
    """
    global _CONTEXT_CACHE
    if _CONTEXT_CACHE is not None:
        return _CONTEXT_CACHE

    _CONTEXT_CACHE = {
        "git_commit": _git_commit_short(),
        "service_pid": os.getpid(),
        "running_since": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
    }
    return _CONTEXT_CACHE


def _validate_event_type(event_type: str) -> None:
    """Mirror of migration 035's regex CHECK on event_type, raised client-side
    so callers get a precise typed error before the DB rejects."""
    if not isinstance(event_type, str):
        raise ValueError(f"event_type must be a string, got {type(event_type).__name__}")
    parts = event_type.split(".")
    if len(parts) != 2:
        raise ValueError(
            f"event_type {event_type!r} must be 'family.subtype' (RFC roadmap §94)"
        )
    family, subtype = parts
    if family != "coordination_failure":
        raise ValueError(
            f"event_type {event_type!r}: family {family!r} not in Wave 0 set "
            f"({{'coordination_failure'}}). Add via migration when extending."
        )
    if not subtype or not all(c.islower() or c == "_" for c in subtype):
        raise ValueError(
            f"event_type {event_type!r}: subtype must be lowercase + underscores"
        )


async def emit_event(
    pool,
    *,
    service: Service,
    event_type: str,
    payload: dict[str, Any] | None = None,
    agent_id: str | None = None,
    ts: datetime | None = None,
) -> UUID:
    """Emit a coordination event into `audit.coordination_events`.

    Args:
        pool: an asyncpg connection pool. Caller-supplied so this module
              doesn't take a hard dependency on a specific pool helper.
        service: emitter identity, must be in the migration-035 service enum.
        event_type: dotted family.subtype, validated client-side here AND
                    server-side by the namespace CHECK constraint.
        payload: event-type-specific structure. MUST be a dict (mirrors the
                 jsonb_typeof = 'object' CHECK). Empty dict is valid.
        agent_id: optional UNITARES UUID when the event is agent-attributable.
        ts: optional override; defaults to now() in UTC. Pass-through for
            tests and for events captured asynchronously where the emit
            happens later than the event itself.

    Returns the event_id UUID (server-generated; useful for tests and replay).

    Failure semantics: this is observability infrastructure for OTHER bugs.
    A failure to emit MUST NOT crash the caller — but the caller is also
    not expected to wrap this in try/except. Errors here are logged at
    WARNING and re-raised so that test infrastructure can catch them; the
    individual call-site wrapping (Wave 0 step 2) controls swallow vs
    propagate per-site based on whether the emitter caller can afford to
    fail (most can't — they're already in an error path).
    """
    _validate_event_type(event_type)
    if payload is not None and not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")

    event_id = uuid4()
    effective_ts = ts or datetime.now(UTC)
    context = _build_context()
    payload_to_write = payload or {}

    sql = """
    INSERT INTO audit.coordination_events
        (ts, event_id, service, event_type, agent_id, payload, context)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
    """

    import json
    async with pool.acquire() as conn:
        await conn.execute(
            sql,
            effective_ts,
            event_id,
            service,
            event_type,
            agent_id,
            json.dumps(payload_to_write),
            json.dumps(context),
        )

    logger.debug(
        "[coord-events] emitted %s/%s event_id=%s agent_id=%s",
        service,
        event_type,
        event_id,
        agent_id,
    )
    return event_id


def reset_context_cache_for_tests() -> None:
    """Clear the cached context so tests can re-derive without process reload."""
    global _CONTEXT_CACHE
    _CONTEXT_CACHE = None
