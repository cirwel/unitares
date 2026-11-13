"""
Update Enrichments — Phase 6 functions for process_agent_update.

Each function enriches ctx.response_data with one concern.
Every function is fail-safe: wraps its logic in try/except so a single
enrichment failure never crashes the update.
"""

import asyncio
import hashlib  # noqa: F401
import json
import os
import re
from datetime import datetime, timezone  # noqa: F401
from typing import Any, Optional  # noqa: F401

from config.governance_config import GovernanceConfig
from src.knowledge_authority import is_imported_context
from src.logging_utils import get_logger
from src.monitor_result import DIVERGENCE_LINE_THRESHOLD  # noqa: F401
from src.thread_identity import (
    LINEAGE_SPAWN_REASONS,  # noqa: F401
    classify_episode_fork,  # noqa: F401
    fork_honest_message,  # noqa: F401
)

from .context import UpdateContext
from .pipeline import enrichment
logger = get_logger(__name__)

from .enrichment_helpers import (
    _enrich_trajectory_identity_impl,
    _behavioral_coherence_pattern,  # noqa: F401
    _enrich_learning_context_impl,
    _enrich_websocket_broadcast_impl,
    _enrich_mirror_signals_impl,
    _GAMING_VARIANCE_THRESHOLD,  # noqa: F401
    _GAMING_VARIANCE_NEAR_MAX,  # noqa: F401
    _detect_gaming,  # noqa: F401
    _mark_surfacing_degraded,  # noqa: F401
    _search_kg_by_checkin_text,  # noqa: F401
    _full_text_search_with_recall,  # noqa: F401
    _cached_kg_search_by_checkin_text,  # noqa: F401
    _proactive_kg_due,  # noqa: F401
    _review_nudge_trigger,  # noqa: F401
    _review_nudge_novel,  # noqa: F401
    _dedupe_surfaced_kg,  # noqa: F401
    _has_tight_margin,  # noqa: F401
    _get_complexity_disagreement,  # noqa: F401
    _generate_mirror_reflection,  # noqa: F401
    _should_search_kg_by_checkin_text,  # noqa: F401
    _classify_fork,
    _fork_honest_message,
    enrich_grounding,  # noqa: F401
    run_grounding_stage,  # noqa: F401
)


# Below this blended relevance, a related-discovery match is noise rather than
# signal and is not surfaced as a "build on these" suggestion. The score is the
# post-blend value (semantic similarity blended with connectivity, then decayed),
# so this floor sits well under the raw similarity gate.
_RELATED_DISCOVERY_RELEVANCE_FLOOR = 0.1

# ─── Opt-in proactive KG surfacing (adoption v0) ────────────────────────
# Check-ins never reach this cadence gate unless the caller explicitly sets
# include_memory_suggestions=true. Within that deliberate recall request,
# proactive surfacing can offer strong prior work during healthy steady state.
# It is additionally OFF by default and bounded on three axes so an opt-in
# cannot regress the steady-state latency budget (KG calls amplify ~60x
# in-handler, see CLAUDE.md "Substrate Tax"):
#   1. Cadence — only every UNITARES_KG_PROACTIVE_EVERY-th check-in fires a
#      search (0 = disabled, the default), so cost is amortized to 1/N.
#   2. Relevance — a higher floor than the reactive path: a proactive nudge
#      must be a strong match, not a marginal one.
#   3. Novelty — session-scoped dedup (Redis) so a given discovery is surfaced
#      at most once per session; what reaches the agent is always new to it.
_KG_PROACTIVE_FLOOR = 0.35
# KG-search timeout for the mirror-signals enrichment. The KG semantic/full-text
# search is the dominant check-in tail: per-enrichment telemetry (2026-06-28,
# n=2133) put enrich_mirror_signals at p99=281ms / max=2624ms while every other
# enrichment was <16ms mean — the cost is this anyio-amplified KG I/O (see
# CLAUDE.md "Substrate Tax"). KG surfacing is advisory, so a slow search degrades
# to "no surfacing this turn" (return []) instead of blocking the response.
_KG_SEARCH_TIMEOUT = float(os.getenv("UNITARES_KG_SEARCH_TIMEOUT_S", "0.25"))
# Session-scope TTL for the per-agent set of already-surfaced discovery_ids.
_KG_SURFACED_TTL_SECONDS = 86400

# ─── In-flow review nudge (adoption increment 2, #1685) ─────────────────
# When a check-in itself reports uncertain ground, suggest the one-call
# review surface in the same response. The #1387 adjudication confirmed the
# entry-cost hypothesis (agents do reach for request_review when it works);
# this closes the discovery gap for agents who don't know the surface exists
# at the moment it would help. OFF by default (UNITARES_REVIEW_NUDGE unset).
# Conservative by design — an irrelevant nudge trains agents that the channel
# is noise (the proactive-KG relevance lesson), so it keys only on what the
# agent itself reported or the policy's own guide verdict, never on cadence,
# and fires at most once per agent-session.
_REVIEW_NUDGE_CONF_CEILING = 0.4
_REVIEW_NUDGE_COMPLEXITY_FLOOR = 0.8
_REVIEW_NUDGE_TTL_SECONDS = GovernanceConfig.SESSION_TTL_SECONDS

# ─── Identity Reminder ─────────────────────────────────────────────────

@enrichment(order=10)
def enrich_identity_reminder(ctx: UpdateContext) -> None:
    """Suggest agents set label/purpose during their first 3 updates."""
    try:
        meta = ctx.meta
        if not meta:
            return
        update_count = getattr(meta, 'total_updates', 0) or 0
        if update_count > 3:
            return
        has_label = bool(getattr(meta, 'label', None))
        has_purpose = bool(getattr(meta, 'purpose', None))
        if has_label and has_purpose:
            return
        missing = []
        if not has_label:
            from ..identity_bootstrap import SET_DISPLAY_NAME_CALL

            missing.append(f"label ({SET_DISPLAY_NAME_CALL})")
        if not has_purpose:
            missing.append("purpose (process_agent_update with purpose='...')")
        ctx.response_data['identity_reminder'] = {
            'message': f"Consider setting your {' and '.join(missing)} for better governance tracking.",
            'missing': missing,
            'update_count': update_count,
        }
    except Exception as e:
        logger.debug(f"Could not enrich identity reminder: {e}")

# ─── Interpretation & Feedback ──────────────────────────────────────────

