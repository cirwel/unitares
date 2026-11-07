"""Post-update effects and persistence helpers for the process_agent_update pipeline."""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

from src import agent_storage as _default_agent_storage
from src.auto_outcome_labels import (
    COMPLETION_SIGNAL_RE as _COMPLETION_SIGNAL_RE,  # noqa: F401 — compatibility re-export for phases
    FAILURE_SIGNAL_RE as _FAILURE_SIGNAL_RE,  # noqa: F401
)
from src.logging_utils import get_logger
from .context import UpdateContext

logger = get_logger("src.mcp_handlers.updates.phases")


def _p_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get("src.mcp_handlers.updates.phases")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


class _AgentStorageProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_p_attr("agent_storage", _default_agent_storage), name)


agent_storage = _AgentStorageProxy()


def _proxy(name: str):
    def _call(*args: Any, **kwargs: Any) -> Any:
        return _p_attr(name)(*args, **kwargs)
    return _call


_persist_agent_state_row = _proxy("_persist_agent_state_row")
_record_auto_outcome_event = _proxy("_record_auto_outcome_event")

# ─── Purpose Inference ─────────────────────────────────────────────────

_PURPOSE_KEYWORDS = {
    'debugging': ('debug', 'fix', 'bug', 'error', 'traceback', 'exception', 'crash'),
    'implementation': ('implement', 'build', 'create', 'add', 'feature', 'develop'),
    'testing': ('test', 'assert', 'coverage', 'pytest', 'unittest', 'spec'),
    'review': ('review', 'audit', 'inspect', 'check', 'verify', 'validate'),
    'deployment': ('deploy', 'release', 'ship', 'publish', 'launch', 'rollout'),
    'exploration': ('explore', 'research', 'investigate', 'analyze', 'understand', 'learn'),
}

def _infer_purpose(response_text: str) -> Optional[str]:
    """Infer agent purpose from response text via keyword matching."""
    text_lower = response_text.lower()
    best_purpose = None
    best_count = 0
    for purpose, keywords in _PURPOSE_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in text_lower)
        if count > best_count:
            best_count = count
            best_purpose = purpose
    return best_purpose if best_count >= 1 else None


def _derive_outcome(evidence: dict) -> tuple:
    """Map a ToolResultEvidence dict to (outcome_type, is_bad) per spec §1 mapping table.

    Evidence arrives as a plain dict because params_step.py calls model_dump() which
    flattens nested Pydantic models. Use dict access throughout.
    """
    is_bad = evidence.get("is_bad")
    if is_bad is None:
        exit_code = evidence.get("exit_code")
        is_bad = (exit_code is not None and exit_code != 0)
    kind = evidence.get("kind")
    if kind == "test":
        return ("test_failed" if is_bad else "test_passed", is_bad)
    if kind == "tool_call" and is_bad:
        return ("tool_rejected", is_bad)
    return ("task_failed" if is_bad else "task_completed", is_bad)


def _prediction_id_for_phase5_evidence(ctx: UpdateContext, evidence: dict) -> str | None:
    """Return the prediction id to attach to one Phase-5 evidence row.

    Caller-supplied evidence ids win. For unbound evidence rows, mint a fresh
    tactical prediction from the current process_update confidence so each row
    can resolve through outcome_event's one-shot registry path independently.
    Reusing the process_update response's single prediction id would bind only
    the first row; the rest would fall into missing/consumed fallback lanes.

    The mint is ``advertise=False``: the reply surfaces the monitor's
    ``_last_prediction_id``, and this emitter runs after the check-in minted
    its own. An advertised evidence mint replaced that pointer with an id the
    row then claimed through the exactly-once binding, so the caller was
    handed a consumed id and its ``outcome_event`` came back
    PREDICTION_REUSE_CONFLICT (observed live 2026-09-16).
    """
    explicit_prediction_id = evidence.get("prediction_id")
    if explicit_prediction_id:
        return explicit_prediction_id
    if ctx.confidence is None:
        return None
    decision = (ctx.result or {}).get("decision") or {}
    decision_action = decision.get("action")
    checkpoint_id = getattr(
        getattr(ctx, "checkpoint_receipt", None), "checkpoint_id", None
    )
    register_prediction = getattr(ctx.monitor, "register_tactical_prediction", None)
    if callable(register_prediction):
        try:
            kwargs: Dict[str, Any] = {
                "decision_action": decision_action,
                "advertise": False,
            }
            if checkpoint_id is not None:
                kwargs["checkpoint_id"] = checkpoint_id
            try:
                minted = register_prediction(float(ctx.confidence), **kwargs)
            except TypeError:
                minted = register_prediction(
                    float(ctx.confidence),
                    decision_action=decision_action,
                    advertise=False,
                )
            return str(minted) if minted else None
        except Exception as exc:
            logger.debug("Phase-5 prediction id mint skipped: %s", exc, exc_info=True)
            return None
    if getattr(ctx, "monitor", None) is None and getattr(ctx, "agent_id", None):
        try:
            from src.monitor_prediction import register_agent_prediction

            minted = register_agent_prediction(
                ctx.agent_id,
                float(ctx.confidence),
                decision_action=decision_action,
                checkpoint_id=checkpoint_id,
                advertise=False,
            )
            return str(minted) if minted else None
        except Exception as exc:
            logger.debug("Phase-5 neutral prediction id mint skipped: %s", exc, exc_info=True)
    return None

