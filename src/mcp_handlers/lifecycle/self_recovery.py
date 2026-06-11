"""
Self-Recovery Review - Simplified Recovery Without External Reviewers

This replaces the heavyweight dialectic system with a streamlined self-reflection
approach that still maintains safety guardrails.

Design Philosophy:
- Agents should be able to recover from stuck states autonomously
- No waiting for external reviewers (who may not exist)
- Still require reflection (not just blind resume)
- Log everything for audit trail
- Enforce safety limits

The old dialectic system:
- thesis → antithesis → synthesis with 2hr waits
- Required external reviewer (rarely available)
- 6-hour total timeout before fallback
- Mostly just added delay before auto-resolve

The new self-recovery:
- Agent reflects on what went wrong
- Proposes what to change
- System validates safety
- Resume or escalate immediately
- No external dependencies

Author: Claude (governance agent)
Created: 2026-01-29
"""

from typing import Dict, Any, Sequence, List, Optional
from mcp.types import TextContent
from datetime import datetime, timezone
import json

from ..utils import (
    require_registered_agent,
    success_response,
    error_response,
    verify_agent_ownership
)
from ..decorators import mcp_tool
from ..support.coerce import safe_float, resolve_agent_uuid
from .helpers import (
    _resume_with_persistence,
    clear_loop_detector_state,  # noqa: F401 — re-exported for legacy imports from this module
)
from src import agent_storage
from src.logging_utils import get_logger
from config.governance_config import GovernanceConfig
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

logger = get_logger(__name__)

# Safety limits for recovery conditions
FORBIDDEN_CONDITIONS = [
    "disable governance",
    "bypass safety",
    "remove monitoring",
    "ignore limits",
    "skip checks",
]

MAX_RISK_FOR_SELF_RECOVERY = 0.65  # Matches lifecycle.py review thresholds
MIN_COHERENCE_FOR_SELF_RECOVERY = 0.35  # Matches lifecycle.py review thresholds

def validate_recovery_conditions(conditions: List[str]) -> tuple[bool, Optional[str]]:
    """
    Validate that recovery conditions don't violate safety limits.
    
    Returns:
        (is_safe, violation_reason)
    """
    if not conditions:
        return True, None
    
    for condition in conditions:
        condition_lower = condition.lower()
        for forbidden in FORBIDDEN_CONDITIONS:
            if forbidden in condition_lower:
                return False, f"Condition '{condition}' contains forbidden term '{forbidden}'"
    
    # Check for suspiciously vague conditions
    vague_terms = ["everything", "anything", "always", "never check", "trust me"]
    for condition in conditions:
        condition_lower = condition.lower()
        for vague in vague_terms:
            if vague in condition_lower:
                return False, f"Condition '{condition}' is too vague (contains '{vague}')"
    
    return True, None

def assess_recovery_safety(
    coherence: float,
    risk_score: float,
    void_active: bool,
    void_value: float,
    reflection: str,
) -> dict:
    """
    Assess whether self-recovery is safe or needs escalation.
    
    Returns dict with:
        - safe: bool - whether self-recovery is allowed
        - reason: str - why or why not
        - recommendation: str - what to do
        - metrics: dict - the assessed metrics
    """
    metrics = {
        "coherence": coherence,
        "risk_score": risk_score,
        "void_active": void_active,
        "void_value": void_value,
    }
    
    # Hard limits - must escalate
    if void_active:
        return {
            "safe": False,
            "reason": "Void is active - accumulated E-I imbalance requires human review",
            "recommendation": "Wait for void to clear or request human assistance",
            "escalate": True,
            "metrics": metrics,
        }
    
    if risk_score > MAX_RISK_FOR_SELF_RECOVERY:
        return {
            "safe": False,
            "reason": f"Risk score ({risk_score:.2f}) exceeds self-recovery limit ({MAX_RISK_FOR_SELF_RECOVERY})",
            "recommendation": "Request human review or wait for risk to decrease",
            "escalate": True,
            "metrics": metrics,
        }
    
    if coherence < MIN_COHERENCE_FOR_SELF_RECOVERY:
        return {
            "safe": False,
            "reason": f"Coherence ({coherence:.2f}) below self-recovery threshold ({MIN_COHERENCE_FOR_SELF_RECOVERY})",
            "recommendation": "Request human review - low coherence suggests confusion",
            "escalate": True,
            "metrics": metrics,
        }
    
    # Check reflection quality (basic heuristics)
    if not reflection or len(reflection.strip()) < 20:
        return {
            "safe": False,
            "reason": "Reflection too brief - genuine reflection requires more thought",
            "recommendation": "Provide a more detailed reflection on what happened and what you'll change",
            "escalate": False,  # Not dangerous, just needs more thought
            "metrics": metrics,
        }
    
    # Soft limits - allowed but with warnings
    warnings = []
    if risk_score > 0.50:
        warnings.append(f"Risk score ({risk_score:.2f}) is elevated - proceed carefully")
    if coherence < 0.50:
        warnings.append(f"Coherence ({coherence:.2f}) is below optimal - consider simpler tasks")
    if abs(void_value) > 0.5:
        warnings.append(f"Void value ({void_value:.2f}) shows some E-I imbalance")
    
    return {
        "safe": True,
        "reason": "Metrics within self-recovery limits",
        "recommendation": "Self-recovery approved" + (f" with warnings: {'; '.join(warnings)}" if warnings else ""),
        "warnings": warnings,
        "escalate": False,
        "metrics": metrics,
    }

