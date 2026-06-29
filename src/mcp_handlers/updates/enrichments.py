"""
Update Enrichments — Phase 6 functions for process_agent_update.

Each function enriches ctx.response_data with one concern.
Every function is fail-safe: wraps its logic in try/except so a single
enrichment failure never crashes the update.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from src.logging_utils import get_logger
from src.monitor_result import DIVERGENCE_LINE_THRESHOLD
from src.thread_identity import (
    LINEAGE_SPAWN_REASONS,
    classify_episode_fork,
    fork_honest_message,
)

from .context import UpdateContext
from .pipeline import enrichment
logger = get_logger(__name__)

# Below this blended relevance, a related-discovery match is noise rather than
# signal and is not surfaced as a "build on these" suggestion. The score is the
# post-blend value (semantic similarity blended with connectivity, then decayed),
# so this floor sits well under the raw similarity gate.
_RELATED_DISCOVERY_RELEVANCE_FLOOR = 0.1

# ─── Proactive KG surfacing (adoption v0) ───────────────────────────────
# The reactive gate (_should_search_kg_by_checkin_text) only surfaces prior
# work when an agent is already in trouble or already asking. Proactive
# surfacing also offers strong, relevant prior discoveries during *healthy,
# steady-state* work — the moment a "someone already solved this" note prevents
# wasted effort. It is OFF by default and bounded on three axes so it cannot
# regress the steady-state latency budget (KG calls amplify ~60x in-handler,
# see CLAUDE.md "Substrate Tax"):
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
            missing.append("label (identity(name='YourName'))")
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

@enrichment(order=130, lite_safe=True)
async def enrich_knowledge_surfacing(ctx: UpdateContext) -> None:
    """Surface top 3 relevant discoveries based on agent tags."""
    try:
        agent_tags = ctx.meta.tags if ctx.meta and ctx.meta.tags else []

        if agent_tags:
            from src.knowledge_graph import get_knowledge_graph
            graph = await get_knowledge_graph()

            tag_matches = await graph.query(tags=agent_tags, status="open", limit=10)

            scored = []
            agent_tags_set = set(agent_tags)
            for disc in tag_matches:
                disc_tags_set = set(disc.tags)
                overlap = len(agent_tags_set & disc_tags_set)
                if overlap > 0:
                    scored.append((overlap, disc))

            scored.sort(reverse=True, key=lambda x: x[0])
            relevant_discoveries = [disc.to_dict(include_details=False) for _, disc in scored[:3]]

            if relevant_discoveries:
                ctx.response_data["relevant_discoveries"] = {
                    "message": f"Found {len(relevant_discoveries)} relevant discovery/discoveries matching your tags",
                    "discoveries": relevant_discoveries
                }
    except Exception as e:
        logger.debug(f"Could not surface relevant discoveries: {e}")

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
                    "message": "API key created (use get_agent_api_key to retrieve full key)",
                    "next_steps": [
                        "Call get_agent_api_key(agent_id) to retrieve your full API key",
                        "Identity auto-binds on first tool call - API key auto-retrieved for all subsequent calls",
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
                ctx.response_data["api_key_warning"] = "Use get_agent_api_key(agent_id) to retrieve your API key. Save it securely."
            elif ctx.key_was_generated:
                ctx.response_data["api_key_warning"] = "API key regenerated (migration). Use get_agent_api_key(agent_id) to retrieve it."
            elif ctx.api_key_auto_retrieved:
                ctx.response_data["api_key_info"] = "Session authenticated via stored credentials. No need to pass api_key."

        meta = ctx.meta
        if meta and meta.total_updates == 1:
            ctx.response_data["welcome"] = (
                "Welcome to the governance system! This is your first update. "
                "The system tracks your work's thermodynamic state (E, I, S, V) and provides "
                "supportive feedback. Use the metrics and sampling parameters as helpful guidance, "
                "not requirements. The knowledge graph contains discoveries from other agents - "
                "feel free to explore it when relevant. "
                "\n\nYour identity auto-binds to this session. Use identity() to check it, "
                "or identity(name='YourName_model_date') to name yourself."
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
                    "metric": "V (Void)",
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
                        "action": "Use store_knowledge_graph with response_to to contribute your perspective."
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
    trajectory_signature = ctx.arguments.get("trajectory_signature")

    # Compute behavioral trajectory for non-embodied agents (no anima sensors)
    if not trajectory_signature or not isinstance(trajectory_signature, dict):
        try:
            monitor = ctx.monitor
            if monitor and getattr(monitor.state, 'update_count', 0) >= 10:
                # Track task_type counts on the monitor
                monitor._task_type_counts = getattr(monitor, '_task_type_counts', {})
                tt = getattr(ctx, 'task_type', 'mixed') or 'mixed'
                monitor._task_type_counts[tt] = monitor._task_type_counts.get(tt, 0) + 1

                from src.behavioral_trajectory import compute_behavioral_trajectory
                from src.mcp_handlers.updates.context import get_mean_calibration_error

                cal_error = get_mean_calibration_error(ctx)

                # Use lifetime update count (persisted in DB), not session count.
                # Session count resets on service restart, which prevents trust
                # tiers from ever graduating past "emerging".
                lifetime_updates = monitor.state.update_count
                if ctx.meta and hasattr(ctx.meta, 'total_updates'):
                    lifetime_updates = max(lifetime_updates, ctx.meta.total_updates)

                trajectory_signature = compute_behavioral_trajectory(
                    E_history=list(monitor.state.E_history),
                    I_history=list(monitor.state.I_history),
                    S_history=list(monitor.state.S_history),
                    V_history=list(monitor.state.V_history),
                    coherence_history=list(monitor.state.coherence_history),
                    decision_history=list(getattr(monitor.state, 'decision_history', [])),
                    regime_history=list(getattr(monitor.state, 'regime_history', [])),
                    update_count=lifetime_updates,
                    task_type_counts=getattr(monitor, '_task_type_counts', None),
                    calibration_error=cal_error,
                )
                if trajectory_signature:
                    logger.debug(f"[TRAJECTORY] Behavioral trajectory computed for {ctx.agent_uuid[:8]}... (update_count={monitor.state.update_count})")
        except Exception as e:
            logger.debug(f"[TRAJECTORY] Behavioral trajectory computation failed: {e}")
            pass  # Fail-safe: trajectory stays None, no crash

    if not trajectory_signature or not isinstance(trajectory_signature, dict):
        return

    # Override observation_count with governance lifetime count for ALL agents
    # (including embodied agents that provide their own trajectory_signature).
    # The anima-mcp observation_count tracks internal cycles, not governance check-ins.
    try:
        lifetime_updates = 0
        monitor = ctx.monitor
        if monitor:
            lifetime_updates = getattr(monitor.state, 'update_count', 0)
        if ctx.meta and hasattr(ctx.meta, 'total_updates'):
            lifetime_updates = max(lifetime_updates, ctx.meta.total_updates)
        if lifetime_updates > trajectory_signature.get("observation_count", 0):
            trajectory_signature["observation_count"] = lifetime_updates
            # Recompute identity_confidence with governance lifetime count
            stability = trajectory_signature.get("stability_score", 0.5)
            trajectory_signature["identity_confidence"] = min(1.0, lifetime_updates / 200.0) * stability
    except Exception:
        pass  # Non-critical: use original values

    try:
        from src.trajectory_identity import TrajectorySignature, update_current_signature
        sig = TrajectorySignature.from_dict(trajectory_signature)
        trajectory_result = await update_current_signature(ctx.agent_uuid, sig)

        if trajectory_result and not trajectory_result.get("error"):
            ctx.response_data["trajectory_identity"] = {
                "updated": trajectory_result.get("stored", False),
                "observation_count": trajectory_result.get("observation_count"),
                "identity_confidence": trajectory_result.get("identity_confidence"),
            }
            from src.governance_glossary import annotate_trajectory_signature_terms
            signature_glossary = annotate_trajectory_signature_terms(trajectory_signature)
            if signature_glossary:
                ctx.response_data["trajectory_identity"]["signature_glossary"] = signature_glossary

            if "lineage_similarity" in trajectory_result:
                ctx.response_data["trajectory_identity"]["lineage"] = {
                    "similarity": trajectory_result["lineage_similarity"],
                    "threshold": trajectory_result.get("lineage_threshold", 0.6),
                    "is_anomaly": trajectory_result.get("is_anomaly", False),
                }
                if trajectory_result.get("is_anomaly"):
                    ctx.response_data["trajectory_identity"]["warning"] = trajectory_result.get("warning")
                    logger.warning(f"[TRAJECTORY] Anomaly detected for {ctx.agent_uuid[:8]}...")

            elif trajectory_result.get("genesis_created"):
                ctx.response_data["trajectory_identity"]["genesis_created"] = True
                logger.info(f"[TRAJECTORY] Created genesis S_0 for {ctx.agent_uuid[:8]}... on first update")

            # Trust tier computation (S6 Option B: substrate-earned routing)
            try:
                from src.identity.trust_tier_routing import resolve_trust_tier
                from src.db import get_db as _get_db

                trust_tier = trajectory_result.get("trust_tier")
                if not trust_tier:
                    identity = await _get_db().get_identity(ctx.agent_uuid)
                    if identity and identity.metadata:
                        _meta = ctx.meta
                        trust_tier = await resolve_trust_tier(
                            ctx.agent_uuid,
                            identity.metadata,
                            prefetched_tags=getattr(_meta, "tags", None) if _meta else None,
                            prefetched_label=getattr(_meta, "label", None) if _meta else None,
                        )

                if trust_tier:
                    from src.governance_glossary import explain_trust_tier
                    explained_trust_tier = explain_trust_tier(trust_tier)
                    ctx.response_data["trajectory_identity"]["trust_tier"] = explained_trust_tier

                    mcp_server = ctx.mcp_server
                    meta = ctx.meta or mcp_server.agent_metadata.get(ctx.agent_id)
                    if meta:
                        meta.trust_tier = explained_trust_tier.get("name", "unknown")
                        meta.trust_tier_num = explained_trust_tier.get("tier", 0)

                    tier_num = explained_trust_tier.get("tier", 0)
                    is_anomaly = trajectory_result.get("is_anomaly", False)

                    risk_adj = 0.0
                    risk_reason = None

                    if is_anomaly:
                        risk_adj = 0.15
                        risk_reason = "Behavioral deviation detected (lineage < 0.6)"
                    elif tier_num <= 1:
                        risk_adj = 0.05
                        risk_reason = f"Trust tier {tier_num} ({explained_trust_tier['name']}): identity not yet established"
                    elif tier_num == 3:
                        risk_adj = -0.05
                        risk_reason = f"Trust tier 3 (verified): earned trust reduces friction"

                    if risk_adj != 0.0 and "metrics" in ctx.response_data:
                        original_risk = ctx.response_data["metrics"].get("risk_score")
                        if original_risk is not None:
                            adjusted_risk = max(0.0, min(1.0, original_risk + risk_adj))
                            ctx.response_data["metrics"]["risk_score"] = round(adjusted_risk, 4)
                            ctx.response_data["metrics"]["trajectory_risk_adjustment"] = {
                                "original": round(original_risk, 4),
                                "adjusted": round(adjusted_risk, 4),
                                "delta": risk_adj,
                                "reason": risk_reason,
                            }
                            logger.info(
                                f"[TRAJECTORY] Risk adjusted for {ctx.agent_uuid[:8]}...: "
                                f"{original_risk:.3f} -> {adjusted_risk:.3f} ({risk_reason})"
                            )
            except Exception as e:
                logger.debug(f"[TRAJECTORY] Trust tier computation failed: {e}")

    except Exception as e:
        logger.debug(f"[TRAJECTORY] Could not update trajectory: {e}")

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
            sat_diag = compute_saturation_diagnostics(unitares_state, theta)
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
                    "action_needed": "Submit antithesis via submit_antithesis()",
                    "created_at": session.created_at.isoformat() if session.created_at else None
                })
            elif session.paused_agent_id == ctx.agent_id and session.phase == DialecticPhase.SYNTHESIS:
                pending_dialectic.append({
                    "session_id": session_id,
                    "role": "initiator",
                    "phase": "synthesis",
                    "partner": session.reviewer_agent_id,
                    "topic": getattr(session, 'topic', None),
                    "action_needed": "Submit synthesis via submit_synthesis()",
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
        from src.eisv_validator import validate_governance_response
        validate_governance_response(ctx.response_data)
    except ImportError:
        pass
    except Exception as validation_error:
        logger.warning(f"EISV validation warning: {validation_error}")
        ctx.response_data["_eisv_validation_warning"] = str(validation_error)

# ─── Learning Context ──────────────────────────────────────────────────

@enrichment(order=210, lite_safe=True)
async def enrich_learning_context(ctx: UpdateContext) -> None:
    """Surface agent's own history for in-context learning."""
    try:
        learning_context = {}

        # 1. Recent decisions from audit log
        try:
            from src.audit_log import AuditLogger
            audit_logger = AuditLogger()
            # query_audit_log does a reverse-scan over .jsonl files (sync
            # blocking I/O on the event loop). Push to the default executor
            # so other handlers can progress while the scan runs — the scan
            # itself is the ~700ms floor on agents with deep audit history
            # (post-lock enrichment profile, 2026-05-28).
            loop = asyncio.get_event_loop()
            recent_events = await loop.run_in_executor(
                None,
                lambda: audit_logger.query_audit_log(agent_id=ctx.agent_id, limit=10),
            )
            if recent_events:
                recent_decisions = []
                for event in recent_events[:5]:
                    details = event.get("details", {})
                    decision_summary = {
                        "timestamp": event.get("timestamp", "")[:19],
                        "action": details.get("action") or details.get("decision") or event.get("event_type"),
                        "risk": round(details.get("risk_score", 0), 2) if details.get("risk_score") else None,
                        "confidence": round(details.get("confidence", 0), 2) if details.get("confidence") else None,
                    }
                    if decision_summary.get("action"):
                        recent_decisions.append(decision_summary)

                if recent_decisions:
                    learning_context["recent_decisions"] = {
                        "count": len(recent_decisions),
                        "decisions": recent_decisions,
                        "insight": "Your recent actions - notice patterns in what worked"
                    }
        except Exception as e:
            logger.debug(f"Could not fetch recent decisions: {e}")

        # 2. Agent's own knowledge graph contributions
        try:
            from src.knowledge_graph import get_knowledge_graph
            graph = await get_knowledge_graph()
            my_discoveries = await graph.query(agent_id=ctx.agent_id, limit=5)
            if my_discoveries:
                learning_context["my_contributions"] = {
                    "count": len(my_discoveries),
                    "recent": [
                        {
                            "summary": d.summary[:100] + "..." if len(d.summary) > 100 else d.summary,
                            "type": d.discovery_type,
                            "status": d.status
                        }
                        for d in my_discoveries[:3]
                    ],
                    "insight": "Your recent discoveries - build on these"
                }
        except Exception as e:
            logger.debug(f"Could not fetch agent's discoveries: {e}")

        # 3. Calibration insight
        try:
            from src.calibration import calibration_checker

            bin_stats = calibration_checker.bin_stats
            total = sum(s['count'] for s in bin_stats.values())

            if total >= 10:
                total_healthy = sum(s.get('actual_correct', 0) for s in bin_stats.values())
                trajectory_health = total_healthy / total if total > 0 else 0

                high_conf_bins = ['0.7-0.8', '0.8-0.9', '0.9-1.0']
                low_conf_bins = ['0.0-0.5', '0.5-0.7']

                high_conf_total = sum(bin_stats.get(b, {}).get('count', 0) for b in high_conf_bins)
                high_conf_healthy = sum(bin_stats.get(b, {}).get('actual_correct', 0) for b in high_conf_bins)
                high_conf_trajectory_health = high_conf_healthy / high_conf_total if high_conf_total > 0 else 0

                low_conf_total = sum(bin_stats.get(b, {}).get('count', 0) for b in low_conf_bins)
                low_conf_healthy = sum(bin_stats.get(b, {}).get('actual_correct', 0) for b in low_conf_bins)
                low_conf_trajectory_health = low_conf_healthy / low_conf_total if low_conf_total > 0 else 0

                # Require sufficient samples in BOTH groups before comparing
                min_per_group = 5
                if high_conf_total < min_per_group or low_conf_total < min_per_group:
                    cal_insight = None  # Insufficient per-group data — don't claim inversion
                elif high_conf_trajectory_health < low_conf_trajectory_health - 0.2:
                    cal_insight = (
                        "INVERTED CALIBRATION: High confidence correlates with "
                        "LOWER trajectory health. Consider being more humble."
                    )
                elif abs(high_conf_trajectory_health - low_conf_trajectory_health) < 0.1:
                    cal_insight = "Well calibrated - confidence tracks trajectory health"
                else:
                    cal_insight = f"Calibration data available ({total} decisions auto-evaluated)"

                if cal_insight is not None:
                    learning_context["calibration"] = {
                        "total_decisions": total,
                        # Self-describe the denominator so it can't be confused
                        # with calibration_feedback's sample count (dogfood
                        # 2026-06-13: two calibration counts in one payload, no
                        # indication of what each measured). Both count the same
                        # fleet-wide STRATEGIC trajectory population.
                        "scope": "fleet",
                        "population": "strategic_trajectory_decisions",
                        "trajectory_health": round(trajectory_health, 2),
                        "high_confidence_trajectory_health": round(high_conf_trajectory_health, 2),
                        "low_confidence_trajectory_health": round(low_conf_trajectory_health, 2),
                        # Legacy aliases retained for downstream clients that still
                        # read strategic trajectory bins under their old names.
                        "overall_accuracy": round(trajectory_health, 2),
                        "high_confidence_accuracy": round(high_conf_trajectory_health, 2),
                        "low_confidence_accuracy": round(low_conf_trajectory_health, 2),
                        "insight": cal_insight,
                        "source": (
                            "auto-collected from trajectory outcomes "
                            "(strategic health; no human input required)"
                        ),
                    }

        except Exception as e:
            logger.debug(f"Could not fetch calibration data: {e}")

        # 4. Pattern detection
        try:
            monitor = ctx.monitor
            if monitor is None:
                raise ValueError("no monitor")
            state = monitor.state

            patterns = []

            if hasattr(state, 'regime'):
                regime_duration = getattr(state, 'regime_duration', 0)
                if regime_duration > 5:
                    patterns.append(f"In {state.regime} regime for {regime_duration} updates")

            E = ctx.response_data.get('metrics', {}).get('E', 0.7)
            if E > 0.85:
                patterns.append("High energy - consider channeling into focused work")
            elif E < 0.5:
                patterns.append("Low energy - consider taking a step back")

            coherence_val = ctx.response_data.get('metrics', {}).get('coherence', 0.5)
            if coherence_val < 0.4:
                patterns.append("Low coherence - your approach may be scattered")
            elif coherence_val > 0.8:
                patterns.append("High coherence - maintaining consistent approach")

            if patterns:
                learning_context["patterns"] = {
                    "observations": patterns,
                    "insight": "Patterns from your work - use these for self-awareness"
                }
        except Exception as e:
            logger.debug(f"Could not detect patterns: {e}")

        if learning_context:
            ctx.response_data["learning_context"] = {
                "_purpose": "Your own history, surfaced for in-context learning",
                **learning_context
            }
    except Exception as e:
        logger.debug(f"Could not build learning context: {e}")