def _restamp_fork_after_thread_identity_update(ctx: UpdateContext) -> None:
    """Override S22 fork fields with the post-``node_index``-mutation classification.

    The early stamp at ``prepare_unlocked_inputs`` runs before
    ``execute_locked_update`` mutates ``ctx.meta.node_index`` on session-key
    transitions (lines ~849-858 below). Without this re-stamp, a fresh-session
    transition would persist ``episode_fork_kind="none"`` while the response
    pipeline returns ``"sibling_locus"`` — split-brain on the durable side.
    """
    from src.provenance_context import classify_fork_for_s22_context

    pc = ctx.agent_state.get("provenance_context")
    if not isinstance(pc, dict):
        return
    fork_kind, lineage_fork = classify_fork_for_s22_context(ctx.meta, ctx.agent_uuid)
    if fork_kind is not None:
        pc["episode_fork_kind"] = fork_kind
    if lineage_fork is not None:
        pc["identity_lineage_fork"] = lineage_fork



def _self_report_record(ctx) -> dict:
    """What the agent stated on this check-in, for the telemetry envelope.

    Measurement-only: recorded so a calibration referent can later pair stated
    confidence with independently verified outcomes. ``reported`` is the
    caller's value as coerced, ``effective`` the value after identity-assurance
    dampening; nothing on the estimator or policy path reads either from here.
    """
    raw = (ctx.arguments or {}).get("confidence")
    try:
        reported = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        reported = None
    return {
        "confidence_reported": reported,
        "confidence_effective": ctx.confidence,
        "confidence_dampened": bool(getattr(ctx, "confidence_dampened", False)),
    }

