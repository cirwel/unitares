"""
CIRS state_announce handler — emit and query EISV + trajectory state.

Includes trajectory signature helper functions used by state_announce emit.
"""

from typing import Dict, Any, Sequence
from datetime import datetime

from mcp.types import TextContent
from ..decorators import mcp_tool
from ..utils import success_response, error_response, require_registered_agent
from src.logging_utils import get_logger
from .types import StateAnnounce
from .storage import _store_state_announce, _get_state_announces, _state_announce_buffer

logger = get_logger(__name__)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
# =============================================================================
# Trajectory Signature Helper Functions
# =============================================================================

def _compute_decision_bias(state) -> str:
    """Compute decision bias from decision history"""
    if not hasattr(state, 'decision_history') or not state.decision_history:
        return "neutral"

    recent = state.decision_history[-10:]
    proceed_count = sum(1 for d in recent if d in ["proceed", "approve"])
    pause_count = sum(1 for d in recent if d in ["pause", "reject"])

    if proceed_count > pause_count * 2:
        return "proceed_bias"
    elif pause_count > proceed_count * 2:
        return "pause_bias"
    return "balanced"

def _compute_focus_stability(_state) -> None:
    """Retired: legacy C(V) variance is not evidence of focus stability."""
    return None

def _compute_maturity(state) -> str:
    """Compute agent maturity from update count"""
    updates = getattr(state, 'update_count', 0)
    if updates < 5:
        return "nascent"
    elif updates < 20:
        return "developing"
    elif updates < 50:
        return "maturing"
    else:
        return "mature"

def _compute_convergence_rate(state) -> float:
    """Compute convergence rate from entropy history"""
    if not hasattr(state, 'S_history') or len(state.S_history) < 5:
        return 0.0

    recent = state.S_history[-10:]
    if len(recent) < 2:
        return 0.0

    import numpy as np
    x = np.arange(len(recent))
    slope, _ = np.polyfit(x, recent, 1)
    return float(-slope)

