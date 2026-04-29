"""
Observability tool handlers.
"""

import asyncio
from typing import Dict, Any, Sequence
from mcp.types import TextContent
import sys
from ..utils import success_response, error_response, require_argument, require_registered_agent
from ..decorators import mcp_tool
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.agent_monitor_state import ensure_hydrated
logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)


def _coerce_float_metric(value: Any, default: float) -> float:
    """Return a finite-ish float for optional metric fields."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _agent_display_name(agent_id: str) -> str | None:
    """Best-effort display label for event payloads."""
    meta = mcp_server.agent_metadata.get(agent_id)
    if meta is None:
        return None
    for attr in ("display_name", "label", "structured_id", "public_agent_id", "agent_id"):
        value = getattr(meta, attr, None)
        if value:
            return str(value)
    return None


def _resolve_agent_from_memory(target: str) -> str | None:
    """Resolve a UUID/public label using only the in-memory metadata snapshot."""
    if len(target) == 36 and target.count('-') == 4:
        return target if target in mcp_server.agent_metadata else None

    for candidate_uuid, meta in mcp_server.agent_metadata.items():
        if target in (
            getattr(meta, 'label', None),
            getattr(meta, 'display_name', None),
            getattr(meta, 'structured_id', None),
            getattr(meta, 'public_agent_id', None),
        ):
            return candidate_uuid
    return None


def _get_observable_monitor(agent_id: str):
    """Return an in-memory/sync-loaded monitor without awaiting DB hydration."""
    monitor = mcp_server.monitors.get(agent_id)
    if monitor is not None:
        return monitor

    try:
        persisted_state = mcp_server.load_monitor_state(agent_id)
    except Exception as e:
        logger.debug(f"Could not load monitor snapshot for {agent_id}: {e}")
        persisted_state = None

    if not persisted_state:
        return None

    monitor = mcp_server.get_or_create_monitor(agent_id)
    try:
        monitor.state = persisted_state
    except Exception:
        pass
    return monitor


@mcp_tool("observe_agent", timeout=15.0, register=False)
async def handle_observe_agent(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Observe another agent's governance state with pattern analysis"""
    from src.governance_monitor import UNITARESMonitor
    # Resolve the TARGET agent to observe.
    # Accept "target_agent_id" (preferred) or "agent_id" (legacy, only if it
    # doesn't match the caller's session-bound UUID — avoids self-observe).
    target = arguments.get("target_agent_id")
    if not target:
        # Legacy fallback: use agent_id only if it differs from session-bound caller
        explicit_id = arguments.get("agent_id")
        try:
            from ..context import get_context_agent_id
            caller_uuid = get_context_agent_id()
            if explicit_id and explicit_id != caller_uuid:
                target = explicit_id
        except Exception:
            target = explicit_id
    if not target:
        # Default to self-observe when session is bound
        try:
            from ..context import get_context_agent_id
            caller_uuid = get_context_agent_id()
            if caller_uuid:
                target = caller_uuid
        except Exception:
            pass
    if not target:
        return [error_response(
            "target_agent_id required: specify which agent to observe",
            recovery={
                "action": "Provide target_agent_id (UUID or label) of the agent to observe",
                "related_tools": ["list_agents"],
                "example": "observe(action='agent', target_agent_id='<agent-label>')"
            }
        )]
    # Resolve label/UUID from the in-memory metadata snapshot only. Avoid
    # async DB lookups here: MCP/anyio request handlers can deadlock when they
    # await asyncpg/Redis operations. Background loaders keep this snapshot fresh.
    agent_id = _resolve_agent_from_memory(target)
    if not agent_id:
        return [error_response(
            f"Agent '{target}' not found in active metadata. They may need to check in first.",
            recovery={"related_tools": ["list_agents"]}
        )]
    
    include_history = arguments.get("include_history", True)
    analyze_patterns_flag = arguments.get("analyze_patterns", True)
    
    # Read a monitor snapshot without DB hydration. If no in-memory/sync-loaded
    # snapshot exists yet, return a clear cache-miss response instead of doing a
    # request-time async DB fetch.
    monitor = _get_observable_monitor(agent_id)
    if monitor is None:
        return [error_response(
            f"Observation snapshot for agent '{target}' is not available yet.",
            recovery={
                "related_tools": ["get_governance_metrics", "list_agents"],
                "hint": "Have the agent check in, or retry after background metadata/state loading catches up."
            }
        )]

    # Perform pattern analysis
    if analyze_patterns_flag:
        observation = mcp_server.analyze_agent_patterns(monitor, include_history=include_history)
    else:
        # Just return current state without analysis
        metrics = monitor.get_metrics()
        try:
            pE, pI, pS, pV = monitor.get_primary_eisv()
        except (AttributeError, TypeError, ValueError):
            pE, pI, pS, pV = float(monitor.state.E), float(monitor.state.I), float(monitor.state.S), float(monitor.state.V)
        observation = {
            "current_state": {
                "E": pE,
                "I": pI,
                "S": pS,
                "V": pV,
                "coherence": float(monitor.state.coherence),
                "risk_score": float(metrics.get("risk_score") or metrics.get("current_risk") or 0.0),  # Governance/operational risk
                "phi": metrics.get("phi"),  # Primary physics signal
                "verdict": metrics.get("verdict"),  # Primary governance signal
                "lambda1": float(monitor.state.lambda1),
                "update_count": monitor.state.update_count
            }
        }

    # Use meta.total_updates as authoritative count (Postgres-backed, survives restarts)
    meta = mcp_server.agent_metadata.get(agent_id)
    if meta and observation.get("current_state"):
        observation["current_state"]["update_count"] = meta.total_updates
        observation["summary"] = observation.get("summary", {})
        observation["summary"]["total_updates"] = meta.total_updates

    # Agent profile — differentiated metrics outside the ODE
    profile_data = None
    try:
        from src.agent_profile import get_agent_profile, get_all_profiles
        if agent_id in get_all_profiles():
            profile_data = get_agent_profile(agent_id).to_summary()
    except Exception:
        pass

    # Add EISV labels for API documentation
    response_data = {
        "agent_id": agent_id,
        "observation": observation,
        # eisv_labels omitted by default — use get_governance_metrics(lite=false) for labels
    }
    if profile_data:
        response_data["profile"] = profile_data

    return success_response(response_data)