async def _track_thread_identity(ctx: "UpdateContext") -> bool:
    """Resolve `ctx.meta.thread_id` + `node_index` for a (possibly) new session.

    Closes #424 (\"two independent thread_id mint paths\"). The previous
    implementation called ``uuid.uuid4()`` mid-update whenever the in-memory
    metadata cache had no thread_id — producing a UUID-format id that
    conflicted with onboard's ``t-<sha16>`` format and resetting node_index
    to 1 even when the durable PG record knew otherwise. The 2026-05-08
    trace showed the path was reachable on a 30s session-resolve-miss
    cadence in normal operation, not a rare race.

    Resolution order (no random mints):

    1. In-memory ``ctx.meta`` already has ``thread_id``. Use it; only bump
       ``node_index`` and stamp ``active_session_key``.

    2. ``ctx.meta`` lacks ``thread_id`` → read ``core.identities.metadata``
       for the durable copy. The onboard-time persist may have committed
       there even when the in-memory cache hadn't been rehydrated yet
       (fresh process-instance, IP+UA fingerprint resolve, etc.).

    3. PG also empty → derive deterministically from ``ctx.session_key``
       via ``generate_thread_id`` — the exact function onboard uses, so
       the format stays consistent. This is the final \"no random UUID\"
       guarantee.

    Returns True when the session-key actually changed and downstream
    re-stamp/persist effects should fire; False on a no-op (already
    tracking the same session).

    **Heal-on-read for pre-#483 UUID-format thread_ids (#484, 2026-05-20).**
    886 of 2521 populated ``core.identities.metadata.thread_id`` rows still
    carry UUID-format values from the broken mint path that pre-dated #483.
    Step 1 detects them by the ``t-`` prefix check and treats them as malformed
    — the value is skipped, and step 2 derives a fresh deterministic
    ``t-<sha16>`` from ``session_key``. One update per affected agent migrates
    the row via the regular persist path. The single downstream consumer that
    keys on thread_id format is ``src/db/mixins/thread.py:71``; lookups on
    the old UUID value will miss until that agent next updates, which is the
    expected migration behavior. Spread cost, no concentrated burst.

    ``node_index`` and ``active_session_key`` are still imported from the
    persisted row — only ``thread_id`` is the corrupt field.
    """
    if not (ctx.meta and ctx.session_key):
        return False
    if getattr(ctx.meta, "active_session_key", None) == ctx.session_key:
        return False

    # Step 1: hydrate from durable PG metadata when the in-memory cache is
    # empty. ExecutorPool means we can `await` asyncpg directly here without
    # the locked-update concern (per CLAUDE.md post-PR-#218 note).
    if not getattr(ctx.meta, "thread_id", None) and ctx.agent_uuid:
        try:
            from src.db import get_db
            identity_record = await get_db().get_identity(ctx.agent_uuid)
            if identity_record and identity_record.metadata:
                persisted_thread_id = identity_record.metadata.get("thread_id")
                persisted_node_index = identity_record.metadata.get("node_index")
                persisted_session_key = identity_record.metadata.get(
                    "active_session_key"
                )
                # #484 heal-on-read: pre-#483 mint path emitted UUID-format
                # thread_ids. Treat them as malformed so step 2 re-derives
                # the canonical t-<sha16> shape via generate_thread_id.
                if persisted_thread_id and not persisted_thread_id.startswith("t-"):
                    logger.info(
                        "[PROCESS_UPDATE] heal-on-read: skipping legacy "
                        "UUID-format thread_id %s... for agent=%s...; "
                        "step 2 will re-derive canonical form",
                        persisted_thread_id[:8],
                        (ctx.agent_uuid or "?")[:8],
                    )
                    persisted_thread_id = None
                if persisted_thread_id:
                    ctx.meta.thread_id = persisted_thread_id
                if persisted_node_index is not None and not getattr(
                    ctx.meta, "node_index", None
                ):
                    ctx.meta.node_index = persisted_node_index
                if persisted_session_key is not None and getattr(
                    ctx.meta, "active_session_key", None
                ) is None:
                    ctx.meta.active_session_key = persisted_session_key
        except Exception as e:
            logger.debug(
                "thread_id PG hydration skipped for %s...: %s",
                (ctx.agent_uuid or "?")[:8], e,
            )

    # Re-check after hydration: caller may have just adopted the session
    # already (rare — only if PG had the exact same active_session_key).
    if getattr(ctx.meta, "active_session_key", None) == ctx.session_key:
        return False

    # Step 2: deterministic fallback if still no thread_id. Matches onboard
    # format; never invents a random UUID.
    if not getattr(ctx.meta, "thread_id", None):
        from src.thread_identity import generate_thread_id
        ctx.meta.thread_id = generate_thread_id(ctx.session_key)
        logger.info(
            "[PROCESS_UPDATE] thread_id fallback-derived from session_key "
            "(no in-memory cache or PG record): %s... agent=%s...",
            ctx.meta.thread_id[:8],
            (ctx.agent_uuid or "?")[:8],
        )

    # node_index bookkeeping. Re-entry into the same session leaves
    # position alone (first branch); a session transition with a tracked
    # prior session bumps (second branch). The PG hydration above already
    # imported persisted_node_index when present, so process-restart no
    # longer silently loses position continuity.
    if getattr(ctx.meta, "active_session_key", None) is None:
        ctx.meta.node_index = getattr(ctx.meta, "node_index", None) or 1
    else:
        ctx.meta.node_index = (getattr(ctx.meta, "node_index", None) or 1) + 1
    ctx.meta.active_session_key = ctx.session_key
    return True


async def _persist_thread_identity_async(agent_uuid: str, metadata: dict) -> None:
    """Fire-and-forget thread-identity metadata persist. Eventual consistency
    is fine: in-memory ctx.meta is the source of truth within this process,
    PG copy is for cross-process visibility. Errors are swallowed because
    failure here doesn't change governance correctness — next session will
    re-derive thread_id/node_index from in-memory state.

    Same shape as PR #360's `_hydrate_metadata_cache_async`: sequential
    awaits in our own loop, moved out of the critical section so the agent
    lock isn't held across a PG UPDATE roundtrip. Classification: NOT an
    anyio/asyncio coupling pattern — just lock-holding-too-long. See
 `` v0.2 RESOLUTION.
    """
    try:
        from src.db import get_db
        db = get_db()
        await db.update_identity_metadata(agent_uuid, metadata=metadata, merge=True)
        logger.info(
            f"Thread identity persisted (deferred) for {agent_uuid[:8]}... "
            f"-> thread {metadata.get('thread_id', '')[:8]}... "
            f"(node {metadata.get('node_index')})"
        )
    except Exception as e:
        logger.debug(f"Could not persist thread identity (deferred): {e}")


