"""
Agent loop detection and authenticated update processing.

Detects recursive self-monitoring loops, processes authenticated updates,
auto-initiates dialectic recovery for paused agents.
"""

from __future__ import annotations

import os
import re
import json
import time
import asyncio
from typing import Any, Dict, Optional
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import partial
from uuid import uuid4

from src.logging_utils import get_logger
from src.agent_metadata_model import agent_metadata
from src.agent_monitor_state import monitors, save_monitor_state, save_monitor_state_async
from src.agent_identity_auth import verify_agent_ownership
from src.agent_metadata_persistence import load_metadata_async
from src.perf_monitor import record_ms as _perf_record_ms
from src.loop_rules import LoopWindow, evaluate_loop_rules

logger = get_logger(__name__)

# Telemetry: ring buffer of governance circuit breaker pause timestamps
_governance_pause_timestamps: deque[datetime] = deque(maxlen=100)

# prevent fire-and-forget tasks from being GC'd (P001)
_background_tasks: set[asyncio.Task] = set()


def mark_circuit_breaker_enforcement_applied(
    result: Dict[str, Any],
    *,
    actor: str,
    effect: str,
    actuation_id: str | None = None,
    applied_at: str | None = None,
) -> str:
    """Mark that the runtime actuator applied a policy-requested pause.

    ``monitor_result`` records policy evaluation and an unapplied enforcement
    candidate. The authenticated update boundary is where that candidate becomes
    an actual circuit breaker by mutating agent metadata. Keep the mutation
    explicit so measurement, policy, and actuator state remain inspectable.
    """
    prior = result.get("enforcement")
    prior = dict(prior) if isinstance(prior, dict) else {}
    resolved_actuation_id = actuation_id or str(uuid4())
    resolved_applied_at = applied_at or datetime.now(timezone.utc).isoformat()
    result["enforcement"] = {
        **prior,
        "schema": prior.get("schema") or "governance.enforcement.v1",
        "scope": prior.get("scope") or "runtime_circuit_breaker",
        "requested": True,
        "applied": True,
        "mode": "circuit_breaker",
        "actor": actor,
        "effect": effect,
        "actuation_id": resolved_actuation_id,
        "applied_at": resolved_applied_at,
        "note": "Circuit breaker applied at the runtime boundary after policy evaluation.",
    }
    result["paused"] = True
    result["circuit_breaker_triggered"] = True
    return resolved_actuation_id


def get_circuit_breaker_telemetry() -> Dict[str, Any]:
    """Return governance circuit breaker telemetry snapshot."""
    now = datetime.now(timezone.utc)
    trips = list(_governance_pause_timestamps)
    trips_1h = sum(1 for t in trips if (now - t).total_seconds() <= 3600)
    trips_24h = sum(1 for t in trips if (now - t).total_seconds() <= 86400)
    last_trip = trips[-1].isoformat() if trips else None
    return {
        "trips_1h": trips_1h,
        "trips_24h": trips_24h,
        "last_trip": last_trip,
    }


def _within_recovery_grace(meta, now: datetime) -> bool:
    recovery_attempt_at = getattr(meta, "recovery_attempt_at", None)
    if not recovery_attempt_at:
        return False
    try:
        recovery_time = datetime.fromisoformat(recovery_attempt_at)
        return (now - recovery_time).total_seconds() < 120.0
    except (ValueError, TypeError):
        return False


def _recent_rapid_timestamps(values: list[str], now: datetime) -> list[str]:
    recent = []
    for value in values:
        try:
            if (now - datetime.fromisoformat(value)).total_seconds() <= 30.0:
                recent.append(value)
        except (ValueError, TypeError):
            continue
    return recent


def _skip_rapid_rules(meta, server_start_time: datetime) -> bool:
    grace_period = timedelta(minutes=5)
    if datetime.now() - server_start_time < grace_period:
        return True
    try:
        raw_created = meta.created_at
        agent_created = datetime.fromisoformat(
            raw_created.replace("Z", "+00:00") if "Z" in raw_created else raw_created
        )
        agent_age = (
            datetime.now(agent_created.tzinfo) - agent_created
            if agent_created.tzinfo
            else datetime.now() - agent_created.replace(tzinfo=None)
        )
        return agent_age < grace_period
    except (ValueError, TypeError, AttributeError):
        return False


