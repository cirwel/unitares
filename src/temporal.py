"""
Temporal Narrator — contextual time awareness for agents.

Silence by default, signal when time matters.
Reads existing timestamped data and produces short, relative,
human-readable temporal context when thresholds are crossed.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from config.governance_config import GovernanceConfig
from src.logging_utils import get_logger

logger = get_logger(__name__)


def _format_duration(td: timedelta) -> str:
    """Format a timedelta as a human-readable relative string."""
    total_seconds = int(td.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}min"
    hours = minutes // 60
    remaining_min = minutes % 60
    if hours < 24:
        if remaining_min:
            return f"{hours}h {remaining_min}min"
        return f"{hours}h"
    days = hours // 24
    if days == 1:
        return "1 day"
    return f"{days} days"


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def build_temporal_context(
    agent_id: str,
    db,
    include_cross_agent: bool = False,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    Build temporal context string for an agent.

    Returns None if time is unremarkable. Returns a short plain-text
    string when one or more temporal thresholds are crossed.
    """
    try:
        now = now or datetime.now(timezone.utc)
        signals = []

        # Phase 1 (sequential): identity gates all per-identity queries.
        identity = await db.get_identity(agent_id)
        if not identity:
            return None
        identity_id = identity.identity_id

        # Phase 2 (parallel): the 4-5 per-identity reads are mutually
        # independent. Running them sequentially serialized through the
        # shared executor thread under N-way concurrent enrichment and
        # turned a ~5ms enricher into a ~500ms one (post-lock enrichment
        # profile, 2026-05-28). Concurrent fan-out collapses that to one
        # round-trip's worth of executor pressure.
        coros = [
            db.get_active_sessions_for_identity(identity_id),
            db.get_last_inactive_session(identity_id),
            db.get_latest_agent_state(identity_id),
            db.get_agent_state_history(identity_id, limit=50),
        ]
        if include_cross_agent:
            coros.append(db.get_recent_cross_agent_activity(identity_id))

        results = await asyncio.gather(*coros, return_exceptions=True)

        def _ok(idx, label):
            r = results[idx]
            if isinstance(r, BaseException):
                logger.debug(f"Temporal: {label} query failed: {r}")
                return None
            return r

        sessions = _ok(0, "session")
        last_session = _ok(1, "gap")
        latest_state = _ok(2, "idle")
        history = _ok(3, "density")
        cross_activity = _ok(4, "cross-agent") if include_cross_agent else None

        # Current session duration
        if sessions:
            session_start = _ensure_utc(sessions[0].created_at)
            session_duration = now - session_start
            if session_duration > timedelta(hours=GovernanceConfig.TEMPORAL_LONG_SESSION_HOURS):
                signals.append(f"Session: {_format_duration(session_duration)}.")

        # Gap since last session
        last_session_end = None
        if last_session and last_session.last_active:
            last_session_end = _ensure_utc(last_session.last_active)
            gap = now - last_session_end
            if gap > timedelta(hours=GovernanceConfig.TEMPORAL_GAP_HOURS):
                signals.append(f"Last session: {_format_duration(gap)} ago.")

        # Idle within session (time since last check-in)
        if latest_state and latest_state.recorded_at:
            recorded = _ensure_utc(latest_state.recorded_at)
            idle = now - recorded
            if idle > timedelta(minutes=GovernanceConfig.TEMPORAL_IDLE_MINUTES):
                signals.append(f"Idle: {_format_duration(idle)} since last check-in.")

        # High check-in density
        if history:
            window = timedelta(minutes=GovernanceConfig.TEMPORAL_HIGH_CHECKIN_WINDOW_MINUTES)
            cutoff = now - window
            recent_count = sum(
                1 for s in history
                if _ensure_utc(s.recorded_at) > cutoff
            )
            if recent_count >= GovernanceConfig.TEMPORAL_HIGH_CHECKIN_COUNT:
                signals.append(f"High activity: {recent_count} check-ins in {_format_duration(window)}.")

        # Cross-agent activity
        if include_cross_agent and cross_activity:
            entry = cross_activity[0]
            agent_time = _ensure_utc(entry.get("recorded_at", now))
            ago = _format_duration(now - agent_time)
            count = entry.get("count", 1)
            signals.append(f"Another agent active {ago} ago ({count} updates).")

        # Phase 3 (sequential): new discoveries since last session — depends
        # on last_session_end from phase 2, so cannot join the gather batch.
        if last_session_end:
            try:
                discoveries = await db.kg_query(
                    created_after=last_session_end.isoformat(),
                    limit=50,
                )
                if discoveries:
                    count = len(discoveries)
                    signals.append(
                        f"{count} knowledge graph {'entry' if count == 1 else 'entries'} "
                        f"added since last session."
                    )
            except Exception as e:
                logger.debug(f"Temporal: discovery query failed: {e}")

        if not signals:
            return None

        return " ".join(signals)

    except Exception as e:
        logger.debug(f"Temporal narrator failed: {e}")
        return None
