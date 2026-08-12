"""
CIRS coherence_report handler — compute and query pairwise similarity.
"""

from typing import Dict, Any, Sequence, Optional
from datetime import datetime

from mcp.types import TextContent
from ..decorators import mcp_tool
from ..utils import success_response, error_response, require_registered_agent
from src.logging_utils import get_logger
from .types import CoherenceReport
from .storage import _store_coherence_report, _get_coherence_reports

logger = get_logger(__name__)

def compute_pairwise_similarity(source_monitor, target_monitor) -> Optional[CoherenceReport]:
    """Compute similarity between two monitors. Returns CoherenceReport or None on error.

    Pure computation — no MCP, no persistence. Caller decides what to do with the report.
    """
    try:
        source_metrics = source_monitor.get_metrics()
        target_metrics = target_monitor.get_metrics()

        source_eisv = {
            "E": float(source_metrics.get("E", 0.7)),
            "I": float(source_metrics.get("I", 0.8)),
            "S": float(source_metrics.get("S", 0.2)),
            "V": float(source_metrics.get("V", 0.0)),
        }
        target_eisv = {
            "E": float(target_metrics.get("E", 0.7)),
            "I": float(target_metrics.get("I", 0.8)),
            "S": float(target_metrics.get("S", 0.2)),
            "V": float(target_metrics.get("V", 0.0)),
        }

        eisv_similarity = {}
        for dim in ["E", "I", "S", "V"]:
            diff = abs(source_eisv[dim] - target_eisv[dim])
            if dim == "V":
                sim = 1.0 - min(1.0, diff / 0.4)
            else:
                sim = 1.0 - diff
            eisv_similarity[dim] = round(sim, 3)

        overall_eisv_sim = (
            eisv_similarity["E"] * 0.25 +
            eisv_similarity["I"] * 0.35 +
            eisv_similarity["S"] * 0.25 +
            eisv_similarity["V"] * 0.15
        )

        source_regime = str(source_metrics.get("regime", "divergence"))
        target_regime = str(target_metrics.get("regime", "divergence"))
        regime_match = source_regime == target_regime

        source_verdict = str(source_metrics.get("verdict", "caution"))
        target_verdict = str(target_metrics.get("verdict", "caution"))
        verdict_match = source_verdict == target_verdict

        trajectory_similarity = None
        try:
            source_state = source_monitor.state
            target_state = target_monitor.state

            traj_sims = {}
            lambda_diff = abs(float(source_state.lambda1) - float(target_state.lambda1))
            traj_sims["lambda1"] = 1.0 - min(1.0, lambda_diff / 2.0)

            max_updates = max(source_state.update_count, target_state.update_count, 1)
            update_diff = abs(source_state.update_count - target_state.update_count)
            traj_sims["maturity"] = 1.0 - min(1.0, update_diff / max_updates)

            trajectory_similarity = {k: round(v, 3) for k, v in traj_sims.items()}
        except Exception:
            pass

        traj_factor = None
        if trajectory_similarity:
            traj_factor = sum(trajectory_similarity.values()) / len(trajectory_similarity)

        weighted_score = (
            overall_eisv_sim * 0.6
            + (0.1 if regime_match else 0.0)
            + (0.1 if verdict_match else 0.0)
        )
        available_weight = 0.8
        if traj_factor is not None:
            weighted_score += traj_factor * 0.2
            available_weight += 0.2
        similarity_score = round(weighted_score / available_weight, 3)

        return CoherenceReport(
            source_agent_id=getattr(source_monitor, 'agent_id', ''),
            timestamp=datetime.now().isoformat(),
            target_agent_id=getattr(target_monitor, 'agent_id', ''),
            similarity_score=similarity_score,
            eisv_similarity=eisv_similarity,
            regime_match=regime_match,
            verdict_match=verdict_match,
            trajectory_similarity=trajectory_similarity,
            similarity_provenance={
                "formula": "cirs_pairwise_similarity.v2",
                "included_components": {
                    "eisv": ["E", "I", "S", "V"],
                    "trajectory": ["lambda1", "maturity"],
                    "categorical": ["regime_match", "verdict_match"],
                },
                "excluded_components": {
                    "coherence": {
                        "reason": "legacy_tanh_v_is_non_discriminative_control_feedback",
                        "health_evidence": False,
                    }
                },
                "missing_component_policy": "renormalize_available_weights",
            },
        )
    except Exception as e:
        logger.debug(f"Could not compute pairwise similarity: {e}")
        return None
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
@mcp_tool("coherence_report", timeout=15.0, register=False, description="CIRS Protocol: Compute and share pairwise similarity metrics between agents")
async def handle_coherence_report(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    CIRS COHERENCE_REPORT - Multi-agent similarity analysis.

    Two modes:
    1. COMPUTE mode (action='compute'): Compute similarity to another agent
    2. QUERY mode (action='query'): Get recent coherence reports
    """
    action = arguments.get("action", "").lower()

    if not action or action not in ("compute", "query"):
        return [error_response(
            "action parameter required: 'compute' or 'query'",
            recovery={
                "valid_actions": ["compute", "query"],
                "compute_example": "coherence_report(action='compute', target_agent_id='...')",
                "query_example": "coherence_report(action='query', min_similarity=0.7)"
            }
        )]

    if action == "compute":
        return await _handle_coherence_report_compute(arguments)
    else:
        return await _handle_coherence_report_query(arguments)

async def _handle_coherence_report_compute(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle COHERENCE_REPORT compute action"""
    source_agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    target_agent_id = arguments.get("target_agent_id")
    if not target_agent_id:
        return [error_response(
            "target_agent_id required for compute action",
            recovery={
                "action": "Provide target_agent_id parameter",
                "example": "coherence_report(action='compute', target_agent_id='other-agent')"
            }
        )]

    # Get source agent state
    source_monitor = mcp_server.get_or_create_monitor(source_agent_id)
    from src.agent_monitor_state import ensure_hydrated
    await ensure_hydrated(source_monitor, source_agent_id)

    # Get target agent state
    target_monitor = mcp_server.monitors.get(target_agent_id)
    if target_monitor is None:
        import asyncio
        loop = asyncio.get_running_loop()
        persisted_state = await loop.run_in_executor(None, mcp_server.load_monitor_state, target_agent_id)
        if persisted_state:
            from src.governance_monitor import UNITARESMonitor
            target_monitor = UNITARESMonitor(target_agent_id, load_state=False)
            target_monitor.state = persisted_state
        else:
            return [error_response(
                f"Target agent '{target_agent_id}' not found or has no state",
                recovery={
                    "action": "Ensure target agent exists and has been initialized",
                    "related_tools": ["list_agents", "state_announce(action='query')"]
                }
            )]

    report = compute_pairwise_similarity(source_monitor, target_monitor)
    if report is None:
        return [error_response("Failed to compute similarity")]

    _store_coherence_report(report)

    source_metrics = source_monitor.get_metrics()
    target_metrics = target_monitor.get_metrics()
    source_eisv = {d: float(source_metrics.get(d, 0.5)) for d in ["E", "I", "S", "V"]}
    target_eisv = {d: float(target_metrics.get(d, 0.5)) for d in ["E", "I", "S", "V"]}

    overall_eisv_sim = (
        report.eisv_similarity["E"] * 0.25 +
        report.eisv_similarity["I"] * 0.35 +
        report.eisv_similarity["S"] * 0.25 +
        report.eisv_similarity["V"] * 0.15
    )

    source_regime = str(source_metrics.get("regime", "divergence"))
    target_regime = str(target_metrics.get("regime", "divergence"))
    source_verdict = str(source_metrics.get("verdict", "caution"))
    target_verdict = str(target_metrics.get("verdict", "caution"))

    return success_response({
        "action": "compute",
        "report": report.to_dict(),
        "details": {
            "source_eisv": source_eisv,
            "target_eisv": target_eisv,
            "source_regime": source_regime,
            "target_regime": target_regime,
            "source_verdict": source_verdict,
            "target_verdict": target_verdict,
        },
        "message": f"Coherence report: {report.similarity_score:.1%} similarity with {target_agent_id}",
        "cirs_protocol": "COHERENCE_REPORT"
    }, agent_id=source_agent_id)

async def _handle_coherence_report_query(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle COHERENCE_REPORT query action"""
    source_agent_id = arguments.get("source_agent_id")
    target_agent_id = arguments.get("target_agent_id")
    min_similarity = arguments.get("min_similarity")
    limit = int(arguments.get("limit", 50))

    if min_similarity is not None:
        min_similarity = float(min_similarity)

    reports = _get_coherence_reports(
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        min_similarity=min_similarity,
        limit=limit
    )

    summary = {
        "total_reports": len(reports),
        "avg_similarity": sum(r.get("similarity_score", 0) for r in reports) / len(reports) if reports else 0,
        "regime_matches": sum(1 for r in reports if r.get("regime_match")),
        "verdict_matches": sum(1 for r in reports if r.get("verdict_match")),
    }

    return success_response({
        "action": "query",
        "reports": reports,
        "summary": summary,
        "cirs_protocol": "COHERENCE_REPORT",
        "filters_applied": {
            "source_agent_id": source_agent_id,
            "target_agent_id": target_agent_id,
            "min_similarity": min_similarity,
            "limit": limit
        }
    })
