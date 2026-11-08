"""Trajectory identity, learning context, WebSocket broadcast, mirror signals, and grounding helpers for update enrichments.

Extracted from ``updates/enrichments.py`` to keep phase-6 enrichment pipelines modular.
"""

import asyncio
import hashlib
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from src.knowledge_authority import is_imported_context
from src.logging_utils import get_logger
from src.monitor_result import DIVERGENCE_LINE_THRESHOLD
from src.thread_identity import (
    LINEAGE_SPAWN_REASONS,
    classify_episode_fork,
    fork_honest_message,
)
from src.grounding.entropy import compute_entropy
from src.grounding.mutual_info import compute_mutual_info
from src.grounding.free_energy import compute_free_energy
from src.grounding.coherence import compute_coherence
from src.grounding.class_indicator import classify_agent
from src.coherence_provenance import (
    LEGACY_COHERENCE_SOURCE,
    annotate_coherence_metrics,
)

from .context import UpdateContext

logger = get_logger(__name__)


def _e() -> Any:
    mod = sys.modules.get("src.mcp_handlers.updates.enrichments")
    if mod is None:
        from src.mcp_handlers.updates import enrichments as mod
    return mod


def _e_attr(name: str, default: Any = None) -> Any:
    mod = _e()
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


async def _enrich_trajectory_identity_impl(
    ctx: UpdateContext,
    *,
    annotate_fn: Any = None,
    explain_tier_fn: Any = None,
    glossary_key: str = "signature_glossary",
) -> None:
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
            if annotate_fn is None:
                from src.governance_glossary import annotate_trajectory_signature_terms as annotate_fn
            signature_glossary = annotate_fn(trajectory_signature)
            if signature_glossary:
                ctx.response_data["trajectory_identity"][glossary_key] = signature_glossary

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
                    if explain_tier_fn is None:
                        from src.governance_glossary import explain_trust_tier as explain_tier_fn
                    explained_trust_tier = explain_tier_fn(trust_tier)
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


def _behavioral_coherence_pattern(metrics: dict) -> str | None:
    """Return coaching only for the behavioral update-consistency producer.

    The canonical field is producer-overloaded. Legacy ODE feedback and the
    E/I/S manifold are useful measurements in their own roles, but neither can
    support claims that an agent's approach is scattered or consistent.
    Unknown provenance fails closed.
    """
    if metrics.get("coherence_role") != "behavioral_update_consistency":
        return None
    coherence = metrics.get("coherence")
    if coherence is None:
        return None
    if coherence < 0.4:
        return "Low behavioral update consistency - review whether the approach is scattered"
    if coherence > 0.8:
        return "High behavioral update consistency - maintaining a consistent approach"
    return None

async def _enrich_learning_context_impl(ctx: UpdateContext) -> None:
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

            coherence_pattern = _behavioral_coherence_pattern(
                ctx.response_data.get('metrics', {})
            )
            if coherence_pattern:
                patterns.append(coherence_pattern)

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


async def _enrich_websocket_broadcast_impl(ctx: UpdateContext) -> None:
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

        # Keep the broadcaster/ring-buffer payload compact.  The full bounded
        # derivation envelope and raw sensor values are persisted in
        # core.agent_state and available on authenticated full-history surfaces;
        # the dashboard needs only source, dimension names/counts, confidence,
        # missingness, policy, and actuator status for its source lanes. Never
        # duplicate the original unbounded sensor_data into the event ring.
        from src.eisv_telemetry import summarize_eisv_telemetry
        eisv_telemetry = summarize_eisv_telemetry(
            ctx.response_data.get("eisv_telemetry")
        )

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
            "eisv_telemetry": eisv_telemetry,
        })
        logger.debug(f"Broadcast EISV update for agent {ctx.declared_agent_id}: eisv={eisv_data}, coherence={coherence_val}")
    except Exception as e:
        logger.debug(f"Could not broadcast EISV update: {e}")


