"""
Stuck agent detection and recovery.

Extracted from lifecycle.py for maintainability.
"""

import asyncio
from typing import Dict, Any, Sequence
from datetime import datetime, timezone

from ..decorators import mcp_tool
from ..utils import success_response, error_response
from src.logging_utils import get_logger
from config.governance_config import GovernanceConfig
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

logger = get_logger(__name__)

async def _should_add_stuck_note(agent_id: str, meta, note_cooldown_minutes: float) -> bool:
    """Check if we should add a stuck note (no existing open note + cooldown respected)."""
    try:
        from src.db import get_db
        db = get_db()
        if hasattr(db, '_pool') and db._pool:
            async with db.acquire() as conn:
                existing_note = await conn.fetchval("""
                    SELECT 1 FROM knowledge.discoveries
                    WHERE agent_id = $1
                    AND tags @> ARRAY['stuck-agent']
                    AND status = 'open'
                    LIMIT 1
                """, agent_id)
                if existing_note:
                    return False
    except Exception:
        pass

    if note_cooldown_minutes > 0 and meta:
        for event in reversed(meta.lifecycle_events or []):
            if event.get("event") != "stuck_note":
                continue
            ts = event.get("timestamp")
            if not ts:
                continue
            try:
                last_note = datetime.fromisoformat(ts)
                if last_note.tzinfo is None:
                    last_note = last_note.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_note).total_seconds() < note_cooldown_minutes * 60:
                    return False
            except Exception:
                continue

    return True

async def _trigger_dialectic_for_stuck_agent(
    agent_id: str,
    paused_agent_state: dict,
    note: str,
) -> dict | None:
    """
    Create a dialectic session for a stuck agent.

    Returns recovery info dict on success, None on failure, if the agent
    already has an active session, or if no peer reviewer is available.
    """
    from src.dialectic_protocol import DialecticSession
    from src.mcp_handlers.dialectic.reviewer import select_reviewer
    from ..dialectic.session import save_session
    from src.dialectic_db import is_agent_in_active_session_async

    has_session = await is_agent_in_active_session_async(agent_id)
    if has_session:
        logger.debug(f"[STUCK_AGENT_RECOVERY] Agent {agent_id[:8]}... already has active dialectic session")
        return None

    paused_tags = paused_agent_state.get("tags") or []
    reviewer_id = await select_reviewer(
        paused_agent_id=agent_id,
        metadata=mcp_server.agent_metadata,
        paused_agent_state=paused_agent_state,
        paused_agent_tags=paused_tags,
    )

    # No self-review. A session whose reviewer is the paused agent can never
    # resolve via peer review, and the stuck detector then re-fires and creates
    # another doomed session and another. If no peer is eligible, skip this
    # cycle — auto_initiate_dialectic_recovery (in agent_loop_detection) owns
    # the LLM-assisted fallback for single-agent deployments.
    if reviewer_id is None:
        logger.info(
            f"[STUCK_AGENT_RECOVERY] No peer reviewer available for {agent_id[:8]}... "
            f"— skipping dialectic this cycle"
        )
        return None

    session = DialecticSession(
        paused_agent_id=agent_id,
        reviewer_agent_id=reviewer_id,
        paused_agent_state=paused_agent_state,
    )
    await save_session(session)

    logger.info(
        f"[STUCK_AGENT_RECOVERY] Triggered dialectic for {agent_id[:8]}... "
        f"(reviewer: {reviewer_id[:8]}..., session: {session.session_id[:8]}...)"
    )
    return {
        "agent_id": agent_id,
        "action": "dialectic_triggered",
        "reason": paused_agent_state.get("stuck_reason", "unknown"),
        "reviewer_id": reviewer_id,
        "session_id": session.session_id,
        "note": note,
    }

