"""
Dialectic Session Persistence

Handles saving and loading dialectic sessions to/from disk.
All I/O operations are async to prevent blocking the event loop.
"""

from typing import Dict, Optional, List, Any
from pathlib import Path
import json
import os
import asyncio
from datetime import datetime, timedelta

from src.dialectic_protocol import DialecticSession, DialecticPhase
from src.db.acquire_compat import compatible_acquire
from src.logging_utils import get_logger

logger = get_logger(__name__)
# PostgreSQL backend (cross-process shared state)
from src.dialectic_db import (
    get_session_async as pg_get_session,
    get_active_sessions_async as pg_get_active_sessions,
)

project_root = Path(__file__).parent.parent.parent
SESSION_STORAGE_DIR = project_root / "data" / "dialectic_sessions"

# Active dialectic sessions (in-memory + persistent storage)
ACTIVE_SESSIONS: Dict[str, DialecticSession] = {}

# Session metadata cache for fast lookups (avoids repeated disk I/O)
# Format: {agent_id: {'in_session': bool, 'timestamp': float, 'session_ids': [str]}}
_SESSION_METADATA_CACHE: Dict[str, Dict] = {}
_CACHE_TTL = 60.0  # Cache TTL in seconds (1 minute)

# Per-session asyncio locks for serializing phase transitions. Council 2026-05-06
# NEW-1: handle_submit_synthesis loads → mutates → writes without a row-level
# lock; concurrent synthesis calls with agrees=True from both participants can
# both pass the SYNTHESIS-phase check on their own in-memory copies and both
# call finalize_resolution, with the second pg_resolve_session overwriting the
# first. In-process asyncio.Lock per session_id serializes the critical region
# without adding a new asyncpg await (per CLAUDE.md "Substrate Tax" guidance,
# new DB awaits in MCP handlers are accreted workarounds, not progress).
#
# Single-process MCP deployment makes this sufficient. If/when multi-process
# lands, this gets replaced by a postgres advisory lock or SELECT FOR UPDATE
# pattern. Track that decision via the open-question doc, not by accreting
# more in-process state.
_SESSION_LOCKS: Dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_DICT_LOCK = asyncio.Lock()