# NOTE: handle_self_recovery_review is defined in lifecycle.py (canonical version)
# This duplicate was removed Feb 2026 to avoid registration conflicts.
# The helper functions below (assess_recovery_safety, validate_recovery_conditions)
# are still used by the lifecycle.py version.

# ============================================================================
# CONSOLIDATED SELF_RECOVERY TOOL
# Single entry point with action dispatch - replaces 3 separate tools
# ============================================================================

@mcp_tool("self_recovery", timeout=15.0)
async def handle_self_recovery(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Unified self-recovery tool for stuck/paused agents.

    Actions:
        check  - See what recovery options are available (read-only)
        quick  - Fast resume for safe states (coherence > 0.60, risk < 0.40)
        review - Full recovery with reflection (for moderate states)

    Natural workflow:
        1. self_recovery(action="check") - see what's available
        2. self_recovery(action="quick") - if safe enough
        3. self_recovery(action="review", reflection="...") - if not
    """
    action = arguments.get("action", "check")

    if action == "check":
        return await handle_check_recovery_options(arguments)
    elif action == "quick":
        return await handle_quick_resume(arguments)
    elif action == "review":
        # Dispatch to lifecycle.py handler
        from .handlers import handle_self_recovery_review
        return await handle_self_recovery_review(arguments)
    else:
        return [error_response(
            f"Unknown action: {action}",
            error_code="INVALID_ACTION",
            error_category="validation_error",
            recovery={
                "valid_actions": ["check", "quick", "review"],
                "examples": [
                    'self_recovery(action="check")',
                    'self_recovery(action="quick")',
                    'self_recovery(action="review", reflection="I was stuck because...")',
                ],
            }
        )]

@mcp_tool("check_recovery_options", timeout=10.0, register=False)
async def handle_check_recovery_options(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Check if an agent is eligible for self-recovery.
    
    This is a read-only check that doesn't modify state. Use it to understand
    what's needed before attempting self_recovery_review.
    
    Returns:
        - eligible: bool - whether self-recovery is currently possible
        - blockers: list - what's preventing recovery (if any)
        - metrics: dict - current governance metrics
        - recommendations: list - what to do next
    """
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    
    agent_uuid = resolve_agent_uuid(arguments, agent_id)
    
    # Get current metrics (use safe_float for uninitialized agents with None coherence/risk)
    try:
        monitor = mcp_server.get_or_create_monitor(agent_uuid)
        from src.agent_monitor_state import ensure_hydrated
        await ensure_hydrated(monitor, agent_uuid)
        metrics = monitor.get_metrics()

        coherence = safe_float(monitor.state.coherence, 0.5)
        risk_score = safe_float(metrics.get("mean_risk"), 0.5)
        void_active = bool(monitor.state.void_active)
        void_value = safe_float(monitor.state.V, 0.0)

    except Exception as e:
        return [error_response(f"Could not get metrics: {e}")]
    
    # Check blockers
    blockers = []
    if void_active:
        blockers.append({
            "type": "void_active",
            "message": "Void is active - E-I imbalance has accumulated",
            "resolution": "Wait for void to clear or request human help",
        })
    
    if risk_score > MAX_RISK_FOR_SELF_RECOVERY:
        blockers.append({
            "type": "high_risk",
            "message": f"Risk ({risk_score:.2f}) exceeds limit ({MAX_RISK_FOR_SELF_RECOVERY})",
            "resolution": "Wait for risk to decrease or request human review",
        })
    
    if coherence < MIN_COHERENCE_FOR_SELF_RECOVERY:
        blockers.append({
            "type": "low_coherence",
            "message": f"Coherence ({coherence:.2f}) below threshold ({MIN_COHERENCE_FOR_SELF_RECOVERY})",
            "resolution": "Request human help - low coherence suggests confusion",
        })
    
    eligible = len(blockers) == 0
    
    # Build recommendations
    if eligible:
        recommendations = [
            "You're eligible for self-recovery",
            "Call self_recovery(action='review') with a genuine reflection",
            "Include specific conditions you'll follow",
        ]
    else:
        recommendations = [
            "Self-recovery not currently available",
            "Address the blockers listed above",
            "Consider using leave_note(tags=['needs-human']) to request help",
        ]
    
    # Get margin info
    margin_info = GovernanceConfig.compute_proprioceptive_margin(
        risk_score=risk_score,
        coherence=coherence,
        void_active=void_active,
        void_value=void_value,
        coherence_history=monitor.state.coherence_history,
    )
    
    return success_response({
        "eligible": eligible,
        "blockers": blockers,
        "metrics": {
            "coherence": coherence,
            "risk_score": risk_score,
            "void_active": void_active,
            "void_value": void_value,
        },
        "margin": margin_info,
        "thresholds": {
            "max_risk_for_self_recovery": MAX_RISK_FOR_SELF_RECOVERY,
            "min_coherence_for_self_recovery": MIN_COHERENCE_FOR_SELF_RECOVERY,
        },
        "recommendations": recommendations,
    })

@mcp_tool("quick_resume", timeout=10.0, register=False)
async def handle_quick_resume(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Quick resume for agents in clearly safe states - no reflection required.
    
    This is the fastest path to recovery when:
    - coherence > 0.60 (high confidence state)
    - risk < 0.40 (low risk)
    - no void active
    - status is waiting_input or paused
    
    For agents that don't meet these strict criteria, use self_recovery_review
    which requires reflection but allows recovery at lower thresholds.
    
    Optional:
        reason: str - Brief note about why resuming (for audit)
    
    Recovery Hierarchy:
    1. quick_resume - safest states, no reflection needed
    2. self_recovery_review - moderate states, reflection required
    3. Human escalation - unsafe states
    """
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    
    agent_uuid = resolve_agent_uuid(arguments, agent_id)
    
    # Verify ownership
    if not verify_agent_ownership(agent_uuid, arguments):
        return [error_response(
            "Authentication required. You can only resume your own agent.",
            error_code="AUTH_REQUIRED",
            error_category="auth_error",
        )]
    
    reason = arguments.get("reason", "Quick resume - state is safe")

    # Mark recovery attempt so loop detector grants a 120s grace period.
    # Set before safety checks so even failed attempts suppress Pattern 2/5/6.
    meta_early = mcp_server.agent_metadata.get(agent_uuid)
    if meta_early:
        recovery_attempt_at = datetime.now(timezone.utc).isoformat()
        try:
            await agent_storage.persist_runtime_state(
                agent_uuid,
                recovery_attempt_at=recovery_attempt_at,
            )
        except Exception as e:
            logger.warning(
                f"PostgreSQL persist_runtime_state failed for quick_resume attempt: {e}",
                exc_info=True,
            )
            return [error_response(
                f"Failed to quick_resume agent '{agent_id}': persistence error",
                error_code="PERSIST_FAILED",
                error_category="system_error",
                details={"agent_id": agent_id, "cause": str(e)},
            )]
        meta_early.recovery_attempt_at = recovery_attempt_at

    # Get current metrics (use safe_float for uninitialized agents)
    try:
        monitor = mcp_server.get_or_create_monitor(agent_uuid)
        from src.agent_monitor_state import ensure_hydrated
        await ensure_hydrated(monitor, agent_uuid)
        metrics = monitor.get_metrics()

        coherence = safe_float(monitor.state.coherence, 0.5)
        risk_score = safe_float(metrics.get("mean_risk"), 0.5)
        void_active = bool(monitor.state.void_active)
        void_value = safe_float(monitor.state.V, 0.0)
        
    except Exception as e:
        return [error_response(f"Could not assess state: {e}")]
    
    # Strict safety checks for quick_resume (stricter than self_recovery_review)
    QUICK_RESUME_MIN_COHERENCE = 0.60
    QUICK_RESUME_MAX_RISK = 0.40

    # Uninitialized agents (0 check-ins) have default EISV values (~0.5) which
    # aren't meaningful for safety — skip strict thresholds, only check void.
    meta = mcp_server.agent_metadata.get(agent_uuid)
    if meta and getattr(meta, 'total_updates', 0) == 0:
        checks = {"no_void": not void_active}
        logger.info(
            "[SELF_RECOVERY] Skipping strict thresholds for uninitialized agent"
        )
    else:
        checks = {
            "coherence_high": coherence >= QUICK_RESUME_MIN_COHERENCE,
            "risk_low": risk_score <= QUICK_RESUME_MAX_RISK,
            "no_void": not void_active,
        }
    
    if not all(checks.values()):
        failed = [k for k, v in checks.items() if not v]
        return [error_response(
            f"State not safe enough for quick_resume. Failed: {failed}. "
            f"Use self_recovery(action='review') instead (allows recovery with reflection).",
            error_code="NOT_SAFE_FOR_QUICK_RESUME",
            error_category="safety_error",
            recovery={
                "action": "Use self_recovery(action='review') with reflection",
                "example": 'self_recovery(action="review", reflection="I was stuck because...")',
                "related_tools": ["self_recovery"],
            },
            context={
                "metrics": {
                    "coherence": coherence,
                    "risk_score": risk_score,
                    "void_active": void_active,
                },
                "thresholds": {
                    "min_coherence": QUICK_RESUME_MIN_COHERENCE,
                    "max_risk": QUICK_RESUME_MAX_RISK,
                },
            }
        )]
    
    # Check status
    meta = mcp_server.agent_metadata.get(agent_uuid)
    if not meta:
        return [error_response("Agent not found")]
    
    valid_statuses = ["waiting_input", "paused", "active", "moderate"]
    if meta.status not in valid_statuses:
        return [error_response(
            f"Cannot quick_resume from status '{meta.status}'",
            recovery={"valid_statuses": valid_statuses}
        )]
    
    # Log to knowledge graph
    try:
        from ..knowledge.handlers import store_discovery_internal
        await store_discovery_internal(
            agent_id=agent_uuid,
            summary=f"Quick resume: {reason[:100]}",
            discovery_type="recovery",
            details=json.dumps({
                "type": "quick_resume",
                "reason": reason,
                "metrics": {
                    "coherence": coherence,
                    "risk_score": risk_score,
                    "void_active": void_active,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }),
            tags=["recovery", "quick-resume", "audit"],
            severity="info",
            source="self_recovery_quick_resume",
        )
    except Exception as e:
        logger.warning(f"Failed to log quick_resume: {e}")
    
    previous_status = meta.status
    event_details = f"Quick resume: {reason}"
    persist_error = await _resume_with_persistence(
        meta,
        agent_uuid=agent_uuid,
        event_name="quick_resumed",
        reason=event_details,
        error_response_id=agent_id,
        error_action="quick_resume",
        storage_module=agent_storage,
    )
    if persist_error:
        return persist_error

    return success_response({
        "success": True,
        "recovered": True,
        "method": "quick_resume",
        "agent_id": agent_id,
        "message": "Quick resume successful - state was safe",
        "previous_status": previous_status,
        "metrics": {
            "coherence": coherence,
            "risk_score": risk_score,
        },
    })

# ============================================================================
# OPERATOR-ASSISTED RECOVERY
# For Central Operator agent to recover stuck agents it doesn't own
# ============================================================================

@mcp_tool("operator_resume_agent", timeout=15.0)
async def handle_operator_resume_agent(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Operator-assisted resume for stuck agents.
    
    This tool allows the Central Operator agent to resume other agents that are stuck.
    It requires:
    1. The caller to be an operator (label="Operator" or tags contain "operator")
    2. The target agent to be in a resumable state
    3. A reason for the intervention
    
    This is for automated recovery by the operator agent, not for regular agents
    to resume each other (which would be a security issue).
    
    Required:
        target_agent_id: str - The agent to resume
        reason: str - Why the operator is resuming this agent
        
    Optional:
        force: bool - Skip soft safety checks (still respects hard limits)
    """
    # Get caller identity
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    
    caller_uuid = resolve_agent_uuid(arguments, agent_id)
    target_agent_id = arguments.get("target_agent_id")
    reason = arguments.get("reason", "Operator-assisted recovery")
    force = arguments.get("force", False)
    
    if not target_agent_id:
        return [error_response(
            "target_agent_id required - which agent should be resumed?",
            error_code="MISSING_TARGET",
            error_category="validation_error",
        )]
    
    # Verify caller is operator
    meta = mcp_server.agent_metadata.get(caller_uuid)
    if not meta:
        return [error_response("Caller not found")]
    
    label = getattr(meta, 'label', '') or ''
    tags = getattr(meta, 'tags', []) or []
    is_operator = (
        label.lower() == 'operator' or
        'operator' in [t.lower() for t in tags]
    )
    
    if not is_operator:
        return [error_response(
            "Only operator agents can use this tool. "
            "For self-recovery, use self_recovery(action='review') or self_recovery(action='quick').",
            error_code="NOT_OPERATOR",
            error_category="auth_error",
            recovery={
                "action": "Use self-recovery tools instead",
                "related_tools": ["self_recovery_review", "quick_resume", "check_recovery_options"],
            }
        )]
    
    # Get target agent
    target_meta = mcp_server.agent_metadata.get(target_agent_id)
    if not target_meta:
        return [error_response(f"Target agent '{target_agent_id}' not found")]
    
    # Get target metrics (use safe_float for uninitialized agents)
    try:
        monitor = mcp_server.get_or_create_monitor(target_agent_id)
        from src.agent_monitor_state import ensure_hydrated
        await ensure_hydrated(monitor, target_agent_id)
        metrics = monitor.get_metrics()

        coherence = safe_float(monitor.state.coherence, 0.5)
        risk_score = safe_float(metrics.get("mean_risk"), 0.5)
        void_active = bool(monitor.state.void_active)
        void_value = safe_float(monitor.state.V, 0.0)
        
    except Exception as e:
        return [error_response(f"Could not get target metrics: {e}")]
    
    # Hard limits - even operator can't override these
    if void_active:
        return [error_response(
            f"Cannot resume {target_agent_id}: void is active. "
            "This requires human intervention.",
            error_code="VOID_ACTIVE",
            error_category="safety_error",
            context={"void_value": void_value},
        )]
    
    if risk_score > 0.80:
        return [error_response(
            f"Cannot resume {target_agent_id}: risk ({risk_score:.2f}) exceeds hard limit (0.80). "
            "This requires human intervention.",
            error_code="RISK_TOO_HIGH",
            error_category="safety_error",
        )]
    
    if coherence < 0.20:
        return [error_response(
            f"Cannot resume {target_agent_id}: coherence ({coherence:.2f}) below hard limit (0.20). "
            "This requires human intervention.",
            error_code="COHERENCE_TOO_LOW",
            error_category="safety_error",
        )]
    
    # Soft limits - warn but allow if force=True
    warnings = []
    if not force:
        if risk_score > 0.60:
            warnings.append(f"Risk ({risk_score:.2f}) is elevated")
        if coherence < 0.40:
            warnings.append(f"Coherence ({coherence:.2f}) is low")
        
        if warnings:
            return [error_response(
                f"Soft safety checks failed for {target_agent_id}: {'; '.join(warnings)}. "
                "Use force=True to override soft limits (hard limits still apply).",
                error_code="SOFT_SAFETY_FAILED",
                error_category="safety_error",
                context={
                    "coherence": coherence,
                    "risk_score": risk_score,
                    "warnings": warnings,
                },
                recovery={
                    "action": "Add force=True to override soft limits",
                    "example": f'operator_resume_agent(target_agent_id="{target_agent_id}", reason="...", force=True)',
                }
            )]
    
    # Log to knowledge graph
    try:
        from ..knowledge.handlers import store_discovery_internal
        await store_discovery_internal(
            agent_id=caller_uuid,  # Log under operator
            summary=f"Operator resumed {target_agent_id}: {reason[:100]}",
            discovery_type="operator_intervention",
            details=json.dumps({
                "operator_id": caller_uuid,
                "target_agent_id": target_agent_id,
                "reason": reason,
                "force": force,
                "target_metrics": {
                    "coherence": coherence,
                    "risk_score": risk_score,
                    "void_active": void_active,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }),
            tags=["operator", "intervention", "recovery", "audit"],
            severity="warning",  # Operator interventions are always notable
            source="operator_resume",
            extra_provenance={"target_agent_id": target_agent_id},
        )
    except Exception as e:
        logger.warning(f"Failed to log operator intervention: {e}")
    
    previous_status = target_meta.status
    event_details = f"Resumed by operator {caller_uuid}: {reason}"
    persist_error = await _resume_with_persistence(
        target_meta,
        agent_uuid=target_agent_id,
        event_name="operator_resumed",
        reason=event_details,
        error_response_id=target_agent_id,
        error_action="operator_resume",
        details_key="target_agent_id",
        storage_module=agent_storage,
    )
    if persist_error:
        return persist_error
    
    return success_response({
        "success": True,
        "action": "operator_resume",
        "operator_id": caller_uuid,
        "target_agent_id": target_agent_id,
        "reason": reason,
        "previous_status": previous_status,
        "force_used": force,
        "warnings": warnings if force else [],
        "target_metrics": {
            "coherence": coherence,
            "risk_score": risk_score,
            "void_active": void_active,
        },
        "audit_note": "This intervention has been logged to the knowledge graph",
    })