@enrichment(order=20)
async def enrich_state_interpretation(ctx: UpdateContext) -> None:
    """Map raw EISV to semantic state (health / mode / basin)."""
    try:
        monitor = ctx.monitor
        if monitor is None:
            return
        task_type = ctx.agent_state.get("task_type", "mixed")
        interpreted_state = monitor.state.interpret_state(
            risk_score=ctx.risk_score,
            task_type=task_type
        )
        ctx.response_data['state'] = interpreted_state
        from src.governance_glossary import (
            explain_basin,
            explain_mode,
            explain_trajectory,
        )
        state_glossary = {}
        if interpreted_state.get("mode") is not None:
            state_glossary["mode"] = explain_mode(interpreted_state.get("mode"))
        if interpreted_state.get("basin") is not None:
            state_glossary["basin"] = explain_basin(interpreted_state.get("basin"))
        if interpreted_state.get("trajectory") is not None:
            state_glossary["trajectory"] = explain_trajectory(interpreted_state.get("trajectory"))
        if state_glossary:
            ctx.response_data['state_glossary'] = state_glossary

        health = interpreted_state.get('health', 'unknown')
        mode = interpreted_state.get('mode', 'unknown')
        basin = interpreted_state.get('basin', 'unknown')
        ctx.response_data['summary'] = f"{health} | {mode} | {basin} basin"
    except Exception as e:
        logger.debug(f"Could not generate state interpretation: {e}")

@enrichment(order=30)
def enrich_actionable_feedback(ctx: UpdateContext) -> None:
    """Generate context-aware actionable feedback."""
    try:
        from ..utils import generate_actionable_feedback
        monitor = ctx.monitor

        previous_coherence = None
        try:
            if hasattr(monitor, 'state') and hasattr(monitor.state, 'coherence_history'):
                history = monitor.state.coherence_history
                if len(history) >= 2:
                    previous_coherence = history[-2]
        except Exception:
            pass

        actionable_feedback = generate_actionable_feedback(
            metrics=ctx.metrics_dict,
            interpreted_state=ctx.response_data.get('state'),
            task_type=ctx.task_type,
            response_text=ctx.response_text,
            previous_coherence=previous_coherence,
        )
        if actionable_feedback:
            ctx.response_data['actionable_feedback'] = actionable_feedback
    except Exception as e:
        logger.debug(f"Could not generate actionable feedback: {e}")

@enrichment(order=40)
async def enrich_llm_coaching(ctx: UpdateContext) -> None:
    """LLM-powered coaching on guide/pause/reject verdicts only."""
    try:
        verdict = ctx.metrics_dict.get('verdict', 'proceed')
        if verdict == 'proceed':
            return

        from ..support.llm_delegation import explain_anomaly, generate_recovery_coaching

        eisv = {
            'E': ctx.metrics_dict.get('E'),
            'I': ctx.metrics_dict.get('I'),
            'S': ctx.metrics_dict.get('S'),
            'V': ctx.metrics_dict.get('V'),
        }

        if verdict in ('guide', 'pause', 'reject'):
            explanation = await explain_anomaly(
                agent_id=ctx.agent_id,
                anomaly_type=verdict,
                description=ctx.response_data.get('actionable_feedback', verdict),
                metrics=eisv,
                max_tokens=200,
            )
            if explanation:
                ctx.response_data['llm_coaching'] = explanation

        if verdict in ('pause', 'reject'):
            blockers = []
            feedback = ctx.response_data.get('actionable_feedback')
            if isinstance(feedback, str):
                blockers.append(feedback)
            elif isinstance(feedback, dict):
                blockers.append(feedback.get('message', str(feedback)))

            coaching = await generate_recovery_coaching(
                agent_id=ctx.agent_id,
                blockers=blockers or [f"Verdict: {verdict}"],
                current_state={'eisv': eisv},
                max_tokens=200,
            )
            if coaching:
                ctx.response_data['recovery_coaching'] = coaching
    except Exception as e:
        logger.debug(f"LLM coaching enrichment skipped: {e}")


@enrichment(order=50)
def enrich_calibration_feedback(ctx: UpdateContext) -> None:
    """Add calibration feedback (complexity + confidence)."""
    try:
        calibration_feedback = {}

        if 'metrics' in ctx.result:
            metrics = ctx.result['metrics']
            reported_complexity = ctx.complexity
            derived_complexity = metrics.get('complexity', None)
            if derived_complexity is not None and reported_complexity is not None:
                discrepancy = abs(reported_complexity - derived_complexity)
                calibration_feedback['complexity'] = {
                    'reported': reported_complexity,
                    'derived': derived_complexity,
                    'discrepancy': discrepancy,
                    'message': (
                        f"Your reported complexity ({reported_complexity:.2f}) vs system-derived ({derived_complexity:.2f}) "
                        f"differs by {discrepancy:.2f}. "
                        f"{'High discrepancy - consider calibrating your complexity estimates' if discrepancy > 0.3 else 'Good alignment'}"
                    )
                }

        from src.mcp_handlers.utils import get_calibration_feedback
        confidence_feedback = get_calibration_feedback(include_complexity=False)
        if confidence_feedback:
            calibration_feedback.update(confidence_feedback)

        if ctx.calibration_correction_info:
            calibration_feedback['auto_correction'] = {
                'applied': True,
                'details': ctx.calibration_correction_info,
                'message': "Your reported confidence was adjusted based on historical accuracy. This helps calibrate your estimates automatically."
            }

        if calibration_feedback:
            ctx.response_data['calibration_feedback'] = calibration_feedback
    except Exception as e:
        logger.debug(f"Could not generate calibration feedback: {e}")