async def get_session_lock(session_id: str) -> asyncio.Lock:
    """Return the lock for `session_id`, creating it if absent.

    The dict-of-locks itself is guarded by `_SESSION_LOCKS_DICT_LOCK` to make
    lazy creation safe under concurrent first-acquires. Locks are not actively
    pruned — sessions terminate, traffic doesn't grow indefinitely. If memory
    becomes a concern, prune RESOLVED/FAILED entries on save_session.
    """
    async with _SESSION_LOCKS_DICT_LOCK:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT = os.getenv("UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

def _reconstruct_session_from_dict(session_id: str, session_data: Dict) -> Optional[DialecticSession]:
    """Reconstruct DialecticSession from a dict (from JSON file or PostgreSQL)."""
    try:
        from src.dialectic_protocol import DialecticMessage, Resolution

        # Reconstruct transcript
        transcript = []
        for msg_dict in session_data.get("transcript") or session_data.get("messages") or []:
            # DB uses message_type; JSON uses phase.
            phase = msg_dict.get("phase") or msg_dict.get("message_type") or "thesis"
            msg = DialecticMessage(
                phase=phase,
                agent_id=msg_dict.get("agent_id", ""),
                timestamp=msg_dict.get("timestamp", ""),
                root_cause=msg_dict.get("root_cause"),
                observed_metrics=msg_dict.get("observed_metrics"),
                proposed_conditions=msg_dict.get("proposed_conditions"),
                reasoning=msg_dict.get("reasoning"),
                agrees=msg_dict.get("agrees"),
                concerns=msg_dict.get("concerns"),
            )
            transcript.append(msg)

        # Reconstruct resolution if present
        resolution = None
        if session_data.get("resolution"):
            res_dict = session_data["resolution"]
            resolution = Resolution(
                action=res_dict.get("action", "resume"),
                conditions=res_dict.get("conditions", []),
                root_cause=res_dict.get("root_cause", ""),
                reasoning=res_dict.get("reasoning", ""),
                signature_a=res_dict.get("signature_a", ""),
                signature_b=res_dict.get("signature_b", ""),
                timestamp=res_dict.get("timestamp", datetime.now().isoformat()),
            )

        # Phase
        phase_str = session_data.get("phase", "thesis")
        try:
            phase = DialecticPhase(phase_str)
        except ValueError:
            phase = DialecticPhase.THESIS

        paused_agent_state = session_data.get("paused_agent_state", {}) or {}
        session_type = session_data.get("session_type", "review") or "review"
        topic = session_data.get("topic")
        max_synthesis_rounds = session_data.get("max_synthesis_rounds", 5) or 5

        session = DialecticSession(
            paused_agent_id=session_data.get("paused_agent_id", ""),
            reviewer_agent_id=session_data.get("reviewer_agent_id") or None,
            paused_agent_state=paused_agent_state,
            discovery_id=session_data.get("discovery_id"),
            dispute_type=session_data.get("dispute_type"),
            session_type=session_type,
            topic=topic,
            max_synthesis_rounds=max_synthesis_rounds,
        )

        session.session_id = session_id
        session.phase = phase
        session.transcript = transcript
        session.resolution = resolution
        session.synthesis_round = int(session_data.get("synthesis_round", 0) or 0)
        created_at_str = session_data.get("created_at")
        if created_at_str:
            # Handle both string and datetime objects from different backends
            if isinstance(created_at_str, str):
                session.created_at = datetime.fromisoformat(created_at_str)
            elif isinstance(created_at_str, datetime):
                session.created_at = created_at_str

        # Restore timeouts based on session type
        if session.session_type == "exploration":
            session._max_antithesis_wait = timedelta(hours=24)
            session._max_synthesis_wait = timedelta(hours=6)
            session._max_total_time = timedelta(hours=72)
        else:
            session._max_antithesis_wait = session.MAX_ANTITHESIS_WAIT
            session._max_synthesis_wait = session.MAX_SYNTHESIS_WAIT
            session._max_total_time = session.MAX_TOTAL_TIME

        return session
    except Exception as e:
        logger.error(f"Error reconstructing session {session_id}: {e}", exc_info=True)
        return None

async def save_session(session: DialecticSession) -> None:
    """
    Persist dialectic session to PostgreSQL (upsert) and JSON (snapshot).

    Uses pg_update_phase to sync phase/synthesis_round to PG (not INSERT).
    The JSON snapshot captures the full in-memory state for offline debugging.
    """
    # Primary: update phase + resolution in PostgreSQL
    try:
        from src.dialectic_db import update_session_phase_async as pg_update_phase
        from src.dialectic_db import resolve_session_async as pg_resolve_session
        from .beam_resolve_client import beam_resolve, beam_update_phase
        if session.phase in (DialecticPhase.RESOLVED, DialecticPhase.FAILED):
            resolution_dict = session.resolution.to_dict() if session.resolution else None
            status = session.phase.value if session.phase == DialecticPhase.RESOLVED else "failed"
            # save_session is the catch-all flush that resolves the session in the
            # orchestrated-reviewer flow (verified 2026-06-28: it, not the explicit
            # handle_submit_synthesis sites, wrote the terminal row there). Route it
            # through BEAM so the sole-writer invariant holds for that flow too;
            # fail-safe fallback to Python keeps it flag-off-safe.
            beam_done = await beam_resolve(
                session_id=session.session_id,
                paused_agent_id=getattr(session, "paused_agent_id", None),
                reviewer_agent_id=getattr(session, "reviewer_agent_id", None),
                resolution=resolution_dict or {},
                status=status,
            )
            if beam_done is None:
                await pg_resolve_session(
                    session_id=session.session_id,
                    resolution=resolution_dict,
                    status=status,
                )
        else:
            beam_ph = await beam_update_phase(session.session_id, session.phase.value)
            if beam_ph is None:
                await pg_update_phase(session.session_id, session.phase.value)
        logger.debug(f"Session {session.session_id} synced to PostgreSQL (phase={session.phase.value})")
    except Exception as e:
        logger.error(f"PostgreSQL sync failed for session {session.session_id}: {e}")

    # Secondary: JSON snapshot (for offline access / debugging)
    try:
        if not UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT:
            return

        session_file = SESSION_STORAGE_DIR / f"{session.session_id}.json"
        session_data = session.to_dict()

        loop = asyncio.get_running_loop()

        def _write_session_sync():
            SESSION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            # default=str so non-JSON-native values nested in the snapshot (e.g.
            # datetime objects in paused_agent_state) serialize instead of raising
            # "Object of type datetime is not JSON serializable" — this is an
            # offline debug snapshot, so str() rendering is fine.
            json_str = json.dumps(session_data, indent=2, default=str)
            with open(session_file, 'w', encoding='utf-8') as f:
                f.write(json_str)
                f.flush()
                os.fsync(f.fileno())

        await loop.run_in_executor(None, _write_session_sync)

    except Exception as e:
        logger.error(f"JSON snapshot save failed for session {session.session_id}: {e}", exc_info=True)

async def load_all_sessions() -> int:
    """
    Load all active dialectic sessions from PostgreSQL into ACTIVE_SESSIONS.
    Called on server startup to restore sessions after restart.

    Returns:
        Number of sessions loaded
    """
    loaded_count = 0
    try:
        sessions = await pg_get_active_sessions(limit=500)
        for s in sessions:
            session_id = s.get("session_id")
            if not session_id:
                continue
            if session_id in ACTIVE_SESSIONS:
                continue
            full = await pg_get_session(session_id)
            if not full:
                continue
            session = _reconstruct_session_from_dict(session_id, full)
            if session and session.phase not in [DialecticPhase.RESOLVED, DialecticPhase.FAILED]:
                ACTIVE_SESSIONS[session_id] = session
                loaded_count += 1
        if loaded_count > 0:
            logger.info(f"Loaded {loaded_count} active dialectic session(s) from PostgreSQL")
        return loaded_count
    except Exception as e:
        logger.error(f"Failed to load sessions from PostgreSQL: {e}", exc_info=True)
        return 0

async def load_session(session_id: str) -> Optional[DialecticSession]:
    """Load dialectic session from PostgreSQL."""
    try:
        session_data = await pg_get_session(session_id)
        if not session_data:
            return None
        # Normalize keys to match reconstruction function expectations
        if "messages" in session_data and "transcript" not in session_data:
            session_data["transcript"] = session_data["messages"]
        session_data.setdefault("session_type", session_data.get("session_type") or "review")
        session_data.setdefault("max_synthesis_rounds", session_data.get("max_synthesis_rounds") or 5)
        session_data.setdefault("synthesis_round", session_data.get("synthesis_round") or 0)
        return _reconstruct_session_from_dict(session_id, session_data)
    except Exception as e:
        logger.error(f"Failed to load session {session_id} from PostgreSQL: {e}", exc_info=True)
        return None

async def load_session_as_dict(session_id: str) -> Optional[Dict[str, Any]]:
    """Load session data formatted for API response, skipping object reconstruction.

    This is a fast path for read-only consumers (e.g. dashboard) that don't need
    DialecticSession objects — avoids the reconstruct→to_dict() round-trip.
    Returns None if DB unavailable so caller can fall back to full load_session().
    """
    try:
        from src.dialectic_db import get_dialectic_db
        db = await get_dialectic_db()
        await db._ensure_pool()
        async with compatible_acquire(db._pool) as conn:
            row = await conn.fetchrow("""
                SELECT session_id, phase, status, session_type,
                       paused_agent_id, reviewer_agent_id, topic,
                       created_at, resolution_json, reason, trigger_source
                FROM core.dialectic_sessions WHERE session_id = $1
            """, session_id)
            if not row:
                return None

            msg_rows = await conn.fetch("""
                SELECT message_type, agent_id, timestamp, reasoning,
                       root_cause, proposed_conditions, concerns, agrees
                FROM core.dialectic_messages
                WHERE session_id = $1 ORDER BY message_id
            """, session_id)

            created = row["created_at"]
            result = {
                "session_id": row["session_id"],
                "phase": row["phase"] or row["status"] or "unknown",
                "session_type": row["session_type"] or "unknown",
                "paused_agent": row["paused_agent_id"] or "unknown",
                "reviewer": row["reviewer_agent_id"],
                "topic": row["topic"] or "",
                "created": created.isoformat() if hasattr(created, 'isoformat') else str(created or ""),
                "reason": row.get("reason") or None,
                "trigger_source": row.get("trigger_source") or None,
                "message_count": len(msg_rows),
                "transcript": [],
            }

            res = row["resolution_json"]
            if res:
                result["resolution"] = res if isinstance(res, dict) else json.loads(res)

            for msg in msg_rows:
                reasoning = msg["reasoning"] or ""
                m = {
                    "phase": msg["message_type"],
                    "role": msg["message_type"],
                    "agent_id": msg["agent_id"],
                    "timestamp": msg["timestamp"].isoformat() if hasattr(msg["timestamp"], 'isoformat') else msg["timestamp"],
                    "reasoning": reasoning,
                    "content": reasoning,  # Frontend expects content or reasoning
                }
                if msg["root_cause"]:
                    m["root_cause"] = msg["root_cause"]
                if msg["proposed_conditions"]:
                    val = msg["proposed_conditions"]
                    m["proposed_conditions"] = val if isinstance(val, (list, dict)) else json.loads(val)
                if msg["concerns"]:
                    val = msg["concerns"]
                    m["concerns"] = val if isinstance(val, (list, dict)) else json.loads(val)
                if msg["agrees"] is not None:
                    m["agrees"] = bool(msg["agrees"])
                result["transcript"].append(m)

            # Derive synthesizer: first agent who submitted synthesis and is not requestor/reviewer
            paused = row["paused_agent_id"] or ""
            reviewer = row["reviewer_agent_id"] or ""
            participants = {paused, reviewer}
            for m in result["transcript"]:
                if m.get("phase") == "synthesis" and m.get("agent_id") and m["agent_id"] not in participants:
                    result["synthesizer"] = m["agent_id"]
                    break

            return result
    except Exception as e:
        logger.warning(f"Fast load failed for session {session_id}: {e}")
        return None

async def verify_data_consistency() -> Dict[str, Any]:
    """Verify dialectic data consistency. PostgreSQL is the sole backend."""
    return {"consistent": True, "stats": {}, "issues": []}

async def run_startup_consolidation() -> Dict[str, Any]:
    """No-op. PostgreSQL is the sole dialectic backend."""
    return {"exported": 0, "synced": 0, "errors": []}

async def list_all_sessions(
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    include_transcript: bool = False
) -> List[Dict[str, Any]]:
    """
    List all dialectic sessions with optional filtering.

    Backend is PostgreSQL (core.dialectic_sessions).

    Args:
        agent_id: Filter by agent (requestor or reviewer)
        status: Filter by phase (e.g., 'resolved', 'failed', 'pending')
        limit: Max sessions to return (default 50)
        include_transcript: Include full transcript (default False for performance)

    Returns:
        List of session summaries
    """
    # Primary: Query PostgreSQL
    try:
        from src.dialectic_db import get_dialectic_db
        db = await get_dialectic_db()
        await db._ensure_pool()

        async with compatible_acquire(db._pool) as conn:
            # Build query with filters (LEFT JOIN for pre-aggregated message count)
            # Exclude test/demo agent sessions (same patterns as _is_test_agent in lifecycle.py)
            test_filter = """
                AND ds.paused_agent_id NOT LIKE 'test_%'
                AND ds.paused_agent_id NOT LIKE 'demo_%'
                AND LOWER(ds.paused_agent_id) NOT LIKE '%test%'
                AND LOWER(ds.paused_agent_id) NOT LIKE '%demo%'
            """

            query = """
                SELECT
                    ds.session_id,
                    ds.phase,
                    ds.status,
                    ds.session_type,
                    ds.paused_agent_id,
                    ds.reviewer_agent_id,
                    ds.topic,
                    ds.created_at,
                    ds.resolution_json,
                    COALESCE(mc.cnt, 0) as message_count,
                    (SELECT dm.agent_id
                     FROM core.dialectic_messages dm
                     WHERE dm.session_id = ds.session_id
                       AND dm.message_type = 'synthesis'
                       AND dm.agent_id IS NOT NULL
                       AND dm.agent_id <> ds.paused_agent_id
                       AND (ds.reviewer_agent_id IS NULL OR dm.agent_id <> ds.reviewer_agent_id)
                     ORDER BY dm.message_id
                     LIMIT 1) as synthesizer
                FROM core.dialectic_sessions ds
                LEFT JOIN (
                    SELECT session_id, COUNT(*) as cnt
                    FROM core.dialectic_messages
                    GROUP BY session_id
                ) mc ON mc.session_id = ds.session_id
                WHERE 1=1
            """ + test_filter
            params = []
            param_idx = 1

            if agent_id:
                query += f" AND (ds.paused_agent_id = ${param_idx} OR ds.reviewer_agent_id = ${param_idx + 1})"
                params.extend([agent_id, agent_id])
                param_idx += 2

            if status:
                query += f" AND (LOWER(ds.phase) LIKE ${param_idx} OR LOWER(ds.status) LIKE ${param_idx})"
                params.append(f"%{status.lower()}%")
                param_idx += 1

            query += f" ORDER BY ds.created_at DESC LIMIT ${param_idx}"
            params.append(limit)

            rows = await conn.fetch(query, *params)

            result = []
            for row in rows:
                created_at = row["created_at"]
                if created_at and hasattr(created_at, 'isoformat'):
                    created_at = created_at.isoformat()

                summary = {
                    "session_id": row["session_id"],
                    "phase": row["phase"] or row["status"] or "unknown",
                    "session_type": row["session_type"] or "unknown",
                    "paused_agent": row["paused_agent_id"] or "unknown",
                    "reviewer": row["reviewer_agent_id"],
                    "synthesizer": row.get("synthesizer") if "synthesizer" in row else None,
                    "topic": row["topic"] or "",
                    "created": created_at or "",
                    "message_count": row["message_count"] or 0,
                }

                # Parse resolution if present
                resolution = row["resolution_json"]
                if resolution:
                    if isinstance(resolution, str):
                        try:
                            summary["resolution"] = json.loads(resolution)
                        except Exception:
                            pass
                    elif isinstance(resolution, dict):
                        summary["resolution"] = resolution

                # Include transcript if requested
                if include_transcript:
                    msg_rows = await conn.fetch("""
                        SELECT message_type, agent_id, timestamp, root_cause,
                               proposed_conditions, reasoning, observed_metrics,
                               concerns, agrees
                        FROM core.dialectic_messages
                        WHERE session_id = $1
                        ORDER BY message_id
                    """, row["session_id"])

                    messages = []
                    for msg_row in msg_rows:
                        reasoning = msg_row["reasoning"] or ""
                        msg = {
                            "phase": msg_row["message_type"],
                            "role": msg_row["message_type"],
                            "agent_id": msg_row["agent_id"],
                            "timestamp": msg_row["timestamp"].isoformat() if msg_row["timestamp"] and hasattr(msg_row["timestamp"], 'isoformat') else msg_row["timestamp"],
                            "reasoning": reasoning,
                            "content": reasoning,
                            "root_cause": msg_row["root_cause"],
                        }
                        if msg_row["proposed_conditions"]:
                            val = msg_row["proposed_conditions"]
                            msg["proposed_conditions"] = val if isinstance(val, (list, dict)) else json.loads(val)
                        if msg_row["concerns"]:
                            val = msg_row["concerns"]
                            msg["concerns"] = val if isinstance(val, (list, dict)) else json.loads(val)
                        if msg_row["agrees"] is not None:
                            msg["agrees"] = bool(msg_row["agrees"])
                        messages.append(msg)

                    summary["transcript"] = messages

                    # Derive synthesizer from transcript (third agent who submitted synthesis)
                    paused = row["paused_agent_id"] or ""
                    reviewer = row["reviewer_agent_id"] or ""
                    participants = {paused, reviewer}
                    for m in messages:
                        if m.get("phase") == "synthesis" and m.get("agent_id") and m["agent_id"] not in participants:
                            summary["synthesizer"] = m["agent_id"]
                            break

                result.append(summary)

        logger.debug(f"Listed {len(result)} sessions from PostgreSQL")
        return result
    except Exception as e:
        logger.warning(f"PostgreSQL list_sessions failed: {e}")
        return []
