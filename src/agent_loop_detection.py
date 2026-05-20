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
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from functools import partial

from src.logging_utils import get_logger
from src.agent_metadata_model import agent_metadata
from src.agent_monitor_state import monitors, save_monitor_state, save_monitor_state_async
from src.agent_identity_auth import verify_agent_ownership
from src.agent_metadata_persistence import load_metadata_async
from src.perf_monitor import record_ms as _perf_record_ms

logger = get_logger(__name__)

# Loop-pattern freshness guards. Both decision-based branches of Pattern 4
# (proceed-count, pause-count) and Pattern 7 count entries in fixed-size
# windows over recent_decisions. Without a "newest timestamp is recent"
# floor, a dormant history keeps re-firing: any new update sees the old
# burst, gets rejected, and the burst never rolls off. Proceeds naturally
# fire in tight bursts (<300s window), pauses spread out more, so the
# pause guard is deliberately wider.
PROCEED_LOOP_FRESHNESS_SECONDS = 600
PAUSE_LOOP_FRESHNESS_SECONDS = 3600

# Telemetry: ring buffer of governance circuit breaker pause timestamps
_governance_pause_timestamps: deque[datetime] = deque(maxlen=100)

# prevent fire-and-forget tasks from being GC'd (P001)
_background_tasks: set[asyncio.Task] = set()


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