@enrichment(order=55)
async def enrich_external_grounding(ctx: UpdateContext) -> None:
    """Score the agent's confidence claims against externally-verified outcomes.

    The one piece of self-knowledge an agent cannot derive from its own
    context: whether its claims actually verify. Scores only
    external_signal outcomes (never self-reported ones) against confidence
    the agent itself reported on prior check-ins (never server-derived), and
    self-discloses
    insufficiency below the sample floor instead of going silent — the
    floor counts distinct sessions, not rows, because adjudication batches
    are not independent samples (unitares#1370).
    """
    MIN_N = 10
    MIN_BATCHES = 3
    try:
        if not ctx.agent_id:
            return
        from src.db import get_db
        stats = await get_db().get_agent_external_calibration(ctx.agent_id)
        if not isinstance(stats, dict):
            return  # nothing accrued (or mocked backend) — stay silent rather than report noise

        block: dict[str, Any] = dict(stats)
        if stats["n"] < MIN_N or stats["n_batches"] < MIN_BATCHES:
            block["status"] = "accruing"
            block["message"] = (
                f"External grounding accruing: {stats['n']} externally-verified "
                f"outcome(s) across {stats['n_batches']} session(s) scoreable "
                f"against your confidence claims (floor: {MIN_N} outcomes / "
                f"{MIN_BATCHES} sessions). No calibration verdict yet."
            )
        else:
            gap = stats["mean_claim"] - stats["success_rate"]
            block["status"] = "grounded"
            block["claim_gap"] = round(gap, 3)
            direction = "over-claiming" if gap > 0.1 else (
                "under-claiming" if gap < -0.1 else "well-calibrated")
            block["message"] = (
                f"Across {stats['n']} externally-verified outcomes "
                f"({stats['n_batches']} sessions): mean claimed confidence "
                f"{stats['mean_claim']:.2f} vs verified success rate "
                f"{stats['success_rate']:.2f} — {direction}. "
                f"Brier {stats['brier']:.3f}."
            )
        feedback = ctx.response_data.setdefault('calibration_feedback', {})
        feedback['external_grounding'] = block
    except Exception as e:
        logger.debug(f"Could not compute external grounding: {e}")


# ─── Warnings & Loop Detection ─────────────────────────────────────────

@enrichment(order=60)
def enrich_warnings(ctx: UpdateContext) -> None:
    """Collect warnings: loop cooldown, default agent_id, policy warnings."""
    try:
        mcp_server = ctx.mcp_server
        if not hasattr(ctx, 'warnings') or ctx.warnings is None:
            ctx.warnings = []

        # Loop cooldown
        ctx.loop_info = None
        if ctx.meta and hasattr(ctx.meta, 'loop_cooldown_until') and ctx.meta.loop_cooldown_until:
            try:
                cooldown_until = datetime.fromisoformat(ctx.meta.loop_cooldown_until)
                now = datetime.now()
                if now < cooldown_until:
                    remaining_seconds = (cooldown_until - now).total_seconds()
                    ctx.loop_info = {
                        "active": True,
                        "cooldown_remaining_seconds": round(remaining_seconds, 1),
                        "message": f"Loop detection cooldown active. Wait {remaining_seconds:.1f}s before rapid updates."
                    }
                else:
                    ctx.meta.loop_cooldown_until = None
            except (ValueError, TypeError, AttributeError):
                pass

        # Default agent_id warning
        try:
            default_warning = mcp_server.check_agent_id_default(ctx.agent_id)
            if default_warning:
                ctx.warnings.append(default_warning)
        except (NameError, AttributeError):
            pass
        except Exception as e:
            logger.warning(f"Could not check agent_id default: {e}")

        # Policy warnings
        if ctx.policy_warnings:
            ctx.warnings.extend(ctx.policy_warnings)

        # Apply to response_data
        if ctx.loop_info:
            ctx.response_data['loop_detection'] = ctx.loop_info
        if ctx.warnings:
            ctx.response_data["warning"] = "\n\n".join(ctx.warnings)
    except Exception as e:
        logger.debug(f"Could not enrich warnings: {e}")

# ─── Metric Standardization ────────────────────────────────────────────

@enrichment(order=70)
def enrich_metric_standardization(ctx: UpdateContext) -> None:
    """Standardize metric reporting with agent_id and context."""
    try:
        from src.mcp_handlers.utils import format_metrics_report
        mcp_server = ctx.mcp_server

        if 'metrics' not in ctx.response_data:
            ctx.response_data['metrics'] = {}

        standardized_metrics = format_metrics_report(
            metrics=ctx.response_data['metrics'],
            agent_id=ctx.agent_id,
            include_timestamp=True,
            include_context=True
        )
        ctx.response_data['metrics'] = standardized_metrics
        ctx.response_data["agent_id"] = ctx.agent_id
    except Exception as e:
        logger.debug(f"Could not standardize metrics: {e}")

@enrichment(order=80)
def enrich_health_status_toplevel(ctx: UpdateContext) -> None:
    """Ensure health_status is at top level for easy access."""
    try:
        if 'metrics' in ctx.response_data:
            metrics = ctx.response_data['metrics']
            if 'health_status' in metrics:
                ctx.response_data["health_status"] = metrics['health_status']
                ctx.response_data["health_message"] = metrics.get('health_message', '')
            else:
                ctx.response_data["health_status"] = ctx.response_data.get('status', 'unknown')
                ctx.response_data["health_message"] = ''
        else:
            ctx.response_data["health_status"] = ctx.response_data.get('status', 'unknown')
            ctx.response_data["health_message"] = ''

        # Ensure EISV metrics are always present
        if 'metrics' in ctx.response_data:
            metrics = ctx.response_data['metrics']
            for dim in ('E', 'I', 'S', 'V'):
                if dim not in metrics:
                    metrics[dim] = metrics.get('eisv', {}).get(dim, 0.0)

            if 'eisv' not in metrics:
                metrics['eisv'] = {d: metrics.get(d, 0.0) for d in ('E', 'I', 'S', 'V')}
            else:
                for d in ('E', 'I', 'S', 'V'):
                    metrics['eisv'][d] = metrics.get(d, metrics['eisv'].get(d, 0.0))

            # Ensure risk metrics consistent with get_governance_metrics
            if 'current_risk' not in metrics or 'mean_risk' not in metrics:
                try:
                    monitor = ctx.monitor
                    if monitor is None:
                        raise ValueError("no monitor")
                    monitor_metrics = monitor.get_metrics()
                    if 'current_risk' not in metrics:
                        metrics['current_risk'] = monitor_metrics.get('current_risk')
                    if 'mean_risk' not in metrics:
                        metrics['mean_risk'] = monitor_metrics.get('mean_risk')
                    if 'latest_risk_score' not in metrics:
                        metrics['latest_risk_score'] = monitor_metrics.get('latest_risk_score')
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"Could not enrich health status: {e}")


