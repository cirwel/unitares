"""Pause TTL — auto-expire stale paused states across all gate-traversal sites.

A paused agent's status persists in `mcp_server.agent_metadata` until an
explicit `self_recovery` call. Sleep-wake artifacts (Mac clamshell, host
suspend, Pi power blip) can trigger a categorizer-driven pause whose
underlying "stale state input" cause has long since resolved, but the
pause itself persists indefinitely because every subsequent
gate-traversal is rejected before the categorizer can re-evaluate.

This module provides a TTL: pauses older than
`GovernanceConfig.PAUSE_AUTO_EXPIRE_SECONDS` auto-clear on the next
gate-traversal, letting the categorizer re-evaluate. A genuinely
degraded agent re-pauses on the next cycle via the normal
circuit-breaker path. A sleep-wake artifact resumes clean.

All callers share `_pause_is_stale` for the decision and
`_apply_in_memory_expire` for the mutation, so behavior is identical
across all the pause gates in the system. Audit visibility via
`audit_logger.log_pause_auto_expired` happens in both sync and async
entry points.

The async entry point awaits persistence; the sync entry point
fire-and-forget schedules it on the running loop (or AuditLogger's
captured main loop) using the same pattern as
`src/coordination_failure_emit.py:_schedule_coordination_events_dual_write`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


# ─── Stale-pause detection ─────────────────────────────────────────────


def _pause_is_stale(paused_at: Optional[str]) -> bool:
    """Return True if the recorded pause is older than the TTL.

    `paused_at` is an ISO 8601 string per `AgentMetadata`. Unparseable
    or absent values are treated as not-stale (fail-closed: keep the
    pause rather than auto-clear on bad data).

    Naive timestamps are interpreted as UTC. Both the canonical write
    path (`src/agent_loop_detection.py:514` via
    `datetime.now(timezone.utc).isoformat()`) and legacy persisted
    naive forms compare correctly under this convention.
    """
    if not paused_at:
        return False
    try:
        from config.governance_config import GovernanceConfig
        threshold_s = int(GovernanceConfig.PAUSE_AUTO_EXPIRE_SECONDS)
        paused_dt = datetime.fromisoformat(paused_at.replace("Z", "+00:00"))
        if paused_dt.tzinfo is None:
            # Legacy naive form — interpret as UTC, matching the
            # convention used elsewhere when serializing timestamps.
            paused_dt = paused_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - paused_dt).total_seconds() > threshold_s
    except (ValueError, TypeError, AttributeError, ImportError):
        return False


def _apply_in_memory_expire(meta: Any) -> Optional[str]:
    """Flip status and clear paused_at in-memory; record a lifecycle event.

    Returns the original paused_at string for use by callers that need
    to log it. Preserves the system invariant that
    `status == "paused" ⟺ paused_at is truthy` (asserted at
    `src/agent_metadata_model.py:260` and read at
    `src/auto_ground_truth.py:285`).
    """
    original_paused_at = meta.paused_at
    meta.status = "active"
    meta.paused_at = None
    meta.add_lifecycle_event(
        "pause_auto_expired",
        f"pause from {original_paused_at} aged out beyond "
        f"PAUSE_AUTO_EXPIRE_SECONDS; categorizer re-evaluation triggered "
        f"on this gate-traversal",
    )
    return original_paused_at


def _emit_audit(agent_uuid: str, original_paused_at: Optional[str]) -> None:
    """Fire-and-forget audit emit. Never raises."""
    try:
        from src.audit_log import audit_logger
        elapsed_s: float = 0.0
        if original_paused_at:
            try:
                paused_dt = datetime.fromisoformat(
                    original_paused_at.replace("Z", "+00:00")
                )
                if paused_dt.tzinfo is None:
                    paused_dt = paused_dt.replace(tzinfo=timezone.utc)
                elapsed_s = (
                    datetime.now(timezone.utc) - paused_dt
                ).total_seconds()
            except (ValueError, TypeError):
                pass
        audit_logger.log_pause_auto_expired(
            agent_id=agent_uuid,
            original_paused_at=original_paused_at,
            elapsed_seconds=elapsed_s,
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask
        logger.debug(
            "[pause-ttl] audit emit failed for %s: %r", agent_uuid[:12], exc
        )


# ─── Async entry point (process_agent_update path) ─────────────────────


async def maybe_auto_expire_pause_async(agent_uuid: str, meta: Any) -> bool:
    """Auto-expire a stale pause; await persistence. Returns True if expired.

    Callers should invoke this BEFORE returning the AGENT_PAUSED error
    response. If it returns True, the gate should fall through to
    normal processing (the agent is no longer paused). If False, the
    pause is not stale and the gate should reject as before.
    """
    if not _pause_is_stale(meta.paused_at):
        return False

    original_paused_at = _apply_in_memory_expire(meta)
    logger.warning(
        "[pause-ttl] auto-expired stale pause for %s (paused_at=%s); "
        "categorizer will re-evaluate",
        agent_uuid[:12],
        original_paused_at,
    )
    _emit_audit(agent_uuid, original_paused_at)

    try:
        from src import agent_storage
        await agent_storage.persist_runtime_state(
            agent_uuid,
            paused_at=None,
            append_lifecycle_event={
                "event": "pause_auto_expired",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": f"pause from {original_paused_at} aged out",
            },
        )
    except Exception as exc:  # noqa: BLE001 — persistence is non-fatal
        # In-memory flip already lets this gate through. If persistence
        # fails, the next load_metadata_async(force=True) may re-hydrate
        # paused_at; this code path will fire again on the next stale
        # check-in, producing one extra audit row per check-in until
        # persistence succeeds.
        logger.warning(
            "[pause-ttl] persist_runtime_state(pause_auto_expired) failed "
            "for %s: %r",
            agent_uuid[:12],
            exc,
        )
    return True


# ─── Sync entry point (check_agent_can_operate path) ───────────────────


# Module-local strong-ref set for in-flight fire-and-forget persistence
# tasks. Mirrors the pattern in `src/coordination_failure_emit.py`: we
# can't rely on the background-tasks supervisor because callers of this
# helper are themselves invoked from a wide variety of contexts (sync
# tools, REST endpoints, MCP handlers).
_inflight_persistence_tasks: "set[asyncio.Task]" = set()


def _spawn_persistence_task(loop: asyncio.AbstractEventLoop, coro: Any) -> None:
    """Spawn coro on `loop` and pin a strong ref until it completes."""
    task = loop.create_task(coro, name="pause_ttl_fire_and_forget_persist")
    _inflight_persistence_tasks.add(task)
    task.add_done_callback(_inflight_persistence_tasks.discard)


def _schedule_persistence_fire_and_forget(
    agent_uuid: str, original_paused_at: Optional[str]
) -> None:
    """Schedule the persistence coroutine on the best-available loop.

    Mirrors `src/coordination_failure_emit.py:_schedule_coordination_events_dual_write`.
    """
    try:
        from src import agent_storage

        def _coro_factory():
            return agent_storage.persist_runtime_state(
                agent_uuid,
                paused_at=None,
                append_lifecycle_event={
                    "event": "pause_auto_expired",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": f"pause from {original_paused_at} aged out "
                              f"(sync path)",
                },
            )

        try:
            loop = asyncio.get_running_loop()
            _spawn_persistence_task(loop, _coro_factory())
            return
        except RuntimeError:
            pass

        captured_loop = None
        try:
            from src.audit_log import AuditLogger
            captured_loop = getattr(AuditLogger, "_event_loop", None)
        except Exception:  # noqa: BLE001
            captured_loop = None

        if captured_loop is not None and captured_loop.is_running():
            def _spawn_on_main():
                _spawn_persistence_task(captured_loop, _coro_factory())
            captured_loop.call_soon_threadsafe(_spawn_on_main)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[pause-ttl] persistence schedule failed for %s: %r",
            agent_uuid[:12],
            exc,
        )


def maybe_auto_expire_pause_sync(agent_uuid: str, meta: Any) -> bool:
    """Sync variant: in-memory flip is synchronous, persistence is scheduled.

    Returns True if the pause was expired; False if it should still
    block. Callers should check the return value before returning the
    AGENT_PAUSED error.
    """
    if not _pause_is_stale(meta.paused_at):
        return False

    original_paused_at = _apply_in_memory_expire(meta)
    logger.warning(
        "[pause-ttl] auto-expired stale pause for %s (paused_at=%s); "
        "categorizer will re-evaluate (sync path)",
        agent_uuid[:12],
        original_paused_at,
    )
    _emit_audit(agent_uuid, original_paused_at)
    _schedule_persistence_fire_and_forget(agent_uuid, original_paused_at)
    return True