async def _persist_inferred_purpose_async(agent_id: str, purpose: str) -> None:
    """Fire-and-forget purpose persist. Same rationale as
    `_persist_thread_identity_async` — in-memory `meta.purpose` is the
    process-local source of truth; PG copy is for cross-process visibility.
    Failure here doesn't change governance correctness.
    """
    try:
        await agent_storage.update_agent(agent_id, purpose=purpose)
        logger.debug(f"Auto-inferred purpose '{purpose}' persisted (deferred) for {agent_id[:12]}...")
    except Exception as e:
        logger.debug(f"Could not persist inferred purpose (deferred): {e}")

async def _r2_post_update_hook(ctx: UpdateContext) -> None:
    """R2 PR 5: post-update lineage trigger.

    For agents with confirmed lineage, increment ``chain_obs_count``
    (cheap single UPDATE — fine to ``await`` inline; matches the shape
    of other UPDATEs already awaited in ``execute_post_update_effects``).

    For agents with any lineage edge, dispatch ``evaluate_lineage_for``
    in a tracked task (anyio-safe: must NOT inline-await R1's per-dim
    DTW + audit write under the MCP handler's anyio task group).

    Cadence guard inside the FSM (``eval_cadence`` default 1h) prevents
    tight re-eval if multiple check-ins fire in the same window.

    All paths fail-soft — failures here must not break the
    ``process_agent_update`` response.
    """
    agent_id = ctx.agent_id
    # Fast-path: most agents are orphan. Skip the DB roundtrip if the
    # in-memory metadata has no parent_agent_id. The cache is set at
    # onboard (PR 3 wiring) and reliably populated for declared-lineage
    # agents in this process. The DB read remains the source of truth
    # but we avoid hitting it for the orphan-majority case.
    meta = getattr(ctx, "meta", None)
    if not (meta is not None and getattr(meta, "parent_agent_id", None)):
        return
    try:
        from src.db import get_db
        backend = get_db()
        if backend is None:
            return
        lineage = await backend.read_lineage_state(agent_id)
        if not lineage or not lineage.get("parent_agent_id"):
            return
        # Confirmed lineage: increment chain counter (await OK — single UPDATE)
        if (
            lineage.get("confirmed_at") is not None
            and not lineage.get("provisional_lineage")
        ):
            try:
                await backend.increment_chain_obs_count(agent_id)
            except Exception as e:
                # Counter miscount is recoverable — sweeper will reconcile
                # chain_obs_count on the next scheduled eval (≤6h). Keep at
                # debug to avoid log noise on transient UPDATE failures.
                logger.debug(
                    f"[R2] increment_chain_obs_count failed for "
                    f"{agent_id[:8]}...: {e}"
                )
        # Dispatch FSM eval (fire-and-forget; cadence guard inside)
        try:
            from src.background_tasks import create_tracked_task
            from src.identity.lineage_lifecycle import evaluate_lineage_for
            create_tracked_task(
                evaluate_lineage_for(agent_id),
                name=f"r2_lineage_eval_{agent_id[:8]}",
            )
        except Exception as e:
            # Dispatch failure means R2 governance is silently degraded for
            # this agent — FSM never runs for this check-in, lineage state
            # stays stale until the sweeper picks it up (up to 6h later).
            # Worth operator attention.
            logger.warning(
                f"[R2] lineage eval dispatch failed for "
                f"{agent_id[:8]}...: {e}"
            )
    except Exception as e:
        # Outer read failure means DB issue or schema drift — both warrant
        # operator attention, not silent debug.
        logger.warning(
            f"[R2] post-update lineage hook failed for "
            f"{agent_id[:8]}...: {e}"
        )



def _rewrap_behavioral_verdict(result: dict, decision: dict) -> None:
    """Re-wrap the nested behavioral verdict after a post-ODE escalation.

    build_result wrapped it with the decision as it stood then; the escalation
    replaced that decision, so the wrapped next_action would describe the old
    one (explain_verdict follows the decision it is given). The dialectic
    escalation is capped at guide (dialectic/enforcement.py), so today this
    re-wraps proceed to the same text; it stays so a later rewriter that can
    stop the agent cannot leave a stale wrap.
    """
    try:
        assessment = result.get('behavioral', {}).get('assessment', {})
        verdict = assessment.get('verdict')
        if isinstance(verdict, dict) and verdict.get('value') is not None:
            from src.governance_glossary import explain_verdict
            assessment['verdict'] = explain_verdict(
                verdict['value'], decision_action=decision.get('action')
            )
    except Exception as e:
        logger.debug(f"Behavioral verdict re-wrap skipped: {e}")