@enrichment(order=85, lite_safe=True)
def enrich_input_glossary(ctx: UpdateContext) -> None:
    """Attach point-of-use glossary for positional input vectors."""
    try:
        from src.governance_glossary import explain_ethical_drift_vector

        ctx.response_data["input_glossary"] = {
            "ethical_drift": explain_ethical_drift_vector(ctx.ethical_drift),
        }
    except Exception as e:
        logger.debug(f"Could not enrich input glossary: {e}")

# ─── CIRS Response Fields ──────────────────────────────────────────────

@enrichment(order=90)
def enrich_cirs_response_fields(ctx: UpdateContext) -> None:
    """Include CIRS protocol info (void alert, state announce, outcome event)."""
    try:
        if ctx.cirs_alert:
            ctx.response_data["cirs_void_alert"] = {
                "emitted": True,
                "severity": ctx.cirs_alert.get("severity"),
                "V_snapshot": ctx.cirs_alert.get("V_snapshot"),
                "message": f"VOID_ALERT broadcast to peer agents: {ctx.cirs_alert.get('severity', 'warning').upper()}"
            }

        if ctx.outcome_event_id:
            ctx.response_data["outcome_event"] = {
                "emitted": True,
                "outcome_id": ctx.outcome_event_id,
                "outcome_type": "task_completed",
                "message": "Outcome event recorded for EISV validation"
            }

        if ctx.cirs_state_announce:
            ctx.response_data["cirs_state_announce"] = {
                "emitted": True,
                "regime": ctx.cirs_state_announce.get("regime"),
                "update_count": ctx.cirs_state_announce.get("update_count"),
                "message": "STATE_ANNOUNCE broadcast to peer agents"
            }
    except Exception as e:
        logger.debug(f"Could not enrich CIRS fields: {e}")

@enrichment(order=100)
def enrich_cirs_dampening_advisory(ctx: UpdateContext) -> None:
    """Surface CIRS oscillation dampening as an advisory when resonance is active."""
    try:
        cirs = ctx.response_data.get('cirs', {})
        if not cirs.get('resonant'):
            return
        response_tier = cirs.get('response_tier', 'proceed')
        oi = cirs.get('oi', 0.0)
        flips = cirs.get('flips', 0)
        severity = 'high' if response_tier == 'hard_block' else 'moderate'
        ctx.response_data.setdefault('advisories', []).append({
            'source': 'cirs',
            'severity': severity,
            'message': (
                f"Oscillation detected (OI={oi:.2f}, {flips} flips). "
                f"Governance thresholds dampened to stabilize verdict sequence."
            )
        })
    except Exception as e:
        logger.debug(f"Could not enrich CIRS dampening advisory: {e}")

@enrichment(order=110)
def enrich_detected_patterns(ctx: UpdateContext) -> None:
    """Surface pattern tracker detections (loops, time-box, untested hypotheses) as advisories."""
    try:
        from src.pattern_tracker import get_pattern_tracker
        tracker = get_pattern_tracker()
        if not ctx.agent_id:
            return

        patterns_data = tracker.get_patterns(ctx.agent_id)
        detected = []

        # get_patterns returns time_box and untested_hypothesis patterns
        for p in patterns_data.get('patterns', []):
            if p.get('detected', True):  # time_box/hypothesis patterns don't have 'detected' key
                detected.append(p)

        # Also check for loops by scanning recent history
        history = tracker.pattern_history.get(ctx.agent_id, [])
        if history:
            from datetime import datetime, timezone, timedelta
            window_start = datetime.now(timezone.utc) - timedelta(minutes=tracker.window_minutes)
            recent = [p for p in history if p.timestamp >= window_start]
            # Count by (tool, args_hash)
            from collections import Counter
            counts = Counter((p.tool_name, p.args_hash) for p in recent)
            for (tool, _), count in counts.items():
                if count >= tracker.loop_threshold:
                    detected.append({
                        'type': 'loop',
                        'tool_name': tool,
                        'count': count,
                        'message': f"Called {tool} with similar arguments {count} times recently. Consider a different approach.",
                    })

        if not detected:
            return

        severity_map = {'loop': 'high', 'time_box': 'moderate', 'untested_hypothesis': 'moderate'}
        for pattern in detected:
            ctx.response_data.setdefault('advisories', []).append({
                'source': 'pattern_tracker',
                'severity': severity_map.get(pattern.get('type'), 'low'),
                'type': pattern.get('type'),
                'message': pattern.get('message', 'Behavioral pattern detected'),
            })
    except Exception as e:
        logger.debug(f"Could not enrich detected patterns: {e}")

# ─── Knowledge Surfacing ───────────────────────────────────────────────

_QUERY_STOPWORDS = frozenset({
    "about", "after", "again", "against", "because", "been", "before", "being",
    "below", "between", "both", "could", "does", "doing", "down", "during",
    "each", "from", "further", "have", "having", "here", "into", "just", "more",
    "most", "once", "only", "other", "over", "same", "should", "some", "such",
    "than", "that", "their", "them", "then", "there", "these", "they", "this",
    "those", "through", "under", "until", "very", "were", "what", "when",
    "where", "which", "while", "with", "would", "your",
})
_MAX_QUERY_TERMS = 8
_MIN_TERM_LEN = 4
_NON_TOPICAL_AGENT_TAGS = frozenset({
    "autonomous",
    "embodied",
    "engaged_ephemeral",
    "ephemeral",
    "persistent",
    "pioneer",
    "protected",
    "session_like",
})