# ─── WebSocket Broadcast ───────────────────────────────────────────────

@enrichment(order=220)
async def enrich_websocket_broadcast(ctx: UpdateContext) -> None:
    """Broadcast EISV update to dashboard via WebSocket."""
    try:
        from src.broadcaster import broadcaster_instance

        if broadcaster_instance is None:
            return

        mcp_server = ctx.mcp_server
        metrics = ctx.response_data.get("metrics", {})

        logger.info(
            f"Broadcast metrics for {ctx.declared_agent_id}: "
            f"E={metrics.get('E')}, I={metrics.get('I')}, S={metrics.get('S')}, V={metrics.get('V')}, "
            f"coherence={metrics.get('coherence')}"
        )

        # Extract sensor data if present (Lumen check-ins)
        # Accept as direct argument (preferred) or legacy parameters list format
        broadcast_sensor_data = ctx.arguments.get("sensor_data")
        if broadcast_sensor_data is None:
            params_raw = ctx.arguments.get("parameters", [])
            if isinstance(params_raw, list):
                for p in params_raw:
                    if isinstance(p, dict) and p.get("key") == "sensor_data":
                        try:
                            broadcast_sensor_data = json.loads(p.get("value"))
                            break
                        except Exception:
                            pass

        # Extract EISV values
        eisv_nested = metrics.get("eisv", {})
        eisv_data = {
            "E": metrics.get("E") if metrics.get("E") is not None else eisv_nested.get("E", 0),
            "I": metrics.get("I") if metrics.get("I") is not None else eisv_nested.get("I", 0),
            "S": metrics.get("S") if metrics.get("S") is not None else eisv_nested.get("S", 0),
            "V": metrics.get("V") if metrics.get("V") is not None else eisv_nested.get("V", 0)
        }
        coherence_val = metrics.get("coherence") if metrics.get("coherence") is not None else 0

        # Display name
        display_name = ctx.label if ctx.label else ctx.declared_agent_id
        if display_name and len(display_name) == 36 and '-' in display_name:
            try:
                if ctx.agent_uuid in mcp_server.agent_metadata:
                    cached_label = getattr(mcp_server.agent_metadata[ctx.agent_uuid], 'label', None)
                    if cached_label:
                        display_name = cached_label
            except Exception:
                pass

        # Risk values
        risk_adjusted = metrics.get("risk_score", 0)
        risk_raw = metrics.get("current_risk") or metrics.get("latest_risk_score") or risk_adjusted
        trajectory_adj = metrics.get("trajectory_risk_adjustment", {})
        risk_adj_delta = trajectory_adj.get("delta", 0) if trajectory_adj else 0
        risk_adj_reason = trajectory_adj.get("reason", "") if trajectory_adj else ""

        # Governance events
        governance_events = []
        try:
            from src.event_detector import event_detector
            decision = ctx.response_data.get("decision", {})
            ethical_drift = ctx.ethical_drift
            governance_events = event_detector.detect_events(
                agent_id=ctx.agent_uuid,
                agent_name=display_name,
                action=decision.get("action", "proceed"),
                risk=risk_adjusted,
                risk_raw=risk_raw,
                risk_adjustment=risk_adj_delta,
                risk_reason=risk_adj_reason,
                drift=ethical_drift if isinstance(ethical_drift, list) else [0, 0, 0],
                verdict=metrics.get("verdict", "safe"),
            )
            if governance_events:
                logger.info(f"Events detected for {display_name}: {[e['type'] for e in governance_events]}")
        except Exception as e:
            logger.debug(f"Could not detect events: {e}")

        # Drift trends
        drift_trends = {}
        try:
            from src.event_detector import event_detector as _ed
            drift_trends = _ed.get_drift_trends(ctx.agent_uuid)
        except Exception:
            pass

        await broadcaster_instance.broadcast({
            "type": "eisv_update",
            "agent_id": ctx.agent_uuid,
            "agent_name": display_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "eisv": eisv_data,
            "coherence": coherence_val,
            "metrics": metrics,
            "decision": ctx.response_data.get("decision", {}),
            "inputs": {
                "complexity": ctx.complexity,
                "confidence": ctx.confidence,
                "ethical_drift": ctx.ethical_drift if isinstance(ctx.ethical_drift, list) else [0, 0, 0]
            },
            "risk": risk_adjusted,
            "risk_raw": risk_raw,
            "risk_adjustment": risk_adj_delta,
            "risk_reason": risk_adj_reason,
            "events": governance_events,
            "drift_trends": drift_trends,
            "sensor_data": broadcast_sensor_data
        })
        logger.debug(f"Broadcast EISV update for agent {ctx.declared_agent_id}: eisv={eisv_data}, coherence={coherence_val}")
    except Exception as e:
        logger.debug(f"Could not broadcast EISV update: {e}")