async def _post_update_health_and_baselines(ctx: UpdateContext) -> None:
    mcp_server = ctx.mcp_server
    agent_id = ctx.agent_id

    # Heartbeat
    try:
        await ctx.loop.run_in_executor(None, mcp_server.process_mgr.write_heartbeat)
    except Exception as e:
        logger.debug(f"Heartbeat write skipped: {e}")

    # Health status
    ctx.metrics_dict = ctx.result.get('metrics', {})
    ctx.risk_score = ctx.metrics_dict.get('risk_score', None)
    ctx.coherence = ctx.metrics_dict.get('coherence', None)
    void_active = ctx.metrics_dict.get('void_active', False)

    # Record current signals into per-agent behavioral baseline (post-ODE)
    try:
        from src.agent_behavioral_baseline import get_agent_behavioral_baseline, schedule_baseline_save
        baseline = get_agent_behavioral_baseline(agent_id)
        # Retain legacy C(V) statistics for compatibility/replay, but never pass
        # them to compute_anomaly_entropy: the producer is not behavioral-health
        # evidence and its narrow range cannot support a z-score actuator.
        if ctx.coherence is not None:
            baseline.update("coherence", ctx.coherence)
        # Tool usage signals
        try:
            from src.tool_usage_tracker import get_tool_usage_tracker
            _tu = get_tool_usage_tracker().get_usage_stats(
                agent_id=agent_id, window_hours=1,
            )
            _tu_total = _tu.get("total_calls", 0)
            if _tu_total > 0:
                _tu_failed = sum(
                    t.get("error_count", 0)
                    for t in _tu.get("tools", {}).values()
                )
                baseline.update("tool_error_rate", _tu_failed / _tu_total)
                baseline.update("tool_call_velocity", _tu_total / 60.0)
        except Exception:
            pass
        # Complexity divergence (post-ODE: cm is the current check-in's ContinuityMetrics)
        monitor = mcp_server.monitors.get(agent_id)
        if monitor:
            from src.dual_log.continuity import should_exclude_template_divergence
            cm = getattr(monitor, '_last_continuity_metrics', None)
            if (
                cm
                and hasattr(cm, 'complexity_divergence')
                and not should_exclude_template_divergence(cm)
            ):
                baseline.update("complexity_divergence", cm.complexity_divergence)
        # Fire-and-forget persist to PostgreSQL
        schedule_baseline_save(agent_id)
    except Exception as e:
        logger.debug(f"Baseline recording skipped for {agent_id}: {e}")

    # Agent profile — differentiated per-agent metrics (outside ODE)
    try:
        from src.agent_profile import get_agent_profile, save_profile_to_postgres
        profile = get_agent_profile(agent_id)
        profile.record_checkin(
            complexity=ctx.complexity,
            confidence=ctx.confidence,
            ethical_drift=ctx.ethical_drift,
            verdict=ctx.metrics_dict.get('verdict'),
        )
        # Persist every 10th update to avoid excessive writes
        if profile.total_updates % 10 == 0:
            await save_profile_to_postgres(agent_id)
    except Exception as e:
        logger.debug(f"Agent profile update skipped for {agent_id}: {e}")

    # Post-ODE: enforce risk targets and retire stored, unprovenanced coherence targets.
    try:
        if ctx.meta and getattr(ctx.meta, 'dialectic_conditions', None):
            from ..dialectic.enforcement import enforce_post_ode_conditions
            decision = ctx.result.get('decision', {})
            escalated_decision, condition_warnings = enforce_post_ode_conditions(
                ctx.meta.dialectic_conditions, ctx.metrics_dict, decision
            )
            if escalated_decision is not decision:
                ctx.result['decision'] = escalated_decision
                ctx.result['dialectic_escalation'] = True
                _rewrap_behavioral_verdict(ctx.result, escalated_decision)
            ctx.warnings.extend(condition_warnings)
    except Exception as e:
        logger.debug(f"Dialectic condition enforcement skipped: {e}")

    try:
        ctx.health_status, ctx.health_message = mcp_server.health_checker.get_health_status(
            risk_score=ctx.risk_score,
            coherence=ctx.coherence,
            void_active=void_active
        )
    except Exception as e:
        logger.debug(f"Health status check failed: {e}")

    if 'metrics' not in ctx.result:
        ctx.result['metrics'] = {}
    _hs = getattr(ctx.health_status, 'value', 'unknown') if ctx.health_status else 'unknown'
    ctx.result['metrics']['health_status'] = _hs
    ctx.result['metrics']['health_message'] = ctx.health_message or ""

    if ctx.meta:
        ctx.meta.health_status = _hs

def _eisv_instrument_stamp(monitor) -> Optional[dict]:
    """The row's instrument stamp; ``None`` (row persists unstamped) on any failure."""
    if monitor is None:
        return None
    from src.eisv_instrument import stamp

    return stamp(monitor)