def detect_loop_pattern(agent_id: str) -> tuple[bool, str]:
    """Detect update-loop patterns through the ordered pure rule engine."""
    from src.agent_process_mgmt import SERVER_START_TIME

    if agent_id not in agent_metadata:
        return False, ""

    meta = agent_metadata[agent_id]
    if meta.loop_cooldown_until:
        cooldown_until = datetime.fromisoformat(meta.loop_cooldown_until)
        if datetime.now() < cooldown_until:
            remaining = (cooldown_until - datetime.now()).total_seconds()
            return True, f"Loop cooldown active. Wait {remaining:.1f}s before retrying."

    if len(meta.recent_update_timestamps) < 3:
        return False, ""

    now = datetime.now()
    timestamps = meta.recent_update_timestamps[-10:]
    decisions = meta.recent_decisions[-10:]
    tags = {tag.lower() for tag in (getattr(meta, "tags", None) or [])}
    is_autonomous = bool({"autonomous", "embodied"} & tags)
    if is_autonomous:
        logger.debug(
            "Agent '%s' is autonomous — skipping decision-based loop patterns (4-6)",
            agent_id[:8],
        )

    reason = evaluate_loop_rules(
        LoopWindow(
            timestamps=timestamps,
            decisions=decisions,
            rapid_timestamps=_recent_rapid_timestamps(timestamps, now),
            now=now,
            skip_rapid=_skip_rapid_rules(meta, SERVER_START_TIME),
            in_recovery_grace=_within_recovery_grace(meta, now),
            is_autonomous=is_autonomous,
        )
    )
    return (True, reason) if reason else (False, "")


def process_update_authenticated(
    agent_id: str,
    api_key: str,
    agent_state: dict,
    auto_save: bool = True
) -> dict:
    """
    Process governance update with authentication enforcement (synchronous version).

    This is the SECURE entry point for processing updates.

    Raises:
        PermissionError: If authentication fails
        ValueError: If agent_id is invalid
    """
    from src.agent_lifecycle import get_or_create_monitor

    is_valid, error_msg = verify_agent_ownership(agent_id, api_key)
    if not is_valid:
        raise PermissionError(f"Authentication failed: {error_msg}")

    monitor = get_or_create_monitor(agent_id)
    result = monitor.process_update(agent_state)

    if auto_save:
        save_monitor_state(agent_id, monitor)

        meta = agent_metadata[agent_id]
        now = datetime.now(timezone.utc).isoformat()
        meta.last_update = now
        meta.total_updates += 1

        decision_action = result.get('decision', {}).get('action', 'unknown')
        meta.add_recent_update(now, decision_action)

    return result


# Alias for cleaner naming (backward compatible)
update_agent_auth = process_update_authenticated