# ─── Mirror Signals ───────────────────────────────────────────────────

@enrichment(order=240, lite_safe=True)
async def enrich_mirror_signals(ctx: UpdateContext) -> None:
    """Build actionable self-awareness signals for mirror response mode.

    Produces:
      - _mirror_signals: list of short insight strings
      - _mirror_kg_results: relevant KG discoveries by text search
      - _mirror_reflection: targeted descriptive reflection when state warrants it
      - _has_sensor_data: bool for auto-mode routing
    """
    try:
        # Detect embodiment (sensor_data presence) for auto-mode routing
        has_sensor_data = bool(ctx.arguments.get("sensor_data"))
        if not has_sensor_data:
            params_raw = ctx.arguments.get("parameters", [])
            if isinstance(params_raw, list):
                for p in params_raw:
                    if isinstance(p, dict) and p.get("key") == "sensor_data":
                        has_sensor_data = True
                        break
        ctx.response_data["_has_sensor_data"] = has_sensor_data

        signals = []
        # Phase 0 instrumentation: structured trigger records for the numeric
        # signals, surfaced-or-not is decided later in response_formatter once
        # the response_mode is resolved (mirror-effectiveness-measurement-v0).
        signal_records: list = []

        # 1. Gaming / autopilot detection (low variance in recent reports)
        signals.extend(_detect_gaming(ctx, records=signal_records))

        # 2. Targeted reflection — descriptive, only when state warrants it
        reflection = _generate_mirror_reflection(ctx, signals)
        if reflection:
            ctx.response_data["_mirror_reflection"] = reflection

        # 3. Reactive KG search — useful when there is a concrete signal /
        #    reflection / edge, not on steady-state check-ins.
        if _should_search_kg_by_checkin_text(ctx, signals, reflection):
            kg_results = await _search_kg_by_checkin_text(ctx)
            if kg_results:
                ctx.response_data["_mirror_kg_results"] = kg_results
        # 3b. Proactive KG surfacing — on a throttled cadence, even in healthy
        #     steady state, offer strong & session-novel prior work. OFF by
        #     default (UNITARES_KG_PROACTIVE_EVERY=0). Emits an attribution
        #     record so adoption can measure surfaced-vs-acted-on.
        elif _proactive_kg_due(ctx):
            proactive = await _search_kg_by_checkin_text(ctx, floor=_KG_PROACTIVE_FLOOR)
            proactive = await _dedupe_surfaced_kg(ctx, proactive)
            if proactive:
                ctx.response_data["_mirror_kg_results"] = proactive
                signal_records.append({
                    "signal_type": "kg_proactive_surface",
                    "metric": "kg_relevance",
                    "value": max(
                        (r.get("relevance", 0) or 0) for r in proactive
                    ),
                    "threshold": _KG_PROACTIVE_FLOOR,
                    "fired": True,
                    "discovery_ids": [
                        r.get("discovery_id") for r in proactive if r.get("discovery_id")
                    ],
                })

        # Complexity-divergence trigger record — same gate the surfaced line
        # uses (_get_complexity_disagreement honors the >3-update baseline and
        # the novelty gate), so the record fires exactly when the signal does.
        disagreement = _get_complexity_disagreement(ctx.response_data, ctx.meta)
        if disagreement and disagreement.get("divergence") is not None:
            threshold = (
                DIVERGENCE_LINE_THRESHOLD
                if disagreement.get("source") == "continuity"
                else 0.3
            )
            signal_records.append({
                "signal_type": "complexity_divergence",
                "metric": "complexity_divergence",
                "value": disagreement.get("divergence"),
                "threshold": threshold,
                "fired": True,
                "reported": disagreement.get("reported"),
                "derived": disagreement.get("derived"),
                "source": disagreement.get("source"),
            })

        if signals:
            ctx.response_data["_mirror_signals"] = signals
        if signal_records:
            ctx.response_data["_mirror_signal_records"] = signal_records

    except Exception as e:
        logger.debug(f"Could not enrich mirror signals: {e}")