# Cadence-silence (soft) detection tunables. An agent that was checking in at a
# regular ACTIVE cadence and then went silent for far longer than that cadence is
# possibly hung/abandoned mid-work. Gated on prior-active-cadence so it does NOT
# fire on orphans or naturally-slow/idle agents — which is exactly why the old
# blunt "no updates > 30 min" rule was removed. Soft signal: surfaced (audit
# trail), never auto-recovered. Origin: dogfood 2026-06-04 (agent finished a
# task, was told "proceed on your own accord", wandered off-task + hung ~3h, and
# nothing flagged it).
CADENCE_MIN_UPDATES = 5                  # had a real session (>= this many check-ins)
CADENCE_ACTIVE_MAX_GAP_MINUTES = 30.0   # avg gap <= this => an active cadence, not a slow cron
CADENCE_SILENCE_FLOOR_MINUTES = 30.0    # never flag before this much silence
CADENCE_SILENCE_MULTIPLIER = 6.0        # silent for >= this many times its own cadence
# Upper bound: only flag agents that went quiet RECENTLY. Beyond this, an idle
# agent is abandoned / archive-pending, not a fresh hang worth a live signal —
# this is what keeps the audit trail from filling with finished-but-unarchived
# ephemeral agents (which cadence cannot distinguish from a genuine hang).
CADENCE_SILENCE_STALE_CAP_MINUTES = 1440.0   # 24h
# The margin timeout rules get the same upper bound. An abandoned identity's
# margin is computed from its FROZEN state — no new check-ins ever arrive to
# move it off critical/tight — so without a cap the agent stays "stuck" forever,
# pinning the stuck KPI and re-firing the audit trail on every sweep (observed
# 2026-06-12: ~24 day-old unnamed identities held the count indefinitely).
MARGIN_STUCK_STALE_CAP_MINUTES = 1440.0      # 24h, matches the cadence cap
# NOTE on the cadence proxy: avg_gap is computed over the agent's WHOLE life
# (created_at -> last_update), which is a coarse proxy for "recent rhythm." A
# recent-window cadence (last N gaps) would be more precise but needs the
# per-check-in timestamp series, which this path doesn't have. Coarse-but-safe:
# it errs toward flagging, and the signal is soft. Also note the MULTIPLIER is
# floor-dominated for fast agents (avg_gap < 5min => 6*gap < 30 => floor wins).

# Lineage succession: a parent process is "succeeded" — not stuck — when a child
# that declared it via parent_agent_id at onboard (the predecessor's UUID; see
# the Minimal Agent Workflow in CLAUDE.md) is still active and has checked in
# within this window. The process rotated, lineage continuous (KG 2026-05-06,
# empirically the Discord-Dispatch turn chains). Matches CADENCE_ACTIVE_MAX_GAP:
# the child counts as live only while it still has an active cadence.
LINEAGE_SUCCESSION_FRESH_WINDOW_MINUTES = 30.0