async def process_update_authenticated_async(
    agent_id: str,
    api_key: str,
    agent_state: dict,
    auto_save: bool = True,
    confidence: Optional[float] = None,
    task_type: str = "mixed",
    session_bound: bool = False
) -> dict:
    """
    Process governance update with authentication enforcement (async version).

    This is the SECURE async entry point for processing updates.

    Raises:
        PermissionError: If authentication fails
        ValueError: If agent_id is invalid
    """
    from src.agent_lifecycle import get_or_create_monitor

    loop = asyncio.get_running_loop()
    _t0 = time.perf_counter()
    is_valid, error_msg = await loop.run_in_executor(
        None, verify_agent_ownership, agent_id, api_key, session_bound
    )
    _perf_record_ms("ode.auth_ms", (time.perf_counter() - _t0) * 1000.0)
    if not is_valid:
        raise PermissionError(f"Authentication failed: {error_msg}")

    # Sticky-archive gate for in-process callers. The MCP-tool path
    # (handle_process_agent_update → phases.py) has its own auto-resume /
    # refusal logic before it reaches this function, but in-process callers
    # like Steward (unitares-pi-plugin) reach us directly and would otherwise
    # silently resurrect archived identities. Refuse here so the gate is
    # uniform across all paths.
    meta = agent_metadata.get(agent_id)
    if meta is not None and getattr(meta, "status", None) == "archived":
        raise ValueError(
            f"Agent '{agent_id}' is archived and cannot be updated. "
            f"Use self_recovery(action='quick') to restore, or "
            f"onboard(force_new=true) for a new identity."
        )

    # Check for loop pattern BEFORE processing
    _t_loop = time.perf_counter()
    is_loop, loop_reason = await loop.run_in_executor(None, detect_loop_pattern, agent_id)
    _perf_record_ms("ode.loop_detect_ms", (time.perf_counter() - _t_loop) * 1000.0)
    if is_loop:
        meta = agent_metadata[agent_id]

        if "Loop cooldown active" in loop_reason:
            match = re.search(r'Wait ([\d.]+)s', loop_reason)
            if match:
                remaining = float(match.group(1))
                raise ValueError(
                    f"Self-monitoring loop detected: {loop_reason}. "
                    f"Cooldown expires in {remaining:.1f} seconds."
                )
            else:
                raise ValueError(f"Self-monitoring loop detected: {loop_reason}")

        # Set cooldown period (pattern-specific)
        if "Rapid-fire updates detected" in loop_reason:
            cooldown_seconds = 5
        elif "Rapid update pattern" in loop_reason or "Recursive reject pattern" in loop_reason:
            cooldown_seconds = 15
        else:
            cooldown_seconds = 30

        cooldown_until = datetime.now() + timedelta(seconds=cooldown_seconds)
        meta.loop_cooldown_until = cooldown_until.isoformat()

        if not hasattr(meta, 'loop_incidents') or meta.loop_incidents is None:
            meta.loop_incidents = []

        incident = {
            'detected_at': datetime.now().isoformat(),
            'reason': loop_reason,
            'cooldown_seconds': cooldown_seconds,
            'timestamp_history': meta.recent_update_timestamps.copy() if meta.recent_update_timestamps else []
        }
        meta.loop_incidents.append(incident)

        if len(meta.loop_incidents) > 20:
            meta.loop_incidents = meta.loop_incidents[-20:]

        if not meta.loop_detected_at:
            meta.loop_detected_at = datetime.now().isoformat()
            meta.add_lifecycle_event("loop_detected", loop_reason)
            logger.warning(f"⚠️  Loop detected for agent '{agent_id}': {loop_reason} (cooldown: {cooldown_seconds}s)")
        else:
            incident_count = len(meta.loop_incidents)
            logger.warning(f"⚠️  Loop incident #{incident_count} for agent '{agent_id}': {loop_reason} (cooldown: {cooldown_seconds}s)")

        cooldown_time_str = cooldown_until.strftime('%Y-%m-%d %H:%M:%S')

        recovery_tools = []
        if cooldown_seconds <= 5:
            recovery_tools.append("self_recovery(action='quick') (if state is safe)")
        else:
            recovery_tools.append("self_recovery(action='quick') (if state is safe)")
            recovery_tools.append("request_dialectic_review (for peer assistance)")

        recovery_guidance = (
            f"\n\n🔧 Recovery Options:\n"
            f"- Wait {cooldown_seconds}s for cooldown to expire (automatic)\n"
            f"- Use {recovery_tools[0]} to resume immediately if your state is safe\n"
        )
        if len(recovery_tools) > 1:
            recovery_guidance += f"- Use {recovery_tools[1]} to get peer assistance\n"
        recovery_guidance += (
            f"\n💡 Tip: These recovery tools can help you get unstuck faster. "
            f"See AI_ASSISTANT_GUIDE.md for details."
        )

        raise ValueError(
            f"Self-monitoring loop detected: {loop_reason}. "
            f"Updates blocked for {cooldown_seconds} seconds to prevent system crash. "
            f"Cooldown until: {cooldown_time_str} ({cooldown_seconds}s remaining)"
            + recovery_guidance
        )

    # Get or create monitor
    _t_setup = time.perf_counter()
    monitor = await loop.run_in_executor(None, get_or_create_monitor, agent_id)
    # Heal the DB ↔ file persistence split: if the on-disk state file is
    # missing but core.agent_state has history, hydrate update_count +
    # rolling histories from DB so this check-in continues from real state
    # rather than from a fresh zero.
    from src.agent_monitor_state import hydrate_from_db_if_fresh
    await hydrate_from_db_if_fresh(monitor, agent_id)
    _perf_record_ms("ode.monitor_setup_ms", (time.perf_counter() - _t_setup) * 1000.0)

    task_type = agent_state.get("task_type", "mixed")

    # Numpy ODE step — `monitor.process_update` dispatched via the default
    # executor pool. v0.3 RESOLUTION's "load-bearing unknown" was "what's
    # in the 7s ODE remainder?"; this timer answers that. Named
    # `ode.numpy_step_ms` (not `compute_ms`) because the wall-clock here
    # includes executor queue-wait time as well as the numpy work — under
    # concurrent load on a saturated default pool, queue-wait can dominate
    # numpy compute. Disambiguating "numpy slow" from "executor saturated"
    # requires looking at executor queue depth alongside, which is out of
    # scope for this branch; called out in the eval doc's falsifier matrix.
    _t_numpy = time.perf_counter()
    result = await loop.run_in_executor(
        None,
        partial(monitor.process_update, agent_state, confidence=confidence, task_type=task_type)
    )
    _perf_record_ms("ode.numpy_step_ms", (time.perf_counter() - _t_numpy) * 1000.0)

    if auto_save:
        decision_action = result.get('decision', {}).get('action', 'unknown')
        now = datetime.now(timezone.utc).isoformat()

        meta = agent_metadata.get(agent_id)
        if meta is not None:
            meta.last_update = now
            meta.add_recent_update(now, decision_action)

        # Atomically increment total_updates in PostgreSQL
        _t_persist = time.perf_counter()
        try:
            from src import agent_storage
            db = agent_storage.get_db()
            new_count = await db.increment_update_count(agent_id, extra_metadata={
                "recent_update_timestamps": meta.recent_update_timestamps if meta else [now],
                "recent_decisions": meta.recent_decisions if meta else [decision_action],
            })
            _perf_record_ms("ode.persist_ms", (time.perf_counter() - _t_persist) * 1000.0)
            if meta is not None:
                meta.total_updates = new_count
        except Exception as e:
            # Record the failed-persist sample under a distinct key so the
            # success-path series (`ode.persist_ms`) reflects only successful
            # writes, while operators can still tell "failure" from "never
            # fired" in perf_monitor snapshots.
            _perf_record_ms("ode.persist_failed_ms", (time.perf_counter() - _t_persist) * 1000.0)
            logger.warning(f"Failed to increment update count for {agent_id[:8]}...: {e}")
            if meta is not None:
                meta.total_updates += 1

        # Enforce pause decisions (circuit breaker)
        if decision_action == 'pause' and meta is not None:
            meta.status = "paused"
            meta.paused_at = now
            decision_reason = result.get('decision', {}).get('reason', 'Circuit breaker triggered')
            actuation_id = mark_circuit_breaker_enforcement_applied(
                result,
                actor="agent_loop_detection",
                effect="agent_metadata.status=paused",
                applied_at=now,
            )
            meta.add_lifecycle_event(
                "paused",
                decision_reason,
                actuation_id=actuation_id,
            )
            logger.warning(f"⚠️  Circuit breaker triggered for agent '{agent_id}': {decision_reason}")

            # P011: persist paused_at + the lifecycle event so they survive
            # the next load_metadata_async(force=True). Without this, the
            # agent record shows paused_at=null and lifecycle_events=[]
            # despite the pause having actually fired.
            try:
                from src import agent_storage
                await agent_storage.persist_runtime_state(
                    agent_id,
                    paused_at=now,
                    append_lifecycle_event={
                        "event": "paused",
                        "timestamp": now,
                        "reason": decision_reason,
                        "actuation_id": actuation_id,
                    },
                )
            except Exception as e:
                logger.warning(
                    f"persist_runtime_state(paused) failed for {agent_id[:8]}...: {e}"
                )

            # Telemetry: record governance pause timestamp
            _governance_pause_timestamps.append(datetime.now(timezone.utc))

            # Broadcast circuit_breaker_trip event
            try:
                from src.broadcaster import broadcaster_instance
                task = loop.create_task(broadcaster_instance.broadcast_event(
                    "circuit_breaker_trip",
                    agent_id=agent_id,
                    payload={
                        "reason": decision_reason,
                        "actuation_id": actuation_id,
                    },
                ))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            except Exception as e:
                logger.debug(f"Could not broadcast circuit_breaker_trip: {e}")

            try:
                auto_recovery = os.getenv("UNITARES_AUTO_DIALECTIC_RECOVERY", "1").strip().lower() not in ("0", "false", "no")
                if auto_recovery:
                    task = loop.create_task(_auto_initiate_dialectic_recovery(agent_id, decision_reason))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                    result["auto_recovery_initiated"] = True
                    result["auto_recovery_note"] = "Dialectic recovery auto-initiated (self-governance mode)"
            except Exception as e:
                logger.warning(f"Could not auto-initiate dialectic recovery: {e}")

        # Clear cooldown if it has passed
        if meta is not None and meta.loop_cooldown_until:
            cooldown_until = datetime.fromisoformat(meta.loop_cooldown_until)
            if datetime.now() >= cooldown_until:
                meta.loop_cooldown_until = None

        await save_monitor_state_async(agent_id, monitor)

    return result