def _distinctive_terms(text: str, limit: int = _MAX_QUERY_TERMS) -> list:
    """Pick a bounded set of salient terms from free text for a KG lookup.

    Bounded because `websearch_to_tsquery` ANDs bare terms: handing it a whole
    check-in matches nothing, and OR-ing a whole check-in matches everything.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    seen: list = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", text.lower()):
        if len(token) < _MIN_TERM_LEN or token in _QUERY_STOPWORDS:
            continue
        if token not in seen:
            seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def _topical_agent_tags(tags: list | None) -> list:
    """Exclude identity/lifecycle classes from the discovery-tag fallback."""
    return [
        tag
        for tag in (tags or [])
        if isinstance(tag, str)
        and tag.strip()
        and tag.strip().lower() not in _NON_TOPICAL_AGENT_TAGS
    ]


def _memory_suggestions_requested(ctx: UpdateContext) -> bool:
    """Whether this check-in explicitly opted into shared-memory retrieval."""
    arguments = getattr(ctx, "arguments", {})
    value = arguments.get("include_memory_suggestions", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


@enrichment(order=130, lite_safe=True)
async def enrich_knowledge_surfacing(ctx: UpdateContext) -> None:
    """Surface up to 3 prior discoveries when the caller explicitly opts in.

    Retrieval is seeded from the agent's own check-in text, because the tag
    intersection this used to rely on cannot match: agent tags are lifecycle
    values (`ephemeral`, `engaged_ephemeral`) while discovery tags are topical,
    so the two vocabularies overlap on a single token fleet-wide. Tag overlap
    is kept as a fallback for callers that do carry topical tags.
    """
    if not _memory_suggestions_requested(ctx):
        return

    try:
        relevant_discoveries: list = []
        match_basis = ""

        # Reuse the shared check-in-text search rather than issuing our own:
        # it is semantic-first with a full-text fallback, applies the relevance
        # floor, and — the part that matters on this path — wraps every KG call
        # in _KG_SEARCH_TIMEOUT. Measured tail for one such call is p99 281ms /
        # max 2624ms, so an unbudgeted search here would hang the check-in.
        relevant_discoveries = await _cached_kg_search_by_checkin_text(ctx)
        if relevant_discoveries:
            match_basis = "your check-in text"

        if not relevant_discoveries:
            agent_tags = _topical_agent_tags(
                ctx.meta.tags if ctx.meta and ctx.meta.tags else []
            )
            if agent_tags:
                from src.knowledge_graph import get_knowledge_graph
                graph = await get_knowledge_graph()
                tag_matches = await asyncio.wait_for(
                    graph.query(tags=agent_tags, status="open", limit=10),
                    timeout=_KG_SEARCH_TIMEOUT,
                )
                agent_tags_set = set(agent_tags)
                scored = [
                    (len(agent_tags_set & set(d.tags)), d)
                    for d in tag_matches
                    if agent_tags_set & set(d.tags) and not is_imported_context(d)
                ]
                scored.sort(reverse=True, key=lambda x: x[0])
                relevant_discoveries = [
                    d.to_dict(include_details=False) for _, d in scored
                ]
                if relevant_discoveries:
                    match_basis = "your tags"

        relevant_discoveries = relevant_discoveries[:3]

        if relevant_discoveries:
            ctx.response_data["relevant_discoveries"] = {
                "message": (
                    f"Found {len(relevant_discoveries)} relevant "
                    f"discovery/discoveries matching {match_basis}"
                ),
                "discoveries": relevant_discoveries,
                "match_basis": match_basis,
            }
    except Exception as e:
        # Was logger.debug, which made a broken lookup indistinguishable from
        # "nothing relevant" — the field is simply absent either way.
        logger.warning(f"Knowledge surfacing failed: {e}")
        ctx.response_data["knowledge_surfacing_degraded"] = True

# ─── Onboarding Info ───────────────────────────────────────────────────

@enrichment(order=120)
def enrich_onboarding_info(ctx: UpdateContext) -> None:
    """Include onboarding guidance, API key hints, welcome message."""
    try:
        mcp_server = ctx.mcp_server

        if ctx.onboarding_guidance:
            ctx.response_data["onboarding"] = ctx.onboarding_guidance

        if ctx.is_new_agent or ctx.key_was_generated or ctx.api_key_auto_retrieved:
            meta = ctx.meta
            if not meta:
                meta = mcp_server.agent_metadata.get(ctx.agent_id)
            if meta:
                api_key_hint = meta.api_key[:8] + "..." if meta.api_key and len(meta.api_key) > 8 else meta.api_key
                ctx.response_data["api_key_hint"] = api_key_hint
                ctx.response_data["_onboarding"] = {
                    "api_key_hint": api_key_hint,
                    # Do not assert a key was created: this branch also runs for
                    # a new agent that got none, and agents are no longer minted
                    # with the api_key the party HMAC needs (skills/
                    # dialectic-reasoning/SKILL.md). Report what is actually
                    # here, and name the credential that actually authenticates.
                    "message": (
                        "Legacy API key on record; the agent_uuid is what "
                        "authenticates"
                        if api_key_hint
                        else "Identity established; the agent_uuid is what "
                             "authenticates"
                    ),
                    "next_steps": [
                        "Keep the returned client_session_id and pass it on "
                        "later calls in this process",
                        "Identity auto-binds on first tool call - no explicit "
                        "binding needed",
                    ],
                    "identity_binding": {
                        "auto": True,
                        "benefit": "Identity auto-binds on first tool call - no explicit binding needed",
                    },
                    "security_note": "Full API keys are not included in responses to prevent context leakage in multi-agent environments."
                }
                if os.getenv("UNITARES_INCLUDE_API_KEY_IN_RESPONSES") == "1" or os.getenv("PYTEST_CURRENT_TEST"):
                    ctx.response_data["api_key"] = meta.api_key
            if ctx.is_new_agent:
                ctx.response_data["api_key_warning"] = "API keys are deprecated: pass the client_session_id from start_session on later calls as this process's proof; identity(client_session_id='...') shows your agent_uuid."
            elif ctx.key_was_generated:
                ctx.response_data["api_key_warning"] = "API key regenerated (migration). API keys are deprecated: pass the client_session_id from start_session on later calls as this process's proof; identity(client_session_id='...') shows your agent_uuid."
            elif ctx.api_key_auto_retrieved:
                ctx.response_data["api_key_info"] = "Session authenticated via stored credentials. No need to pass api_key."

        meta = ctx.meta
        if meta and meta.total_updates == 1:
            from ..identity_bootstrap import SET_DISPLAY_NAME_CALL

            ctx.response_data["welcome"] = (
                "Welcome to the governance system! This is your first update. "
                "The system keeps a behavioral state estimate of your work (E, I, S, V) and provides "
                "supportive feedback. Use the metrics and sampling parameters as helpful guidance, "
                "not requirements. The knowledge graph contains discoveries from other agents - "
                "feel free to explore it when relevant. "
                "\n\nPass the client_session_id start_session returned on every "
                "call. identity(client_session_id='...') reports the identity "
                f"it names, and {SET_DISPLAY_NAME_CALL} names it."
            )
    except Exception as e:
        logger.debug(f"Could not enrich onboarding info: {e}")

# ─── Convergence Guidance ──────────────────────────────────────────────

@enrichment(order=140)
async def enrich_convergence_guidance(ctx: UpdateContext) -> None:
    """Behavioral EISV guidance for new agents based on safety thresholds."""
    try:
        mcp_server = ctx.mcp_server
        meta = mcp_server.agent_metadata.get(ctx.agent_id)
        if meta and meta.total_updates < 20:
            # Suppress detailed EISV guidance on first few check-ins.
            # Values are still near initialization defaults — guidance based on
            # them is misleading and erodes trust on first interaction.
            if meta.total_updates <= 3:
                ctx.response_data["convergence_guidance"] = {
                    "message": "Not enough data yet to provide meaningful guidance.",
                    "note": f"EISV metrics need several check-ins to diverge from defaults. "
                            f"Current update count: {meta.total_updates}. "
                            f"Detailed guidance will appear after a few more check-ins.",
                    "suppressed": True,
                    "updates_until_guidance": 4 - meta.total_updates,
                }
                return

            metrics_dict = ctx.response_data.get("metrics", {})
            E = metrics_dict.get("E", 0.7)
            I = metrics_dict.get("I", 0.8)
            S = metrics_dict.get("S", 0.2)
            V = metrics_dict.get("V", 0.0)

            # Behavioral EISV: no equilibrium targets. Guidance is based on
            # safety floors and healthy operating ranges, not ODE attractors.
            guidance_items = []

            if S > 0.3:
                guidance_items.append({
                    "metric": "S (Entropy)",
                    "current": f"{S:.3f}",
                    "guidance": "High entropy. Focus on coherent, consistent work to reduce uncertainty.",
                    "priority": "high" if S > 0.5 else "medium"
                })

            if I < 0.4:
                guidance_items.append({
                    "metric": "I (Information Integrity)",
                    "current": f"{I:.3f}",
                    "guidance": "Low integrity. Focus on consistent, well-structured work with accurate confidence.",
                    "priority": "high" if I < 0.3 else "medium"
                })

            if E < 0.4:
                guidance_items.append({
                    "metric": "E (Energy)",
                    "current": f"{E:.3f}",
                    "guidance": "Low energy. Increase productive engagement.",
                    "priority": "high" if E < 0.3 else "medium"
                })

            if abs(V) > 0.2:
                guidance_items.append({
                    "metric": "V (Valence)",
                    "current": f"{V:.3f}",
                    "guidance": "Energy-integrity imbalance. Balance exploration (E) with consistency (I).",
                    "priority": "medium" if abs(V) > 0.3 else "low"
                })

            if guidance_items:
                ctx.response_data["convergence_guidance"] = {
                    "message": "Behavioral guidance (safety thresholds)",
                    "current_state": {"E": E, "I": I, "S": S, "V": V},
                    "guidance": guidance_items,
                    "note": "Guidance based on safety thresholds. Each agent develops its own operating point over time."
                }
    except Exception as e:
        logger.debug(f"Could not generate convergence guidance: {e}", exc_info=True)

# ─── Anti-Stasis Perturbation ──────────────────────────────────────────

@enrichment(order=150)
async def enrich_anti_stasis_perturbation(ctx: UpdateContext) -> None:
    """Surface an open question for stable agents to prevent stasis."""
    try:
        mcp_server = ctx.mcp_server
        meta = mcp_server.agent_metadata.get(ctx.agent_id)
        health_status = ctx.response_data.get("health_status", "unknown")

        if (meta and meta.total_updates >= 10 and
                health_status == "healthy" and
                ctx.response_data.get("metrics", {}).get("S", 1.0) < 0.15):

            last_perturbation = getattr(meta, '_last_perturbation_update', 0)
            if meta.total_updates - last_perturbation >= 5:

                from src.knowledge_graph import get_knowledge_graph
                graph = await get_knowledge_graph()

                agent_tags = meta.tags if meta.tags else []
                open_questions = await graph.query(
                    type="question",
                    status="open",
                    tags=agent_tags if agent_tags else None,
                    limit=3
                )

                if open_questions:
                    question = open_questions[0]
                    ctx.response_data["perturbation"] = {
                        "message": "You've been stable. Here's something unresolved to consider:",
                        "question": {
                            "id": question.id,
                            "summary": question.summary[:300],
                            "tags": question.tags[:5] if question.tags else [],
                            "by": question.agent_id
                        },
                        "invitation": "Stable systems need perturbation to grow. Consider engaging with this open question.",
                        "action": "Use knowledge(action='store') with response_to to contribute your perspective."
                    }
                    meta._last_perturbation_update = meta.total_updates
                    logger.debug(f"Perturbed stable agent {ctx.agent_id[:8]}... with open question")
    except Exception as e:
        logger.debug(f"Could not generate perturbation: {e}")

# ─── Basin Tracking ────────────────────────────────────────────────────

@enrichment(order=160)
def enrich_basin_tracking(ctx: UpdateContext) -> None:
    """Surface v4.1 basin/convergence tracking when available."""
    try:
        metrics_dict = ctx.response_data.get("metrics", {})
        v41_block = metrics_dict.get("unitares_v41")
        if isinstance(v41_block, dict):
            ctx.response_data["unitares_v41"] = v41_block
    except Exception:
        pass

# ─── Trajectory Identity ───────────────────────────────────────────────


@enrichment(order=170)
async def enrich_trajectory_identity(ctx: UpdateContext) -> None:
    """Compare trajectory signature if provided, or compute behavioral trajectory."""
    from src.governance_glossary import (
        annotate_trajectory_signature_terms,
        explain_trust_tier,
    )

    await _enrich_trajectory_identity_impl(
        ctx,
        annotate_fn=annotate_trajectory_signature_terms,
        explain_tier_fn=explain_trust_tier,
        glossary_key="signature_glossary",
    )

# ─── Saturation Diagnostics ────────────────────────────────────────────

@enrichment(order=180)
def enrich_saturation_diagnostics(ctx: UpdateContext) -> None:
    """v4.2-P saturation diagnostics — pressure gauge for I-channel."""
    try:
        from governance_core import compute_saturation_diagnostics
        from governance_core.parameters import DEFAULT_THETA

        monitor = ctx.monitor
        if monitor is None:
            return
        unitares_state = monitor.state.unitaires_state
        theta = getattr(monitor.state, 'unitaires_theta', None) or DEFAULT_THETA

        if unitares_state:
            from src.monitor_setpoint import setpoint_for_monitor
            sat_diag = compute_saturation_diagnostics(
                unitares_state, theta, s_setpoint=setpoint_for_monitor(monitor))
            ctx.response_data['saturation_diagnostics'] = {
                'sat_margin': sat_diag['sat_margin'],
                'dynamics_mode': sat_diag['dynamics_mode'],
                'will_saturate': sat_diag['will_saturate'],
                'at_boundary': sat_diag['at_boundary'],
                'I_equilibrium': sat_diag['I_equilibrium_linear'],
                # Disambiguate from unitares_v41.equilibrium.I_target: this is
                # the *instantaneous* fixed-point implied by the linear I-channel
                # at the current state/params, not the prescribed design target
                # the controller converges toward (dogfood 2026-06-13: 0.52 here
                # vs 1.0 there, surfaced side by side with no label).
                'I_equilibrium_kind': 'instantaneous_linear_fixed_point',
                'forcing_term_A': sat_diag['A'],
                '_interpretation': (
                    "Positive sat_margin means push-to-boundary (logistic mode will saturate I->1)"
                    if sat_diag['sat_margin'] > 0
                    else "Negative sat_margin - stable interior equilibrium exists"
                ),
                '_equilibrium_note': (
                    "I_equilibrium is the instantaneous linear fixed-point for current "
                    "params/state; it is NOT unitares_v41.equilibrium.I_target (the "
                    "prescribed design attractor the controller converges toward)."
                ),
            }
    except Exception as e:
        logger.debug(f"Could not compute saturation diagnostics: {e}")

# ─── Drift Forecast ────────────────────────────────────────────────────

@enrichment(order=185)
def enrich_drift_forecast(ctx: UpdateContext) -> None:
    """Predict drift threshold crossings and project EISV forward."""
    try:
        monitor = ctx.monitor
        if monitor is None or getattr(monitor.state, 'update_count', 0) < 5:
            return

        from src.event_detector import predict_drift_crossing, DRIFT_AXES
        from src.behavioral_trajectory import project_eisv_trajectory

        # Drift EWMA forecast per axis
        drift_forecast = {}
        prev_state = None
        try:
            from src.event_detector import event_detector
            prev_state = event_detector._prev_state.get(ctx.agent_id)
        except Exception:
            pass

        if prev_state:
            drift_history = prev_state.get("drift_history", {})
            for axis in DRIFT_AXES:
                axis_hist = drift_history.get(axis, [])
                if len(axis_hist) >= 3:
                    forecast = predict_drift_crossing(axis_hist)
                    if forecast["predicted_crossing_steps"] is not None:
                        drift_forecast[axis] = forecast

        # EISV forward projection
        state = monitor.state
        eisv_proj = project_eisv_trajectory(
            E_history=list(state.E_history[-20:]),
            I_history=list(state.I_history[-20:]),
            S_history=list(state.S_history[-20:]),
            V_history=list(state.V_history[-20:]),
            steps=5,
        )

        if drift_forecast or (eisv_proj and eisv_proj.get("warnings")):
            ctx.response_data["drift_forecast"] = {
                "axis_forecasts": drift_forecast if drift_forecast else None,
                "eisv_projection": eisv_proj,
            }
    except Exception as e:
        logger.debug(f"Could not compute drift forecast: {e}")

# ─── Pending Dialectic ─────────────────────────────────────────────────

@enrichment(order=190)
async def enrich_pending_dialectic(ctx: UpdateContext) -> None:
    """Notify agent of pending dialectic sessions where they owe a response."""
    try:
        from ..dialectic import ACTIVE_SESSIONS
        from src.dialectic_protocol import DialecticPhase

        pending_dialectic = []
        for session_id, session in ACTIVE_SESSIONS.items():
            if session.reviewer_agent_id == ctx.agent_id and session.phase == DialecticPhase.ANTITHESIS:
                pending_dialectic.append({
                    "session_id": session_id,
                    "role": "reviewer",
                    "phase": "antithesis",
                    "partner": session.paused_agent_id,
                    "topic": getattr(session, 'topic', None),
                    "action_needed": "Submit antithesis via dialectic(action='antithesis')",
                    "created_at": session.created_at.isoformat() if session.created_at else None
                })
            elif session.paused_agent_id == ctx.agent_id and session.phase == DialecticPhase.SYNTHESIS:
                pending_dialectic.append({
                    "session_id": session_id,
                    "role": "initiator",
                    "phase": "synthesis",
                    "partner": session.reviewer_agent_id,
                    "topic": getattr(session, 'topic', None),
                    "action_needed": "Submit synthesis via dialectic(action='synthesis')",
                    "created_at": session.created_at.isoformat() if session.created_at else None
                })

        if pending_dialectic:
            ctx.response_data["pending_dialectic"] = {
                "message": f"You have {len(pending_dialectic)} pending dialectic session(s) awaiting your response!",
                "sessions": pending_dialectic,
                "note": "Dialectic sessions enable collaborative exploration and recovery. Respond to keep the conversation going."
            }
    except Exception as e:
        logger.debug(f"Could not check pending dialectic sessions: {e}")

# ─── EISV Validation ───────────────────────────────────────────────────

@enrichment(order=200)
def enrich_eisv_validation(ctx: UpdateContext) -> None:
    """Ensure all four EISV metrics are present (prevents selection bias)."""
    try:
        from src.eisv.validation import validate_governance_response
        validate_governance_response(ctx.response_data)
    except ImportError:
        pass
    except Exception as validation_error:
        logger.warning(f"EISV validation warning: {validation_error}")
        ctx.response_data["_eisv_validation_warning"] = str(validation_error)

# ─── Outcomes in E (#2610) ──────────────────────────────────────────

@enrichment(order=205, lite_safe=True)
def enrich_outcomes_in_e(ctx: UpdateContext) -> None:
    """Say which recorded outcomes are moving this agent's behavioral E.

    Restates the outcome term of the derivation this check-in already built;
    computes no new signal and reads nothing the policy path uses. ``changed``
    is True when the counts differ from the last account this process showed
    the agent, so filtered modes surface it once per change, not every turn.
    """
    try:
        from src.outcomes_in_e import build_outcomes_in_e, evidence_key

        evidence = build_outcomes_in_e(ctx.agent_state.get("_eisv_derivation"))
        if evidence is None:
            return
        key = evidence_key(evidence)
        monitor = ctx.monitor
        evidence["changed"] = key != getattr(monitor, "_outcomes_in_e_key", None)
        if monitor is not None:
            monitor._outcomes_in_e_key = key
        ctx.response_data["outcomes_in_e"] = evidence
    except Exception as e:
        logger.debug(f"Could not build outcomes_in_e: {e}")


# ─── Reading coverage ───────────────────────────────────────────────

@enrichment(order=206, lite_safe=True)
def enrich_reading_coverage(ctx: UpdateContext) -> None:
    """Say which behavioral axes were measured and which are defaults.

    Restates the observed flags of the derivation this check-in already built;
    computes no new signal and reads nothing the policy path uses. ``changed``
    is True when the set of defaulted axes differs from the previous behavioral
    check-in in this process. Filtered modes show the account on every such
    change, in either direction, so the last account shown always matches the
    stored key. A process starts from "nothing defaulted", so a first reading
    that is fully measured is not news and costs nothing.
    """
    try:
        from src.reading_coverage import build_reading_coverage, evidence_key

        coverage = build_reading_coverage(ctx.agent_state.get("_eisv_derivation"))
        if coverage is None:
            return
        key = evidence_key(coverage)
        monitor = ctx.monitor
        coverage["changed"] = key != getattr(monitor, "_reading_coverage_key", ())
        if monitor is not None:
            monitor._reading_coverage_key = key
        ctx.response_data["reading_coverage"] = coverage
    except Exception as e:
        logger.debug(f"Could not build reading_coverage: {e}")

# ─── Learning Context ──────────────────────────────────────────────────


@enrichment(order=210, lite_safe=True)
async def enrich_learning_context(ctx: UpdateContext) -> None:
    """Surface agent's own history for in-context learning."""
    await _enrich_learning_context_impl(ctx)