_GAMING_VARIANCE_THRESHOLD = 0.005
# Phase 0.5: the just-above-threshold band whose NON-fired check-ins we also log
# as RDD control points (mirror-effectiveness-measurement-v0). Bounded so the
# extra writes are only the near-miss neighbourhood of the cutoff, not every
# high-variance check-in. These records carry fired=False and produce NO prose
# signal — the agent sees nothing; they exist only for the offline re-eval.
_GAMING_VARIANCE_NEAR_MAX = 0.010


def _detect_gaming(ctx: UpdateContext, records: list | None = None) -> list:
    """Detect low-variance reporting patterns that suggest autopilot.

    When ``records`` is provided, append a structured trigger record per fired
    signal (signal_type, the measured variance, the threshold). Phase 0.5 also
    appends ``fired=False`` records for check-ins whose variance lands in the
    just-above-threshold band (``[threshold, NEAR_MAX)``) — the control side of
    the threshold the offline RDD compares against. Non-fired records carry no
    prose line, so they are emit-only and never reach the agent.
    """
    signals = []

    def _record(signal_type: str, metric: str, value: float, fired: bool) -> None:
        if records is not None:
            records.append({
                "signal_type": signal_type,
                "metric": metric,
                "value": value,
                "threshold": _GAMING_VARIANCE_THRESHOLD,
                "fired": fired,
            })

    try:
        mcp_server = ctx.mcp_server
        if not mcp_server:
            return signals

        monitor = ctx.monitor
        if monitor is None or not hasattr(monitor, 'state'):
            return signals

        state = monitor.state

        # Check complexity history variance
        if hasattr(state, 'complexity_history') and len(state.complexity_history) >= 5:
            recent = list(state.complexity_history)[-5:]
            import statistics
            if len(set(recent)) > 1:
                variance = statistics.variance(recent)
                if variance < _GAMING_VARIANCE_THRESHOLD:
                    signals.append(
                        f"Your last {len(recent)} complexity reports vary little "
                        f"(variance={variance:.4f}) — flat enough to read as autopilot."
                    )
                    _record("autopilot_complexity", "complexity_variance", variance, True)
                elif variance < _GAMING_VARIANCE_NEAR_MAX:
                    _record("autopilot_complexity", "complexity_variance", variance, False)
            elif len(set(recent)) == 1:
                signals.append(
                    f"Your last {len(recent)} complexity reports were all {recent[0]:.2f} — "
                    "no variance, reads as autopilot."
                )
                _record("autopilot_complexity", "complexity_variance", 0.0, True)

        # Check confidence history variance
        if hasattr(state, 'confidence_history') and len(state.confidence_history) >= 5:
            recent_conf = [c for c in list(state.confidence_history)[-5:] if c is not None]
            if len(recent_conf) >= 5:
                import statistics
                if len(set(recent_conf)) > 1:
                    conf_var = statistics.variance(recent_conf)
                    if conf_var < _GAMING_VARIANCE_THRESHOLD:
                        signals.append(
                            f"Your confidence reports show very low variance ({conf_var:.4f}) — "
                            "flat across recent check-ins."
                        )
                        _record("autopilot_confidence", "confidence_variance", conf_var, True)
                    elif conf_var < _GAMING_VARIANCE_NEAR_MAX:
                        _record("autopilot_confidence", "confidence_variance", conf_var, False)
                elif len(set(recent_conf)) == 1:
                    signals.append(
                        f"Your last {len(recent_conf)} confidence reports were all {recent_conf[0]:.2f} — "
                        "no variance across check-ins."
                    )
                    _record("autopilot_confidence", "confidence_variance", 0.0, True)
    except Exception as e:
        logger.debug(f"Gaming detection failed: {e}")
    return signals