async def _post_update_record_state(ctx: UpdateContext) -> bool:
    agent_id = ctx.agent_id
    mcp_server = ctx.mcp_server
    monitor = ctx.monitor
    # Snapshot the behavioral baseline so the DB row carries it (survives a
    # JSON-snapshot loss + DB-hydrate restart). Fail-open: a serialization
    # error must never break the state record. (Fleet starvation fix 2026-06-03.)
    try:
        behavioral_snapshot = (
            monitor._behavioral_state.to_dict_for_persistence()
            if monitor is not None else None
        )
        # Tag the snapshot with the source its observations came from. The
        # row-level `sensor_eisv_source` records what was SUBMITTED; this
        # records what the verdict-authoritative estimator actually CONSUMED.
        # For an embodied agent under UNITARES_SENSOR_COUPLING=behavioral_only
        # those differ in implication: the physical sensor is cut from the ODE
        # spring but still feeds behavioral state, and nothing said so.
        if behavioral_snapshot is not None and monitor is not None:
            obs_source = getattr(monitor, "_behavioral_obs_source", None)
            if obs_source:
                behavioral_snapshot["obs_source"] = obs_source
    except Exception:
        behavioral_snapshot = None

    # Assemble a versioned, append-only provenance envelope.  This is a pure
    # serialization step: it reads values already computed by the monitor and
    # never feeds back into measurement, policy, or enforcement.
    eisv_telemetry = None
    try:
        from src.eisv_telemetry import build_eisv_telemetry_envelope
        shadow_ablations = {
            name: dict(candidate)
            for name, candidate in (
                ctx.agent_state.get("_eisv_shadow_ablations") or {}
            ).items()
            if isinstance(candidate, dict)
        }
        # Observe the legacy C(V) dependency in trajectory identity without
        # changing the signature, similarity, trust tier, or risk policy.  This
        # runs at the append-only state boundary so every eligible check-in can
        # be replayed before any live identity migration is considered.
        try:
            from src.behavioral_trajectory import (
                compute_legacy_coherence_identity_shadow,
            )

            state = monitor.state if monitor is not None else None
            lifetime_updates = getattr(state, "update_count", 0) if state else 0
            if ctx.meta and hasattr(ctx.meta, "total_updates"):
                try:
                    lifetime_updates = max(
                        int(lifetime_updates or 0),
                        int(ctx.meta.total_updates or 0),
                    )
                except (TypeError, ValueError):
                    lifetime_updates = int(lifetime_updates or 0)
            coherence_history = list(
                getattr(state, "coherence_history", []) if state else []
            )
            eisv_history_count = min(
                (
                    len(list(getattr(state, name, []) or []))
                    for name in ("E_history", "I_history", "S_history", "V_history")
                ),
                default=0,
            )
            trajectory_shadow = compute_legacy_coherence_identity_shadow(
                coherence_history=coherence_history,
                update_count=lifetime_updates,
                eisv_history_count=eisv_history_count,
            )
            shadow_ablations.setdefault("legacy_coherence_neutralized", {})[
                "trajectory_identity"
            ] = trajectory_shadow
        except Exception as exc:
            logger.debug(
                "Legacy-coherence trajectory identity shadow skipped for %s...: %s",
                (agent_id or "")[:8],
                exc,
            )
        confidence_shadows = (
            (ctx.result.get("confidence_reliability") or {}).get(
                "shadow_ablations"
            )
            or {}
        )
        for name, candidate in confidence_shadows.items():
            if not isinstance(candidate, dict):
                continue
            shadow_ablations.setdefault(name, {})[
                "derived_confidence"
            ] = dict(candidate)
        eisv_telemetry = build_eisv_telemetry_envelope(
            metrics=ctx.metrics_dict,
            behavioral_snapshot=behavioral_snapshot,
            submitted_sensor=ctx.agent_state.get("sensor_eisv"),
            submitted_source=ctx.agent_state.get("sensor_eisv_source"),
            submitted_afferents=ctx.submitted_afferents,
            derivation=ctx.agent_state.get("_eisv_derivation"),
            policy_evaluation=ctx.result.get("policy_evaluation"),
            enforcement=ctx.result.get("enforcement"),
            shadow_ablations=shadow_ablations,
            observed_at=ctx.result.get("timestamp"),
            self_report=_self_report_record(ctx),
        )
        # Full response mode can expose the same record that was persisted;
        # compact/mirror modes continue to omit it through response_formatter.
        ctx.result["eisv_telemetry"] = eisv_telemetry
    except Exception as exc:
        logger.warning(
            "EISV telemetry envelope skipped for %s...: %s",
            (agent_id or "")[:8],
            exc,
        )


    return await _persist_agent_state_row(ctx, behavioral_snapshot, eisv_telemetry)