# ─── WebSocket Broadcast ───────────────────────────────────────────────

@enrichment(order=220)
async def enrich_websocket_broadcast(ctx: UpdateContext) -> None:
    """Broadcast EISV update to dashboard via WebSocket."""
    await _enrich_websocket_broadcast_impl(ctx)


# ─── Mirror Signals ───────────────────────────────────────────────────

@enrichment(order=240, lite_safe=True)
async def enrich_mirror_signals(ctx: UpdateContext) -> None:
    """Build actionable self-awareness signals for mirror response mode."""
    await _enrich_mirror_signals_impl(ctx)

# ─── Identity Notifications ──────────────────────────────────────────

# P004: bound every Redis await in this MCP-handler path. The anyio<->asyncpg/
# Redis seam can stall an await indefinitely (see CLAUDE.md "Known Issue"); an
# unguarded await here would hang the whole update-enrichment pipeline. This is a
# best-effort surfacing of pending notifications, so on timeout we degrade to
# "no notifications this turn" rather than block. Mirrors the
# _REDIS_RECOVERY_TIMEOUT idiom in mcp_handlers/middleware/identity_step.py.
_REDIS_NOTIF_TIMEOUT = 0.5


@enrichment(order=250)
async def enrich_identity_notifications(ctx: UpdateContext) -> None:
    """Surface pending identity notifications (e.g., session accessed from elsewhere)."""
    try:
        from src.cache.redis_client import get_redis

        redis = await get_redis()
        if not redis:
            return

        key = f"identity_notifications:{ctx.agent_uuid}"
        notifications = await asyncio.wait_for(
            redis.lrange(key, 0, -1), timeout=_REDIS_NOTIF_TIMEOUT
        )
        if not notifications:
            return

        parsed = []
        for n in notifications:
            try:
                parsed.append(json.loads(n) if isinstance(n, (str, bytes)) else n)
            except Exception:
                parsed.append({"message": str(n)})

        if parsed:
            ctx.response_data["_identity_notifications"] = parsed
            # Clear after delivery
            await asyncio.wait_for(redis.delete(key), timeout=_REDIS_NOTIF_TIMEOUT)

    except asyncio.TimeoutError:
        logger.debug(
            "identity notifications check timed out after %.3fs (degrading to none)",
            _REDIS_NOTIF_TIMEOUT,
        )
    except Exception as e:
        logger.debug(f"Could not check identity notifications: {e}")