async def _enrich_mirror_signals_impl(ctx: UpdateContext) -> None:
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

        # 3. KG recall is pull-based. Even a concrete mirror signal does not
        #    silently spend retrieval/context budget unless this check-in set
        #    include_memory_suggestions=true. Explicit search_shared_memory is
        #    the normal path for deliberate recall.
        if _e_attr("_memory_suggestions_requested")(ctx):
            if _should_search_kg_by_checkin_text(ctx, signals, reflection):
                kg_results = await _cached_kg_search_by_checkin_text(ctx)
                if kg_results:
                    ctx.response_data["_mirror_kg_results"] = kg_results
            # Proactive surfacing remains additionally cadence-gated. The
            # caller opt-in is necessary but not sufficient for this branch.
            elif _proactive_kg_due(ctx):
                proactive = await _cached_kg_search_by_checkin_text(
                    ctx, floor=getattr(_e(), "_KG_PROACTIVE_FLOOR")
                )
                proactive = await _dedupe_surfaced_kg(ctx, proactive)
                if proactive:
                    ctx.response_data["_mirror_kg_results"] = proactive
                    signal_records.append({
                        "signal_type": "kg_proactive_surface",
                        "metric": "kg_relevance",
                        "value": max(
                            (r.get("relevance", 0) or 0) for r in proactive
                        ),
                        "threshold": getattr(_e(), "_KG_PROACTIVE_FLOOR"),
                        "fired": True,
                        "discovery_ids": [
                            r.get("discovery_id") for r in proactive if r.get("discovery_id")
                        ],
                    })

        # 3c. In-flow review nudge (adoption increment 2, #1685) — when this
        #     check-in itself reports uncertain ground, offer the one-call
        #     review surface in the same response. OFF by default
        #     (UNITARES_REVIEW_NUDGE unset); at most once per agent-session,
        #     fail-closed on Redis. Independent of the KG blocks above.
        nudge_reason = _review_nudge_trigger(ctx)
        if nudge_reason and await _review_nudge_novel(ctx):
            ctx.response_data["_review_nudge"] = {"trigger": nudge_reason}
            signal_records.append({
                "signal_type": "review_nudge",
                "metric": "review_nudge_trigger",
                "value": (
                    ctx.confidence if nudge_reason == "low_confidence"
                    else ctx.complexity if nudge_reason == "high_complexity"
                    else None
                ),
                "threshold": (
                    getattr(_e(), "_REVIEW_NUDGE_CONF_CEILING") if nudge_reason == "low_confidence"
                    else getattr(_e(), "_REVIEW_NUDGE_COMPLEXITY_FLOOR") if nudge_reason == "high_complexity"
                    else None
                ),
                "fired": True,
                "trigger": nudge_reason,
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


def _mark_surfacing_degraded(ctx: UpdateContext) -> None:
    """Record that a KG lookup degraded rather than finding nothing.

    Without this the two outcomes are indistinguishable downstream: both leave
    the surfacing key absent, so a broken or timing-out search reads to the
    agent exactly like "no prior work exists".
    """
    try:
        ctx.response_data["knowledge_surfacing_degraded"] = True
    except Exception:  # response_data shape is not worth failing a check-in over
        pass


async def _search_kg_by_checkin_text(
    ctx: UpdateContext, floor: float = 0.1
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
                graph.semantic_search(response_text, limit=3), timeout=getattr(_e(), "_KG_SEARCH_TIMEOUT")
            )
        except asyncio.TimeoutError:
            logger.debug("KG semantic_search exceeded %.0fms budget; skipping surfacing", getattr(_e(), "_KG_SEARCH_TIMEOUT") * 1000)
            _mark_surfacing_degraded(ctx)
            return []
        except (AttributeError, NotImplementedError):
            try:
                results = await asyncio.wait_for(
                    _full_text_search_with_recall(graph, response_text),
                    timeout=getattr(_e(), "_KG_SEARCH_TIMEOUT"),
                )
            except asyncio.TimeoutError:
                logger.debug("KG full_text_search exceeded %.0fms budget; skipping surfacing", getattr(_e(), "_KG_SEARCH_TIMEOUT") * 1000)
                _mark_surfacing_degraded(ctx)
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
            # A surfaced entry carries only id, summary, writer and relevance,
            # so an unpromoted import would arrive as "build on this" with
            # nothing to mark it as one harness's unreviewed claim. Leave it to
            # explicit search, where it is labelled and down-ranked.
            if is_imported_context(disc):
                continue

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
        logger.warning(f"KG text search failed: {e}")
        _mark_surfacing_degraded(ctx)
        return []


async def _full_text_search_with_recall(graph: Any, response_text: str) -> list:
    """Run precise AND retrieval, then a recall-oriented OR fallback.

    The caller wraps this whole sequence in one timeout, so the fallback does
    not double the advisory path's latency budget.
    """
    terms = _e_attr("_distinctive_terms")(response_text)
    query = " ".join(terms) or response_text
    results = await graph.full_text_search(query, limit=3)
    if not results and len(terms) > 1:
        results = await graph.full_text_search(query, limit=3, operator="OR")
    return list(results or [])


async def _cached_kg_search_by_checkin_text(
    ctx: UpdateContext, floor: float = 0.1
) -> list:
    """Return one KG lookup per check-in, filtered for the caller's floor.

    Both the general knowledge-surfacing enrichment and the mirror enrichment
    consume the same response text. Without a per-context cache an actionable
    check-in pays for the identical advisory lookup twice, each with its own
    timeout budget. Cache the default-floor result and let stricter consumers
    (currently proactive surfacing) filter that bounded list locally.
    """
    if not getattr(ctx, "_kg_search_ready", False):
        results = await _e_attr("_search_kg_by_checkin_text", _search_kg_by_checkin_text)(
            ctx, floor=getattr(_e(), "_RELATED_DISCOVERY_RELEVANCE_FLOOR")
        )
        ctx._kg_search_results = list(results)
        ctx._kg_search_ready = True

    results = list(getattr(ctx, "_kg_search_results", []))
    if floor <= getattr(_e(), "_RELATED_DISCOVERY_RELEVANCE_FLOOR"):
        return results

    filtered = []
    for entry in results:
        try:
            if float(entry.get("relevance", 0)) >= floor:
                filtered.append(entry)
        except (AttributeError, TypeError, ValueError):
            continue
    return filtered


def _proactive_kg_due(ctx: UpdateContext) -> bool:
    """Add a cadence gate to explicit check-in KG recall; never enable it alone.

    The caller has already opted in with ``include_memory_suggestions=true``.
    This branch remains OFF unless ``UNITARES_KG_PROACTIVE_EVERY`` is a positive
    integer N, in which case it fires on every Nth check-in past warmup. It is a
    cost throttle, not an injection switch.
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


def _review_nudge_trigger(ctx: UpdateContext) -> Optional[str]:
    """Return the trigger reason when an in-flow review nudge is due, else None.

    Triggers, any one sufficient (checked in this order):
      * the agent reported confidence at or under _REVIEW_NUDGE_CONF_CEILING
      * the agent reported complexity at or over _REVIEW_NUDGE_COMPLEXITY_FLOOR
      * the policy decision came back sub_action == "guide"

    Gated by UNITARES_REVIEW_NUDGE (default off) and a >3-update warmup — a
    settling identity gets onboarding guidance, not review nudges.
    """
    try:
        if os.getenv("UNITARES_REVIEW_NUDGE", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            return None
        total = getattr(ctx.meta, "total_updates", 0) if ctx.meta else 0
        if total <= 3:
            return None
        if ctx.confidence is not None and ctx.confidence <= getattr(_e(), "_REVIEW_NUDGE_CONF_CEILING"):
            return "low_confidence"
        if ctx.complexity is not None and ctx.complexity >= getattr(_e(), "_REVIEW_NUDGE_COMPLEXITY_FLOOR"):
            return "high_complexity"
        decision = ctx.response_data.get("decision")
        if isinstance(decision, dict) and decision.get("sub_action") == "guide":
            return "guide_verdict"
        return None
    except Exception:
        return None


async def _review_nudge_novel(ctx: UpdateContext) -> bool:
    """True when this agent has not been nudged this session (Redis SET NX).

    FAIL-CLOSED — no Redis, timeout, or error means no nudge. This deliberately
    inverts _dedupe_surfaced_kg's fail-open: for a prior discovery, missing a
    relevant lead is the harm; for a suggestion, repetition is the harm — a
    nudge that keeps firing trains the reader to ignore the channel.
    """
    try:
        session_key = str(ctx.session_key or "").strip()
        if not session_key:
            return False
        # Scope dedup to the resolved session, without placing the session proof
        # itself in a Redis key. The TTL matches the canonical session binding:
        # once that binding expires, reusing the same caller token is a new
        # server-side session for dedup purposes.
        session_scope = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        from src.cache.redis_client import get_redis

        redis = await get_redis()
        if not redis:
            return False
        added = await asyncio.wait_for(
            redis.set(
                f"review_nudge:{ctx.agent_uuid}:{session_scope}",
                "1",
                nx=True,
                ex=getattr(_e(), "_REVIEW_NUDGE_TTL_SECONDS"),
            ),
            timeout=getattr(_e(), "_REDIS_NOTIF_TIMEOUT"),
        )
        return bool(added)
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
                    redis.sadd(key, did), timeout=getattr(_e(), "_REDIS_NOTIF_TIMEOUT")
                )
            except (asyncio.TimeoutError, Exception):
                fresh.append(entry)  # fail-open on this entry
                continue
            if added:  # sadd returns 1 when newly added → unseen this session
                fresh.append(entry)
        if fresh:
            try:
                await asyncio.wait_for(
                    redis.expire(key, getattr(_e(), "_KG_SURFACED_TTL_SECONDS")),
                    timeout=getattr(_e(), "_REDIS_NOTIF_TIMEOUT"),
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

        # `borderline` is a BASIN property -- where the EISV state sits in
        # state-space -- while margin/nearest_edge describe distance to a
        # decision THRESHOLD. ORing them together meant a boundary-basin agent
        # with a comfortable margin was told "You're close to a governance
        # edge" while `nearest_edge` was None: edge proximity asserted with no
        # edge to name, and nothing the agent could check. Same conflation
        # fixed in monitor_decision (#1774) and the recovery hint (#1776); this
        # was the third surface carrying it.
        if _has_tight_margin(response_data):
            nearest_edge = decision.get("nearest_edge")
            if nearest_edge == "coherence":
                return "You're close to a coherence edge."
            if nearest_edge in ("risk", "risk_threshold"):
                return "You're close to a risk edge."
            if nearest_edge:
                return f"You're close to a {nearest_edge} edge."
            # An actionable margin that names no edge should be unreachable
            # after #1774. Report the margin rather than inventing an edge.
            return "Your policy margin is tight."

        if isinstance(state, dict) and state.get("borderline"):
            return "Your state sits near a basin boundary."

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
    """Build the thin process_agent_update fork message from R6 v2.

    ``ctx.meta`` does not record whether this UUID was minted at its thread
    position or resumed there, so the sibling text must hold for both.
    """
    return fork_honest_message(
        episode_fork_kind, parent_uuid, spawn_reason, minted_fresh=None
    )


async def enrich_grounding(ctx: UpdateContext) -> None:
    """Swap grounded E/I/S/coherence into canonical metrics slots.

    Class-conditional: classifies the agent first, sets ctx.agent_class so
    compute functions can look up per-class scale constants. This function is
    called only by the explicit pre-persist grounding stage; it is deliberately
    not registered in the late response-enrichment pipeline.
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
    annotate_coherence_metrics(metrics, source=c.source)

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
      * neither flag set  -> values remain untouched; attach source/role
        metadata and stamp coherence_form="legacy_tanh_v" for persistence.
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
    result = ctx.result or {}
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return

    # Provenance is not conditional on an experiment flag. With both flags off
    # the canonical value is the legacy ODE tanh(V) form, so stamp that fact on
    # every persisted row AND response. The two additive metadata fields do not
    # alter the value, policy, or enforcement.
    if "coherence" in metrics:
        source = metrics.get("coherence_source") or LEGACY_COHERENCE_SOURCE
        annotate_coherence_metrics(metrics, source=source)
        ctx.coherence_form = source

    if not (shadow or apply):
        return

    if "E_legacy" in metrics:
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

    # Which instrument actually produced the coherence that gets persisted and
    # returned. The ctx field preserves the historical state_json contract;
    # source/role travel with the response measurement itself.
    ctx.coherence_form = (
        (metrics.get("coherence_source") or "grounded")
        if apply
        else LEGACY_COHERENCE_SOURCE
    )

    if not apply:
        # Behavior-neutral: restore the ungrounded canonical values and drop
        # grounded bookkeeping. Keep only the winning producer/role metadata.
        for d in dims:
            metrics[d] = pre[d]
        for k in ("E_legacy", "I_legacy", "S_legacy", "coherence_legacy",
                  "e_source", "i_source", "s_source", "coherence_source",
                  "coherence_role", "agent_class"):
            metrics.pop(k, None)
        annotate_coherence_metrics(metrics, source=LEGACY_COHERENCE_SOURCE)
    else:
        annotate_coherence_metrics(metrics, source=ctx.coherence_form)