async def _recover_missing_agent_for_state_record(ctx: UpdateContext) -> None:
    agent_id = ctx.agent_id
    mcp_server = ctx.mcp_server
    await agent_storage.create_agent(
        agent_id=agent_id,
        api_key=ctx.api_key or "",
        status='active',
    )
    # S21-b §1: hydrate dict so require_registered_agent sees the new
    # row immediately (axiom-#3 H14). Self-healing path: this branch
    # fires when record_agent_state finds no PG row for this agent_id.
    try:
        from src.agent_metadata_persistence import register_minted_agent_in_dict
        register_minted_agent_in_dict(agent_id, status='active')
    except Exception as hyd_err:
        logger.debug(f"Phase eager hydration failed for {agent_id}: {hyd_err}")
    # S8a Phase-2: stamp default class tag on the recovery-create path.
    # Same rationale as the is_new_agent branch above; this branch
    # fires when record_agent_state hits a missing-row ValueError.
    try:
        from src.grounding.onboard_classifier import stamp_default_class_tags
        recovery_meta = mcp_server.agent_metadata.get(agent_id)
        # Resolve a name explicitly: prefer ctx.label (always set
        # at phase 1), fall back to meta.label. Without this, a
        # known resident hitting the recovery path would land as
        # ``ephemeral`` (council finding 2026-04-30 HIGH#4).
        recovery_label = (
            getattr(ctx, "label", None)
            or (recovery_meta.label if recovery_meta else None)
        )
        stamped = await stamp_default_class_tags(
            agent_id, recovery_label, meta=recovery_meta
        )
        if stamped is not None:
            logger.info(
                f"[PROCESS_UPDATE] S8a default-stamp (recovery): "
                f"{agent_id[:8]}... tagged {stamped} (label={recovery_label!r})"
            )
    except Exception as stamp_err:
        # Recovery path is exactly the case where stamping matters
        # most — the agent was found missing from PG. Log at
        # warning so a silently-failed stamp here is visible.
        logger.warning(
            f"[PROCESS_UPDATE] recovery default-stamp failed for "
            f"{agent_id[:8]}... (agent will be misclassified until "
            f"next stamp): {stamp_err}"
        )

async def _post_update_save_baseline(ctx: UpdateContext) -> None:
    agent_id = ctx.agent_id
    # PostgreSQL: Save agent baseline (fire-and-forget, matches record_agent_state pattern)
    try:
        from governance_core import get_baseline_or_none
        baseline = get_baseline_or_none(agent_id)
        if baseline:
            from src.db import get_db
            db = get_db()
            if db:
                await db.save_agent_baseline(baseline.to_dict())
                logger.debug(f"PostgreSQL: Saved baseline for {agent_id[:12]}...")
    except Exception as e:
        logger.debug(f"Baseline save skipped: {e}")


