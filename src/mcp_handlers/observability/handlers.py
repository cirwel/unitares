"""
Observability tool handlers.
"""

import asyncio
import math
from typing import Dict, Any, Sequence
from mcp.types import TextContent
from ..utils import success_response, error_response, require_registered_agent
from ..decorators import mcp_tool
from src.governance_glossary import get_eisv_glossary
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.agent_monitor_state import ensure_hydrated
from src.mcp_handlers.observability.analytics_helpers import (  # noqa: F401
    DEFAULT_ANOMALY_SCAN_CAP,
    _OUTCOME_AGGREGATE_BATCH,
    _handle_aggregate_metrics,
    _handle_audit_events,
    _handle_detect_anomalies,
    _handle_outcome_evidence,
    _parse_window_arg,
    anomaly_change_token,
)
logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)


def _coerce_float_metric(value: Any, default: float) -> float:
    """Return a finite float, falling back when sparse state returns None."""
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return default
    return coerced if math.isfinite(coerced) else default


class AmbiguousAgentTarget(LookupError):
    """A public identifier that names more than one agent.

    Carries how many agents matched. Their identities are not listed: the
    caller presented a public string, and which registry rows share it is not
    something that string entitles it to.
    """

    def __init__(self, target: str, count: int):
        super().__init__(f"'{target}' names {count} agents")
        self.target = target
        self.count = count


# Statuses whose agents no longer act. A label or other alias held by one of
# these yields to a live holder of the same alias.
_RETIRED_STATUSES = ("archived", "deleted")


def _resolve_agent_from_memory(target: str) -> str | None:
    """Resolve a UUID, an id the listing printed, or an alias, to a registry key.

    Three tiers, strongest claim first. A later tier is consulted only when
    the earlier ones matched nothing:

    1. The registry UUID.
    2. A server-issued listed identifier: what agent(action="list") prints in
       place of a withheld UUID, when the server composed it (the
       {public_agent_id}_{uuid8} handle, or the agent-redacted digest).
    3. An alias: label, display name, structured id, or the bare bucket
       public_agent_id. One live holder wins; with no live holder, a single
       retired one does.

    Raises AmbiguousAgentTarget when a tier matches more than one agent it
    cannot choose between. Returns None when nothing matches.
    """
    if target in mcp_server.agent_metadata:
        return target

    # Lazy: the lifecycle package imports widely, and this module is loaded
    # by the consolidated router before most handlers.
    from ..lifecycle.disclosure import listed_agent_identifier

    # The listing shows a non-operator caller a public identifier in place of
    # the UUID, and the observe schema sends readers to that listing to find a
    # target. The stored public_agent_id carries no suffix, so comparing the
    # stored fields never matches the id the listing printed; this compares
    # against exactly what the listing prints.
    #
    # It outranks aliases because the server composes it and a label is chosen
    # by the agent that wears it. Ranked the other way, an agent that labelled
    # itself with another agent's handle would take that agent's lookups; and
    # treating the pair as ambiguous instead would let it switch them off.
    # A row whose listed identifier is only its own label or display name is
    # self-chosen, so it belongs to the alias tier.
    issued = [
        uuid_key
        for uuid_key, meta in mcp_server.agent_metadata.items()
        if listed_agent_identifier(uuid_key, meta) == target
        and target not in (getattr(meta, "label", None), getattr(meta, "display_name", None))
    ]
    if len(issued) == 1:
        return issued[0]
    if issued:
        # uuid8 is the first block of the UUID, not a uniqueness guarantee.
        raise AmbiguousAgentTarget(target, len(issued))

    aliased = [
        uuid_key
        for uuid_key, meta in mcp_server.agent_metadata.items()
        if target in (
            getattr(meta, "label", None),
            getattr(meta, "display_name", None),
            getattr(meta, "structured_id", None),
            getattr(meta, "public_agent_id", None),
        )
    ]
    if not aliased:
        return None
    # The registry snapshot keeps archived rows and is loaded newest first, so
    # "first match" answered with whichever row the snapshot held first, live
    # or not. An archived predecessor and its live successor share a label by
    # design (a colliding label is only renamed while the incumbent is
    # active); the live one is the agent the label now names.
    live = [
        uuid_key
        for uuid_key in aliased
        if getattr(mcp_server.agent_metadata[uuid_key], "status", None) not in _RETIRED_STATUSES
    ]
    pool = live or aliased
    if len(pool) == 1:
        return pool[0]
    raise AmbiguousAgentTarget(target, len(pool))


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


