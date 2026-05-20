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
    # Resolve label to UUID if needed
    agent_id = target
    if not (len(target) == 36 and target.count('-') == 4):
        # Looks like a label, resolve to UUID
        resolved = None
        try:
            from src.mcp_handlers.identity.handlers import _find_agent_by_label
            resolved = await _find_agent_by_label(target)
        except Exception as e:
            logger.debug(f"Label DB lookup failed for '{target}': {e}")

        if not resolved:
            # Fallback: search in-memory metadata by label.
            # Was force=True; dropped because the in-memory dict is kept current
            # by the regular write paths (process_agent_update / onboard /
            # background load), and a full PG reload here would block all
            # other handlers ~16s per call. If the agent isn't in memory,
            # the DB lookup above already missed it.
            await mcp_server.load_metadata_async()
            for uuid, meta in mcp_server.agent_metadata.items():
                if getattr(meta, 'label', None) == target:
                    resolved = uuid
                    break

        if resolved:
            agent_id = resolved
        else:
            return [error_response(
                f"Agent '{target}' not found. Use list_agents to see available agents.",
                recovery={"related_tools": ["list_agents"]}
            )]
    # Verify agent exists. Was force=True full reload; dropped — the
    # in-memory dict is kept current by the write paths, so reloading all
    # 3221 agents to look up one is overkill. If the agent is genuinely
    # missing from in-memory, force-reloading will not surface it (the
    # DB row was already there or wasn't).
    await mcp_server.load_metadata_async()
    if agent_id not in mcp_server.agent_metadata:
        return [error_response(
            f"Agent '{target}' not found in active metadata. They may need to check in first.",
            recovery={"related_tools": ["list_agents"]}
        )]
    
    include_history = arguments.get("include_history", True)
    analyze_patterns_flag = arguments.get("analyze_patterns", True)
    
    # Load monitor state from disk if not in memory (consistent with get_governance_metrics)
    monitor = mcp_server.get_or_create_monitor(agent_id)
    await ensure_hydrated(monitor, agent_id)

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
    # Was force=True with a "non-blocking" comment that was wrong — the
    # implementation does 3221 sequential per-agent cache.set awaits
    # (~16s per call). Drop force; in-memory cache is fresh enough for
    # comparison via process_agent_update / onboard / background loads,
    # and per-agent state hydration below uses load_monitor_state directly.
    await mcp_server.load_metadata_async()
    
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
    loop = asyncio.get_running_loop()
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

        # Skip agents with no measured activity — transient seed-default monitors
        # would dilute the comparison. Mirrors the prior "no persisted state ⇒
        # skip" behavior, but gated on metadata (in-memory, no I/O).
        meta = mcp_server.agent_metadata.get(agent_id)
        if not meta or int(getattr(meta, "total_updates", 0) or 0) == 0:
            continue

        monitor = await loop.run_in_executor(None, mcp_server.get_or_create_monitor, agent_id)
        await ensure_hydrated(monitor, agent_id)

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
    
    # Was force=True; dropped per Wave 0 follow-up. ensure_hydrated below
    # loads the requesting agent's state directly (single-agent fetch),
    # which is the only state this handler actually needs fresh — the
    # comparison cohort is read from already-in-memory metadata.
    await mcp_server.load_metadata_async()

    # Get current agent's metrics
    monitor = mcp_server.get_or_create_monitor(agent_id)
    await ensure_hydrated(monitor, agent_id)
    my_metrics = monitor.get_metrics()
    my_meta = mcp_server.agent_metadata.get(agent_id)
    
    my_E = float(my_metrics.get('E', 0.7))
    my_I = float(my_metrics.get('I', 0.8))
    my_S = float(my_metrics.get('S', 0.2))
    my_coherence = float(my_metrics.get('coherence', 0.5))
    my_phi = my_metrics.get('phi', 0.0)
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

            other_E = float(other_metrics.get('E', 0.7))
            other_I = float(other_metrics.get('I', 0.8))
            other_S = float(other_metrics.get('S', 0.2))
            other_coherence = float(other_metrics.get('coherence', 0.5))
            
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
                        "phi": other_metrics.get('phi', 0.0),
                        "verdict": other_metrics.get('verdict', 'caution'),
                        "risk_score": other_metrics.get('risk_score', 0.4)
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
            "risk_score": my_metrics.get('risk_score', 0.4)
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
    import asyncio

    # Wave 0 follow-up: was force=True, which forced a full PostgreSQL
    # reload + 3221 sequential per-agent cache.set awaits on every call.
    # That's the same anti-pattern list_agents removed (see
    # src/mcp_handlers/lifecycle/query.py:41 — "A forced full DB reload
    # here caused 14s+ timeouts and ClosedResourceError crashes").
    # In-memory metadata is kept current by process_agent_update / onboard
    # / background load paths; using it for fleet-overview tools accepts
    # at most a few seconds staleness in exchange for not blocking the
    # shared event loop. Concurrent observe calls were also the cause of
    # bystander timeouts on list_agents post-2A merge.
    await mcp_server.load_metadata_async()
    
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
        # Skip agents with no measured activity — pattern analysis on seed
        # defaults yields no anomalies anyway, and skipping avoids the per-call
        # disk read.
        meta = mcp_server.agent_metadata.get(agent_id)
        if not meta or int(getattr(meta, "total_updates", 0) or 0) == 0:
            return

        monitor = await loop.run_in_executor(None, mcp_server.get_or_create_monitor, agent_id)
        await ensure_hydrated(monitor, agent_id)

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
    """Get fleet-level health overview.

    Postgres-canonical. Reads core.mv_latest_agent_states for per-agent state
    rollups, core.agents for active membership + currently-paused, audit.events
    for current-epoch pause counts, and audit.r1_score_audit for verdict
    distribution. No in-memory monitor iteration — the prior implementation
    summed monitor.state across in-process monitors, which is load-once-per-
    process and drifts from PG truth on cross-process writes, producing
    persistent "0 pauses" reports while audit.events has hundreds.
    """
    from datetime import datetime, timezone
    from src.db import get_db

    agent_ids = arguments.get("agent_ids")
    include_health_breakdown = arguments.get("include_health_breakdown", True)

    db = get_db()
    async with db.acquire() as conn:
        # Current epoch: max(epoch) in core.epochs is the authoritative
        # boundary for "this epoch" rollups; outcome_events.epoch and
        # agent_state.epoch are written against the same value via
        # GovernanceConfig.CURRENT_EPOCH.
        epoch_row = await conn.fetchrow(
            "SELECT epoch, started_at FROM core.epochs ORDER BY epoch DESC LIMIT 1"
        )
        current_epoch = epoch_row["epoch"] if epoch_row else 1
        epoch_started_at = epoch_row["started_at"] if epoch_row else None

        # Build agent scope. agent_ids=None → all active agents. agent_ids=list
        # → that explicit set (no active-status filter; matches prior behavior
        # of trusting the caller).
        if agent_ids:
            scope_clause = "WHERE a.id = ANY($1::text[])"
            scope_args: tuple = (list(agent_ids),)
        else:
            scope_clause = "WHERE a.status = 'active'"
            scope_args = ()

        # State rollups via matview. The matview excludes synthetic bootstrap
        # rows at definition time (migration 023), so no extra filter needed.
        # LEFT JOIN preserves agents that never wrote a measured state row.
        state_sql = f"""
            WITH scope AS (
                SELECT a.id AS agent_id, a.status
                FROM core.agents a
                {scope_clause}
            )
            SELECT
              count(*)::int AS total_agents,
              count(ls.identity_id)::int AS agents_with_data,
              avg(ls.risk_score)::real AS mean_risk_score,
              avg(ls.coherence)::real AS mean_coherence,
              count(*) FILTER (
                  WHERE ls.regime IN ('nominal','STABLE','CONVERGENCE')
              )::int AS healthy,
              count(*) FILTER (
                  WHERE ls.regime IN ('warning','EXPLORATION','recovery')
              )::int AS moderate,
              count(*) FILTER (
                  WHERE ls.regime IN ('critical','DIVERGENCE')
              )::int AS critical,
              count(*) FILTER (WHERE ls.identity_id IS NULL)::int AS unknown_health,
              count(*) FILTER (WHERE scope.status = 'paused')::int AS paused_now,
              EXTRACT(EPOCH FROM (now() - min(ls.recorded_at)))::bigint
                  AS staleness_oldest_seconds,
              EXTRACT(EPOCH FROM (now() - max(ls.recorded_at)))::bigint
                  AS staleness_newest_seconds
            FROM scope
            LEFT JOIN core.mv_latest_agent_states ls ON ls.agent_id = scope.agent_id
        """
        state_row = await conn.fetchrow(state_sql, *scope_args)

        # total_updates: persisted as core.identities.metadata->>'total_updates'
        # (atomically incremented by db.increment_update_count on every
        # process_agent_update). chain_obs_count is an R2-specific lineage
        # counter — only ~27 across all active agents — so don't confuse it.
        updates_sql = f"""
            SELECT COALESCE(
                SUM(COALESCE((i.metadata->>'total_updates')::int, 0)), 0
            )::bigint AS total_updates
            FROM core.agents a
            JOIN core.identities i ON i.agent_id = a.id
            {scope_clause}
        """
        total_updates = await conn.fetchval(updates_sql, *scope_args)

        # Pauses this epoch: governance pauses persist to audit.events as
        # event_type='lifecycle_paused' (fired from agent_loop_detection.py
        # when a circuit-breaker pause decision lands). circuit_breaker_trip
        # fires 1:1 alongside; counting both would double-count.
        if epoch_started_at is not None:
            pause_sql = """
                SELECT count(*)::int FROM audit.events
                WHERE ts >= $1 AND event_type = 'lifecycle_paused'
            """
            pauses_this_epoch = await conn.fetchval(pause_sql, epoch_started_at)

            # Proceed proxy: trajectory_validated outcomes in audit.outcome_events
            # for this epoch. Not a perfect 1:1 with "governance said proceed"
            # — there is no lifecycle_proceeded event today — but it's the
            # measured "agent kept moving" signal scoped to the same window.
            proceed_sql = """
                SELECT count(*)::int FROM audit.outcome_events
                WHERE epoch = $1 AND outcome_type = 'trajectory_validated'
            """
            proceed_this_epoch = await conn.fetchval(proceed_sql, current_epoch)

            # Verdict distribution from r1 score audit, current-epoch window.
            verdict_sql = """
                SELECT verdict, count(*)::int AS n
                FROM audit.r1_score_audit
                WHERE recorded_at >= $1 AND verdict IS NOT NULL
                GROUP BY verdict
            """
            verdict_rows = await conn.fetch(verdict_sql, epoch_started_at)
        else:
            pauses_this_epoch = 0
            proceed_this_epoch = 0
            verdict_rows = []

    # Map r1 verdicts (plausible/inconclusive/unsupported) into the
    # legacy verdict_distribution surface (safe/caution/high-risk) so
    # external readers don't break. R1 vocabulary is the persisted truth;
    # the legacy keys are presentation.
    R1_TO_LEGACY = {
        "plausible": "safe",
        "inconclusive": "caution",
        "unsupported": "high-risk",
    }
    verdict_distribution = {"safe": 0, "caution": 0, "high-risk": 0}
    for row in verdict_rows:
        legacy_key = R1_TO_LEGACY.get(row["verdict"], row["verdict"])
        verdict_distribution[legacy_key] = (
            verdict_distribution.get(legacy_key, 0) + int(row["n"])
        )
    verdict_distribution["total"] = sum(
        v for k, v in verdict_distribution.items() if k != "total"
    )

    decision_distribution = {
        "proceed": int(proceed_this_epoch or 0),
        "pause": int(pauses_this_epoch or 0),
        "total": int((proceed_this_epoch or 0) + (pauses_this_epoch or 0)),
    }

    aggregate_data = {
        "total_agents": int(state_row["total_agents"]),
        "agents_with_data": int(state_row["agents_with_data"]),
        "total_updates": int(total_updates or 0),
        "mean_risk_score": float(state_row["mean_risk_score"] or 0.0),
        "mean_risk": float(state_row["mean_risk_score"] or 0.0),  # DEPRECATED alias
        "mean_coherence": float(state_row["mean_coherence"] or 0.0),
        "paused_now": int(state_row["paused_now"] or 0),
        "pauses_this_epoch": int(pauses_this_epoch or 0),
        "epoch": int(current_epoch),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "staleness": {
            "oldest_state_seconds": int(state_row["staleness_oldest_seconds"] or 0),
            "newest_state_seconds": int(state_row["staleness_newest_seconds"] or 0),
        },
        "decision_distribution": decision_distribution,
        "verdict_distribution": verdict_distribution,
    }

    if include_health_breakdown:
        aggregate_data["health_breakdown"] = {
            "healthy": int(state_row["healthy"]),
            "moderate": int(state_row["moderate"]),
            "critical": int(state_row["critical"]),
            "unknown": int(state_row["unknown_health"]),
        }

    return success_response({
        "aggregate": aggregate_data,
    })