async def _post_update_auto_outcome(ctx: UpdateContext) -> None:
    agent_id = ctx.agent_id
    # Auto-emitted outcomes are weak agent-reported labels inferred from text.
    # Score the label itself (completed=1, failed=0); neither behavioral update
    # consistency nor legacy ODE control feedback measures outcome quality.
    # Non-authored substrate observations/interpretations and runtime tool-count
    # rollups ("N tool calls completed: ...") are not task-completion claims (#2418).
    from src.dual_log.continuity import is_automatic_or_rollup_checkin
    from src.auto_outcome_labels import AUTO_OUTCOME_RULE_VERSION, infer_outcome_label

    ctx.outcome_event_id = None
    if is_automatic_or_rollup_checkin(
        response_text=ctx.response_text,
        epistemic_class=ctx.epistemic_class,
    ):
        return
    try:
        if ctx.response_text and ctx.complexity >= 0.3:
            label = infer_outcome_label(ctx.response_text)
            if label == "task_completed":
                from src.db import get_db
                _db = get_db()
                if _db:
                    _summary = ctx.response_text[:500] if len(ctx.response_text) > 500 else ctx.response_text
                    _detail = {
                        'source': 'auto_checkin',
                        'auto_outcome_rule_version': AUTO_OUTCOME_RULE_VERSION,
                        'outcome_score_source': 'agent_reported_completion_label',
                        'coherence_used_for_outcome_score': False,
                        'complexity': ctx.complexity,
                        'confidence': ctx.arguments.get('confidence'),
                        'summary': _summary,
                    }
                    from src.outcome_corroboration import (
                        GRADE_WEIGHTS,
                        TOOL_OBSERVED,
                        assess_outcome_corroboration,
                    )
                    _assessment = assess_outcome_corroboration(
                        'task_completed',
                        _detail,
                        'agent_reported_tool_result',
                    )
                    ctx.outcome_event_id = await _record_auto_outcome_event(
                        _db,
                        agent_id=agent_id,
                        outcome_type='task_completed',
                        is_bad=False,
                        outcome_score=1.0,
                        session_id=ctx.arguments.get('client_session_id'),
                        eisv_e=ctx.metrics_dict.get('E'),
                        eisv_i=ctx.metrics_dict.get('I'),
                        eisv_s=ctx.metrics_dict.get('S'),
                        eisv_v=ctx.metrics_dict.get('V'),
                        eisv_phi=ctx.metrics_dict.get('phi'),
                        eisv_verdict=ctx.metrics_dict.get('verdict'),
                        eisv_coherence=ctx.metrics_dict.get('coherence'),
                        eisv_regime=ctx.metrics_dict.get('regime'),
                        detail=_detail,
                        # Inferred from keyword regex over agent's own
                        # response_text — closest match in v1 enum.
                        verification_source='agent_reported_tool_result',
                    )
                    if ctx.outcome_event_id:
                        logger.debug(f"Auto-emitted outcome event {ctx.outcome_event_id} for {agent_id}")
                        # Record calibration from auto-emitted positive outcome
                        _conf = ctx.confidence
                        if (
                            _conf is not None
                            and _assessment.evidence_weight >= GRADE_WEIGHTS[TOOL_OBSERVED]
                        ):
                            try:
                                from src.calibration import calibration_checker
                                calibration_checker.record_prediction(
                                    confidence=float(_conf),
                                    predicted_correct=(float(_conf) >= 0.5),
                                    actual_correct=1.0,
                                    agent_id=agent_id,
                                )
                            except Exception as _ce:
                                logger.debug(f"Calibration from positive outcome skipped: {_ce}")
            # Auto-emit negative outcome event for failure signals
            if not ctx.outcome_event_id:
                if label == "task_failed":
                    from src.db import get_db
                    _db = get_db()
                    if _db:
                        _summary = ctx.response_text[:500] if len(ctx.response_text) > 500 else ctx.response_text
                        _bad_score = 0.0
                        _detail = {
                            'source': 'auto_checkin',
                            'auto_outcome_rule_version': AUTO_OUTCOME_RULE_VERSION,
                            'outcome_score_source': 'agent_reported_failure_label',
                            'coherence_used_for_outcome_score': False,
                            'complexity': ctx.complexity,
                            'confidence': ctx.arguments.get('confidence'),
                            'summary': _summary,
                            'is_negative': True,
                        }
                        from src.outcome_corroboration import (
                            GRADE_WEIGHTS,
                            TOOL_OBSERVED,
                            assess_outcome_corroboration,
                        )
                        _assessment = assess_outcome_corroboration(
                            'task_failed',
                            _detail,
                            'agent_reported_tool_result',
                        )
                        _bad_oid = await _record_auto_outcome_event(
                        _db,
                            agent_id=agent_id,
                            outcome_type='task_failed',
                            is_bad=True,
                            outcome_score=_bad_score,
                            session_id=ctx.arguments.get('client_session_id'),
                            eisv_e=ctx.metrics_dict.get('E'),
                            eisv_i=ctx.metrics_dict.get('I'),
                            eisv_s=ctx.metrics_dict.get('S'),
                            eisv_v=ctx.metrics_dict.get('V'),
                            eisv_phi=ctx.metrics_dict.get('phi'),
                            eisv_verdict=ctx.metrics_dict.get('verdict'),
                            eisv_coherence=ctx.metrics_dict.get('coherence'),
                            eisv_regime=ctx.metrics_dict.get('regime'),
                            detail=_detail,
                            # Same regex-on-response-text inference as
                            # task_completed above; not server-observed.
                            verification_source='agent_reported_tool_result',
                        )
                        if _bad_oid:
                            logger.debug(f"Auto-emitted negative outcome event {_bad_oid} for {agent_id}")
                            _conf = ctx.confidence
                            if (
                                _conf is not None
                                and _assessment.evidence_weight >= GRADE_WEIGHTS[TOOL_OBSERVED]
                            ):
                                try:
                                    from src.calibration import calibration_checker
                                    calibration_checker.record_prediction(
                                        confidence=float(_conf),
                                        predicted_correct=(float(_conf) >= 0.5),
                                        actual_correct=_bad_score,
                                        agent_id=agent_id,
                                    )
                                except Exception as _ce:
                                    logger.debug(f"Calibration from negative outcome skipped: {_ce}")
    except Exception as e:
        logger.debug(f"Outcome event auto-emit skipped: {e}")
