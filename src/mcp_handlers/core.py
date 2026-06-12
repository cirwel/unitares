"""
Core governance tool handlers.

EISV Completeness: Utilities available in src/eisv_format.py and src/eisv_validator.py
to ensure all metrics (E, I, S, V) are reported together, preventing selection bias.
See docs/guides/EISV_COMPLETENESS.md for usage.
"""

from typing import Dict, Any, Optional, Sequence
from mcp.types import TextContent
from .types import ToolArgumentsDict
from .utils import success_response, error_response, require_agent_id
from .decorators import mcp_tool
from src.logging_utils import get_logger
from src.services.update_workflow_service import run_process_update_workflow

logger = get_logger(__name__)


# Get mcp_server_std module (using shared utility)

from datetime import datetime
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
def _assess_thermodynamic_significance(
    monitor: Optional[Any],  # UNITARESMonitor type (can be None)
    result: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Determine if this update is thermodynamically significant.
    
    Significant events worth logging:
    - Risk spiked > 15%
    - Coherence dropped > 10%
    - Void crossed threshold (|V| > 0.10)
    - Circuit breaker triggered
    - Decision is pause/reject
    
    Returns dict with:
        is_significant: bool
        reasons: list[str]
        timestamp: str
    """
    # Significance thresholds (from config)
    from config.governance_config import config
    RISK_SPIKE_THRESHOLD = config.RISK_SPIKE_THRESHOLD
    COHERENCE_DROP_THRESHOLD = config.COHERENCE_DROP_THRESHOLD
    VOID_THRESHOLD = config.SIGNIFICANCE_VOID_THRESHOLD
    HISTORY_WINDOW = config.SIGNIFICANCE_HISTORY_WINDOW
    
    reasons = []
    
    if not monitor:
        return {
            'is_significant': False,
            'reasons': ['No monitor available'],
            'timestamp': datetime.now().isoformat(),
        }
    
    state = monitor.state
    metrics = result.get('metrics', {})
    
    # Check risk spike (compare latest to average of previous)
    if len(state.risk_history) >= 2:
        current_risk = state.risk_history[-1]
        # Use average of previous history as baseline
        history_slice = state.risk_history[-HISTORY_WINDOW:-1] if len(state.risk_history) > 1 else []
        if history_slice:
            baseline_risk = sum(history_slice) / len(history_slice)
            risk_delta = current_risk - baseline_risk
            if risk_delta > RISK_SPIKE_THRESHOLD:
                reasons.append(f"risk_spike: +{risk_delta:.3f} (from {baseline_risk:.3f} to {current_risk:.3f})")
    
    # Check coherence drop
    if len(state.coherence_history) >= 2:
        current_coherence = state.coherence_history[-1]
        history_slice = state.coherence_history[-HISTORY_WINDOW:-1] if len(state.coherence_history) > 1 else []
        if history_slice:
            baseline_coherence = sum(history_slice) / len(history_slice)
            coh_delta = baseline_coherence - current_coherence
            if coh_delta > COHERENCE_DROP_THRESHOLD:
                reasons.append(f"coherence_drop: -{coh_delta:.3f} (from {baseline_coherence:.3f} to {current_coherence:.3f})")
    
    # Check void threshold
    V = state.V
    if abs(V) > VOID_THRESHOLD:
        reasons.append(f"void_significant: V={V:.4f} (threshold: {VOID_THRESHOLD})")
    
    # Check circuit breaker (extract once to avoid nested .get() calls)
    circuit_breaker = result.get('circuit_breaker', {})
    if circuit_breaker.get('triggered'):
        reasons.append("circuit_breaker_triggered")
    
    # Check decision type (extract once to avoid nested .get() calls)
    decision_dict = result.get('decision', {})
    decision = decision_dict.get('action', '')
    if decision in ['pause', 'reject']:
        reasons.append(f"decision_{decision}")
    
    return {
        'is_significant': len(reasons) > 0,
        'reasons': reasons,
        'timestamp': datetime.now().isoformat(),
    }


def unbound_metrics_payload() -> dict:
    """The unbound ignorance shape for get_governance_metrics (trust
    contract §5). ONE definition shared by the MCP handler below and the
    REST direct handler (`http_tool_service._execute_http_get_governance_
    metrics`) — review of PR #608 found the REST shortcut bypassed the
    handler-level guard entirely, so each transport carries the guard but
    both return this payload.

    #428: wraps the bare "unbound" string with meaning + next_action; the
    peer next_action carries the same hint with a tool + example, and the
    wrapped verdict carries the canonical glossary entry so the two
    surfaces can't drift.
    """
    from src.governance_glossary import explain_verdict
    return {
        "status": "⚪ unbound",
        "verdict": explain_verdict("unbound"),
        "guidance": "Establish identity before reading agent metrics.",
        "next_action": {
            "tool": "identity",
            "example": "identity() or onboard(force_new=true, spawn_reason='new_session')",
            "note": (
                "get_governance_metrics is read-only; it creates no "
                "identity and no state for unbound callers."
            ),
        },
        "related_tools": ["identity", "onboard", "process_agent_update"],
    }


@mcp_tool("get_governance_metrics", timeout=10.0, requires_identity="pre_onboard")
async def handle_get_governance_metrics(arguments: ToolArgumentsDict) -> Sequence[TextContent]:
    """Get current governance state and metrics for an agent without updating state.

    Args:
        verbosity: 'minimal' (default), 'standard', or 'full'. Replaces lite param.
        lite: Backward compat — lite=true maps to verbosity=minimal, lite=false to full.
    """
    # Read-purity (trust contract §3.5): a read must not mint. This tool is
    # requires_identity="pre_onboard", so the dispatch middleware correctly
    # leaves unbound callers unbound — but require_agent_id's handler-layer
    # FALLBACK 2 then auto-generated a fresh in-memory `auto_<ts>_<hex>`
    # identity (plus a monitor) on EVERY unbound call (cold-probe evidence
    # 2026-06-10: three probes, three distinct ghosts). Guard on the actual
    # binding for ALL no-explicit-agent_id calls, not just the
    # stale-client_session_id case this block originally covered.
    if not arguments.get("agent_id"):
        try:
            from src.mcp_handlers.context import get_context_agent_id
            bound_agent_id = get_context_agent_id()
        except Exception:
            bound_agent_id = None
        if not bound_agent_id:
            return success_response(unbound_metrics_payload())

    agent_id, error = require_agent_id(arguments)
    if error:
        return [error]  # Wrap in list for Sequence[TextContent]
    from src.services.runtime_queries import get_governance_metrics_data
    response_data = await get_governance_metrics_data(agent_id, arguments, server=mcp_server)
    return success_response(response_data)

@mcp_tool("simulate_update", timeout=30.0, register=False)
async def handle_simulate_update(arguments: ToolArgumentsDict) -> Sequence[TextContent]:
    """Handle simulate_update tool - dry-run governance cycle without persisting state.

    Works in two modes:
    - With registered agent: Uses their existing EISV state
    - Without registration: Uses fresh default state (E=0.5, I=0.5, S=0.5, V=0)

    This allows quick testing of "what would governance say about X?" without
    requiring onboarding first.
    """
    from src.governance_monitor import UNITARESMonitor

    # Try to get agent_id from session/arguments (but don't require registration)
    agent_id, _ = require_agent_id(arguments)  # Ignore error - we'll handle missing agent

    # Check if agent is registered (exists in metadata)
    agent_state_source = "fresh"  # Default: using fresh state
    monitor = None
    meta = None
    dialectic_enforcement_warning = None

    if agent_id:
        # Check if this agent exists
        meta = mcp_server.agent_metadata.get(agent_id)
        if meta:
            # Agent exists - use their monitor with existing state
            monitor = mcp_server.get_or_create_monitor(agent_id)
            from src.agent_monitor_state import ensure_hydrated
            await ensure_hydrated(monitor, agent_id)
            agent_state_source = "existing"

    if monitor is None:
        # No registered agent - create temporary monitor with fresh default state
        # Use a placeholder ID that won't persist (simulation only)
        monitor = UNITARESMonitor("_simulation_temp_", load_state=False)
        agent_id = "_simulation_temp_"

    # Validate parameters for simulation (coerce str → float)
    raw_complexity = arguments.get("complexity", 0.5)
    try:
        complexity = float(raw_complexity) if raw_complexity is not None else 0.5
    except (TypeError, ValueError):
        complexity = 0.5

    # Dialectic condition enforcement (only applies to existing agents)
    dialectic_warnings = []
    if meta and agent_state_source == "existing":
        try:
            if getattr(meta, "dialectic_conditions", None):
                from .dialectic.enforcement import enforce_complexity_limit
                complexity, cap_warning = enforce_complexity_limit(
                    meta.dialectic_conditions, complexity
                )
                if cap_warning:
                    dialectic_enforcement_warning = cap_warning
                    arguments["complexity"] = complexity
        except Exception as e:
            logger.warning(f"Could not enforce dialectic conditions: {e}", exc_info=True)

    # Confidence: If not provided (None), let governance_monitor derive from state
    raw_confidence = arguments.get("confidence")
    try:
        confidence = float(raw_confidence) if raw_confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    ethical_drift = arguments.get("ethical_drift", [0.0, 0.0, 0.0])

    # Prepare agent state
    import numpy as np
    agent_state = {
        "parameters": np.array(arguments.get("parameters", [])),
        "ethical_drift": np.array(ethical_drift),
        "response_text": arguments.get("response_text", ""),
        "complexity": complexity  # Use validated value
    }

    # Run simulation (doesn't persist state) with confidence
    result = monitor.simulate_update(agent_state, confidence=confidence)

    # Post-ODE: Enforce risk_target and coherence_target
    if meta and agent_state_source == "existing":
        try:
            if getattr(meta, "dialectic_conditions", None):
                from .dialectic.enforcement import enforce_post_ode_conditions
                decision = result.get("decision", {})
                escalated_decision, condition_warnings = enforce_post_ode_conditions(
                    meta.dialectic_conditions, result.get("metrics", {}), decision
                )
                if escalated_decision is not decision:
                    result["decision"] = escalated_decision
                    result["dialectic_escalation"] = True
                dialectic_warnings.extend(condition_warnings)
        except Exception as e:
            logger.warning(f"Could not enforce post-ODE dialectic conditions: {e}", exc_info=True)

    # LITE MODE: Simplified response for smaller models/local agents
    lite_mode = arguments.get("lite", False)
    
    if lite_mode:
        # Minimal response: decision + key metrics only
        response = {
            "simulation": True,
            "agent_state_source": agent_state_source,
            "status": result.get("status", "unknown"),
            "decision": result.get("decision", {}),
            "metrics": {
                "E": result.get("metrics", {}).get("E"),
                "I": result.get("metrics", {}).get("I"),
                "S": result.get("metrics", {}).get("S"),
                "V": result.get("metrics", {}).get("V"),
                "coherence": result.get("metrics", {}).get("coherence"),
                "risk_score": result.get("metrics", {}).get("risk_score"),
            },
            "guidance": result.get("guidance"),
            "_note": "Lite mode: Use lite=false for full diagnostics",
        }
    else:
        # Full response with all details
        response = {
            "simulation": True,
            "agent_state_source": agent_state_source,
            **result
        }

    # Add note if using fresh state
    if agent_state_source == "fresh":
        response["note"] = (
            "Simulated with fresh default state (E=0.5, I=0.5, S=0.5, V=0). "
            "No agent was registered. Call onboard() or process_agent_update() to create one."
        )

    # Add dialectic warnings if applicable
    if dialectic_enforcement_warning:
        response["dialectic_warning"] = dialectic_enforcement_warning
    if dialectic_warnings:
        response["dialectic_condition_warnings"] = dialectic_warnings

    from src.governance_glossary import explain_ethical_drift_vector
    response["input_glossary"] = {
        "ethical_drift": explain_ethical_drift_vector(ethical_drift),
    }

    return success_response(response)

@mcp_tool("process_agent_update", timeout=60.0)
async def handle_process_agent_update(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Share your work and get feedback. Auto-binds identity on first call.

    Args:
        agent_id: Optional display name (auto-generated if not provided)
        response_text: Description of your work
        task_type: "divergent" (exploring) or "convergent" (focused)
        complexity: 0.0-1.0 how complex was this work
        confidence: 0.0-1.0 how confident are you
        lite: If true, returns minimal response (action + margin only). Alias for response_mode='minimal'
        response_mode: 'minimal' (action only), 'compact' (brief metrics), 'standard' (interpreted), 'full' (everything), 'auto' (adapts to health - default)

    No api_key needed - identity is bound to session via UUID.
    """
    from .validators import apply_param_aliases
    from .updates.context import UpdateContext
    import src.mcp_handlers.updates.enrichments  # noqa: F401 — triggers registration

    # MAGNET PATTERN: Accept fuzzy inputs (text, message, work -> response_text)
    arguments = dict(apply_param_aliases("process_agent_update", arguments))

    # Repair LLM-facing MCP payloads that placed S22 provenance metadata inside
    # recent_tool_results. The middleware path performs the same recovery before
    # validation; this direct handler call covers REST/direct HTTP callers.
    try:
        from src.provenance_context import recover_mangled_s22_provenance

        recovery_warnings = recover_mangled_s22_provenance(arguments)
        if recovery_warnings:
            existing = arguments.get("_mangled_s22_recovery_warnings") or []
            arguments["_mangled_s22_recovery_warnings"] = [
                *existing,
                *recovery_warnings,
            ]
    except Exception as exc:
        logger.debug("S22 provenance unmangling skipped at handler entry: %s", exc)

    # LITE MODE SHORTHAND
    if arguments.get("lite") in (True, "true", "1", 1):
        if not arguments.get("response_mode"):
            arguments["response_mode"] = "minimal"

    logger.info(f"[SESSION_DEBUG] process_agent_update() entry: args_keys={list(arguments.keys()) if arguments else []}")

    ctx = UpdateContext(arguments=arguments, mcp_server=mcp_server)

    try:
        return await run_process_update_workflow(ctx, serializer=success_response)
    except PermissionError as e:
        return [error_response(
            f"Authentication failed: {str(e)}",
            details={"error_type": "authentication_error"},
            recovery={
                "action": "Provide a valid API key for this agent",
                "related_tools": ["get_agent_api_key"],
                "workflow": "1. Use get_agent_api_key to retrieve your key 2. Include api_key in your request"
            }
        )]
    except ValueError as e:
        error_msg = str(e)
        if "Self-monitoring loop detected" in error_msg:
            return [error_response(
                error_msg,
                details={"error_type": "loop_detected"},
                recovery={
                    "action": "Wait for cooldown period to expire before retrying",
                    "related_tools": ["get_governance_metrics"],
                    "workflow": "1. Check current agent status 2. Wait for cooldown to expire 3. Retry with different parameters"
                }
            )]
        else:
            return [error_response(
                f"Validation error: {error_msg}",
                details={"error_type": "validation_error"},
                recovery={
                    "action": "Check your parameters and try again",
                    "related_tools": ["health_check"],
                    "workflow": "1. Verify all parameters are valid 2. Check system health 3. Retry"
                }
            )]
    except Exception as e:
        logger.error(f"Unexpected error in process_agent_update: {e}", exc_info=True)
        return [error_response(
            f"An unexpected error occurred: {str(e)}",
            details={"error_type": "unexpected_error"},
            recovery={
                "action": "Check server logs for details. If this persists, try restarting the MCP server",
                "related_tools": ["health_check", "get_server_info"],
                "workflow": "1. Check system health 2. Review server logs 3. Restart MCP server if needed"
            }
        )]