# ─── Thread Identity ───────────────────────────────────────────────────


@enrichment(order=230)
def enrich_thread_identity(ctx: UpdateContext) -> None:
    """Provide thread continuity context across sessions (honest forking)."""
    try:
        if ctx.meta and getattr(ctx.meta, "thread_id", None):
            position = int(getattr(ctx.meta, "node_index", 1) or 1)
            parent_uuid = getattr(ctx.meta, "parent_agent_id", None)
            spawn_reason = getattr(ctx.meta, "spawn_reason", None)
            agent_uuid = (
                ctx.agent_uuid
                or getattr(ctx.meta, "agent_uuid", None)
                or getattr(ctx.meta, "agent_id", "")
            )
            episode_fork_kind, identity_lineage_fork = _classify_fork(
                position,
                agent_uuid,
                parent_uuid,
                spawn_reason,
            )
            ctx.response_data["thread_context"] = {
                "thread_id": ctx.meta.thread_id,
                "position": position,
                "is_fork": position > 1,
                "episode_fork_kind": episode_fork_kind,
                "identity_lineage_fork": identity_lineage_fork,
                "honest_message": _fork_honest_message(
                    episode_fork_kind,
                    parent_uuid,
                    spawn_reason,
                ),
            }
    except Exception as e:
        logger.debug(f"Could not enrich thread identity: {e}")

# ─── Temporal Context ──────────────────────────────────────────────────

from src.temporal import build_temporal_context

@enrichment(order=215)
async def enrich_temporal_context(ctx: UpdateContext) -> None:
    """Inject temporal awareness when time is telling the agent something."""
    try:
        from src.db import get_db
        temporal = await build_temporal_context(ctx.agent_uuid, get_db())
        if temporal:
            ctx.response_data['temporal_context'] = temporal
    except Exception as e:
        logger.debug(f"Could not enrich temporal context: {e}")


@enrichment(order=260)
async def enrich_agent_profile(ctx: UpdateContext) -> None:
    """Add differentiated agent profile metrics to the response."""
    try:
        from src.agent_profile import get_agent_profile, get_all_profiles
        if ctx.agent_id in get_all_profiles():
            profile = get_agent_profile(ctx.agent_id)
            ctx.response_data['agent_profile'] = profile.to_summary()
    except Exception as e:
        logger.debug(f"Agent profile enrichment skipped: {e}")