def _compute_risk_trend(state) -> str:
    """Compute risk trend from risk history"""
    if not hasattr(state, 'risk_history') or len(state.risk_history) < 3:
        return "stable"

    recent = state.risk_history[-5:]
    if len(recent) < 2:
        return "stable"

    first_half = sum(recent[:len(recent)//2]) / (len(recent)//2)
    second_half = sum(recent[len(recent)//2:]) / (len(recent) - len(recent)//2)

    diff = second_half - first_half
    if diff > 0.1:
        return "increasing"
    elif diff < -0.1:
        return "decreasing"
    return "stable"

# =============================================================================
# STATE_ANNOUNCE Tool Handler
# =============================================================================

@mcp_tool("state_announce", timeout=10.0, register=False, description="CIRS Protocol: Broadcast or query agent EISV + trajectory state for multi-agent coordination")
async def handle_state_announce(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    CIRS STATE_ANNOUNCE - Multi-agent state broadcasting.

    Two modes:
    1. EMIT mode (action='emit'): Broadcast your current state
    2. QUERY mode (action='query'): Get recent state announcements from peers
    """
    action = arguments.get("action", "").lower()

    if not action or action not in ("emit", "query"):
        return [error_response(
            "action parameter required: 'emit' or 'query'",
            recovery={
                "valid_actions": ["emit", "query"],
                "emit_example": "state_announce(action='emit')",
                "query_example": "state_announce(action='query', regime='convergence')"
            }
        )]

    if action == "emit":
        return await _handle_state_announce_emit(arguments)
    else:
        return await _handle_state_announce_query(arguments)

async def _handle_state_announce_emit(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle STATE_ANNOUNCE emit action"""
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    # Get current state
    monitor = mcp_server.get_or_create_monitor(agent_id)
    from src.agent_monitor_state import ensure_hydrated
    await ensure_hydrated(monitor, agent_id)
    metrics = monitor.get_metrics()

    # Build EISV dict
    eisv = {
        "E": float(metrics.get("E", 0.7)),
        "I": float(metrics.get("I", 0.8)),
        "S": float(metrics.get("S", 0.2)),
        "V": float(metrics.get("V", 0.0)),
    }

    # Get trajectory signature if requested
    trajectory_signature = None
    include_trajectory = arguments.get("include_trajectory", True)
    if include_trajectory:
        try:
            state = monitor.state
            trajectory_signature = {
                "pi": {
                    "regime": str(getattr(state, 'regime', 'divergence')),
                    "task_type": str(getattr(state, 'task_type', 'mixed')),
                },
                "beta": {
                    "lambda1": float(state.lambda1),
                    "decision_bias": _compute_decision_bias(state),
                },
                "alpha": {
                    "coherence": float(state.coherence),
                    "focus_stability": _compute_focus_stability(state),
                    "focus_stability_provenance": {
                        "status": "retired",
                        "former_source": "legacy_tanh_v",
                        "former_role": "ode_control_feedback",
                        "health_evidence": False,
                        "reason": "non_discriminative_producer",
                    },
                },
                "rho": {
                    "update_count": int(state.update_count),
                },
                "delta": {
                    "maturity": _compute_maturity(state),
                    "convergence_rate": _compute_convergence_rate(state),
                },
                "eta": {
                    "void_frequency": float(metrics.get("void_frequency", 0.0)),
                    "risk_trend": _compute_risk_trend(state),
                },
            }
        except Exception as e:
            logger.debug(f"Could not compute trajectory signature: {e}")
            trajectory_signature = None

    # Get agent purpose and trust tier if available
    purpose = None
    trust_tier_name = None
    meta = mcp_server.agent_metadata.get(agent_id)
    if meta:
        purpose = getattr(meta, 'purpose', None)
        trust_tier_name = getattr(meta, 'trust_tier', None)

    # Create and store the announcement
    announce = StateAnnounce(
        agent_id=agent_id,
        timestamp=datetime.now().isoformat(),
        eisv=eisv,
        coherence=float(metrics.get("coherence", 0.5)),
        regime=str(metrics.get("regime", "divergence")),
        phi=float(metrics.get("phi", 0.0)),
        verdict=str(metrics.get("verdict", "caution")),
        risk_score=float(metrics.get("risk_score") or metrics.get("current_risk") or 0.0),
        coherence_source=str(metrics.get("coherence_source") or "unknown"),
        coherence_role=str(metrics.get("coherence_role") or "unknown"),
        trajectory_signature=trajectory_signature,
        purpose=purpose,
        update_count=int(metrics.get("updates", 0)),
        trust_tier=trust_tier_name,
    )

    _store_state_announce(announce)

    return success_response({
        "action": "emit",
        "announcement": announce.to_dict(),
        "message": f"STATE_ANNOUNCE broadcast from {agent_id}",
        "cirs_protocol": "STATE_ANNOUNCE",
        "active_agents": len(_state_announce_buffer)
    }, agent_id=agent_id)

async def _handle_state_announce_query(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle STATE_ANNOUNCE query action"""
    agent_ids = arguments.get("agent_ids")
    regime = arguments.get("regime")
    min_coherence = arguments.get("min_coherence")
    max_risk = arguments.get("max_risk")
    limit = int(arguments.get("limit", 50))

    valid_regimes = ["divergence", "transition", "convergence", "stable"]
    if regime and regime.lower() not in valid_regimes:
        return [error_response(
            f"Invalid regime: {regime}",
            recovery={"valid_values": valid_regimes}
        )]

    if min_coherence is not None:
        return [error_response(
            "min_coherence is retired: the announced scalar may be legacy C(V) "
            "control feedback and cannot support a peer-quality threshold.",
            error_code="UNSUPPORTED_COHERENCE_FILTER",
            error_category="validation_error",
            recovery={
                "action": "Filter by max_risk, regime, or explicit agent_ids instead",
                "invalid_parameter": "min_coherence",
            },
        )]

    announces = _get_state_announces(
        agent_ids=agent_ids,
        regime=regime.lower() if regime else None,
        max_risk=float(max_risk) if max_risk is not None else None,
        limit=limit
    )

    regimes = [a.get("regime", "unknown") for a in announces]
    verdicts = [a.get("verdict", "unknown") for a in announces]
    coherence_roles = [a.get("coherence_role", "unknown") for a in announces]
    coherence_sources = [a.get("coherence_source", "unknown") for a in announces]

    summary = {
        "total_agents": len(announces),
        "by_regime": {r: regimes.count(r) for r in set(regimes)},
        "by_verdict": {v: verdicts.count(v) for v in set(verdicts)},
        "avg_coherence": sum(a.get("coherence", 0) for a in announces) / len(announces) if announces else 0,
        "coherence_role_counts": {
            role: coherence_roles.count(role) for role in set(coherence_roles)
        },
        "avg_coherence_provenance": {
            "health_evidence": False,
            "source_counts": {
                source: coherence_sources.count(source)
                for source in set(coherence_sources)
            },
            "warning": "Do not compare or threshold averages across producer roles",
        },
        "avg_risk": sum(a.get("risk_score", 0) for a in announces) / len(announces) if announces else 0,
    }

    return success_response({
        "action": "query",
        "announcements": announces,
        "summary": summary,
        "cirs_protocol": "STATE_ANNOUNCE",
        "filters_applied": {
            "agent_ids": agent_ids,
            "regime": regime,
            "max_risk": max_risk,
            "limit": limit
        }
    })