def _agent_age_minutes(meta, current_time) -> float | None:
    """Minutes since meta.last_update (falling back to created_at). None if unparseable."""
    try:
        last_update_str = meta.last_update or meta.created_at
        if isinstance(last_update_str, str):
            dt = datetime.fromisoformat(
                last_update_str.replace('Z', '+00:00') if 'Z' in last_update_str else last_update_str
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = last_update_str
        return (current_time - dt).total_seconds() / 60
    except (ValueError, TypeError, AttributeError):
        return None


def _live_lineage_parent_ids(
    current_time, window_minutes: float = LINEAGE_SUCCESSION_FRESH_WINDOW_MINUTES
) -> set:
    """Identities that have an active lineage child checking in recently.

    One O(n) pass over the agent pool. A child declares its predecessor at
    onboard via parent_agent_id; when that child is still active and has updated
    within window_minutes, the predecessor's silence is explained by succession,
    not a hang. Returns the set of parent_agent_id values that are currently
    succeeded so callers can both (a) suppress stuck flags on the parent and
    (b) archive it as lineage_succession.

    Trusting parent_agent_id here is consistent with the current security
    posture: the ghost-proliferation hijack surface was closed by PRs #78/#81/#83
    (plugin #14); this is the leftover ergonomic surface that fix left open.
    """
    live: set = set()
    for meta in mcp_server.agent_metadata.values():
        parent = getattr(meta, "parent_agent_id", None)
        if not parent:
            continue
        if getattr(meta, "status", None) != "active":
            continue
        # A child pointing at its own id is not a successor — guard self-loops.
        if parent in (getattr(meta, "agent_uuid", None), getattr(meta, "agent_id", None)):
            continue
        # Subagent/compaction children do NOT supersede their parent: their
        # parent is concurrent-by-design (a dispatcher mid-dispatch, a
        # compaction fork), not an exited predecessor. Treating their presence
        # as succession archives the LIVE parent out from under itself — which
        # is exactly how a council of dispatched subagents archived the main
        # session 2026-06-16 (parent had ~12.9k updates, last check-in seconds
        # earlier). These spawn_reasons declare ancestry with a live parent by
        # definition; only an exited predecessor is a genuine succession.
        if (getattr(meta, "spawn_reason", None) or "") in ("subagent", "compaction"):
            continue
        age = _agent_age_minutes(meta, current_time)
        if age is not None and age <= window_minutes:
            live.add(parent)
    return live


def _live_child_uuids(
    current_time, window_minutes: float = LINEAGE_SUCCESSION_FRESH_WINDOW_MINUTES
) -> set:
    """UUIDs of the active, recent SUCCESSION children feeding _live_lineage_parent_ids.

    Same selection as _live_lineage_parent_ids (active + recent + non-subagent/
    compaction spawn_reason), but returns the CHILD UUIDs rather than their
    declared parents — the seed set for transitive succession-ancestor lookups.
    """
    children: set = set()
    for meta in mcp_server.agent_metadata.values():
        parent = getattr(meta, "parent_agent_id", None)
        if not parent:
            continue
        if getattr(meta, "status", None) != "active":
            continue
        if (getattr(meta, "spawn_reason", None) or "") in ("subagent", "compaction"):
            continue
        age = _agent_age_minutes(meta, current_time)
        if age is not None and age <= window_minutes:
            uid = getattr(meta, "agent_uuid", None) or getattr(meta, "agent_id", None)
            if uid:
                children.add(uid)
    return children


def _is_superseded_by_lineage(agent_id, meta, live_parent_ids: set) -> bool:
    """True if a live lineage child has taken over from this agent."""
    if not live_parent_ids:
        return False
    return (
        agent_id in live_parent_ids
        or getattr(meta, "agent_uuid", None) in live_parent_ids
    )


def stuck_change_token(stuck_agents: list) -> str:
    """Emit-on-change dedup token for the stuck_detected audit write.

    Keyed on the detected set itself — sorted (agent_id, reason) pairs — not on
    counts or human-readable details, so the token advances exactly when an
    agent enters or leaves the stuck set or its reason changes. Repeated sweeps
    over an unchanged set yield an identical token and write nothing. Mirrors
    anomaly_change_token (#638).
    """
    import hashlib
    basis = "|".join(
        sorted(f"{s.get('agent_id')}:{s.get('reason')}" for s in stuck_agents)
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


# Last-written stuck_detected token (process-lifetime; a restart re-emits one
# row for a persisting set, which is acceptable and self-documents the restart).
_last_stuck_audit_token: str | None = None


def _detect_stuck_agents(
    max_age_minutes: float = 30.0,  # Unused, kept for API compatibility
    critical_margin_timeout_minutes: float = 5.0,
    tight_margin_timeout_minutes: float = 15.0,
    include_pattern_detection: bool = True,
    min_updates: int = 3,
) -> list:
    """
    Detect stuck agents using proprioceptive margin + patterns.

    IMPORTANT: Inactivity alone does NOT mean stuck!
    An agent is stuck when it's in a problematic state AND not recovering.

    Detection rules:
    1. Critical margin + no updates > 5 min → stuck (can't proceed safely)
    2. Tight margin + no updates > 15 min → potentially stuck (struggling)
    3. Cognitive loop pattern → stuck (repeating unproductive behavior)
    4. Time box exceeded → stuck (taking too long on a task)
    5. Cadence-silence (SOFT) → reason "cadence_silence". An agent that HAD an
       active check-in cadence and then went silent for >> that cadence. The one
       inactivity-based signal — deliberately gated on prior-active-cadence
       (>= CADENCE_MIN_UPDATES at avg gap <= CADENCE_ACTIVE_MAX_GAP_MINUTES) so it
       does NOT regress to flagging idle agents. soft=True → surfaced via the
       audit trail, never auto-recovered.

    NOT stuck:
    - Idle/inactive with NO prior active cadence (orphans, slow cron agents) —
      raw inactivity alone is still not stuck.
    - Low update count (that's orphan/test agent, not stuck)

    Args:
        critical_margin_timeout_minutes: Timeout for critical margin state
        tight_margin_timeout_minutes: Timeout for tight margin state
        min_updates: Minimum updates before an agent can be considered stuck.
            Agents with fewer updates are likely orphans/one-shots, not stuck.

    Returns:
        List of stuck agents with detection reasons
    """
    stuck_agents = []
    current_time = datetime.now(timezone.utc)
    # Parents whose declared-lineage child is actively checking in. Computed once
    # per sweep so the per-agent supersession test below is O(1).
    live_parent_ids = _live_lineage_parent_ids(current_time)

    for agent_id, meta in mcp_server.agent_metadata.items():
        # Skip if already archived/deleted
        if meta.status in ["archived", "deleted"]:
            continue

        # Skip if not active
        if meta.status != "active":
            continue

        # Skip autonomous/embodied agents (they manage their own lifecycle)
        agent_tags = getattr(meta, "tags", []) or []
        skip_tags = {"autonomous", "embodied", "anima"}
        if skip_tags & set(t.lower() for t in agent_tags):
            continue

        # Skip agents with too few updates (likely orphan/test agents)
        total_updates = getattr(meta, "total_updates", 0) or 0
        if total_updates < min_updates:
            continue

        # Lineage succession: a parent whose declared-lineage child is actively
        # checking in is not stuck — the process rotated, lineage continuous
        # (KG 2026-05-06). Suppress EVERY stuck reason for it (cadence-silence,
        # margin, pattern); the auto_recover sweep archives it as
        # lineage_succession via _archive_superseded_parents.
        if _is_superseded_by_lineage(agent_id, meta, live_parent_ids):
            continue

        # Calculate age since last update
        try:
            last_update_str = meta.last_update or meta.created_at
            if isinstance(last_update_str, str):
                last_update_dt = datetime.fromisoformat(
                    last_update_str.replace('Z', '+00:00') if 'Z' in last_update_str else last_update_str
                )
                if last_update_dt.tzinfo is None:
                    last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
            else:
                last_update_dt = last_update_str

            age_delta = current_time - last_update_dt
            age_minutes = age_delta.total_seconds() / 60
        except (ValueError, TypeError, AttributeError) as e:
            logger.debug(f"Could not parse last_update for {agent_id}: {e}")
            continue

        # Detection rule 5 (SOFT): cadence-silence. An agent that HAD an active
        # check-in cadence and then went silent for >> that cadence. Independent
        # of margin/monitor (a silent agent's last margin reads healthy, which is
        # exactly the blind spot). Gated on prior-active-cadence so orphans and
        # slow/idle agents don't trip it. Surfaced via the audit trail; soft=True
        # routes it past auto-recovery (the agent is gone, not in a recoverable
        # in-process state, and it may simply have finished and idled).
        try:
            created_dt = last_update_dt
            cstr = meta.created_at
            if isinstance(cstr, str):
                created_dt = datetime.fromisoformat(
                    cstr.replace('Z', '+00:00') if 'Z' in cstr else cstr
                )
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
            elif cstr is not None:
                created_dt = cstr

            span_minutes = (last_update_dt - created_dt).total_seconds() / 60
            if total_updates >= CADENCE_MIN_UPDATES and span_minutes > 0:
                avg_gap_minutes = span_minutes / (total_updates - 1)
                if 0 < avg_gap_minutes <= CADENCE_ACTIVE_MAX_GAP_MINUTES:
                    silence_threshold = max(
                        CADENCE_SILENCE_FLOOR_MINUTES,
                        CADENCE_SILENCE_MULTIPLIER * avg_gap_minutes,
                    )
                    # Window: recently went quiet, but not so long ago it's just
                    # abandoned/archive-pending (which would be noise).
                    if silence_threshold < age_minutes <= CADENCE_SILENCE_STALE_CAP_MINUTES:
                        stuck_agents.append({
                            "agent_id": agent_id,
                            "reason": "cadence_silence",
                            "age_minutes": round(age_minutes, 1),
                            "soft": True,
                            "details": (
                                f"Active cadence ~{avg_gap_minutes:.1f} min over {total_updates} "
                                f"updates, then silent {age_minutes:.0f} min "
                                f"(> {silence_threshold:.0f} min threshold). Possibly hung/abandoned "
                                f"mid-work — verify. Soft signal; not auto-recovered."
                            ),
                        })
                        # A confirmed-silent agent: surface the soft signal and move
                        # on — don't also margin-evaluate its STALE state below (which
                        # would double-list it and is meaningless for a gone agent).
                        continue
        except (ValueError, TypeError, AttributeError) as e:
            logger.debug(f"cadence-silence check failed for {agent_id}: {e}")

        # Stale cap (mirrors CADENCE_SILENCE_STALE_CAP_MINUTES): beyond this,
        # an idle agent is abandoned/archive-pending, not a live hang. Its
        # margin is frozen — no new check-ins will ever move it — so evaluating
        # it below would flag it "stuck" on every sweep until the end of time.
        # Skipping here also avoids hydrating monitors for dead agents.
        if age_minutes > MARGIN_STUCK_STALE_CAP_MINUTES:
            continue

        # Get current metrics to compute margin
        try:
            monitor = mcp_server.monitors.get(agent_id)
            if monitor is None:
                # Require persisted state — inactivity alone isn't stuck
                if mcp_server.load_monitor_state(agent_id) is None:
                    continue
                # Use the cached factory so the monitor lands in mcp_server.monitors
                # and subsequent cycles hit the cache. Previously we constructed a
                # transient UNITARESMonitor here, which bypassed the cache and
                # leaked ~160 inits/min across a large agent pool.
                monitor = mcp_server.get_or_create_monitor(agent_id)

            # Pattern detection: check for cognitive loops and unproductive behavior
            if include_pattern_detection:
                try:
                    from src.pattern_tracker import get_pattern_tracker
                    tracker = get_pattern_tracker()
                    patterns = tracker.get_patterns(agent_id)

                    # Add pattern-based stuck detection
                    for pattern in patterns.get("patterns", []):
                        if pattern["type"] == "loop":
                            stuck_agents.append({
                                "agent_id": agent_id,
                                "reason": "cognitive_loop",
                                "age_minutes": None,  # Pattern-based, not time-based
                                "pattern": pattern,
                                "details": pattern["message"]
                            })
                        elif pattern["type"] == "time_box":
                            stuck_agents.append({
                                "agent_id": agent_id,
                                "reason": "time_box_exceeded",
                                "age_minutes": pattern["total_minutes"],
                                "pattern": pattern,
                                "details": pattern["message"]
                            })
                        elif pattern["type"] == "untested_hypothesis":
                            # Don't mark as stuck, but include in details for context
                            # (This is more of a warning than stuck state)
                            pass
                except Exception as e:
                    logger.debug(f"Pattern detection failed for {agent_id}: {e}")

            if monitor:
                metrics = monitor.get_metrics()
                risk_score = float(metrics.get("mean_risk") or 0.5)
                coherence = float(monitor.state.coherence)
                void_active = bool(monitor.state.void_active)
                void_value = float(monitor.state.V)

                # Compute margin
                margin_info = GovernanceConfig.compute_proprioceptive_margin(
                    risk_score=risk_score,
                    coherence=coherence,
                    void_active=void_active,
                    void_value=void_value,
                    coherence_history=monitor.state.coherence_history,
                )
                margin = margin_info['margin']

                # Detection rule 1: Critical margin + timeout
                if margin == "critical" and age_minutes > critical_margin_timeout_minutes:
                    stuck_agents.append({
                        "agent_id": agent_id,
                        "reason": "critical_margin_timeout",
                        "age_minutes": round(age_minutes, 1),
                        "margin": margin,
                        "nearest_edge": margin_info.get('nearest_edge'),
                        "details": "Critical margin ({}) for {:.1f} minutes".format(
                            margin_info.get('nearest_edge', 'unknown'), age_minutes
                        )
                    })
                    continue

                # Detection rule 2: Tight margin + inactivity + unhealthy state
                # Tight margin alone is NOT stuck — coherence ~0.49 is the steady state
                # for ALL agents. Only flag if the agent also has genuinely degraded
                # metrics (high risk, low coherence, or high entropy).
                # Skip low-update agents (<50) - their EISV dynamics are noise, not signal
                _is_actually_degraded = (
                    risk_score > 0.45  # Approaching pause threshold
                    or coherence < 0.42  # Near critical coherence
                    or float(monitor.state.S) > 0.5  # High entropy
                )
                if margin == "tight" and age_minutes > max(tight_margin_timeout_minutes, 60.0) and total_updates >= 50 and _is_actually_degraded:
                    stuck_agents.append({
                        "agent_id": agent_id,
                        "reason": "tight_margin_timeout",
                        "age_minutes": round(age_minutes, 1),
                        "margin": margin,
                        "nearest_edge": margin_info.get('nearest_edge'),
                        "details": "Tight margin ({}) for {:.1f} minutes".format(
                            margin_info.get('nearest_edge', 'unknown'), age_minutes
                        )
                    })
                    continue

        except Exception as e:
            logger.debug(f"Error computing margin for {agent_id}: {e}")
            # Don't fall back to timeout-only detection - inactivity ≠ stuck
            # An agent can be legitimately idle without being stuck

    return stuck_agents

def _parse_last_update(meta) -> float | None:
    """Return age in minutes since last update, or None on failure."""
    try:
        last_update_str = meta.last_update or meta.created_at
        if isinstance(last_update_str, str):
            last_update_dt = datetime.fromisoformat(
                last_update_str.replace('Z', '+00:00') if 'Z' in last_update_str else last_update_str
            )
            if last_update_dt.tzinfo is None:
                last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
        else:
            last_update_dt = last_update_str
        return (datetime.now(timezone.utc) - last_update_dt).total_seconds() / 60
    except (ValueError, TypeError, AttributeError):
        return None


async def _log_stuck_intervention(agent_id: str, stuck_reason: str) -> None:
    """Best-effort KG note for a stuck agent (deduped)."""
    try:
        from src.knowledge_graph import get_knowledge_graph
        kg = await get_knowledge_graph()
        existing = await kg.query(status="open", agent_id=agent_id, limit=50)
        has_open_stuck = any("stuck-agent" in (d.tags or []) for d in existing)
        if not has_open_stuck:
            from ..knowledge.handlers import handle_leave_note
            await handle_leave_note({
                "summary": f"Auto-recovered stuck agent {agent_id[:8]}... (Reason: {stuck_reason}, Action: auto-resume)",
                "tags": ["auto-recovery", "stuck-agent"]
            })
    except Exception as e:
        logger.debug(f"Could not log auto-recovery: {e}")


async def _handle_safe_active_agent(agent_id, meta, stuck, risk_score, coherence, void_active, note_cooldown_minutes) -> list:
    """Handle recovery for an active agent with safe metrics."""
    results = []
    age_minutes = _parse_last_update(meta)

    if age_minutes is not None and age_minutes > 60.0:
        # Stuck > 1 hour — trigger dialectic
        try:
            result = await _trigger_dialectic_for_stuck_agent(
                agent_id,
                paused_agent_state={
                    "risk_score": risk_score, "coherence": coherence,
                    "void_active": void_active, "stuck_reason": stuck["reason"],
                    "safe_but_stuck": True, "age_minutes": age_minutes,
                },
                note=f"Safe but stuck {age_minutes:.1f} min - triggered dialectic",
            )
            if result:
                results.append(result)
        except Exception as e:
            logger.warning(f"[STUCK_AGENT_RECOVERY] Could not trigger dialectic for safe stuck {agent_id[:8]}...: {e}", exc_info=True)
        return results

    # Not stuck long enough for dialectic — track via note
    effective_age = age_minutes if age_minutes is not None else 0
    should_note = await _should_add_stuck_note(agent_id, meta, note_cooldown_minutes)
    if should_note:
        meta.add_lifecycle_event("stuck_detected", f"{stuck['reason']} ({effective_age:.1f} min)")
        results.append({
            "agent_id": agent_id, "action": "stuck_tracked",
            "reason": stuck["reason"],
            "note": f"Stuck {effective_age:.1f} min - tracked via detect_stuck_agents (no KG write)"
        })
    else:
        results.append({
            "agent_id": agent_id, "action": "note_skipped_recent",
            "reason": stuck["reason"],
            "note": f"Skipped note - recent note within {note_cooldown_minutes:.0f} min"
        })

    await _log_stuck_intervention(agent_id, stuck["reason"])
    return results


async def _archive_superseded_parents(current_time) -> list:
    """Archive active parents whose declared-lineage child has taken over.

    Mirrors the suppression in _detect_stuck_agents: a parent with a live
    lineage successor is retired with a lineage_succession lifecycle event
    rather than left to re-trip detection until the stale cap. Mutation path —
    only called from the auto_recover sweep, never the read-only dashboard
    refresh. Best-effort: _archive_one_agent persists-first and returns False
    on DB failure, so a failed write simply leaves the parent for next sweep.
    """
    from .helpers import _archive_one_agent

    results = []
    live_parent_ids = _live_lineage_parent_ids(current_time)
    if not live_parent_ids:
        return results

    # SHADOW (observation-only, no mutation): measure what transitive
    # succession-reachability WOULD add beyond this single-hop set, and log the
    # AGE-vs-CTE topology agreement (evidence for the AGE-canonical step-1
    # decision). Stays measurement-only until the liveness gate sources ephemeral
    # agents from the lease plane — see lineage_reachability ACTIVATION
    # PREREQUISITE. Never drives the archival loop below.
    try:
        from .lineage_reachability import measure_transitive_expansion
        await measure_transitive_expansion(
            _live_child_uuids(current_time), live_parent_ids
        )
    except Exception as e:  # pragma: no cover - observation must never block archival
        logger.debug("transitive lineage shadow measure failed: %s", type(e).__name__)

    for agent_id, meta in list(mcp_server.agent_metadata.items()):
        if meta.status != "active":
            continue
        if not _is_superseded_by_lineage(agent_id, meta, live_parent_ids):
            continue
        # Initializing agents (never checked in) are ghosts, not retired
        # predecessors — declaring parent_agent_id attests ancestry, not that
        # the parent has exited. A fresh session that onboarded and is working
        # but hasn't checked in yet (total_updates == 0) must never be archived
        # out from under itself the moment a concurrent same-workspace sibling
        # declares it parent. This mirrors classify_for_archival's ghost
        # protection, which this lineage-succession path was bypassing.
        # (Incident 2026-06-14: agent 1b4172bb archived 22 min into active work,
        # 0 check-ins, because sibling ad111882 onboarded declaring it parent.)
        if int(getattr(meta, "total_updates", 0) or 0) == 0:
            continue
        # Self-managed agents own their lifecycle — same exclusion as detection.
        agent_tags = getattr(meta, "tags", []) or []
        if {"autonomous", "embodied", "anima"} & set(t.lower() for t in agent_tags):
            continue
        # Final, conceptually-correct guard: a parent with a LIVE process
        # binding is a running process, not a rotated-out predecessor. A child
        # declaring parent_agent_id attests ancestry, not that the parent
        # exited — so a still-running parent (e.g. mid long tool call or
        # subagent dispatch, silent past the 30-min window) must not be
        # archived just because a concurrent sibling onboarded behind it. The
        # updates==0 guard above is a cheap no-DB fallback for the not-yet-bound
        # case; this is the real liveness signal. Best-effort: get_live_bindings
        # returns [] on DB error, falling back to prior behavior rather than
        # blocking cleanup.
        #
        # Two liveness sources, either of which protects: (1) a process binding
        # (today only written when a client sends process_fingerprint — rare, so
        # this is structurally blind to most ephemeral agents); (2) a live
        # agent:/<uuid> lease-plane presence lease, the signal the check-in-path
        # producer keeps fresh for exactly the ephemeral agents binding-liveness
        # misses. Both fail-open (False on error) so a liveness lookup failure
        # never blocks cleanup; either being live is sufficient to preserve.
        from src.mcp_handlers.identity.process_binding import (
            get_live_bindings,
            has_live_agent_lease,
        )
        if await get_live_bindings(agent_id):
            continue
        if await has_live_agent_lease(getattr(meta, "agent_uuid", None)):
            continue
        ok = await _archive_one_agent(
            agent_id, meta, "lineage_succession", monitors=mcp_server.monitors
        )
        if ok:
            results.append({
                "agent_id": agent_id,
                "action": "archived_lineage_succession",
                "reason": "lineage_succession",
            })
    return results


async def _try_recover_agent(stuck: dict, note_cooldown_minutes: float) -> list:
    """Attempt recovery for a single stuck agent. Returns list of recovery results."""
    agent_id = stuck["agent_id"]
    results = []

    try:
        # Check if agent is responsive
        try:
            monitor = mcp_server.get_or_create_monitor(agent_id)
            from src.agent_monitor_state import ensure_hydrated
            await ensure_hydrated(monitor, agent_id)
            metrics = monitor.get_metrics()
            coherence = float(monitor.state.coherence)
            risk_score = float(metrics.get("mean_risk") or 0.5)
            void_active = bool(monitor.state.void_active)
            responsive = True
        except Exception as e:
            logger.warning(f"[STUCK_AGENT_RECOVERY] Agent {agent_id[:8]}... is unresponsive: {e}")
            responsive = False
            coherence, risk_score, void_active = 0.5, 0.5, False

        # Unresponsive — trigger dialectic immediately
        if not responsive:
            try:
                result = await _trigger_dialectic_for_stuck_agent(
                    agent_id,
                    paused_agent_state={
                        "risk_score": risk_score, "coherence": coherence,
                        "void_active": void_active, "stuck_reason": stuck["reason"],
                        "unresponsive": True, "age_minutes": stuck.get("age_minutes", 0),
                    },
                    note="Unresponsive - triggered dialectic immediately",
                )
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning(f"[STUCK_AGENT_RECOVERY] Could not trigger dialectic for unresponsive {agent_id[:8]}...: {e}", exc_info=True)
            return results

        is_safe = coherence > 0.40 and risk_score < 0.60 and not void_active

        if is_safe:
            meta = mcp_server.agent_metadata.get(agent_id)
            if not meta:
                return results

            if meta.status in ["paused", "waiting_input"]:
                meta.status = "active"
                meta.paused_at = None
                # Clear loop detector state to prevent immediate re-pause
                from .self_recovery import clear_loop_detector_state
                clear_loop_detector_state(meta)
                results.append({"agent_id": agent_id, "action": "auto_resumed", "reason": stuck["reason"]})
            elif meta.status == "active":
                results.extend(await _handle_safe_active_agent(
                    agent_id, meta, stuck, risk_score, coherence, void_active, note_cooldown_minutes
                ))
        else:
            # Unsafe — trigger dialectic
            try:
                result = await _trigger_dialectic_for_stuck_agent(
                    agent_id,
                    paused_agent_state={
                        "risk_score": risk_score, "coherence": coherence,
                        "void_active": void_active, "stuck_reason": stuck["reason"],
                    },
                    note=f"Unsafe stuck - triggered dialectic (risk={risk_score:.2f}, coherence={coherence:.2f})",
                )
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning(f"[STUCK_AGENT_RECOVERY] Could not trigger dialectic for {agent_id[:8]}...: {e}", exc_info=True)

    except Exception as e:
        logger.debug(f"Could not auto-recover {agent_id}: {e}")

    return results


@mcp_tool("detect_stuck_agents", timeout=15.0, requires_identity="pre_onboard")
async def handle_detect_stuck_agents(arguments: Dict[str, Any]) -> Sequence:
    """Detect stuck agents using proprioceptive margin + patterns.

    IMPORTANT: Inactivity alone does NOT mean stuck!

    Detection rules:
    1. Critical margin + no updates > 5 min → stuck
    2. Tight margin + no updates > 15 min → potentially stuck
    3. Cognitive loop / time box exceeded → stuck

    NOT stuck: Simply being idle (that's normal behavior).

    Args:
        critical_margin_timeout_minutes: Timeout for critical margin (default: 5)
        tight_margin_timeout_minutes: Timeout for tight margin (default: 15)
        auto_recover: If True, automatically recover safe stuck agents (default: False)

    Returns:
        List of stuck agents with detection reasons and recovery status
    """
    try:
        # Reload metadata to ensure we have latest state (async for PostgreSQL)
        await mcp_server.load_metadata_async()

        max_age_minutes = float(arguments.get("max_age_minutes", 30.0))
        critical_timeout = float(arguments.get("critical_margin_timeout_minutes", 5.0))
        tight_timeout = float(arguments.get("tight_margin_timeout_minutes", 15.0))
        min_updates = int(arguments.get("min_updates", 1))
        auto_recover = arguments.get("auto_recover", False)
        note_cooldown_minutes = float(arguments.get("note_cooldown_minutes", 120.0))

        # Detect stuck agents (run in executor since _detect_stuck_agents is sync)
        loop = asyncio.get_running_loop()
        include_patterns = arguments.get("include_pattern_detection", True)
        stuck_agents = await loop.run_in_executor(
            None,
            _detect_stuck_agents,
            max_age_minutes,
            critical_timeout,
            tight_timeout,
            include_patterns,
            min_updates
        )

        # Auto-recover if requested
        recovered = []
        if auto_recover:
            # Retire superseded parents first (lineage_succession). These were
            # already suppressed from stuck_agents by detection; archiving them
            # stops the next sweep from re-evaluating a process that rotated.
            superseded = await _archive_superseded_parents(datetime.now(timezone.utc))
            if superseded:
                recovered.extend(superseded)
            for stuck in stuck_agents:
                # Soft signals (e.g. cadence_silence) are surfaced via the audit
                # trail only — never auto-recovered. The agent is silent/gone, not
                # in a recoverable in-process state, and it may simply have
                # finished and idled; recovering it would be a phantom action.
                if stuck.get("soft"):
                    continue
                result = await _try_recover_agent(stuck, note_cooldown_minutes)
                if result:
                    recovered.extend(result if isinstance(result, list) else [result])

        # Log stuck agents to audit trail — emit-on-change only. This handler
        # runs on every dashboard refresh PLUS a 5-min background sweep;
        # unconditional writes filled the audit trail with hundreds of identical
        # rows per hour (observed ~250/h on 2026-06-12). Same pattern as
        # anomaly_change_token (#638). A cleared set resets the token, so a
        # genuine re-occurrence of the same set is logged again.
        # Concurrency note: the read-compare-assign-write below is await-free,
        # so under asyncio it runs atomically between suspension points —
        # concurrent handler invocations serialize around it. No lock needed
        # unless an await is ever introduced inside this block.
        global _last_stuck_audit_token
        if stuck_agents:
            token = stuck_change_token(stuck_agents)
            if token != _last_stuck_audit_token:
                _last_stuck_audit_token = token
                from src.audit_log import audit_logger, AuditEntry
                audit_logger._write_entry(AuditEntry(
                    timestamp=datetime.now().isoformat(),
                    agent_id="system",
                    event_type="stuck_detected",
                    confidence=1.0,
                    details={
                        "count": len(stuck_agents),
                        "agents": [{"agent_id": s.get("agent_id"), "reason": s.get("reason"), "agent_name": s.get("agent_name", "")} for s in stuck_agents[:10]],
                    }
                ))
        else:
            _last_stuck_audit_token = None

        return success_response({
            "stuck_agents": stuck_agents,
            "recovered": recovered if auto_recover else [],
            "summary": {
                "total_stuck": len(stuck_agents),
                "min_updates": min_updates,
                "note_cooldown_minutes": note_cooldown_minutes,
                "total_recovered": len(recovered) if auto_recover else 0,
                "by_reason": {
                    reason: sum(1 for s in stuck_agents if s["reason"] == reason)
                    for reason in ["critical_margin_timeout", "tight_margin_timeout", "cognitive_loop", "time_box_exceeded", "cadence_silence"]
                }
            }
        })

    except Exception as e:
        logger.error(f"Error detecting stuck agents: {e}", exc_info=True)
        return [error_response(f"Error detecting stuck agents: {str(e)}")]
