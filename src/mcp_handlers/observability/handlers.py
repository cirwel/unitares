"""
Observability tool handlers.
"""

import asyncio
import math
from typing import Dict, Any, Sequence
from mcp.types import TextContent
from ..utils import success_response, error_response, require_registered_agent
from ..decorators import mcp_tool
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.agent_monitor_state import ensure_hydrated
logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)


def _coerce_float_metric(value: Any, default: float) -> float:
    """Return a finite float, falling back when sparse state returns None."""
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return default
    return coerced if math.isfinite(coerced) else default


def _resolve_agent_from_memory(target: str) -> str | None:
    if target in mcp_server.agent_metadata:
        return target
    for uuid_key, meta in mcp_server.agent_metadata.items():
        if target in (
            getattr(meta, "label", None),
            getattr(meta, "display_name", None),
            getattr(meta, "structured_id", None),
            getattr(meta, "public_agent_id", None),
        ):
            return uuid_key
    return None


def _get_observable_monitor(agent_id: str):
    return mcp_server.monitors.get(agent_id)


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
    # await asyncpg/Redis work. Background loaders keep this snapshot fresh.
    agent_id = _resolve_agent_from_memory(target)
    if not agent_id:
        return [error_response(
            f"Agent '{target}' not found in active metadata. They may need to check in first.",
            recovery={"related_tools": ["list_agents"]}
        )]
    
    include_history = arguments.get("include_history", True)
    analyze_patterns_flag = arguments.get("analyze_patterns", True)
    
    # Read a monitor snapshot without request-time DB hydration. If no
    # in-memory/sync-loaded snapshot exists yet, return a clear cache miss.
    monitor = _get_observable_monitor(agent_id)
    if monitor is None:
        return [error_response(
            f"Observation snapshot for agent '{target}' is not available yet.",
            recovery={
                "related_tools": ["get_governance_metrics", "list_agents"],
                "hint": "Have the agent check in, or retry after background metadata/state loading catches up.",
            },
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
        # #428: vocabulary at point-of-use — wrap the verdict with
        # meaning + next_action inline so cold-onboarded agents observing
        # themselves don't have to look up what "pause"/"guide" mean.
        from src.governance_glossary import explain_verdict
        observation = {
            "current_state": {
                "E": pE,
                "I": pI,
                "S": pS,
                "V": pV,
                "coherence": float(monitor.state.coherence),
                "risk_score": float(metrics.get("risk_score") or metrics.get("current_risk") or 0.0),  # Governance/operational risk
                "phi": metrics.get("phi"),  # Primary physics signal
                "verdict": explain_verdict(metrics.get("verdict")),  # Primary governance signal
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

    # Decision/verdict distributions from Postgres truth.
    #
    # The summary built above derives decision_distribution / verdict_distribution
    # from monitor.state.decision_history / verdict_history — in-memory lists that
    # are only rebuilt from the DB when the monitor is COLD (update_count==0, see
    # hydrate_from_db_if_fresh). For an agent whose live process_update calls land
    # in ANOTHER process, this observer warms its monitor once and then never
    # refreshes those lists, so pauses written cross-process are invisible — the
    # summary reports decision_distribution.pause=0 for an agent that is actually
    # paused. (EISV/coherence stay correct because a background loader syncs them;
    # the decision/verdict vocabularies are not part of that sync.)
    #
    # Recount from the persisted state_json rows so the distributions reflect PG
    # truth regardless of which process owns the agent. Counts are order-
    # independent, so no chronological reversal is needed. Fail-open: on any DB
    # error keep the monitor-derived distributions rather than breaking observe.
    #
    # IMPORTANT: circuit-breaker / CIRS pauses are NOT recorded as
    # state_json.action='pause' — the breaker enforces the pause out-of-band of
    # the check-in pipeline and emits an audit.events 'lifecycle_paused' row
    # instead (the same source aggregate uses for pauses_this_epoch). Counting
    # only state_json.action therefore still reports pause=0 for an agent the
    # fleet actually paused. Fold the authoritative lifecycle pause count in so
    # observe agrees with `agent get`.
    try:
        from src.agent_storage import get_agent_state_history, extract_actions_verdicts
        from src.pattern_analysis import (
            build_decision_distribution,
            build_verdict_distribution,
        )
        rows = await get_agent_state_history(agent_id, limit=200, exclude_synthetic=True)
        actions, verdicts = extract_actions_verdicts(rows) if rows else ([], [])

        lifecycle_pauses = 0
        try:
            from src.audit_db import query_audit_events_async
            pause_events = await query_audit_events_async(
                agent_id=agent_id, event_type="lifecycle_paused", limit=1000,
            )
            lifecycle_pauses = len(pause_events)
        except Exception:
            logger.debug(
                "observe: lifecycle_paused count skipped for %s", agent_id, exc_info=True
            )

        if rows or lifecycle_pauses:
            summary = observation.setdefault("summary", {})
            if actions or lifecycle_pauses:
                dist = build_decision_distribution(actions)
                # max(), not sum(): an agent that DOES record action='pause'
                # alongside its lifecycle_paused event must not be double-counted,
                # while breaker pauses that never reach the action stream still
                # surface. (Today no path writes action='pause', so this reduces
                # to lifecycle_pauses, but max() keeps it correct either way.)
                dist["pause"] = max(dist["pause"], lifecycle_pauses)
                summary["decision_distribution"] = dist
            if verdicts:
                summary["verdict_distribution"] = build_verdict_distribution(verdicts)
            summary["distribution_source"] = "postgres"
    except Exception:
        logger.debug(
            "observe: PG decision/verdict override skipped for %s",
            agent_id,
            exc_info=True,
        )

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

def anomaly_change_token(anomaly: Dict[str, Any]) -> str:
    """Emit-on-change dedup token for a persisting anomaly.

    Keyed on the data the anomaly was computed from — its type, agent, the
    full-precision context values, and the newest analyzed sample's timestamp —
    NOT on the human-readable description. This means the token advances exactly
    when a new state sample lands: a frozen/idle history yields an identical
    token every evaluation (suppressed), while a genuine recovery-then-respike
    produces a new timestamp and therefore a new token (emitted), even if the
    rounded values happen to match. Deliberately independent of the description
    f-string so message-format edits can never silently re-fire the idle fleet.
    """
    import hashlib
    ctx = anomaly.get("context") or {}
    ctx_repr = "|".join(f"{k}={ctx[k]}" for k in sorted(ctx))
    basis = (
        f"{anomaly.get('type')}|{anomaly.get('agent_id')}|"
        f"{anomaly.get('timestamp')}|{ctx_repr}"
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:16]

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
                "change_token": anomaly_change_token(a),
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
    stale_count = 0
    for anomaly in all_anomalies:
        severity = anomaly.get("severity", "low")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        anomaly_type = anomaly.get("type", "unknown")
        by_type[anomaly_type] = by_type.get(anomaly_type, 0) + 1
        if anomaly.get("stale") is True:
            stale_count += 1

    # Add EISV labels for API documentation
    return success_response({
        "anomalies": all_anomalies,
        "summary": {
            "total_anomalies": len(all_anomalies),
            # stale = recomputed from a frozen (idle) history window; already
            # reported, not a current finding (#637). fresh + stale = total.
            "fresh_anomalies": len(all_anomalies) - stale_count,
            "stale_anomalies": stale_count,
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
              EXTRACT(EPOCH FROM (now() - min(ls.recorded_at)))::bigint
                  AS staleness_oldest_seconds,
              EXTRACT(EPOCH FROM (now() - max(ls.recorded_at)))::bigint
                  AS staleness_newest_seconds
            FROM scope
            LEFT JOIN core.mv_latest_agent_states ls ON ls.agent_id = scope.agent_id
        """
        state_row = await conn.fetchrow(state_sql, *scope_args)

        # paused_now: agents currently in status='paused'. MUST be computed
        # independently of the scope CTE above. When agent_ids is None that CTE
        # is WHERE a.status='active', so an active-only row set can never contain
        # a paused row — the prior `FILTER (WHERE scope.status='paused')` was
        # structurally always 0 and reported "0 paused" for months while the
        # fleet genuinely had paused agents. Scope to agent_ids when the caller
        # passed an explicit set; otherwise count the whole fleet.
        if agent_ids:
            paused_now = await conn.fetchval(
                "SELECT count(*)::int FROM core.agents "
                "WHERE status = 'paused' AND id = ANY($1::text[])",
                list(agent_ids),
            )
        else:
            paused_now = await conn.fetchval(
                "SELECT count(*)::int FROM core.agents WHERE status = 'paused'"
            )

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
        "paused_now": int(paused_now or 0),
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


@mcp_tool("outcome_evidence_query", timeout=15.0, register=False)
async def handle_outcome_evidence(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Audit outcome_events by corroboration grade and claim-field verification."""
    import json
    from src.db import get_db
    from src.outcome_corroboration import assess_outcome_corroboration

    diagnostic = str(
        arguments.get("diagnostic") or arguments.get("mode") or "claim_only_task_completed"
    ).lower()
    valid_diagnostics = {
        "claim_only_task_completed",
        "agent_summary",
        "field_verification",
        "events",
    }
    if diagnostic not in valid_diagnostics:
        return error_response(
            f"invalid diagnostic {diagnostic!r}",
            error_code="invalid_argument",
            recovery={"valid_diagnostics": sorted(valid_diagnostics)},
        )

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
        limit = int(arguments.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    limit = max(1, min(limit, 5000))

    try:
        min_completions = int(arguments.get("min_completions", 3))
    except (TypeError, ValueError):
        min_completions = 3
    min_completions = max(1, min(min_completions, 1000))

    try:
        low_weight_threshold = float(arguments.get("low_weight_threshold", 0.50))
    except (TypeError, ValueError):
        low_weight_threshold = 0.50

    target_agent_id = arguments.get("target_agent_id")
    outcome_type = arguments.get("outcome_type")
    grade_filter = arguments.get("corroboration_grade")
    include_events_arg = arguments.get("include_events")
    include_events = (
        bool(include_events_arg)
        if include_events_arg is not None
        else diagnostic != "agent_summary"
    )
    include_detail = bool(arguments.get("include_detail", False))

    if diagnostic == "claim_only_task_completed":
        outcome_type = outcome_type or "task_completed"
        grade_filter = grade_filter or "claim_only"

    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              outcome_id::text AS outcome_id,
              ts,
              agent_id,
              session_id,
              outcome_type,
              outcome_score,
              is_bad,
              verification_source,
              detail
            FROM audit.outcome_events
            WHERE ts >= $1
              AND ($2::timestamptz IS NULL OR ts <= $2)
              AND ($3::text IS NULL OR agent_id = $3)
              AND ($4::text IS NULL OR outcome_type = $4)
            ORDER BY ts DESC
            LIMIT $5
            """,
            start_dt,
            end_dt,
            target_agent_id,
            outcome_type,
            limit,
        )

    def _row_detail(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    by_grade: Dict[str, int] = {}
    agent_stats: Dict[str, Dict[str, Any]] = {}
    events: list[Dict[str, Any]] = []

    for row in rows:
        detail = _row_detail(row["detail"])
        verification_source = row["verification_source"] or detail.get("verification_source")
        assessment = assess_outcome_corroboration(
            outcome_type=row["outcome_type"],
            detail=detail,
            verification_source=verification_source,
        )
        metadata = assessment.as_metadata()
        grade = metadata["corroboration_grade"]
        by_grade[grade] = by_grade.get(grade, 0) + 1

        aid = row["agent_id"] or "<unknown>"
        stats = agent_stats.setdefault(aid, {
            "agent_id": aid,
            "total_events": 0,
            "task_completed": 0,
            "claim_only_task_completed": 0,
            "total_evidence_weight": 0.0,
            "by_grade": {},
        })
        stats["total_events"] += 1
        stats["total_evidence_weight"] += float(metadata["evidence_weight"])
        stats["by_grade"][grade] = stats["by_grade"].get(grade, 0) + 1
        if row["outcome_type"] == "task_completed":
            stats["task_completed"] += 1
            if grade == "claim_only":
                stats["claim_only_task_completed"] += 1

        include_row = True
        if grade_filter and grade != str(grade_filter):
            include_row = False
        if diagnostic == "field_verification" and not metadata["claimed_fields"]:
            include_row = False
        if include_row and include_events:
            event = {
                "outcome_id": row["outcome_id"],
                "ts": row["ts"].isoformat() if hasattr(row["ts"], "isoformat") else str(row["ts"]),
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "outcome_type": row["outcome_type"],
                "outcome_score": row["outcome_score"],
                "is_bad": row["is_bad"],
                "verification_source": verification_source,
                **metadata,
                "claim_fields": {
                    "claimed": metadata["claimed_fields"],
                    "verified": metadata["verified_fields"],
                    "unverified": metadata["unverified_fields"],
                },
            }
            if include_detail:
                event["detail"] = detail
            else:
                event["detail_summary"] = {
                    key: detail.get(key)
                    for key in ("source", "summary", "tool", "kind", "exit_code")
                    if key in detail
                }
            events.append(event)

    agents: list[Dict[str, Any]] = []
    for stats in agent_stats.values():
        total = max(1, int(stats["total_events"]))
        avg_weight = float(stats["total_evidence_weight"]) / total
        task_completed = int(stats["task_completed"])
        claim_only_completed = int(stats["claim_only_task_completed"])
        stats.pop("total_evidence_weight", None)
        stats["avg_evidence_weight"] = round(avg_weight, 4)
        stats["claim_only_completion_ratio"] = (
            round(claim_only_completed / task_completed, 4)
            if task_completed else 0.0
        )
        stats["low_corroboration"] = (
            task_completed >= min_completions
            and (
                avg_weight < low_weight_threshold
                or claim_only_completed / max(1, task_completed) >= 0.5
            )
        )
        agents.append(stats)

    agents.sort(
        key=lambda a: (
            not a["low_corroboration"],
            -a["task_completed"],
            a["avg_evidence_weight"],
            a["agent_id"],
        )
    )
    low_agents = [a for a in agents if a["low_corroboration"]]

    payload: Dict[str, Any] = {
        "diagnostic": diagnostic,
        "window": {
            "since": start_dt.isoformat(),
            "until": end_dt.isoformat() if end_dt else None,
            "defaulted": window_defaulted,
        },
        "filters": {
            "target_agent_id": target_agent_id,
            "outcome_type": outcome_type,
            "corroboration_grade": grade_filter,
            "min_completions": min_completions,
            "low_weight_threshold": low_weight_threshold,
        },
        "raw_row_count": len(rows),
        "returned_event_count": len(events),
        "by_grade": by_grade,
        "agents": agents,
        "low_corroboration_agents": low_agents,
        "limit_reached": len(rows) >= limit,
    }
    if include_events:
        payload["events"] = events

    return success_response(payload)


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
            details={
                "next_step": (
                    "Retry observe(action='audit_events') with event_type=<name> "
                    "or event_types=[<name>, ...]."
                ),
                "safe_options": [
                    {
                        "action": "query_one_type",
                        "call": (
                            "observe(action='audit_events', "
                            "event_type='continuity_token_deprecated_accept', since='14d')"
                        ),
                    },
                    {
                        "action": "query_multiple_types",
                        "call": (
                            "observe(action='audit_events', "
                            "event_types=['governance_decision', "
                            "'coordination_failure.mcp_handler_timeout.tool_decorator'], "
                            "since='24h')"
                        ),
                    },
                    {
                        "action": "inspect_tool_schema",
                        "call": "describe_tool(tool_name='observe')",
                    },
                ],
            },
            recovery={
                "required_one_of": ["event_type", "event_types"],
                "examples": [
                    "observe(action='audit_events', event_type='continuity_token_deprecated_accept', since='14d')",
                    "observe(action='audit_events', event_types=['governance_decision'], since='24h')",
                ],
            },
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
