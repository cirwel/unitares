"""
Lifecycle operational handlers — resume, ping, response completion, archive cleanup,
and self-recovery review.

Extracted from handlers.py for maintainability.
"""

from typing import Dict, Any, Sequence
from mcp.types import TextContent
from datetime import datetime, timedelta, timezone

from src import agent_storage
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from ..utils import (
    require_registered_agent,
    success_response,
    error_response,
)
from ..error_helpers import (
    agent_not_found_error,
    ownership_error,
)
from ..decorators import mcp_tool
from ..support.coerce import safe_float, resolve_agent_uuid
from src.logging_utils import get_logger
from config.governance_config import GovernanceConfig

from .helpers import (
    _archive_one_agent,
    _is_test_agent,
    _resume_with_persistence,
)

logger = get_logger(__name__)



@mcp_tool("resume_agent", timeout=15.0, register=False)
async def handle_resume_agent(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Resume a paused/stuck agent from the dashboard.

    Lightweight resume handler for human operators (dashboard).
    No ownership check -- mirrors archive_agent pattern.
    Only resumes agents in paused or waiting_input status.
    """
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # Wave 2 audit: force=True dropped per PR #350 precedent. Pre-mutation
    # existence + status check; in-memory cache is fresh enough.
    await mcp_server.load_metadata_async()

    if agent_uuid not in mcp_server.agent_metadata:
        return agent_not_found_error(agent_id)

    meta = mcp_server.agent_metadata[agent_uuid]

    # Allow resuming paused/waiting_input agents AND "unsticking" active agents
    # Stuck agents are typically still "active" but caught in a timeout/loop
    is_stuck_unstick = meta.status == "active" and arguments.get("unstick", False)
    if meta.status not in ("paused", "waiting_input") and not is_stuck_unstick:
        return [error_response(
            f"Agent '{agent_id}' is '{meta.status}', not resumable (must be paused or waiting_input)",
            error_code="AGENT_NOT_RESUMABLE",
            error_category="validation_error",
            details={"error_type": "agent_not_resumable", "agent_id": agent_id, "status": meta.status},
            recovery={
                "action": "Agent must be in paused or waiting_input status to resume",
                "related_tools": ["get_agent_metadata", "list_agents"],
                "workflow": ["1. Check agent status with get_agent_metadata", "2. Only paused/waiting_input agents can be resumed"]
            }
        )]

    reason = arguments.get("reason", "Resumed from dashboard")
    previous_status = meta.status
    event_name = "resumed" if not is_stuck_unstick else "unstuck"
    persist_error = await _resume_with_persistence(
        meta,
        agent_uuid=agent_uuid,
        event_name=event_name,
        reason=reason,
        error_response_id=agent_id,
        error_action="resume",
        cache_agent_id=agent_id,
        storage_module=agent_storage,
    )
    if persist_error:
        return persist_error
    logger.debug(
        "PostgreSQL: %s agent",
        "Unstuck" if is_stuck_unstick else "Resumed",
    )

    response_payload = {
        "success": True,
        "message": f"Agent '{agent_id}' resumed successfully",
        "agent_id": agent_id,
        "lifecycle_status": "active",
        "previous_status": previous_status,
        "reason": reason,
        "resumed_at": datetime.now(timezone.utc).isoformat()
    }
    return success_response(response_payload)

@mcp_tool("mark_response_complete", timeout=5.0, register=False)
async def handle_mark_response_complete(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Mark agent as having completed response, waiting for input"""
    # SECURITY FIX: Require registered agent (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    # Use authoritative UUID for internal lookups
    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # SECURITY: Verify ownership via session binding (UUID-based auth, Dec 2025)
    from ..utils import verify_agent_ownership
    if not verify_agent_ownership(agent_uuid, arguments):
        from ..identity.shared import get_bound_agent_id
        caller_id = get_bound_agent_id(arguments) or "unknown"
        return ownership_error(
            resource_type="agent_response",
            resource_id=agent_uuid,
            owner_agent_id=agent_uuid,
            caller_agent_id=caller_id,
        )

    meta = mcp_server.agent_metadata.get(agent_uuid)
    summary = arguments.get("summary", "")
    now_iso = datetime.now(timezone.utc).isoformat()
    event_details = summary if summary else "Response completed, waiting for input"
    event_entry = {
        "event": "response_completed",
        "reason": event_details,
        "timestamp": now_iso,
    }

    # Persist FIRST so in-memory state can't diverge from DB on persist failure.
    # Runtime state (last_response_at, response_completed, lifecycle_events)
    # must be persisted alongside status or it gets clobbered on force-reload.
    try:
        await agent_storage.update_agent(agent_uuid, status="waiting_input")
        await agent_storage.persist_runtime_state(
            agent_uuid,
            last_response_at=now_iso,
            response_completed=True,
            append_lifecycle_event=event_entry,
        )
    except Exception as e:
        logger.warning(f"PostgreSQL status update failed: {e}", exc_info=True)
        return [error_response(
            f"Failed to mark response complete for '{agent_id}': persistence error",
            error_code="PERSIST_FAILED",
            error_category="system_error",
            details={"agent_id": agent_id, "cause": str(e)},
        )]

    # Persist succeeded — now mutate in-memory state.
    meta.status = "waiting_input"
    meta.last_response_at = now_iso
    meta.response_completed = True
    meta.add_lifecycle_event("response_completed", event_details)

    # MAINTENANCE PROMPT: Surface open discoveries from this session
    # Behavioral nudge: Remind agent to resolve discoveries before ending session
    open_discoveries = []
    try:
        from src.knowledge_graph import get_knowledge_graph
        # Note: datetime and timedelta already imported at module level

        graph = await get_knowledge_graph()

        # Get open discoveries from this agent (recent - last 24 hours)
        now = datetime.now()
        one_day_ago = (now - timedelta(hours=24)).isoformat()

        all_agent_discoveries = await graph.query(
            agent_id=agent_id,
            status="open",
            limit=20  # Get recent discoveries
        )

        # Filter to recent discoveries (last 24 hours)
        recent_open = [
            d for d in all_agent_discoveries
            if d.timestamp >= one_day_ago
        ]

        # Prioritize bug_found and high severity
        recent_open.sort(key=lambda d: (
            0 if d.type == "bug_found" else 1,  # Bugs first
            0 if d.severity == "high" else 1 if d.severity == "medium" else 2,  # High severity first
            d.timestamp  # Then by recency
        ))

        open_discoveries = recent_open[:5]  # Top 5 for prompt

    except Exception as e:
        # Don't fail if knowledge graph check fails - this is a nice-to-have prompt
        logger.warning(f"Could not check open discoveries: {e}")

    response_data = {
        "success": True,
        "message": "Response completion marked",
        "agent_id": agent_id,
        "status": "waiting_input",
        "last_response_at": meta.last_response_at,
        "response_completed": True
    }

    # Add maintenance prompt if there are open discoveries
    if open_discoveries:
        response_data["maintenance_prompt"] = {
            "message": f"You have {len(open_discoveries)} open discovery/discoveries from this session. Consider resolving them:",
            "open_discoveries": [
                {
                    "id": d.id,
                    "summary": d.summary,
                    "type": d.type,
                    "severity": d.severity,
                    "timestamp": d.timestamp
                }
                for d in open_discoveries
            ],
            "suggested_actions": [
                "Mark as resolved: update_discovery_status_graph(discovery_id='...', status='resolved')",
                "Add correction: store_knowledge_graph(response_to={discovery_id='...', response_type='correction'}, ...)",
                "Archive if obsolete: update_discovery_status_graph(discovery_id='...', status='archived')"
            ],
            "related_tools": [
                "update_discovery_status_graph",
                "store_knowledge_graph",
                "search_knowledge_graph"
            ],
            "tip": "Resolving discoveries helps maintain knowledge graph quality. Use response_to for corrections or additions."
        }

    return success_response(response_data)

@mcp_tool("self_recovery_review", timeout=15.0, register=False)  # Use self_recovery(action="review") instead
async def handle_self_recovery_review(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Self-reflection recovery - lightweight alternative to dialectic.

    Agent reflects on what went wrong and proposes recovery conditions.
    System validates safety and resumes if safe, or provides guidance if not.

    This replaces the heavyweight thesis->antithesis->synthesis dialectic
    with a simpler: reflect -> validate -> resume flow.

    Required:
        reflection: str - What went wrong and what you learned (minimum 20 characters)

    Optional:
        proposed_conditions: list[str] - Conditions for resuming (e.g., "reduce complexity", "take breaks")
        root_cause: str - Agent's understanding of root cause
    """

    # 1. Require registered agent
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # 2. Verify ownership (can only recover yourself)
    from ..utils import verify_agent_ownership
    if not verify_agent_ownership(agent_uuid, arguments):
        from ..identity.shared import get_bound_agent_id
        caller_id = get_bound_agent_id(arguments) or "unknown"
        return ownership_error(
            resource_type="agent_recovery",
            resource_id=agent_uuid,
            owner_agent_id=agent_uuid,
            caller_agent_id=caller_id,
        )

    # 3. Get reflection (required)
    reflection = arguments.get("reflection", "").strip()
    if not reflection or len(reflection) < 20:
        return [error_response(
            "Reflection required. Please describe what happened and what you learned. "
            "Minimum 20 characters - genuine reflection helps recovery.",
            error_code="REFLECTION_REQUIRED",
            recovery={
                "action": "Provide a meaningful reflection on what went wrong",
                "example": "self_recovery(action='review', reflection='I got stuck in a loop trying to optimize the same function repeatedly. I should have stepped back and considered alternative approaches.')"
            }
        )]

    # 4. Get current metrics
    meta = mcp_server.agent_metadata.get(agent_uuid)
    if not meta:
        return agent_not_found_error(agent_id)

    # Mark recovery attempt before safety checks so loop detector grants a 120s
    # grace period even if this review attempt fails (agent not yet safe to resume).
    # Persist first — agent_loop_detection reads meta.recovery_attempt_at, and any
    # force-reload in the interim would clobber an in-memory-only mutation.
    from datetime import timezone as _tz
    recovery_ts = datetime.now(_tz.utc).isoformat()
    try:
        await agent_storage.persist_runtime_state(agent_uuid, recovery_attempt_at=recovery_ts)
    except Exception as e:
        logger.warning(f"persist_runtime_state(recovery_attempt_at) failed: {e}")
    meta.recovery_attempt_at = recovery_ts

    monitor = mcp_server.get_or_create_monitor(agent_uuid)
    from src.agent_monitor_state import ensure_hydrated
    await ensure_hydrated(monitor, agent_uuid)
    metrics = monitor.get_metrics()

    coherence = safe_float(monitor.state.coherence, 0.5)
    risk_score = safe_float(metrics.get("mean_risk"), 0.5)
    void_active = bool(monitor.state.void_active)
    void_value = safe_float(monitor.state.V, 0.0)
    status = meta.status

    # 5. Compute margin for context
    margin_info = GovernanceConfig.compute_proprioceptive_margin(
        risk_score=risk_score,
        coherence=coherence,
        void_active=void_active,
        void_value=void_value,
        coherence_history=monitor.state.coherence_history,
    )

    # 6. Safety validation
    proposed_conditions = arguments.get("proposed_conditions", [])
    root_cause = arguments.get("root_cause", "")

    # Check for dangerous conditions (same as dialectic hard limits)
    dangerous_patterns = [
        "disable", "bypass", "ignore safety", "remove monitoring",
        "skip governance", "override limits"
    ]
    conditions_text = " ".join(proposed_conditions).lower()
    for pattern in dangerous_patterns:
        if pattern in conditions_text:
            return [error_response(
                f"Proposed conditions contain dangerous pattern: '{pattern}'. "
                "Recovery conditions cannot disable safety systems.",
                error_code="UNSAFE_CONDITIONS"
            )]

    # 7. Determine if safe to resume
    safety_checks = {
        "coherence_ok": coherence > 0.35,  # Slightly more lenient than direct_resume
        "risk_ok": risk_score < 0.65,      # Slightly more lenient since reflecting
        "no_void": not void_active,
        "has_reflection": len(reflection) >= 20
    }

    all_safe = all(safety_checks.values())

    # 8. Log reflection to knowledge graph (always, even if not resuming)
    reflection_logged = False
    try:
        from ..knowledge.handlers import store_discovery_internal
        await store_discovery_internal(
            agent_id=agent_uuid,
            summary=f"Self-recovery reflection: {reflection[:100]}{'...' if len(reflection) > 100 else ''}",
            discovery_type="recovery_reflection",
            details=f"Reflection: {reflection}\n\nRoot cause: {root_cause}\n\nProposed conditions: {proposed_conditions}\n\nMetrics at reflection: coherence={coherence:.3f}, risk={risk_score:.3f}, void={void_value:.3f}",
            tags=["recovery", "self-reflection", margin_info.get('margin', 'unknown')],
            severity="info" if all_safe else "warning",
            source="self_recovery_reflection",
        )
        reflection_logged = True
    except Exception as e:
        logger.warning(f"Failed to log recovery reflection: {e}")

    # 9. Resume if safe, or provide guidance
    if all_safe:
        resume_reason = f"Self-recovery: {reflection[:50]}... Conditions: {proposed_conditions}"
        event_entry = {
            "event": "resumed",
            "reason": resume_reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # Persist FIRST so in-memory state can't diverge from DB on persist failure.
        # Runtime state (paused_at, lifecycle_events) must be persisted alongside
        # status or it gets clobbered on force-reload.
        try:
            await agent_storage.update_agent(agent_uuid, status="active")
            await agent_storage.persist_runtime_state(
                agent_uuid,
                paused_at=None,
                loop_detected_at=None,
                loop_cooldown_until=None,
                append_lifecycle_event=event_entry,
            )
        except Exception as e:
            logger.warning(f"PostgreSQL status update failed: {e}", exc_info=True)
            return [error_response(
                f"Failed to resume '{agent_id}' after self-recovery: persistence error",
                error_code="PERSIST_FAILED",
                error_category="system_error",
                details={"agent_id": agent_id, "cause": str(e), "reflection_logged": reflection_logged},
            )]

        # Persist succeeded — now mutate in-memory state.
        from .self_recovery import clear_loop_detector_state
        meta.status = "active"
        meta.paused_at = None
        clear_loop_detector_state(meta)
        meta.add_lifecycle_event("resumed", resume_reason)

        return success_response({
            "success": True,
            "action": "resumed",
            "message": "Recovery successful. Agent resumed.",
            "reflection_logged": reflection_logged,
            "conditions": proposed_conditions,
            "metrics": {
                "coherence": coherence,
                "risk_score": risk_score,
                "margin": margin_info.get('margin', 'unknown')
            },
            "guidance": "You've reflected and recovered. Consider your proposed conditions as you continue."
        })

    else:
        # Not safe to resume - provide specific guidance
        failed = [k for k, v in safety_checks.items() if not v]

        guidance = []
        if not safety_checks["coherence_ok"]:
            guidance.append(f"Coherence is low ({coherence:.3f}). Consider what's causing fragmentation in your approach.")
        if not safety_checks["risk_ok"]:
            guidance.append(f"Risk is elevated ({risk_score:.3f}). What could you do differently to reduce risk?")
        if not safety_checks["no_void"]:
            guidance.append("Void is active - there's accumulated E-I imbalance. This needs time to settle.")

        return success_response({
            "success": False,
            "action": "not_resumed",
            "message": "Reflection logged, but not yet safe to resume." if reflection_logged else "Not yet safe to resume (reflection failed to log).",
            "reflection_logged": reflection_logged,
            "failed_checks": failed,
            "metrics": {
                "coherence": coherence,
                "risk_score": risk_score,
                "void_active": void_active,
                "margin": margin_info.get('margin', 'unknown')
            },
            "guidance": guidance,
            "next_steps": [
                "Review the guidance above",
                "Add to your reflection if you have new insights",
                "Try again with self_recovery(action='review') when ready",
                "Or wait for metrics to improve naturally"
            ]
        })

@mcp_tool("ping_agent", timeout=5.0, register=False)
async def handle_ping_agent(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Ping an agent to check if it's responsive/alive.

    Checks if agent can respond by attempting to get its metrics.
    Useful for distinguishing between:
    - Agent is stuck but responsive (can call tools)
    - Agent is dead/unresponsive (can't call tools)

    Args:
        agent_id: Agent ID to ping (optional - defaults to calling agent)

    Returns:
        {
            "agent_id": "...",
            "responsive": true/false,
            "last_update": "...",
            "age_minutes": float,
            "status": "alive" | "stuck" | "unresponsive"
        }
    """
    try:
        # Reload metadata
        await mcp_server.load_metadata_async()

        # Get agent_id (default to calling agent)
        agent_id = arguments.get("agent_id")
        if not agent_id:
            # Use bound agent_id
            from ..utils import get_bound_agent_id
            agent_id = get_bound_agent_id(arguments)

        if not agent_id:
            return [error_response("agent_id required or must be bound to session")]

        # Check if agent exists
        meta = mcp_server.agent_metadata.get(agent_id)
        if not meta:
            return [error_response(f"Agent {agent_id} not found")]

        # Try to get agent's metrics (this is the "ping")
        responsive = False
        try:
            monitor = mcp_server.get_or_create_monitor(agent_id)
            from src.agent_monitor_state import ensure_hydrated
            await ensure_hydrated(monitor, agent_id)
            metrics = monitor.get_metrics()
            responsive = True  # If we can get metrics, agent is responsive
        except Exception as e:
            logger.debug(f"Could not ping agent {agent_id}: {e}")
            responsive = False

        # Calculate age
        try:
            last_update_str = meta.last_update or meta.created_at
            if isinstance(last_update_str, str):
                last_update_dt = datetime.fromisoformat(
                    last_update_str.replace('Z', '+00:00') if 'Z' in last_update_str else last_update_str
                )
                if last_update_dt.tzinfo is None:
                    last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
            else:
                last_update_dt = last_update_str

            age_delta = datetime.now(timezone.utc) - last_update_dt
            age_minutes = age_delta.total_seconds() / 60
        except (ValueError, TypeError, AttributeError):
            age_minutes = None

        # Determine status
        if responsive:
            if age_minutes and age_minutes > 30:
                status = "stuck"  # Responsive but inactive
            else:
                status = "alive"  # Responsive and active
        else:
            status = "unresponsive"  # Can't get metrics

        return success_response({
            "agent_id": agent_id,
            "responsive": responsive,
            "last_update": meta.last_update or meta.created_at,
            "age_minutes": round(age_minutes, 1) if age_minutes else None,
            "status": status,
            "lifecycle_status": meta.status
        })

    except Exception as e:
        logger.error(f"Error pinging agent: {e}", exc_info=True)
        return [error_response(f"Error pinging agent: {str(e)}")]

@mcp_tool("archive_old_test_agents", timeout=20.0, register=False)
async def handle_archive_old_test_agents(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Archive stale agents - test agents by default, or ALL stale agents with include_all=true

    Use include_all=true to clean up any agent inactive for max_age_days (default: 3 days)
    """
    from src.agent_lifecycle import _agent_age_hours

    max_age_hours = arguments.get("max_age_hours", 6)
    max_age_days = arguments.get("max_age_days")
    include_all = arguments.get("include_all", False)
    dry_run = arguments.get("dry_run", False)

    if include_all and max_age_days is None and "max_age_hours" not in arguments:
        max_age_days = 3
    if max_age_days is not None:
        max_age_hours = max_age_days * 24
    if max_age_hours < 0.1:
        return [error_response("max_age_hours must be at least 0.1 (6 minutes)")]

    # Wave 2 audit: force=True dropped per PR #350 precedent. Fleet-wide
    # archival sweep; in-memory cache is fresh enough for status filtering.
    await mcp_server.load_metadata_async()

    archived_agents = []

    for agent_id, meta in list(mcp_server.agent_metadata.items()):
        if meta.status in ("archived", "deleted"):
            continue

        label = getattr(meta, "label", None) or getattr(meta, "display_name", None) or ""
        is_test = _is_test_agent(agent_id, label)
        if not include_all and not is_test:
            continue

        # Immediate archive: test/ping agents with very low update count
        if meta.total_updates <= 2 and is_test:
            reason = f"Auto-archived: test/ping agent with {meta.total_updates} update(s)"
            if not dry_run:
                if not await _archive_one_agent(
                    agent_id, meta, reason, monitors=mcp_server.monitors,
                ):
                    continue
            archived_agents.append({"id": agent_id, "reason": "low_updates", "updates": meta.total_updates})
            continue

        # Age-based archive
        age_h = _agent_age_hours(meta)
        if age_h is None or age_h < max_age_hours:
            continue
        reason = f"Inactive for {age_h:.1f} hours (threshold: {max_age_hours} hours)"
        if not dry_run:
            if not await _archive_one_agent(
                agent_id, meta, reason, monitors=mcp_server.monitors,
            ):
                continue
        archived_agents.append({"id": agent_id, "reason": "stale", "days_inactive": round(age_h / 24, 1)})

    return success_response({
        "success": True,
        "dry_run": dry_run,
        "archived_count": len(archived_agents),
        "archived_agents": archived_agents[:20],
        "total_would_archive": len(archived_agents),
        "max_age_days": max_age_hours / 24,
        "include_all": include_all,
        "action": "preview - use dry_run=false to execute" if dry_run else "archived"
    })

@mcp_tool("archive_orphan_agents", timeout=30.0)
async def handle_archive_orphan_agents(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Aggressively archive orphan agents to prevent proliferation.

    Thin wrapper around the canonical orphan heuristic engine in
    ``agent_lifecycle.auto_archive_orphan_agents``.

    Parameters:
    - max_age_hours: Maximum inactivity before evaluation (default: 6h). Scales internal tiers.
    - max_updates: Skip agents with more updates than this (default: 3).
    - dry_run: Preview without archiving (default: true).
    """
    from src.agent_lifecycle import auto_archive_orphan_agents

    max_age_hours = float(arguments.get("max_age_hours", 6))
    max_updates = int(arguments.get("max_updates", 3))
    dry_run = arguments.get("dry_run", True)

    low_update_hours = min(max(max_age_hours / 2, 1.0), max_age_hours)
    unlabeled_hours = max_age_hours

    # Wave 2 audit: force=True dropped per PR #350 precedent. Fleet-wide
    # orphan archival sweep; in-memory cache is fresh enough for the
    # threshold-based classification.
    await mcp_server.load_metadata_async()

    # Initializing agents (0 updates) are never archived — see
    # classify_for_archival. The old tier-1 zero_update_hours sweep is gone.
    results = await auto_archive_orphan_agents(
        low_update_hours=low_update_hours,
        unlabeled_hours=unlabeled_hours,
        dry_run=dry_run,
        _metadata=mcp_server.agent_metadata,
        _monitors=mcp_server.monitors,
    )

    filtered = [r for r in results if r.get("updates", 0) <= max_updates]

    for r in filtered:
        if len(r.get("id", "")) > 12:
            r["id"] = r["id"][:12] + "..."

    return success_response({
        "success": True,
        "dry_run": dry_run,
        "archived_count": len(filtered),
        "archived_agents": filtered[:30],
        "total_would_archive": len(filtered),
        "thresholds": {
            "max_age_hours": max_age_hours,
            "max_updates": max_updates,
            "low_update_hours": low_update_hours,
            "unlabeled_hours": unlabeled_hours
        },
        "action": "preview - set dry_run=false to execute" if dry_run else "archived"
    })