def _parse_window_arg(value: Any, default_hours: float = 24.0) -> "datetime":
    """Parse `since`/`until` window args into an aware datetime.

    Accepts:
      - shorthand: ``"14d"``, ``"168h"``, ``"30m"``
      - ISO 8601 string (``"2026-05-08T00:00:00Z"`` or ``"2026-05-08T00:00:00+00:00"``)
      - ``None`` → now − default_hours

    Returned datetime is timezone-aware UTC.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    if value is None:
        return now - timedelta(hours=default_hours)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    s = str(value).strip()
    if not s:
        return now - timedelta(hours=default_hours)

    # Shorthand: <int><unit> where unit ∈ d/h/m/s.
    if len(s) >= 2 and s[-1] in {"d", "h", "m", "s"} and s[:-1].lstrip("-").isdigit():
        n = int(s[:-1])
        if n < 0:
            # `since="-14d"` would silently query a future window (now − (−14d) = now + 14d)
            # and return zero rows. Reject explicitly so the operator sees the typo.
            raise ValueError(f"negative shorthand duration not allowed: {s!r}")
        unit = s[-1]
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
            "s": timedelta(seconds=n),
        }[unit]
        return now - delta

    # ISO 8601. Accept trailing 'Z' as UTC.
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@mcp_tool("audit_events_query", timeout=15.0, register=False)
async def handle_audit_events(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Query the Postgres ``audit.events`` table by event_type + time window.

    Issue #422 — S1-c grace-period evaluations could not distinguish "zero emits
    in window" from "no query surface." This action wraps
    ``query_audit_events_async`` so grace-window verdicts and similar
    audit-driven evaluations can be computed without operator SQL access.

    Args (all optional except ``event_type``):
      event_type: str — exact audit event type to filter on
      event_types: list[str] — alternative to event_type, for IN-list filters
      since: str | None — window start; ``"14d"``/``"24h"`` shorthand or ISO; default 24h
      until: str | None — window end; ISO; default now
      target_agent_id: str | None — restrict to one agent. Do NOT use `agent_id`
        — that is auto-injected by the identity middleware with the caller's UUID
        and would silently scope the query to the caller (zero rows for an
        observer asking about fleet activity).
      limit: int — max event rows returned (default 1000, capped at 5000)
      include_events: bool — include event payloads in response (default False;
        counts + per-agent breakdown only)
      include_test_fixtures: bool — include agents whose id (case-insensitive)
        starts with ``test_`` (covers ``test_agent``, ``test_stress``,
        ``test_recovery_agent``, ``Test_Agent_S9``, etc. — verified against the
        live audit.events table 2026-05-20). Default True for transparency; set
        False to mirror the recommended-subtasks filter in #422.

    Returns counts grouped by agent and event_type plus first/last timestamps.
    Always reports test-fixture totals separately so the caller decides whether
    to discount them.
    """
    from src.audit_db import query_audit_events_async

    event_type = arguments.get("event_type")
    event_types_arg = arguments.get("event_types")
    if not event_type and not event_types_arg:
        return error_response(
            "event_type or event_types is required",
            error_code="missing_argument",
        )

    # Default 7d (168h). The primary use case is multi-week grace-window
    # evaluations (issue #422 names a 14-day window). A 24h default would
    # silently undercount when callers omit `since`.
    since_arg = arguments.get("since")
    window_defaulted = since_arg is None
    try:
        start_dt = _parse_window_arg(since_arg, default_hours=168.0)
        end_dt = (
            _parse_window_arg(arguments.get("until"), default_hours=0.0)
            if arguments.get("until") is not None
            else None
        )
    except (ValueError, KeyError) as e:
        return error_response(
            f"invalid window arg: {e}",
            error_code="invalid_argument",
        )

    try:
        limit = int(arguments.get("limit", 1000))
    except (TypeError, ValueError):
        limit = 1000
    limit = max(1, min(limit, 5000))

    # NOTE: `agent_id` is auto-injected by the identity middleware (caller's UUID
    # via AgentIdentityMixin). Use `target_agent_id` for the "audit-events-about-X"
    # filter so we don't silently scope to the caller. Matches observe(action='agent').
    agent_id = arguments.get("target_agent_id")
    include_events = bool(arguments.get("include_events", False))
    include_test_fixtures = bool(arguments.get("include_test_fixtures", True))

    # `event_types` wins when both are provided. Echo the effective filter
    # in the response so callers can see what was actually queried.
    effective_event_type = event_type if event_type and not event_types_arg else None
    effective_event_types = list(event_types_arg) if event_types_arg else None

    events = await query_audit_events_async(
        agent_id=agent_id,
        event_type=effective_event_type,
        event_types=effective_event_types,
        start_time=start_dt.isoformat(),
        end_time=end_dt.isoformat() if end_dt else None,
        limit=limit,
        order="asc",
    )

    def _is_test_fixture(aid: Any) -> bool:
        # Case-insensitive `test_` prefix. Verified against the live audit.events
        # table 2026-05-20: covers `test_agent`, `test_stress`, `test_recovery_agent`,
        # `test_agent_v2`, `test_agent_concurrent`, and the title-case `Test_Agent_*`
        # variants the issue comment names.
        return isinstance(aid, str) and aid.lower().startswith("test_")

    test_fixture_count = sum(1 for e in events if _is_test_fixture(e.get("agent_id")))
    visible_events = events if include_test_fixtures else [
        e for e in events if not _is_test_fixture(e.get("agent_id"))
    ]

    by_agent: Dict[str, int] = {}
    by_event_type: Dict[str, int] = {}
    first_ts: str | None = None
    last_ts: str | None = None
    for e in visible_events:
        aid = e.get("agent_id") or "<unknown>"
        by_agent[aid] = by_agent.get(aid, 0) + 1
        et = e.get("event_type") or "<unknown>"
        by_event_type[et] = by_event_type.get(et, 0) + 1
        ts = e.get("timestamp")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

    payload: Dict[str, Any] = {
        # Echo the EFFECTIVE filter that was applied to the DB query, not the
        # raw inputs. When both event_type and event_types are provided,
        # event_types wins; the response makes that visible.
        "event_type": effective_event_type,
        "event_types": effective_event_types,
        "window": {
            "since": start_dt.isoformat(),
            "until": end_dt.isoformat() if end_dt else None,
            "defaulted": window_defaulted,
        },
        "total_emits": len(visible_events),
        "test_fixture_emits": test_fixture_count,
        "raw_row_count": len(events),
        "by_agent_id": by_agent,
        "by_event_type": by_event_type,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "limit_reached": len(events) >= limit,
        "include_test_fixtures": include_test_fixtures,
    }
    if include_events:
        payload["events"] = visible_events

    return success_response(payload)


# REMOVED: handle_get_status - redundant with status alias → get_governance_metrics
# Use status() or get_governance_metrics() instead
