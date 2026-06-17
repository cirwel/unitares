"""
Shared helpers for lifecycle handler modules.

Private utilities used across query, mutation, and operations modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from mcp.types import TextContent
from src import agent_storage
from src.logging_utils import get_logger
from src.cache import get_metadata_cache
from src.agent_metadata_model import AgentMetadata
from ..utils import error_response

logger = get_logger(__name__)


def clear_loop_detector_state(meta) -> None:
    """Clear loop-detector fields after successful recovery/resume."""
    meta.loop_cooldown_until = None
    meta.loop_detected_at = None
    meta.recent_update_timestamps = []
    meta.recent_decisions = []


# Spawn reasons that represent an intentional, causal lineage edge — as opposed
# to the noisy co-location ``new_session`` default that the SessionStart nudge
# mints between unrelated same-workspace sessions. Mirrors the taxonomy in
# docs/proposals/lineage-causal-only-semantics.md (PR #721): a child declaring
# one of these attests a real dependency, so its parent is something an operator
# probably does not mean to sweep in a bulk "archive everyone" pass.
_CAUSAL_SPAWN_REASONS = frozenset({"subagent", "compaction", "explicit", "dispatch"})


async def manual_archive_liveness_signals(
    agent_uuid: str,
    meta: AgentMetadata,
) -> list[str]:
    """Return human-readable reasons the target looks live / intentionally kept.

    An empty list means "safe to archive without force". Used by the manual
    ``archive_agent`` path to refuse silent archival of an agent that is
    plainly still in use, so a bulk "archive everyone" sweep can't strand a
    running workflow (2026-06-14 council-agent incident). Best-effort and
    fail-open: any signal lookup that errors is simply omitted.

    Two orthogonal signals, deliberately matching the posture of the
    auto-archival guards in PR #720/#721:
      1. A live process binding — the conceptually-correct "running right now"
         signal. (Recent ``last_update`` is intentionally NOT used: a check-in
         minutes ago does not mean the process is still running, and gating on
         it would block the routine archive-the-agent-I-just-looked-at flow.)
      2. A declared *causal* lineage edge — the agent is a parent/successor in
         an intentional chain (subagent/dispatch/explicit/compaction), not a
         coincidental ``new_session`` co-location edge.
    """
    signals: list[str] = []

    try:
        from src.mcp_handlers.identity.process_binding import (
            get_live_bindings,
            has_live_agent_lease,
        )
        bindings = await get_live_bindings(agent_uuid)
        if bindings:
            signals.append(f"{len(bindings)} live process binding(s)")
        # Lease-plane presence — the liveness signal for ephemeral agents that
        # binding-liveness is structurally blind to (see has_live_agent_lease).
        if await has_live_agent_lease(agent_uuid):
            signals.append("live agent:/ lease-plane presence lease")
    except Exception as e:  # pragma: no cover - defensive, fail-open
        logger.debug(f"manual_archive liveness binding check failed: {e}")

    parent = getattr(meta, "parent_agent_id", None)
    spawn = (getattr(meta, "spawn_reason", None) or "").lower()
    if parent and spawn in _CAUSAL_SPAWN_REASONS:
        signals.append(f"declared lineage (spawn_reason={spawn})")

    return signals


async def _resume_with_persistence(
    meta,
    *,
    agent_uuid: str,
    event_name: str,
    reason: str,
    error_response_id: str,
    error_action: str,
    cache_agent_id: Optional[str] = None,
    details_key: str = "agent_id",
    storage_module=agent_storage,
) -> Optional[Sequence[TextContent]]:
    """Apply the canonical persist-first resume pattern.

    Returns an error response sequence on persistence failure, or None on
    success. In-memory metadata is mutated only after PostgreSQL writes and
    cache invalidation complete, preventing the P011 DB/memory drift class from
    reappearing across resume handlers.
    """
    event_entry = {
        "event": event_name,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await storage_module.update_agent(agent_uuid, status="active")
        await storage_module.persist_runtime_state(
            agent_uuid,
            paused_at=None,
            loop_detected_at=None,
            loop_cooldown_until=None,
            append_lifecycle_event=event_entry,
        )
        await _invalidate_agent_cache(cache_agent_id or agent_uuid)
    except Exception as e:
        logger.warning(
            "PostgreSQL update failed for %s: %s",
            error_action,
            e,
            exc_info=True,
        )
        return [error_response(
            f"Failed to {error_action} agent '{error_response_id}': persistence error",
            error_code="PERSIST_FAILED",
            error_category="system_error",
            details={details_key: error_response_id, "cause": str(e)},
        )]

    meta.status = "active"
    meta.paused_at = None
    clear_loop_detector_state(meta)
    meta.add_lifecycle_event(event_name, reason)
    return None


async def _archive_one_agent(
    agent_id: str,
    meta: AgentMetadata,
    reason: str,
    *,
    monitors: dict | None = None,
) -> bool:
    """Persist archival to Postgres, then mutate in-memory state.

    Persist-first: if the DB write fails the in-memory state stays
    unchanged and we return False so the caller can skip the agent.
    This prevents the desync where in-memory says "archived" but
    Postgres still says "active" (P011).
    """
    archived_at = datetime.now(timezone.utc).isoformat()
    try:
        await agent_storage.archive_agent(
            agent_id,
            archived_at=archived_at,
            lifecycle_event=reason,
        )
    except Exception as e:
        logger.warning(
            "Could not persist archival: %s",
            type(e).__name__,
        )
        return False

    meta.status = "archived"
    meta.archived_at = archived_at
    meta.add_lifecycle_event("archived", reason)
    if monitors is not None and agent_id in monitors:
        del monitors[agent_id]
    return True


async def _invalidate_agent_cache(agent_id: str) -> None:
    """Invalidate Redis metadata cache for an agent. Best-effort, never raises."""
    try:
        await get_metadata_cache().invalidate(agent_id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed: {e}")

def _is_test_agent(agent_id: str, label: str | None = None) -> bool:
    """Identify test/demo agents by naming patterns.

    Checks both agent_id and label. CLI-spawned pytest agents use UUIDs
    as agent_id but contain 'cli-pytest' in the label; integration-test
    agents from plugin suites use UUIDs and 'itest-*' labels.

    Used consistently across list_agents handlers to filter test agents.
    """
    agent_id_lower = agent_id.lower()
    if (
        agent_id.startswith("test_") or
        agent_id.startswith("demo_") or
        agent_id.startswith("test") or
        "test" in agent_id_lower or
        "demo" in agent_id_lower
    ):
        return True
    if label:
        label_lower = label.lower()
        if (
            label_lower.startswith("cli-pytest") or
            label_lower.startswith("test_") or
            label_lower.startswith("test-") or
            label_lower.startswith("itest-") or
            label_lower.startswith("itest_") or
            "pytest" in label_lower or
            "itest" in label_lower.split("-") or
            "itest" in label_lower.split("_") or
            "test" in label_lower.split("-") or
            "test" in label_lower.split("_")
        ):
            return True
    return False