def _observed_agent_identifier(agent_id: str, meta: Any) -> tuple[str, bool]:
    """The id to hand back for an observed agent, and whether it was redacted.

    agent(action="list") withholds the UUID from a caller who is neither the
    operator nor the agent itself, so returning it here would undo the
    listing's redaction with one more read. The listing's own rule decides,
    with no exception of observe's own: the operator and the agent itself get
    the UUID, everyone else gets the identifier the listing prints, which
    resolves back to the same agent. That includes a caller who passed the
    UUID. It already holds the value, so nothing is kept from it, and one rule
    with no carve-out is the one a second surface cannot get subtly wrong.
    """
    # Lazy, for the same reason as in _resolve_agent_from_memory.
    from ..lifecycle.disclosure import visible_agent_identifier_for_caller

    return visible_agent_identifier_for_caller(agent_id, meta)


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
                "related_tools": ["agent"],
                "example": "observe(action='agent', target_agent_id='<agent-label>')"
            }
        )]
    # Resolve label/UUID from the in-memory metadata snapshot only. Avoid
    # async DB lookups here: MCP/anyio request handlers can deadlock when they
    # await asyncpg/Redis work. Background loaders keep this snapshot fresh.
    try:
        agent_id = _resolve_agent_from_memory(target)
    except AmbiguousAgentTarget as ambiguous:
        return [error_response(
            f"'{target}' names {ambiguous.count} agents, so it cannot be observed "
            "by that identifier.",
            error_code="AMBIGUOUS_TARGET",
            recovery={
                "action": (
                    "Use the identifier agent(action='list') prints for the "
                    "agent you mean. If the listing prints this same "
                    "identifier for more than one row, only a UUID tells them "
                    "apart, and the operator and each agent itself can see it."
                ),
                "related_tools": ["agent"],
            },
        )]
    if not agent_id:
        return [error_response(
            f"Agent '{target}' not found in active metadata. They may need to check in first.",
            recovery={"related_tools": ["agent"]}
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
                "related_tools": ["get_governance_metrics", "agent"],
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
        risk_score = metrics.get("risk_score")
        observation = {
            "current_state": {
                "E": pE,
                "I": pI,
                "S": pS,
                "V": pV,
                "coherence": float(monitor.state.coherence),
                "risk_score": float(risk_score) if risk_score is not None else None,
                "risk_score_source": metrics.get("risk_score_source"),
                "phi_risk_latest": metrics.get("phi_risk_latest"),
                "phi": metrics.get("phi"),  # Primary physics signal
                "verdict": explain_verdict(
                    metrics.get("verdict"), evidence_source=metrics.get("primary_eisv_source")
                ),  # Primary governance signal
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
    visible_agent_id, uuid_redacted = _observed_agent_identifier(agent_id, meta)
    response_data = {
        "agent_id": visible_agent_id,
        "observation": observation,
        "eisv_labels": get_eisv_glossary(),
    }
    if uuid_redacted:
        response_data["uuid_redacted"] = True
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
                "related_tools": ["agent"],
                "workflow": "1. Call agent(action='list') to see available agents 2. Select 2+ agent_ids to compare"
            }
        )]
    
    requested_compare_metrics = arguments.get("compare_metrics") or [
        "risk_score",
        "E",
        "I",
        "S",
        "V",
    ]
    # Keep the raw scalar visible below for audit compatibility, but never use
    # the overloaded legacy C(V) field to declare peers similar or anomalous.
    # A future producer needs its own calibrated, role-specific comparison.
    compare_metrics = [
        metric for metric in requested_compare_metrics if metric != "coherence"
    ]
    
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
            risk_score = metrics.get("risk_score")
            if risk_score is None:
                risk_score = metrics.get("current_risk")
            coherence = float(monitor.state.coherence) if monitor.state else None
            void_active = bool(monitor.state.void_active) if monitor.state else False
            
            health_status_obj, _ = mcp_server.health_checker.get_health_status(
                risk_score=risk_score,
                coherence=coherence,
                void_active=void_active
            )
            
            # Guard against None values for agents with 0 updates
            # dict.get default is only used when key is ABSENT; if key exists with value=None, it returns None
            _risk = risk_score if risk_score is not None else 0.0
            mean_risk = metrics.get("mean_risk")
            _state = monitor.state
            agents_data.append({
                "agent_id": agent_id,
                # Φ-derived recent trend: telemetry only, never health/recovery authority.
                "current_risk": metrics.get("current_risk"),
                "risk_score": float(_risk),  # Governance/operational risk
                "risk_score_source": metrics.get("risk_score_source"),
                "phi": metrics.get("phi"),  # Primary physics signal
                "verdict": metrics.get("verdict"),  # Primary governance signal
                "mean_risk": mean_risk if mean_risk is not None else 0.0,
                "coherence": float(_state.coherence if _state and _state.coherence is not None else 0.5),
                "coherence_source": metrics.get("coherence_source", "unknown"),
                "coherence_role": metrics.get("coherence_role", "unknown"),
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
                "related_tools": ["agent", "get_governance_metrics", "process_agent_update"],
                "workflow": "1. Call agent(action='list') to verify agents exist 2. Call get_governance_metrics to check if agents have state 3. Call process_agent_update if agents need initialization"
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
        "comparison_policy": {
            "schema": "peer_metric_comparison.v2",
            "requested_metrics": list(requested_compare_metrics),
            "compared_metrics": list(compare_metrics),
            "excluded_metrics": {
                "coherence": {
                    "reason": "producer_overloaded_and_legacy_tanh_v_non_discriminative",
                    "health_evidence": False,
                }
            },
        },
        "eisv_labels": get_eisv_glossary(),
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
    my_V = _coerce_float_metric(my_metrics.get('V'), 0.0)
    my_coherence = _coerce_float_metric(my_metrics.get('coherence'), 0.5)
    my_phi = _coerce_float_metric(my_metrics.get('phi'), 0.0)
    my_verdict = my_metrics.get('verdict', 'caution')
    my_regime = my_metrics.get('regime', 'nominal')
    my_total_updates = my_meta.total_updates if my_meta else 0
    
    # Find similar agents (similar EISV values)
    similar_agents = []
    similarity_threshold = arguments.get("similarity_threshold", 0.15)  # Within 15% on each metric
    similarity_provenance = {
        "formula": "peer_similarity.v2",
        "included_components": ["E", "I", "S"],
        "excluded_components": {
            "coherence": {
                "reason": "producer_overloaded_and_legacy_tanh_v_non_discriminative",
                "health_evidence": False,
            }
        },
    }
    
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
            other_V = _coerce_float_metric(other_metrics.get('V'), 0.0)
            other_coherence = _coerce_float_metric(other_metrics.get('coherence'), 0.5)
            other_phi = _coerce_float_metric(other_metrics.get('phi'), 0.0)
            other_risk = _coerce_float_metric(other_metrics.get('risk_score'), 0.4)
            
            # Calculate similarity (Euclidean distance in EISV space)
            E_diff = abs(my_E - other_E)
            I_diff = abs(my_I - other_I)
            S_diff = abs(my_S - other_S)
            
            # Similar if within threshold on all metrics
            if (E_diff <= similarity_threshold and 
                I_diff <= similarity_threshold and 
                S_diff <= similarity_threshold):
                
                similarity_score = 1.0 - ((E_diff + I_diff + S_diff) / 3.0)
                similar_agents.append({
                    "agent_id": other_id,
                    "similarity_score": similarity_score,
                    "metrics": {
                        "E": other_E,
                        "I": other_I,
                        "S": other_S,
                        "V": other_V,
                        "coherence": other_coherence,
                        "coherence_source": other_metrics.get("coherence_source", "unknown"),
                        "coherence_role": other_metrics.get("coherence_role", "unknown"),
                        "phi": other_phi,
                        "verdict": other_metrics.get('verdict', 'caution'),
                        "risk_score": other_risk
                    },
                    "differences": {
                        "E": other_E - my_E,
                        "I": other_I - my_I,
                        "S": other_S - my_S,
                        "V": other_V - my_V,
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
                "V": my_V,
                "coherence": my_coherence,
                "coherence_source": my_metrics.get("coherence_source", "unknown"),
                "coherence_role": my_metrics.get("coherence_role", "unknown"),
                "phi": my_phi,
                "verdict": my_verdict
            },
            "similarity_provenance": similarity_provenance,
            "eisv_labels": get_eisv_glossary(),
            "suggestion": "Try adjusting similarity_threshold parameter or use observe(action='compare') with specific agent_ids"
        })
    
    # Build comparison response
    comparison_data = {
        "agent_id": agent_id,
        "eisv_labels": get_eisv_glossary(),
        "my_metrics": {
            "E": my_E,
            "I": my_I,
            "S": my_S,
            "V": my_V,
            "coherence": my_coherence,
            "coherence_source": my_metrics.get("coherence_source", "unknown"),
            "coherence_role": my_metrics.get("coherence_role", "unknown"),
            "phi": my_phi,
            "verdict": my_verdict,
            "risk_score": _coerce_float_metric(my_metrics.get('risk_score'), 0.4)
        },
        "similar_agents": top_similar,
        "similarity_provenance": similarity_provenance,
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
        # Rank by absolute delta magnitude
        deltas = [
            ("E", avg_delta_E, "Energy"),
            ("I", avg_delta_I, "Integrity"),
            ("S", avg_delta_S, "Entropy"),
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
                "Try `observe(action='compare')` with specific agent_ids or reduce similarity_threshold to widen the cohort."
            ]

        comparison_data["insights"].append({
            "type": "summary",
            "agent_id": None,
            "insights": bullets,
            "description": "Fallback insights (no threshold-triggered patterns detected)"
        })
    
    return success_response(comparison_data)


async def _fetch_scope_total_updates(conn, scope_clause: str, scope_args: tuple) -> int:
    """Return total_updates across the selected agent scope."""
    updates_sql = f"""
        SELECT COALESCE(
            SUM(COALESCE((i.metadata->>'total_updates')::int, 0)), 0
        )::bigint AS total_updates
        FROM core.agents a
        JOIN core.identities i ON i.agent_id = a.id
        {scope_clause}
    """
    return await conn.fetchval(updates_sql, *scope_args)


@mcp_tool("detect_anomalies", timeout=15.0, register=False)
async def handle_detect_anomalies(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Detect anomalies across agents"""
    return await _handle_detect_anomalies(arguments)


@mcp_tool("aggregate_metrics", timeout=15.0, register=False)
async def handle_aggregate_metrics(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get fleet-level health overview."""
    return await _handle_aggregate_metrics(arguments)


@mcp_tool("outcome_evidence_query", timeout=15.0, register=False)
async def handle_outcome_evidence(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Audit outcome_events by corroboration grade and claim-field verification."""
    return await _handle_outcome_evidence(arguments)


@mcp_tool("audit_events_query", timeout=15.0, register=False)
async def handle_audit_events(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Query the Postgres ``audit.events`` table by event_type + time window."""
    return await _handle_audit_events(arguments)


@mcp_tool("bridge_summary_query", timeout=15.0, register=False)
async def handle_bridge_summary(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Summarize Discord bridge delivery receipts and operator attention.

    This is intentionally backed by the dedicated bridge receipt audit stream,
    not ``/api/events``. The bridge should not re-ingest delivery receipts and
    post about its own posts.
    """
    from src.bridge_events import BridgeEventError, build_bridge_summary

    try:
        payload = await build_bridge_summary(arguments)
    except BridgeEventError as exc:
        return error_response(str(exc), error_code="invalid_argument")
    return success_response(payload)


# REMOVED: handle_get_status - redundant with get_governance_metrics
# Use check_working_state() or get_governance_metrics() instead