async def _search_kg_by_checkin_text(
    ctx: UpdateContext, floor: float = _RELATED_DISCOVERY_RELEVANCE_FLOOR
) -> list:
    """Search knowledge graph using checkin response_text for relevant discoveries.

    ``floor`` is the minimum blended relevance an entry must clear to be
    surfaced. The reactive path uses the permissive default; the proactive path
    passes a higher floor so an unsolicited nudge is only ever a strong match.
    """
    try:
        response_text = ctx.response_text
        if not response_text or len(response_text) < 10:
            return []

        from src.knowledge_graph import get_knowledge_graph
        graph = await get_knowledge_graph()

        # Try semantic search first, fall back to full-text. Both are wrapped in
        # a tight timeout: KG search is the dominant check-in tail (anyio-amplified
        # I/O), and surfacing is advisory — a slow/contended search degrades to
        # "no surfacing this turn" rather than blocking the response up to seconds.
        results = []
        try:
            results = await asyncio.wait_for(
                graph.semantic_search(response_text, limit=3), timeout=_KG_SEARCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.debug("KG semantic_search exceeded %.0fms budget; skipping surfacing", _KG_SEARCH_TIMEOUT * 1000)
            return []
        except (AttributeError, NotImplementedError):
            try:
                results = await asyncio.wait_for(
                    graph.full_text_search(response_text, limit=3), timeout=_KG_SEARCH_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.debug("KG full_text_search exceeded %.0fms budget; skipping surfacing", _KG_SEARCH_TIMEOUT * 1000)
                return []
            except (AttributeError, NotImplementedError):
                pass

        if not results:
            return []

        kg_results = []
        for item in results:
            # semantic_search returns (DiscoveryNode, score) tuples
            if isinstance(item, (tuple, list)) and len(item) == 2:
                disc, score = item
            else:
                disc, score = item, 0

            if isinstance(disc, dict):
                relevance = score or disc.get("relevance", disc.get("score", 0))
                entry = {
                    "discovery_id": disc.get("id") or disc.get("discovery_id"),
                    "summary": disc.get("summary", "")[:200],
                    "agent_id": disc.get("agent_id", "unknown"),
                    "relevance": relevance,
                }
            else:
                relevance = score or getattr(disc, 'relevance', 0)
                entry = {
                    "discovery_id": getattr(disc, 'id', None),
                    "summary": getattr(disc, 'summary', "")[:200],
                    "agent_id": getattr(disc, 'agent_id', "unknown"),
                    "relevance": relevance,
                }
            # Drop noise-floor matches. The surfaced relevance is the *blended*
            # score (similarity*0.7 + connectivity*0.3, then temporal/status
            # decay), so an entry can clear semantic_search's similarity gate yet
            # collapse to ~0.05 — surfacing it as "build on these" is noise, not
            # signal (dogfood 2026-06-13).
            try:
                if float(relevance) < floor:
                    continue
            except (TypeError, ValueError):
                continue
            kg_results.append(entry)
        return kg_results
    except Exception as e:
        logger.debug(f"KG text search failed: {e}")
        return []


def _proactive_kg_due(ctx: UpdateContext) -> bool:
    """Gate the proactive (steady-state) KG surface — cadence + warmup + length.

    OFF unless ``UNITARES_KG_PROACTIVE_EVERY`` is a positive integer N, in which
    case it fires on every Nth check-in past warmup. This is the cost throttle:
    a healthy steady-state check-in normally does no KG search, so we amortize
    the search latency to 1/N of check-ins rather than opening the gate fully.
    """
    try:
        every = int(os.getenv("UNITARES_KG_PROACTIVE_EVERY", "0") or "0")
        if every <= 0:
            return False
        response_text = ctx.response_text or ""
        # Need enough substance for a meaningful semantic query — proactive
        # surfacing on a terse "done" is noise.
        if len(response_text) < 20:
            return False
        total = getattr(ctx.meta, "total_updates", 0) if ctx.meta else 0
        if total <= 3:  # settling — no proactive nudges during warmup
            return False
        return total % every == 0
    except Exception:
        return False


async def _dedupe_surfaced_kg(ctx: UpdateContext, results: list) -> list:
    """Drop discoveries already surfaced to this agent this session (novelty gate).

    Backed by a per-agent Redis set with a session-scope TTL so a given prior
    discovery is offered at most once — what reaches the agent is always new to
    it, not the same strong match re-surfaced every cadence tick. Fail-open: if
    Redis is unavailable or an entry carries no discovery_id, the entry is kept
    (better to occasionally repeat than to silently swallow a relevant nudge).
    """
    if not results:
        return results
    try:
        from src.cache.redis_client import get_redis

        redis = await get_redis()
        if not redis:
            return results

        key = f"kg_surfaced:{ctx.agent_uuid}"
        fresh: list = []
        for entry in results:
            did = entry.get("discovery_id")
            if not did:
                fresh.append(entry)  # cannot dedup → surface
                continue
            try:
                added = await asyncio.wait_for(
                    redis.sadd(key, did), timeout=_REDIS_NOTIF_TIMEOUT
                )
            except (asyncio.TimeoutError, Exception):
                fresh.append(entry)  # fail-open on this entry
                continue
            if added:  # sadd returns 1 when newly added → unseen this session
                fresh.append(entry)
        if fresh:
            try:
                await asyncio.wait_for(
                    redis.expire(key, _KG_SURFACED_TTL_SECONDS),
                    timeout=_REDIS_NOTIF_TIMEOUT,
                )
            except (asyncio.TimeoutError, Exception):
                pass
        return fresh
    except Exception as e:
        logger.debug(f"KG surface dedup failed (fail-open): {e}")
        return results


def _has_tight_margin(response_data: dict) -> bool:
    """Return True when the decision is close enough to a governance edge to warrant a nudge."""
    try:
        decision = response_data.get("decision", {})
        if not isinstance(decision, dict):
            return False
        margin = decision.get("margin")
        if isinstance(margin, str):
            return margin.lower() in {"tight", "warning", "critical"}
        return isinstance(margin, (int, float)) and margin < 0.1
    except Exception:
        return False


def _get_complexity_disagreement(response_data: dict, meta: Any = None) -> dict | None:
    """Return the strongest available complexity disagreement signal, if any."""
    try:
        update_count = getattr(meta, "total_updates", 999) if meta else 999
        if update_count <= 3:
            return None

        continuity = response_data.get("continuity", {})
        if (
            isinstance(continuity, dict)
            and continuity.get("complexity_divergence", 0) > DIVERGENCE_LINE_THRESHOLD
        ):
            # Respect the novelty gate when the payload carries it: a stable
            # session-long gap should not trigger a KG search on every
            # check-in any more than it should repeat the mirror line
            # (review fold, PR #603). A missing key (older builders) keeps
            # the legacy always-fire behavior.
            if continuity.get("divergence_novel") is False:
                return None
            return {
                "reported": continuity.get("self_reported_complexity"),
                "derived": continuity.get("derived_complexity"),
                "divergence": continuity.get("complexity_divergence"),
                "source": "continuity",
            }

        calibration_feedback = response_data.get("calibration_feedback", {})
        if isinstance(calibration_feedback, dict):
            complexity = calibration_feedback.get("complexity", {})
            if isinstance(complexity, dict) and complexity.get("discrepancy", 0) > 0.3:
                return {
                    "reported": complexity.get("reported"),
                    "derived": complexity.get("derived"),
                    "divergence": complexity.get("discrepancy"),
                    "source": "calibration_feedback",
                }
    except Exception:
        return None
    return None


def _generate_mirror_reflection(ctx: UpdateContext, signals: list) -> str | None:
    """Surface a single descriptive reflection when the current state merits one.

    Reflect, don't advise: a mirror shows the agent its own state and lets the
    agent draw the conclusion. Each branch returns the observation only — never a
    directive ("what would you simplify / verify / change?"). Prescription, when
    warranted, is the verdict channel's job, not the mirror's. A mirror that tells
    the agent what to do is a verdict wearing a mirror's clothes. (2026-06-03.)"""
    try:
        response_data = ctx.response_data if isinstance(ctx.response_data, dict) else {}
        decision = response_data.get("decision", {})
        state = response_data.get("state", {})
        restorative = response_data.get("restorative", {})
        conf_rel = response_data.get("confidence_reliability", {})
        text_lower = (ctx.response_text or "").lower()
        verdict = ctx.metrics_dict.get("verdict", "proceed")

        # Only reflect on a genuine first-person "I'm stuck" — not on any text that
        # merely contains the substring "stuck"/"blocked" (e.g. reporting a lease
        # that "blocked my edits" or a fix that "unblocks" something). The bare
        # substring match fabricated "you said you're stuck" on check-ins that
        # described a *resolved* or external block. (2026-06-03 dogfood.)
        first_person_stuck = any(
            p in text_lower
            for p in (
                "i'm stuck", "im stuck", "i am stuck", "i feel stuck",
                "feeling stuck", "i'm blocked", "im blocked", "i am blocked",
                "still stuck",
            )
        )
        if first_person_stuck:
            return "You flagged being stuck in this check-in."

        if verdict in ("guide", "pause", "reject"):
            return f"This check-in triggered a {verdict} verdict."

        if isinstance(restorative, dict) and restorative.get("needs_restoration"):
            return "Your recent pace is above the cooldown threshold."

        if _has_tight_margin(response_data) or (isinstance(state, dict) and state.get("borderline")):
            nearest_edge = decision.get("nearest_edge")
            if nearest_edge == "coherence":
                return "You're close to a coherence edge."
            if nearest_edge in ("risk", "risk_threshold"):
                return "You're close to a risk edge."
            return "You're close to a governance edge."

        # Complexity divergence is surfaced as a neutral, recorded signal line
        # in the mirror (response_formatter._format_mirror), NOT as an
        # in-the-moment question demanding the agent justify itself on an
        # otherwise-healthy check-in. The derived complexity is a surface-feature
        # proxy; the self-report is the richer signal, so a divergence is data
        # for the calibration curve, not an interrogation. (2026-06-03.)

        if isinstance(conf_rel, dict):
            external = conf_rel.get("external_provided")
            derived_cap = conf_rel.get("derived_cap")
            try:
                if external is not None and derived_cap is not None and float(external) - float(derived_cap) > 0.1:
                    return "Your reported confidence is above what the system can justify so far."
            except (TypeError, ValueError):
                pass

        for signal in signals:
            signal_lower = str(signal).lower()
            if "autopilot" in signal_lower:
                return "Your recent check-ins look repetitive (low variance)."
            if "confidence" in signal_lower and "variance" in signal_lower:
                return "Your confidence reports have been very flat."

        return None
    except Exception:
        return None


def _should_search_kg_by_checkin_text(ctx: UpdateContext, signals: list, question: str | None) -> bool:
    """Gate KG search so steady-state mirror responses stay cheap."""
    try:
        response_text = ctx.response_text or ""
        if len(response_text) < 10:
            return False

        if signals or question:
            return True

        response_data = ctx.response_data if isinstance(ctx.response_data, dict) else {}
        state = response_data.get("state", {})
        restorative = response_data.get("restorative", {})
        verdict = ctx.metrics_dict.get("verdict", "proceed")

        if verdict in ("guide", "pause", "reject"):
            return True

        if _has_tight_margin(response_data):
            return True

        if isinstance(state, dict) and state.get("borderline"):
            return True

        if isinstance(restorative, dict) and restorative.get("needs_restoration"):
            return True

        if _get_complexity_disagreement(response_data, ctx.meta):
            return True

        return False
    except Exception:
        return False


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

def _classify_fork(
    position: int,
    agent_uuid: str,
    parent_uuid: Optional[str],
    spawn_reason: Optional[str],
) -> tuple[str, bool]:
    """Return the R6 episode-fork kind and identity-lineage boolean."""
    episode_fork_kind, identity_lineage_fork = classify_episode_fork(
        position,
        agent_uuid,
        parent_uuid,
        spawn_reason,
    )
    if spawn_reason in LINEAGE_SPAWN_REASONS and not parent_uuid:
        logger.warning(
            "[R6_SYNC_RACE] spawn_reason=%s recognized as lineage but "
            "parent_agent_id is None on ctx.meta - possible AgentMetadata sync "
            "failure at handlers.py:1690-1698. Classifying as identity_lineage "
            "anyway. agent_uuid=%s",
            spawn_reason,
            agent_uuid,
        )
    return episode_fork_kind, identity_lineage_fork


def _fork_honest_message(
    episode_fork_kind: str,
    parent_uuid: Optional[str],
    spawn_reason: Optional[str],
) -> str:
    """Build the thin process_agent_update fork message from R6 v2."""
    return fork_honest_message(episode_fork_kind, parent_uuid, spawn_reason)

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


# =================================================================
# Grounding enrichment — spec docs/specs/2026-04-17-eisv-grounding-design.md
# =================================================================
# Runs at order=75, AFTER gating (phases.py) but BEFORE mirror.
# Copies legacy E/I/S/coherence into *_legacy, then overwrites E/I/S/coherence
# with grounded values. Verdicts/basins have already been computed on legacy
# by the time this runs. V is not touched — it's not dual-computed in Phase 1.

from src.grounding.entropy import compute_entropy
from src.grounding.mutual_info import compute_mutual_info
from src.grounding.free_energy import compute_free_energy
from src.grounding.coherence import compute_coherence
from src.grounding.class_indicator import classify_agent


@enrichment(order=75)
async def enrich_grounding(ctx: UpdateContext) -> None:
    """Swap grounded E/I/S/coherence into canonical metrics slots.

    Class-conditional: classifies the agent first, sets ctx.agent_class so
    compute functions can look up per-class scale constants.
    """
    result = ctx.result or {}
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return

    if "E_legacy" in metrics:
        return  # idempotent

    # Classify the agent so compute_* can use class-conditional constants.
    ctx.agent_class = classify_agent(getattr(ctx, "meta", None))

    try:
        e = compute_free_energy(ctx, metrics)
        i = compute_mutual_info(ctx, metrics)
        s = compute_entropy(ctx, metrics)
        c = compute_coherence(ctx, metrics)
    except Exception as exc:
        logger.debug(f"Grounding enrichment failed — legacy values untouched: {exc}")
        return

    for key in ("E", "I", "S", "coherence"):
        if key in metrics:
            metrics[f"{key}_legacy"] = metrics[key]

    metrics["E"] = e.value
    metrics["I"] = i.value
    metrics["S"] = s.value
    metrics["coherence"] = c.value

    metrics["e_source"] = e.source
    metrics["i_source"] = i.source
    metrics["s_source"] = s.source
    metrics["coherence_source"] = c.source

    # Surface the calibration class so audit/broadcast can see what
    # class-conditional constants were applied to this check-in.
    metrics["agent_class"] = ctx.agent_class


async def run_grounding_stage(ctx: UpdateContext) -> None:
    """Run grounding BEFORE persist + response-build, shadow-logging the shift.

    Fixes the #1092 ordering bug: ``enrich_grounding`` was registered in the
    late enrichment pipeline, which runs *after* ``execute_post_update_effects``
    (persist) and ``build_process_update_response_data`` (response) — so its
    grounded E/I/S/coherence reached neither, making grounding a silent no-op
    since it shipped.

    This stage runs early, but is gated:
      * neither flag set  -> no-op (current behavior; the late enrich_grounding
        stays the existing discarded no-op).
      * GROUNDING_SHADOW   -> compute grounded metrics, emit a 'grounding_shadow'
        audit event with the per-dimension shift, then REVERT the live metrics
        (behavior-neutral) unless APPLY is also set.
      * GROUNDING_APPLY    -> keep the grounded values, so persist + response use
        them. LIVE-AFFECTING (coherence/E/I/S shift fleet-wide).
    """
    from config.governance_config import (
        grounding_shadow_enabled,
        grounding_apply_enabled,
    )
    from src.audit_log import audit_logger

    shadow = grounding_shadow_enabled()
    apply = grounding_apply_enabled()
    if not (shadow or apply):
        return

    result = ctx.result or {}
    metrics = result.get("metrics")
    if not isinstance(metrics, dict) or "E_legacy" in metrics:
        return  # nothing to ground, or already grounded

    dims = ("E", "I", "S", "coherence")
    pre = {d: metrics.get(d) for d in dims}

    try:
        await enrich_grounding(ctx)  # mutates metrics in place: sets grounded + *_legacy + *_source
    except Exception as exc:  # pragma: no cover - defensive, must never break check-in
        logger.debug(f"run_grounding_stage failed — metrics untouched: {exc}")
        return

    if "s_source" not in metrics:
        return  # enrich_grounding returned early (e.g. metrics not dict) — nothing applied

    if shadow:
        try:
            audit_logger.log_grounding_shadow(
                agent_id=getattr(ctx, "agent_id", None) or "unknown",
                ungrounded=pre,
                grounded={d: metrics.get(d) for d in dims},
                sources={
                    "E": metrics.get("e_source"),
                    "I": metrics.get("i_source"),
                    "S": metrics.get("s_source"),
                    "coherence": metrics.get("coherence_source"),
                },
                applied=apply,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug(f"grounding_shadow audit failed: {exc}")

    if not apply:
        # Behavior-neutral: restore the ungrounded canonical values and drop the
        # grounding bookkeeping so persist + response are byte-identical to today.
        for d in dims:
            metrics[d] = pre[d]
        for k in ("E_legacy", "I_legacy", "S_legacy", "coherence_legacy",
                  "e_source", "i_source", "s_source", "coherence_source", "agent_class"):
            metrics.pop(k, None)