async def _auto_initiate_dialectic_recovery(agent_id: str, reason: str) -> None:
    """
    SELF-GOVERNANCE: Auto-initiate dialectic recovery for paused agents.

    Tries peer review first. If no peers are available, falls back to
    LLM-assisted dialectic (Ollama) as synthetic reviewer.
    """
    await asyncio.sleep(2)

    try:
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review
        from src.mcp_handlers.dialectic.reviewer import select_reviewer

        logger.info(f"Auto-initiating dialectic recovery for paused agent '{agent_id}'")

        meta = agent_metadata.get(agent_id)
        api_key = meta.api_key if meta else None

        if not api_key:
            logger.warning(f"Cannot auto-initiate dialectic for '{agent_id}': no API key")
            return

        # Wave 2 audit: force=True dropped per PR #350 precedent. Reviewer
        # selection scans the in-memory fleet; cache is fresh enough.
        await load_metadata_async()
        reviewer = await select_reviewer(
            paused_agent_id=agent_id,
            metadata=agent_metadata,
        )

        if reviewer:
            logger.info(f"Peer reviewer '{reviewer[:8]}...' found for '{agent_id}', using peer dialectic")
            result = await handle_request_dialectic_review({
                "agent_id": agent_id,
                "reason": f"Auto-recovery: {reason}",
                "api_key": api_key,
                "reviewer_mode": "auto",
            })
            logger.info(f"Peer dialectic initiated for '{agent_id}'")
            return

        logger.info(f"No peer reviewers available for '{agent_id}', using LLM-assisted dialectic")

        proposed_conditions = []
        monitor = monitors.get(agent_id)
        if monitor and hasattr(monitor, 'state'):
            state = monitor.state
            if hasattr(state, 'S') and state.S > 1.0:
                proposed_conditions.append("Reduce task complexity")
            if hasattr(state, 'V') and abs(state.V) > 0.5:
                proposed_conditions.append("Rebalance energy-integrity ratio")
        if not proposed_conditions:
            proposed_conditions = ["Review and adjust approach", "Reduce scope if needed"]

        result = await handle_request_dialectic_review({
            "agent_id": agent_id,
            "reason": f"Auto-recovery: {reason}",
            "api_key": api_key,
            "reviewer_mode": "llm",
            "root_cause": reason,
            "proposed_conditions": proposed_conditions,
            "reasoning": "Circuit breaker triggered. Auto-recovery attempting LLM-assisted dialectic.",
        })

        if isinstance(result, list) and len(result) > 0:
            try:
                text = result[0].text if hasattr(result[0], 'text') else ""
                content = json.loads(text) if text else {}
                recommendation = content.get("recommendation", "").upper()

                if recommendation == "RESUME":
                    meta = agent_metadata.get(agent_id)
                    if meta:
                        meta.status = "active"
                        meta.paused_at = None
                        meta.loop_cooldown_until = None
                        meta.loop_detected_at = None
                        meta.recent_update_timestamps = []
                        meta.recent_decisions = []
                        resume_reason = (
                            f"LLM dialectic recommended RESUME: "
                            f"{content.get('message', '')[:100]}"
                        )
                        meta.add_lifecycle_event("auto_resumed_dialectic", resume_reason)
                        logger.info(f"Agent '{agent_id}' auto-resumed after LLM dialectic")

                        # P011: persist the resume so paused_at=None survives
                        # reload and the dialectic event is in the audit trail.
                        try:
                            from src import agent_storage
                            await agent_storage.persist_runtime_state(
                                agent_id,
                                paused_at=None,
                                loop_cooldown_until=None,
                                loop_detected_at=None,
                                append_lifecycle_event={
                                    "event": "auto_resumed_dialectic",
                                    "timestamp": datetime.now().isoformat(),
                                    "reason": resume_reason,
                                },
                            )
                        except Exception as e:
                            logger.warning(
                                f"persist_runtime_state(auto_resumed_dialectic) failed "
                                f"for {agent_id[:8]}...: {e}"
                            )
                elif recommendation == "COOLDOWN":
                    logger.info(f"Agent '{agent_id}' in cooldown after LLM dialectic — stuck-detector will handle later")
                else:
                    logger.warning(f"Agent '{agent_id}' needs human attention — LLM dialectic: {recommendation}")
            except (json.JSONDecodeError, AttributeError, KeyError) as e:
                logger.warning(f"Could not parse dialectic result for auto-action: {e}")

    except Exception as e:
        logger.error(f"Failed to auto-initiate dialectic recovery for '{agent_id}': {e}")
        # Safety net: if all dialectic paths failed (no peers, LLM down, DB error),
        # check whether the agent's state is safe enough to auto-resume rather than
        # leaving it paused indefinitely. This prevents single-agent deployments
        # from getting permanently stuck when Ollama is unavailable.
        await _safety_net_resume(agent_id, reason=str(e))