def detect_loop_pattern(agent_id: str) -> tuple[bool, str]:
    """
    Detect recursive self-monitoring loop patterns.

    Detects patterns like:
    - Pattern 1: Multiple updates within same second (rapid-fire)
    - Pattern 2: 3+ updates within 10 seconds with 2+ reject decisions
    - Pattern 3: 4+ updates within 5 seconds (any decisions)
    - Pattern 4: Decision loop - same decision repeated 5+ times in recent history
    - Pattern 5: Slow-stuck pattern - 3+ updates in 60s with any reject
    - Pattern 6: Extended rapid pattern - 5+ updates in 120s regardless of decisions
    - Pattern 7: Slow proceed loop - 8+ proceed decisions in 5 min (no pause/reject needed)

    Returns:
        (is_loop, reason) - True if loop detected, with explanation
    """
    from src.agent_process_mgmt import SERVER_START_TIME

    if agent_id not in agent_metadata:
        return False, ""

    meta = agent_metadata[agent_id]

    # Check cooldown period
    if meta.loop_cooldown_until:
        cooldown_until = datetime.fromisoformat(meta.loop_cooldown_until)
        if datetime.now() < cooldown_until:
            remaining = (cooldown_until - datetime.now()).total_seconds()
            return True, f"Loop cooldown active. Wait {remaining:.1f}s before retrying."

    if len(meta.recent_update_timestamps) < 3:
        return False, ""

    # Check recovery grace period
    in_recovery_grace = False
    recovery_attempt_at = getattr(meta, 'recovery_attempt_at', None)
    if recovery_attempt_at:
        try:
            recovery_time = datetime.fromisoformat(recovery_attempt_at)
            in_recovery_grace = (datetime.now() - recovery_time).total_seconds() < 120.0
        except (ValueError, TypeError):
            pass

    all_timestamps = meta.recent_update_timestamps[-10:]
    all_decisions = meta.recent_decisions[-10:]

    # Filter to recent timestamps (within last 30 seconds) for Pattern 1
    now = datetime.now()
    recent_timestamps_for_pattern1 = []
    for ts_str in all_timestamps:
        try:
            ts = datetime.fromisoformat(ts_str)
            age_seconds = (now - ts).total_seconds()
            if age_seconds <= 30.0:
                recent_timestamps_for_pattern1.append(ts_str)
        except (ValueError, TypeError):
            continue

    recent_timestamps = all_timestamps
    recent_decisions = all_decisions

    # GRACE PERIOD: Allow rapid updates after server restart or agent creation
    server_restart_grace_period = timedelta(minutes=5)
    agent_creation_grace_period = timedelta(minutes=5)

    server_age = datetime.now() - SERVER_START_TIME
    in_server_grace_period = server_age < server_restart_grace_period

    in_agent_grace_period = False
    try:
        agent_created = datetime.fromisoformat(meta.created_at.replace('Z', '+00:00') if 'Z' in meta.created_at else meta.created_at)
        agent_age = datetime.now(agent_created.tzinfo) - agent_created if agent_created.tzinfo else datetime.now() - agent_created.replace(tzinfo=None)
        in_agent_grace_period = agent_age < agent_creation_grace_period
    except (ValueError, TypeError, AttributeError):
        pass

    skip_pattern1 = in_server_grace_period or in_agent_grace_period

    # Pattern 1: Multiple updates within same second (HISTORICAL PATTERN ANALYSIS)
    if not skip_pattern1 and len(recent_timestamps_for_pattern1) >= 2:
        rapid_pairs = []
        try:
            timestamps = [datetime.fromisoformat(ts) for ts in recent_timestamps_for_pattern1]

            for i in range(len(timestamps) - 1):
                time_diff = (timestamps[i + 1] - timestamps[i]).total_seconds()
                if time_diff < 0.3:
                    rapid_pairs.append((i, i + 1, time_diff))

            if rapid_pairs:
                pair_count = len(rapid_pairs)
                fastest_pair = min(rapid_pairs, key=lambda x: x[2])
                return True, f"Rapid-fire updates detected ({pair_count} pair(s) within 0.3s, fastest: {fastest_pair[2]*1000:.1f}ms apart)"
        except (ValueError, TypeError):
            pass

    # Check for 3+ updates within 0.5 seconds
    if not skip_pattern1 and len(recent_timestamps_for_pattern1) >= 3:
        try:
            timestamps = [datetime.fromisoformat(ts) for ts in recent_timestamps_for_pattern1]

            for i in range(len(timestamps) - 2):
                t1 = timestamps[i]
                t3 = timestamps[i + 2]
                if (t3 - t1).total_seconds() < 0.5:
                    return True, f"Rapid-fire updates detected (3+ updates within 0.5 seconds, detected at positions {i}-{i+2})"
        except (ValueError, TypeError):
            pass

    # Check for 4+ updates within 1 second
    if not skip_pattern1 and len(recent_timestamps_for_pattern1) >= 4:
        try:
            timestamps = [datetime.fromisoformat(ts) for ts in recent_timestamps_for_pattern1]

            for i in range(len(timestamps) - 3):
                t1 = timestamps[i]
                t4 = timestamps[i + 3]
                if (t4 - t1).total_seconds() < 1.0:
                    return True, f"Rapid-fire updates detected (4+ updates within 1 second, detected at positions {i}-{i+3})"
        except (ValueError, TypeError):
            pass

    # Pattern 2: 3+ updates within 10 seconds, all with "reject" decisions
    if not in_recovery_grace and len(recent_timestamps) >= 3:
        last_three_timestamps = recent_timestamps[-3:]
        last_three_decisions = recent_decisions[-3:]

        try:
            timestamps = [datetime.fromisoformat(ts) for ts in last_three_timestamps]
            time_span = (timestamps[-1] - timestamps[0]).total_seconds()

            if time_span <= 10.0:
                pause_count = sum(1 for d in last_three_decisions if d in ["pause", "reject"])
                if pause_count >= 2:
                    return True, f"Recursive pause pattern: {pause_count} pause decisions within {time_span:.1f}s"
        except (ValueError, TypeError):
            pass

    # Pattern 3: 4+ updates within 5 seconds with concerning decisions
    if len(recent_timestamps) >= 4:
        last_four_timestamps = recent_timestamps[-4:]
        last_four_decisions = recent_decisions[-4:]
        try:
            timestamps = [datetime.fromisoformat(ts) for ts in last_four_timestamps]
            time_span = (timestamps[-1] - timestamps[0]).total_seconds()

            if time_span <= 5.0:
                concerning_count = sum(1 for d in last_four_decisions if d in ["pause", "reject"])
                if concerning_count >= 1:
                    return True, f"Rapid update pattern: 4+ updates within {time_span:.1f}s with {concerning_count} pause/reject decision(s)"
        except (ValueError, TypeError):
            pass

    # Exempt autonomous/embodied agents from decision-based patterns (4-6).
    # These agents can't change behavior in response to pause decisions —
    # blocking updates prevents EISV recovery. Rapid-fire patterns (1-3)
    # still apply to prevent actual runaway loops.
    agent_tags = set(t.lower() for t in (getattr(meta, 'tags', None) or []))
    is_autonomous = bool({"autonomous", "embodied"} & agent_tags)
    if is_autonomous:
        logger.debug("Agent '%s' is autonomous — skipping decision-based loop patterns (4-6)", agent_id[:8])

    # Pattern 4: Decision loop - same decision repeated 5+ times
    if not is_autonomous and len(recent_decisions) >= 5:
        decision_window = recent_decisions[-10:] if len(recent_decisions) >= 10 else recent_decisions
        decision_counts = Counter(decision_window)

        pause_count = decision_counts.get("pause", 0) + decision_counts.get("reject", 0)
        if pause_count >= 5:
            # Don't fire on stale pause histories — they'd block updates
            # forever, keeping recent_decisions frozen with the same pauses.
            try:
                window_span = min(len(decision_window), len(recent_timestamps))
                if window_span > 0:
                    newest_pause_ts = datetime.fromisoformat(recent_timestamps[-1])
                    newest_age = (now - newest_pause_ts).total_seconds()
                    if newest_age <= PAUSE_LOOP_FRESHNESS_SECONDS:
                        return True, f"Decision loop detected: {pause_count} 'pause' decisions in recent history (stuck state)"
            except (ValueError, TypeError, IndexError):
                # Can't parse timestamps — fall back to original behavior to
                # avoid masking a real loop because of bad metadata.
                return True, f"Decision loop detected: {pause_count} 'pause' decisions in recent history (stuck state)"

        proceed_count = decision_counts.get("proceed", 0) + decision_counts.get("approve", 0) + decision_counts.get("reflect", 0) + decision_counts.get("revise", 0)
        if proceed_count >= 10:
            # Only flag if these 10 proceeds happened within a short window.
            # Cron agents (e.g. Vigil, 30min cycle) naturally accumulate 10
            # proceeds over hours — that's health, not a loop.
            try:
                window_timestamps = [datetime.fromisoformat(ts) for ts in recent_timestamps[-10:]]
                window_span = (window_timestamps[-1] - window_timestamps[0]).total_seconds()
                newest_age = (now - window_timestamps[-1]).total_seconds()
                if window_span <= 300 and newest_age <= PROCEED_LOOP_FRESHNESS_SECONDS:
                    return True, f"Decision loop detected: {proceed_count} 'proceed' decisions in {window_span:.0f}s (agent may be stuck in feedback loop)"
            except (ValueError, TypeError, IndexError):
                pass  # Can't parse timestamps — skip this pattern

    # Pattern 5: Slow-stuck pattern - 3+ updates in 60s with 2+ rejects
    if not is_autonomous and not in_recovery_grace and len(recent_timestamps) >= 3:
        last_three_timestamps = recent_timestamps[-3:]
        last_three_decisions = recent_decisions[-3:]

        try:
            timestamps = [datetime.fromisoformat(ts) for ts in last_three_timestamps]
            time_span = (timestamps[-1] - timestamps[0]).total_seconds()

            if time_span <= 60.0:
                pause_count = sum(1 for d in last_three_decisions if d in ["pause", "reject"])
                if pause_count >= 2:
                    return True, f"Slow-stuck pattern: {pause_count} pause(s) in {len(last_three_timestamps)} updates within {time_span:.1f}s"
        except (ValueError, TypeError):
            pass

    # Pattern 6: Extended rapid pattern - 5+ updates in 120s with concerning decisions
    if not is_autonomous and not in_recovery_grace and len(recent_timestamps) >= 5:
        last_five_timestamps = recent_timestamps[-5:]
        last_five_decisions = recent_decisions[-5:]
        try:
            timestamps = [datetime.fromisoformat(ts) for ts in last_five_timestamps]
            time_span = (timestamps[-1] - timestamps[0]).total_seconds()

            if time_span <= 120.0:
                concerning_count = sum(1 for d in last_five_decisions if d in ["pause", "reject"])
                if concerning_count >= 3:
                    return True, f"Extended rapid pattern: {len(last_five_timestamps)} updates within {time_span:.1f}s with {concerning_count} pause/reject decision(s)"
        except (ValueError, TypeError):
            pass

    # Pattern 7: Slow proceed loop - 8+ proceed decisions in 10 updates within 5 minutes.
    # Catches agents looping without triggering pause/reject verdicts — the gap
    # that Pattern 4 (count-only, no time window) and Pattern 5 (requires
    # pause/reject) both miss.
    if not is_autonomous and not in_recovery_grace and len(recent_timestamps) >= 8:
        window_timestamps = recent_timestamps[-10:]
        window_decisions = recent_decisions[-10:]
        try:
            timestamps = [datetime.fromisoformat(ts) for ts in window_timestamps]
            time_span = (timestamps[-1] - timestamps[0]).total_seconds()
            newest_age = (now - timestamps[-1]).total_seconds()

            if time_span <= 300.0 and newest_age <= PROCEED_LOOP_FRESHNESS_SECONDS:
                proceed_count = sum(
                    1 for d in window_decisions
                    if d in ["proceed", "approve", "reflect", "revise"]
                )
                if proceed_count >= 8:
                    return True, f"Slow proceed loop: {proceed_count} proceed decisions within {time_span:.1f}s (agent may be repeating without progress)"
        except (ValueError, TypeError):
            pass

    return False, ""


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

    # The actual ODE compute — numpy work, no I/O. v0.3 RESOLUTION's
    # "load-bearing unknown" was "what's in the 7s ODE remainder?"; this
    # timer answers that. Combined with ode.auth_ms / ode.loop_detect_ms /
    # ode.monitor_setup_ms / ode.persist_ms, the residual that doesn't sum
    # is event-loop scheduling overhead — the substrate-tax candidate.
    _t_compute = time.perf_counter()
    result = await loop.run_in_executor(
        None,
        partial(monitor.process_update, agent_state, confidence=confidence, task_type=task_type)
    )
    _perf_record_ms("ode.compute_ms", (time.perf_counter() - _t_compute) * 1000.0)

    if auto_save:
        decision_action = result.get('decision', {}).get('action', 'unknown')
        now = datetime.now(timezone.utc).isoformat()

        meta = agent_metadata.get(agent_id)
        if meta is not None:
            meta.last_update = now
            meta.add_recent_update(now, decision_action)

        # Atomically increment total_updates in PostgreSQL
        try:
            from src import agent_storage
            db = agent_storage.get_db()
            _t_persist = time.perf_counter()
            new_count = await db.increment_update_count(agent_id, extra_metadata={
                "recent_update_timestamps": meta.recent_update_timestamps if meta else [now],
                "recent_decisions": meta.recent_decisions if meta else [decision_action],
            })
            _perf_record_ms("ode.persist_ms", (time.perf_counter() - _t_persist) * 1000.0)
            if meta is not None:
                meta.total_updates = new_count
        except Exception as e:
            logger.warning(f"Failed to increment update count for {agent_id[:8]}...: {e}")
            if meta is not None:
                meta.total_updates += 1

        # Enforce pause decisions (circuit breaker)
        if decision_action == 'pause' and meta is not None:
            meta.status = "paused"
            meta.paused_at = now
            decision_reason = result.get('decision', {}).get('reason', 'Circuit breaker triggered')
            meta.add_lifecycle_event("paused", decision_reason)
            logger.warning(f"⚠️  Circuit breaker triggered for agent '{agent_id}': {decision_reason}")
            result["paused"] = True
            result["circuit_breaker_triggered"] = True

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
                    payload={"reason": decision_reason},
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