@mcp_tool("compare_agents", timeout=15.0, register=False)
async def handle_compare_agents(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Compare governance patterns across multiple agents"""
    from src.governance_monitor import UNITARESMonitor
    # Reload metadata to get latest state (handles multi-process sync) - non-blocking
    await mcp_server.load_metadata_async(force=True)
    
    agent_ids = arguments.get("agent_ids", [])
    if not agent_ids or len(agent_ids) < 2:
        return [error_response(
            "At least 2 agent_ids required for comparison",
            recovery={
                "action": "Provide at least 2 agent_ids in the agent_ids array",
                "related_tools": ["list_agents"],
                "workflow": "1. Call list_agents to see available agents 2. Select 2+ agent_ids to compare"
            }
        )]
    
    compare_metrics = arguments.get("compare_metrics") or ["risk_score", "coherence", "E", "I", "S", "V"]
    
    # Get metrics for all agents
    agents_data = []
    for agent_id in agent_ids:
        # Resolve label to UUID if needed (consistent with observe_agent)
        try:
            if not (len(agent_id) == 36 and agent_id.count('-') == 4):
                from src.mcp_handlers.identity.handlers import _find_agent_by_label
                resolved = await _find_agent_by_label(agent_id)
                if resolved:
                    agent_id = resolved
        except Exception:
            pass  # Use agent_id as-is

        monitor = mcp_server.monitors.get(agent_id)
        if monitor is None:
            # Load monitor state (non-blocking)
            loop = asyncio.get_running_loop()
            persisted_state = await loop.run_in_executor(None, mcp_server.load_monitor_state, agent_id)
            if persisted_state:
                monitor = UNITARESMonitor(agent_id, load_state=False)
                monitor.state = persisted_state
        
        if monitor:
            metrics = monitor.get_metrics()
            
            # Calculate health_status consistently with process_agent_update
            # Use health_checker.get_health_status() instead of metrics.get("status")
            risk_score = metrics.get("risk_score") or metrics.get("current_risk")
            coherence = float(monitor.state.coherence) if monitor.state else None
            void_active = bool(monitor.state.void_active) if monitor.state else False
            
            health_status_obj, _ = mcp_server.health_checker.get_health_status(
                risk_score=risk_score,
                coherence=coherence,
                void_active=void_active
            )
            
            # Guard against None values for agents with 0 updates
            # dict.get default is only used when key is ABSENT; if key exists with value=None, it returns None
            _risk = metrics.get("risk_score") or metrics.get("current_risk") or metrics.get("mean_risk") or 0.0
            _state = monitor.state
            agents_data.append({
                "agent_id": agent_id,
                "current_risk": metrics.get("current_risk"),  # Recent trend (last 10) - USED FOR HEALTH STATUS
                "risk_score": float(_risk),  # Governance/operational risk
                "phi": metrics.get("phi"),  # Primary physics signal
                "verdict": metrics.get("verdict"),  # Primary governance signal
                "mean_risk": metrics.get("mean_risk") or 0.0,  # Overall mean (all-time average) - for historical context
                "coherence": float(_state.coherence if _state and _state.coherence is not None else 0.5),
                "E": float(_state.E if _state and _state.E is not None else 0.5),
                "I": float(_state.I if _state and _state.I is not None else 0.5),
                "S": float(_state.S if _state and _state.S is not None else 0.5),
                "V": float(_state.V if _state and _state.V is not None else 0.0),
                "health_status": health_status_obj.value  # Use consistent calculation
            })
    
    if len(agents_data) < 2:
        return [error_response(
            f"Could not load data for at least 2 agents. Loaded: {len(agents_data)}",
            recovery={
                "action": "Ensure agents exist and have state. Some agents may need initial process_agent_update call.",
                "related_tools": ["list_agents", "get_governance_metrics", "process_agent_update"],
                "workflow": "1. Call list_agents to verify agents exist 2. Call get_governance_metrics to check if agents have state 3. Call process_agent_update if agents need initialization"
            }
        )]
    
    # Import numpy for statistical operations
    import numpy as np
    
    # Compute similarities and differences
    similarities = []
    differences = []
    outliers = []
    
    # Compare each metric
    for metric in compare_metrics:
        values = [(a["agent_id"], a.get(metric, 0)) for a in agents_data if metric in a]
        if len(values) < 2:
            continue
        
        metric_values = [v[1] for v in values]
        mean_val = np.mean(metric_values)
        std_val = np.std(metric_values) if len(metric_values) > 1 else 0.0
        
        # Find similar pairs (within 1 std dev)
        for i, (id1, val1) in enumerate(values):
            for j, (id2, val2) in enumerate(values[i+1:], i+1):
                if abs(val1 - val2) < std_val * 0.5:  # Similar if within 0.5 std dev
                    similarities.append({
                        "agents": [id1, id2],
                        "metric": metric,
                        "similarity": 1.0 - abs(val1 - val2) / (mean_val + 0.001),
                        "description": f"Both show similar {metric} patterns"
                    })
        
        # Find outliers (beyond 2 std dev)
        for agent_id, val in values:
            if std_val > 0 and abs(val - mean_val) > 2 * std_val:
                outliers.append({
                    "agent_id": agent_id,
                    "metric": metric,
                    "value": float(val),
                    "mean": float(mean_val),
                    "reason": f"{metric} is {'above' if val > mean_val else 'below'} average"
                })
    
    # Add EISV labels for API documentation
    response_data = {
        "comparison": {
            "agents": agents_data,
            "similarities": similarities[:10],  # Limit to top 10
            "differences": differences,
            "outliers": outliers
        },
        # eisv_labels omitted by default — use get_governance_metrics(lite=false) for labels
    }
    
    return success_response(response_data)

@mcp_tool("compare_me_to_similar", timeout=15.0, register=False)
async def handle_compare_me_to_similar(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Compare yourself to similar agents automatically - finds similar agents and compares
    
    IMPROVEMENT #5: Agent comparison templates
    """
    from src.governance_monitor import UNITARESMonitor
    # SECURITY FIX: Require registered agent (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    
    # Reload metadata to get latest state
    await mcp_server.load_metadata_async(force=True)
    
    # Get current agent's metrics
    monitor = mcp_server.get_or_create_monitor(agent_id)
    await ensure_hydrated(monitor, agent_id)
    my_metrics = monitor.get_metrics()
    my_meta = mcp_server.agent_metadata.get(agent_id)
    
    my_E = _coerce_float_metric(my_metrics.get('E'), 0.7)
    my_I = _coerce_float_metric(my_metrics.get('I'), 0.8)
    my_S = _coerce_float_metric(my_metrics.get('S'), 0.2)
    my_coherence = _coerce_float_metric(my_metrics.get('coherence'), 0.5)
    my_phi = _coerce_float_metric(my_metrics.get('phi'), 0.0)
    my_verdict = my_metrics.get('verdict', 'caution')
    my_regime = my_metrics.get('regime', 'nominal')
    my_total_updates = my_meta.total_updates if my_meta else 0
    
    # Find similar agents (similar EISV values)
    similar_agents = []
    similarity_threshold = arguments.get("similarity_threshold", 0.15)  # Within 15% on each metric
    
    for other_id, other_meta in mcp_server.agent_metadata.items():
        if other_id == agent_id or other_meta.status not in ["active", "waiting_input"]:
            continue
        
        try:
            other_monitor = mcp_server.get_or_create_monitor(other_id)
            await ensure_hydrated(other_monitor, other_id)
            other_metrics = other_monitor.get_metrics()

            other_E = _coerce_float_metric(other_metrics.get('E'), 0.7)
            other_I = _coerce_float_metric(other_metrics.get('I'), 0.8)
            other_S = _coerce_float_metric(other_metrics.get('S'), 0.2)
            other_coherence = _coerce_float_metric(other_metrics.get('coherence'), 0.5)
            other_phi = _coerce_float_metric(other_metrics.get('phi'), 0.0)
            other_risk = _coerce_float_metric(other_metrics.get('risk_score'), 0.4)
            
            # Calculate similarity (Euclidean distance in EISV space)
            E_diff = abs(my_E - other_E)
            I_diff = abs(my_I - other_I)
            S_diff = abs(my_S - other_S)
            coherence_diff = abs(my_coherence - other_coherence)
            
            # Similar if within threshold on all metrics
            if (E_diff <= similarity_threshold and 
                I_diff <= similarity_threshold and 
                S_diff <= similarity_threshold):
                
                similarity_score = 1.0 - ((E_diff + I_diff + S_diff + coherence_diff) / 4.0)
                similar_agents.append({
                    "agent_id": other_id,
                    "similarity_score": similarity_score,
                    "metrics": {
                        "E": other_E,
                        "I": other_I,
                        "S": other_S,
                        "coherence": other_coherence,
                        "phi": other_phi,
                        "verdict": other_metrics.get('verdict', 'caution'),
                        "risk_score": other_risk
                    },
                    "differences": {
                        "E": other_E - my_E,
                        "I": other_I - my_I,
                        "S": other_S - my_S,
                        "coherence": other_coherence - my_coherence
                    },
                    "total_updates": other_meta.total_updates,
                    "status": other_meta.status
                })
        except Exception as e:
            logger.debug(f"Could not compare with agent {other_id}: {e}")
            continue
    
    # Sort by similarity score (highest first)
    similar_agents.sort(key=lambda x: x["similarity_score"], reverse=True)
    
    # Take top 3 most similar
    top_similar = similar_agents[:3]
    
    if not top_similar:
        return success_response({
            "agent_id": agent_id,
            "message": "No similar agents found (within similarity threshold)",
            "my_metrics": {
                "E": my_E,
                "I": my_I,
                "S": my_S,
                "coherence": my_coherence,
                "phi": my_phi,
                "verdict": my_verdict
            },
            "suggestion": "Try adjusting similarity_threshold parameter or use compare_agents with specific agent_ids"
        })
    
    # Build comparison response
    comparison_data = {
        "agent_id": agent_id,
        "my_metrics": {
            "E": my_E,
            "I": my_I,
            "S": my_S,
            "coherence": my_coherence,
            "phi": my_phi,
            "verdict": my_verdict,
            "risk_score": _coerce_float_metric(my_metrics.get('risk_score'), 0.4)
        },
        "similar_agents": top_similar,
        "message": f"Found {len(top_similar)} similar agent(s). Here's how you compare:",
        "insights": []
    }
    
    # Generate pattern-based insights (group-level patterns)
    pattern_insights = []
    
    # Check for common lifecycle stage
    all_update_counts = [s["total_updates"] for s in top_similar] + [my_total_updates]
    avg_updates = sum(all_update_counts) / len(all_update_counts) if all_update_counts else 0
    if avg_updates <= 3:
        pattern_insights.append(f"All similar agents are in early onboarding phase (avg {avg_updates:.0f} updates)")
    elif avg_updates <= 10:
        pattern_insights.append(f"Similar agents are in exploration phase (avg {avg_updates:.0f} updates)")
    elif avg_updates <= 50:
        pattern_insights.append(f"Similar agents are in active development phase (avg {avg_updates:.0f} updates)")
    
    # Check for common verdict
    all_verdicts = [s["metrics"]["verdict"] for s in top_similar] + [my_verdict]
    verdict_counts = {}
    for v in all_verdicts:
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
    most_common_verdict = max(verdict_counts.items(), key=lambda x: x[1])[0] if verdict_counts else None
    if most_common_verdict and verdict_counts[most_common_verdict] == len(all_verdicts):
        pattern_insights.append(f"All similar agents share '{most_common_verdict}' verdict - this is a common pattern")
    
    # Check for regime patterns. The similarity loop above already
    # hydrated each candidate's monitor via ensure_hydrated; reuse from
    # mcp_server.monitors instead of re-hydrating.
    all_regimes = []
    for similar in top_similar:
        try:
            other_monitor = mcp_server.monitors.get(similar["agent_id"])
            if other_monitor is None:
                continue
            other_metrics = other_monitor.get_metrics()
            regime = other_metrics.get('regime', 'nominal')
            all_regimes.append(regime)
        except Exception:
            pass
    all_regimes.append(my_regime)
    if all_regimes:
        regime_counts = {}
        for r in all_regimes:
            regime_counts[r] = regime_counts.get(r, 0) + 1
        most_common_regime = max(regime_counts.items(), key=lambda x: x[1])[0] if regime_counts else None
        if most_common_regime and regime_counts[most_common_regime] == len(all_regimes) and most_common_regime != 'nominal':
            pattern_insights.append(f"All similar agents are in {most_common_regime} regime")
    
    # Check for EISV pattern (high E, high I, low S = exploration/divergence)
    all_E = [s["metrics"]["E"] for s in top_similar] + [my_E]
    all_I = [s["metrics"]["I"] for s in top_similar] + [my_I]
    all_S = [s["metrics"]["S"] for s in top_similar] + [my_S]
    avg_E = sum(all_E) / len(all_E) if all_E else 0.7
    avg_I = sum(all_I) / len(all_I) if all_I else 0.8
    avg_S = sum(all_S) / len(all_S) if all_S else 0.2
    if avg_E > 0.7 and avg_I > 0.7 and avg_S < 0.3:
        pattern_insights.append("High Energy + High Integrity + Low Entropy pattern - productive exploration phase")
    elif avg_E > 0.7 and avg_S > 0.5:
        pattern_insights.append("High Energy + High Entropy pattern - active divergence/exploration")
    elif avg_I > 0.8 and avg_S < 0.2:
        pattern_insights.append("High Integrity + Low Entropy pattern - focused convergence")
    
    # Add pattern insights to response (as group-level insights object)
    if pattern_insights:
        comparison_data["insights"].append({
            "type": "pattern",
            "agent_id": None,  # Pattern insights are group-level, not agent-specific
            "insights": pattern_insights,
            "description": "Common patterns observed across all similar agents"
        })
    
    # Generate individual comparison insights (differences from similar agents)
    for similar in top_similar:
        insights = []
        if similar["metrics"]["I"] > my_I + 0.05:
            insights.append(f"Higher Information Integrity ({similar['metrics']['I']:.6f} vs {my_I:.6f})")
        if similar["metrics"]["S"] < my_S - 0.05:
            insights.append(f"Lower Entropy ({similar['metrics']['S']:.6f} vs {my_S:.6f})")
        if similar["metrics"]["phi"] > my_phi + 0.05:
            insights.append(f"Better phi score ({similar['metrics']['phi']:.6f} vs {my_phi:.6f}) - closer to 'safe' verdict")
        if similar["metrics"]["verdict"] == "safe" and my_verdict != "safe":
            insights.append(f"Achieved 'safe' verdict (you're at '{my_verdict}')")
        
        if insights:
            comparison_data["insights"].append({
                "agent_id": similar["agent_id"],
                "insights": insights,
                "total_updates": similar["total_updates"]
            })

    # If no insights triggered, return a deterministic, actionable fallback so callers
    # don't see an empty list (which reads like a bug).
    if not comparison_data["insights"]:
        # Compute average deltas across the cohort to highlight the biggest gaps.
        avg_delta_E = sum(s["differences"]["E"] for s in top_similar) / len(top_similar)
        avg_delta_I = sum(s["differences"]["I"] for s in top_similar) / len(top_similar)
        avg_delta_S = sum(s["differences"]["S"] for s in top_similar) / len(top_similar)
        avg_delta_C = sum(s["differences"]["coherence"] for s in top_similar) / len(top_similar)

        # Rank by absolute delta magnitude
        deltas = [
            ("E", avg_delta_E, "Energy"),
            ("I", avg_delta_I, "Integrity"),
            ("S", avg_delta_S, "Entropy"),
            ("coherence", avg_delta_C, "Coherence"),
        ]
        deltas.sort(key=lambda x: abs(x[1]), reverse=True)

        top = deltas[:2]
        bullets = []
        for key, delta, label in top:
            if abs(delta) < 0.02:
                continue
            direction = "lower" if delta > 0 else "higher"
            # delta is (peer - me), so delta>0 means I'm lower than peers
            bullets.append(f"Your {label} is {direction} than similar agents on average (Δ≈{delta:+.2f})")

        if not bullets:
            bullets = [
                "No strong metric deltas vs similar agents (all gaps below 0.02).",
                "Try `compare_agents` with specific agent_ids or reduce similarity_threshold to widen the cohort."
            ]

        comparison_data["insights"].append({
            "type": "summary",
            "agent_id": None,
            "insights": bullets,
            "description": "Fallback insights (no threshold-triggered patterns detected)"
        })
    
    return success_response(comparison_data)

@mcp_tool("detect_anomalies", timeout=15.0, register=False)
async def handle_detect_anomalies(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Detect anomalies across agents"""
    from src.governance_monitor import UNITARESMonitor
    import asyncio
    
    # Reload metadata from PostgreSQL (async)
    await mcp_server.load_metadata_async(force=True)
    
    agent_ids = arguments.get("agent_ids")
    anomaly_types = arguments.get("anomaly_types", ["risk_spike", "coherence_drop"])
    min_severity = arguments.get("min_severity", "medium")
    
    severity_levels = {"low": 0, "medium": 1, "high": 2}
    min_severity_level = severity_levels.get(min_severity, 1)
    
    # Get agent list
    if not agent_ids:
        # Scan all agents (limit to active ones for performance)
        agent_ids = [aid for aid, meta in mcp_server.agent_metadata.items() 
                     if meta.status == "active"][:50]  # Limit to 50 agents max
    
    all_anomalies = []
    loop = asyncio.get_running_loop()  # Use get_running_loop() instead of deprecated get_event_loop()
    
    # Process agents in batches to prevent blocking
    async def process_agent(agent_id: str):
        """Process a single agent's anomalies"""
        monitor = mcp_server.monitors.get(agent_id)
        if monitor is None:
            # Load state in executor to avoid blocking
            persisted_state = await loop.run_in_executor(
                None, mcp_server.load_monitor_state, agent_id
            )
            if persisted_state:
                monitor = UNITARESMonitor(agent_id, load_state=False)
                monitor.state = persisted_state
        
        if monitor:
            # Analyze patterns in executor (may do file I/O)
            from src.pattern_analysis import analyze_agent_patterns
            analysis = await loop.run_in_executor(
                None, analyze_agent_patterns, monitor, False
            )
            
            # Filter anomalies by type and severity
            agent_anomalies = []
            agent_name = _agent_display_name(agent_id)
            for anomaly in analysis.get("anomalies", []):
                if anomaly["type"] in anomaly_types:
                    anomaly_severity_level = severity_levels.get(anomaly.get("severity", "low"), 0)
                    if anomaly_severity_level >= min_severity_level:
                        anomaly["agent_id"] = agent_id
                        if agent_name:
                            anomaly["agent_name"] = agent_name
                        agent_anomalies.append(anomaly)
            return agent_anomalies
        return []
    
    # Process agents concurrently (but limit concurrency)
    try:
        # Process in batches of 10 to avoid overwhelming system
        batch_size = 10
        for i in range(0, len(agent_ids), batch_size):
            batch = agent_ids[i:i+batch_size]
            batch_results = await asyncio.gather(*[process_agent(aid) for aid in batch], return_exceptions=True)
            for result in batch_results:
                if isinstance(result, list):
                    all_anomalies.extend(result)
                elif isinstance(result, Exception):
                    # Log but continue
                    import sys
                    logger.warning(f"Error processing agent in detect_anomalies: {result}", exc_info=True)
    except Exception as e:
        logger.error(f"Error in detect_anomalies: {e}", exc_info=True)
        return [error_response(f"Error detecting anomalies: {str(e)}")]
    
    # Dedup anomalies via event_detector before writing to audit trail.
    # Without this, repeated calls (e.g. dashboard polling) write duplicate
    # audit entries for the same persisting condition (e.g. risk_spike).
    new_anomalies = []
    if all_anomalies:
        import hashlib
        from src.event_detector import event_detector

        for a in all_anomalies:
            fp = hashlib.sha256(
                f"detect_anomalies|{a.get('type')}|{a.get('agent_id')}".encode()
            ).hexdigest()[:16]
            stored = event_detector.record_event({
                "fingerprint": fp,
                "type": a.get("type"),
                "severity": a.get("severity"),
                "agent_id": a.get("agent_id"),
                "agent_name": a.get("agent_name"),
                "message": (
                    f"{a.get('agent_name')}: {a.get('description')}"
                    if a.get("agent_name") and a.get("description")
                    else a.get("description", "")
                ),
                "description": a.get("description", ""),
                "source": "detect_anomalies",
            })
            if stored is not None:
                new_anomalies.append(a)

    if new_anomalies:
        from src.audit_log import audit_logger, AuditEntry
        from datetime import datetime
        # Fan out per-agent so audit.events.agent_id matches the affected agent.
        # Prior shape wrote one batch entry with agent_id='system' and a
        # truncated details.anomalies[:10] list — unjoinable in SQL and
        # silently dropped anomalies 11+.
        ts = datetime.now().isoformat()
        for a in new_anomalies:
            audit_logger._write_entry(AuditEntry(
                timestamp=ts,
                agent_id=a.get("agent_id") or "system",
                event_type="anomaly_detected",
                confidence=1.0,
                details={
                    "type": a.get("type"),
                    "severity": a.get("severity"),
                    "description": a.get("description", ""),
                },
            ))

    # Sort by severity (high first)
    all_anomalies.sort(key=lambda x: severity_levels.get(x.get("severity", "low"), 0), reverse=True)
    
    # Count by severity and type
    by_severity = {"high": 0, "medium": 0, "low": 0}
    by_type = {}
    for anomaly in all_anomalies:
        severity = anomaly.get("severity", "low")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        anomaly_type = anomaly.get("type", "unknown")
        by_type[anomaly_type] = by_type.get(anomaly_type, 0) + 1
    
    # Add EISV labels for API documentation
    return success_response({
        "anomalies": all_anomalies,
        "summary": {
            "total_anomalies": len(all_anomalies),
            "by_severity": by_severity,
            "by_type": by_type
        },
        # eisv_labels omitted by default — use get_governance_metrics(lite=false) for labels
    })

@mcp_tool("aggregate_metrics", timeout=15.0, register=False)
async def handle_aggregate_metrics(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get fleet-level health overview"""
    from src.governance_monitor import UNITARESMonitor
    import numpy as np
    
    # Reload metadata to get latest state (handles multi-process sync) - non-blocking
    await mcp_server.load_metadata_async(force=True)
    
    agent_ids = arguments.get("agent_ids")
    include_health_breakdown = arguments.get("include_health_breakdown", True)
    
    # Get agent list
    if not agent_ids:
        agent_ids = [aid for aid, meta in mcp_server.agent_metadata.items() if meta.status == "active"]
    
    # Aggregate metrics
    total_agents = len(agent_ids)
    agents_with_data = 0
    total_updates = 0
    risk_scores = []  # Governance/operational risk scores
    coherence_scores = []
    health_statuses = {"healthy": 0, "moderate": 0, "critical": 0, "unknown": 0}
    decision_counts = {"proceed": 0, "pause": 0}  # Two-tier system (backward compat: approve/reflect/reject mapped)
    verdict_counts = {"safe": 0, "caution": 0, "high-risk": 0}  # Behavioral verdict distribution
    
    for agent_id in agent_ids:
        monitor = mcp_server.monitors.get(agent_id)
        if monitor is None:
            # Load monitor state (non-blocking)
            loop = asyncio.get_running_loop()
            persisted_state = await loop.run_in_executor(None, mcp_server.load_monitor_state, agent_id)
            if persisted_state:
                monitor = UNITARESMonitor(agent_id, load_state=False)
                monitor.state = persisted_state
        
        if monitor:
            agents_with_data += 1
            metrics = monitor.get_metrics()
            
            # Aggregate risk_score and coherence
            risk_score = metrics.get("risk_score") or metrics.get("current_risk")
            if risk_score is not None:
                risk_scores.append(float(risk_score))
            elif monitor.state.risk_history:
                # Fallback to risk_history if risk_score not available
                history_values = [float(r) for r in monitor.state.risk_history[-10:]]  # Last 10 updates
                risk_scores.extend(history_values)
            coherence_scores.append(float(monitor.state.coherence))
            
            # Aggregate health status
            status = metrics.get("status", "unknown")
            health_statuses[status] = health_statuses.get(status, 0) + 1
            
            # Aggregate decisions
            decision_stats = metrics.get("decision_statistics", {})
            # Map old decisions to new system
            proceed_count = decision_stats.get("proceed", 0) + decision_stats.get("approve", 0) + decision_stats.get("reflect", 0) + decision_stats.get("revise", 0)
            pause_count = decision_stats.get("pause", 0) + decision_stats.get("reject", 0)
            decision_counts["proceed"] += proceed_count
            decision_counts["pause"] += pause_count
            # Backward compatibility (keep old keys for compatibility)
            decision_counts["approve"] = decision_stats.get("approve", 0)
            decision_counts["reflect"] = decision_stats.get("reflect", 0) + decision_stats.get("revise", 0)
            decision_counts["reject"] = decision_stats.get("reject", 0)
            
            # Aggregate verdict distribution from metrics
            verdict = metrics.get("verdict")
            if verdict and verdict in verdict_counts:
                verdict_counts[verdict] += 1

            # Count total updates — prefer meta.total_updates (Postgres-backed)
            meta = mcp_server.agent_metadata.get(agent_id)
            total_updates += meta.total_updates if meta else monitor.state.update_count
    
    # Compute aggregate statistics
    aggregate_data = {
        "total_agents": total_agents,
        "agents_with_data": agents_with_data,
        "total_updates": total_updates,
        "mean_risk_score": float(np.mean(risk_scores)) if risk_scores else 0.0,  # Governance/operational risk (mean)
        "mean_risk": float(np.mean(risk_scores)) if risk_scores else 0.0,  # DEPRECATED: Use mean_risk_score instead
        "mean_coherence": float(np.mean(coherence_scores)) if coherence_scores else 0.0,
        "decision_distribution": {
            **decision_counts,
            "total": sum(decision_counts.values())
        },
        "verdict_distribution": {
            **verdict_counts,
            "total": sum(verdict_counts.values())
        }
    }
    
    if include_health_breakdown:
        aggregate_data["health_breakdown"] = health_statuses
    
    # Add EISV labels for API documentation
    return success_response({
        "aggregate": aggregate_data,
        # eisv_labels omitted by default — use get_governance_metrics(lite=false) for labels
    })

# REMOVED: handle_get_status - redundant with status alias → get_governance_metrics
# Use status() or get_governance_metrics() instead