async def _safety_net_resume(agent_id: str, reason: str) -> None:
    """Auto-resume a paused agent if its EISV state is safe, as a last resort.

    Thresholds mirror self_recovery's "quick" action: coherence > 0.40
    and risk < 0.60. If the agent isn't safe, it stays paused for the
    stuck-agent detector to pick up on its next sweep.
    """
    try:
        meta = agent_metadata.get(agent_id)
        if not meta or meta.status != "paused":
            return

        monitor = monitors.get(agent_id)
        if not monitor or not hasattr(monitor, 'state'):
            return

        coherence = getattr(monitor.state, 'coherence', None) or 0.0
        metrics = monitor.get_metrics()
        risk = metrics.get('mean_risk') or metrics.get('risk_score') or 0.5

        if coherence >= 0.40 and risk < 0.60:
            meta.status = "active"
            meta.paused_at = None
            meta.loop_cooldown_until = None
            meta.loop_detected_at = None
            meta.recent_update_timestamps = []
            meta.recent_decisions = []
            resume_reason = (
                f"All dialectic paths failed ({reason}); state safe "
                f"(coherence={coherence:.2f}, risk={risk:.2f}) — auto-resumed"
            )
            meta.add_lifecycle_event("safety_net_resumed", resume_reason)
            logger.info(
                f"Agent '{agent_id}' safety-net resumed "
                f"(coherence={coherence:.2f}, risk={risk:.2f}, dialectic failure: {reason})"
            )

            # P011: persist the resume so paused_at=None survives reload and
            # the safety-net event is in the audit trail.
            try:
                from src import agent_storage
                await agent_storage.persist_runtime_state(
                    agent_id,
                    paused_at=None,
                    loop_cooldown_until=None,
                    loop_detected_at=None,
                    append_lifecycle_event={
                        "event": "safety_net_resumed",
                        "timestamp": datetime.now().isoformat(),
                        "reason": resume_reason,
                    },
                )
            except Exception as e:
                logger.warning(
                    f"persist_runtime_state(safety_net_resumed) failed for {agent_id[:8]}...: {e}"
                )
        else:
            logger.warning(
                f"Agent '{agent_id}' NOT safe for safety-net resume "
                f"(coherence={coherence:.2f}, risk={risk:.2f}) — stays paused"
            )
    except Exception as e:
        logger.error(f"Safety-net resume check failed for '{agent_id}': {e}")
