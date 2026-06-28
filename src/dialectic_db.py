"""
PostgreSQL Backend for Dialectic Sessions

Provides storage for dialectic sessions with PostgreSQL.
"""

import json
import asyncio
from typing import Dict, List, Optional, Any


from src.logging_utils import get_logger
from src.dialectic_protocol import DialecticPhase
from src.db.acquire_compat import compatible_acquire

logger = get_logger(__name__)


# Saga-slot namespace for dialectic phase transitions (BEAM-port prerequisite).
# A transaction-scoped PG advisory lock keyed on this int4 classid + the session
# id is the *cross-process* serialization point for phase/status mutations on
# core.dialectic_sessions. The in-process asyncio.Lock in
# mcp_handlers/dialectic/session.py only serializes one Python process; this lock
# is honored by every writer — a second Python worker, the REST path, and a
# future BEAM GenServer alike — so dual-writer becomes SAFE rather than
# split-brain. That is the correctness prerequisite the dialectic-on-BEAM port
# needs, and it also closes the already-documented intra-Python double-resolve
# race (session.py NEW-1).
#
# 0x444C5048 = 'DLPH'. Deliberately DISJOINT from the agent-lock advisory
# namespace (0x41474E54 'AGNT', src/services/update_workflow_service.py) so the
# two lock spaces never collide (PR #1017 council note on advisory lock-space
# separation). Both use the two-arg pg_advisory_*_lock(classid int4, key int4)
# form, which lives in a different space from the single-arg form.
_DIALECTIC_PHASE_LOCK_NS = 0x444C5048


# =============================================================================
# PostgreSQL Backend (Primary and Only)
# =============================================================================

class DialecticDB:
    """
    PostgreSQL-backed storage for dialectic sessions.

    Uses asyncpg for native async operations. Shares the connection pool
    with the main governance database for unified data access.
    """

    def __init__(self, pool=None):
        """Initialize with an existing asyncpg pool."""
        self._pool = pool
        self._initialized = False

    async def init(self, pool=None):
        """Initialize the database connection."""
        if pool:
            self._pool = pool

        if not self._pool:
            from src.db import get_db
            db = get_db()
            await db.init()
            self._pool = db._pool

        self._initialized = True
        logger.debug("Initialized PostgreSQL dialectic backend")

    def _pool_is_alive(self) -> bool:
        """Check if the cached pool reference is still usable."""
        if self._pool is None:
            return False
        # asyncpg sets _closed=True after pool.close()
        return not getattr(self._pool, '_closed', False)

    async def _ensure_pool(self):
        """Ensure pool is initialized and alive before use.

        Detects stale pool references (e.g. after PostgresBackend
        recreated its pool) and refreshes from the backend.
        """
        if not self._pool_is_alive():
            if self._pool is not None:
                logger.warning("DialecticDB pool is closed/stale, refreshing from backend...")
            else:
                logger.warning("PostgreSQL dialectic pool was None, re-initializing...")
            self._pool = None  # Clear stale reference
            await self.init()
            if self._pool is None:
                raise RuntimeError("Failed to initialize PostgreSQL dialectic pool")

    async def create_session(
        self,
        session_id: str,
        paused_agent_id: str,
        reviewer_agent_id: str = None,
        reason: str = None,
        discovery_id: str = None,
        dispute_type: str = None,
        session_type: str = None,
        topic: str = None,
        max_synthesis_rounds: int = None,
        synthesis_round: int = None,
        paused_agent_state: Dict = None,
        trigger_source: str = None,
    ) -> Dict[str, Any]:
        """Create a new dialectic session."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            try:
                await conn.execute("""
                    INSERT INTO core.dialectic_sessions (
                        session_id, paused_agent_id, reviewer_agent_id,
                        phase, status, session_type, topic,
                        reason, discovery_id, dispute_type,
                        max_synthesis_rounds, synthesis_round, paused_agent_state_json,
                        trigger_source
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                    session_id,
                    paused_agent_id,
                    reviewer_agent_id,
                    DialecticPhase.THESIS.value,
                    "active",
                    session_type,
                    topic,
                    reason,
                    discovery_id,
                    dispute_type,
                    max_synthesis_rounds,
                    synthesis_round or 0,
                    json.dumps(paused_agent_state) if paused_agent_state else None,
                    trigger_source,
                )
                logger.info(f"Created dialectic session {session_id[:16]}... for agent {paused_agent_id}")
                return {"session_id": session_id, "created": True}
            except Exception as e:
                if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                    logger.warning(f"Session {session_id} already exists: {e}")
                    return {"session_id": session_id, "created": False, "error": "already_exists"}
                raise

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID with all messages."""
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                SELECT * FROM core.dialectic_sessions WHERE session_id = $1
            """, session_id)

            if not row:
                return None

            session = dict(row)

            # Handle _json suffix columns
            if "paused_agent_state_json" in session:
                val = session.pop("paused_agent_state_json")
                if val:
                    session["paused_agent_state"] = val if isinstance(val, dict) else json.loads(val)

            if "resolution_json" in session:
                val = session.pop("resolution_json")
                if val:
                    session["resolution"] = val if isinstance(val, dict) else json.loads(val)

            # Get messages
            msg_rows = await conn.fetch("""
                SELECT * FROM core.dialectic_messages
                WHERE session_id = $1
                ORDER BY message_id ASC
            """, session_id)

            session["messages"] = [dict(msg) for msg in msg_rows]
            return session

    async def get_session_by_agent(self, agent_id: str, active_only: bool = True) -> Optional[Dict[str, Any]]:
        """Get session where agent is paused agent or reviewer."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            status_filter = "AND status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')" if active_only else ""
            row = await conn.fetchrow(f"""
                SELECT session_id FROM core.dialectic_sessions
                WHERE (paused_agent_id = $1 OR reviewer_agent_id = $1)
                {status_filter}
                ORDER BY created_at DESC
                LIMIT 1
            """, agent_id)

            if row:
                return await self.get_session(row["session_id"])
            return None

    async def get_all_sessions_by_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        """Get all active sessions where agent is paused agent or reviewer."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT session_id FROM core.dialectic_sessions
                WHERE (paused_agent_id = $1 OR reviewer_agent_id = $1)
                AND status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                ORDER BY created_at DESC
            """, agent_id)

            sessions = []
            for row in rows:
                session = await self.get_session(row["session_id"])
                if session:
                    sessions.append(session)
            return sessions

    async def _acquire_phase_slot(self, conn, session_id: str) -> None:
        """Take the transaction-scoped saga slot for a session's phase line.

        MUST be called as the first statement inside the same transaction as a
        phase/status mutation. pg_advisory_xact_lock auto-releases at COMMIT/
        ROLLBACK, so there is no unlock to leak (unlike a session-level lock).
        Serializes every cross-process writer of this session's phase — see
        _DIALECTIC_PHASE_LOCK_NS.
        """
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1, hashtext($2))",
            _DIALECTIC_PHASE_LOCK_NS,
            session_id,
        )

    async def update_session_phase(self, session_id: str, phase: str) -> bool:
        """Update session phase/status (saga-slot serialized)."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._acquire_phase_slot(conn, session_id)
                result = await conn.execute("""
                    UPDATE core.dialectic_sessions
                    SET phase = $1, updated_at = now()
                    WHERE session_id = $2
                """, phase, session_id)
                return "UPDATE 1" in result

    async def update_session_reviewer(self, session_id: str, reviewer_agent_id: str) -> bool:
        """Assign reviewer to session (saga-slot serialized)."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._acquire_phase_slot(conn, session_id)
                result = await conn.execute("""
                    UPDATE core.dialectic_sessions
                    SET reviewer_agent_id = $1, updated_at = now()
                    WHERE session_id = $2
                """, reviewer_agent_id, session_id)
                return "UPDATE 1" in result

    async def update_session_status(self, session_id: str, status: str) -> bool:
        """Update session status (e.g., to 'failed' for auto-resolve); saga-slot serialized."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._acquire_phase_slot(conn, session_id)
                result = await conn.execute("""
                    UPDATE core.dialectic_sessions
                    SET status = $1, phase = $1, updated_at = now()
                    WHERE session_id = $2
                """, status, session_id)
                return "UPDATE 1" in result

    async def resolve_session(
        self,
        session_id: str,
        resolution: Dict[str, Any],
        status: str = "resolved"
    ) -> bool:
        """Mark session as resolved or failed with resolution data (saga-slot serialized).

        The saga slot closes the NEW-1 race: two concurrent synthesis calls that
        both pass the in-memory SYNTHESIS-phase check can no longer both write a
        resolution — the second serializes behind the first and sees the
        committed terminal state.
        """
        await self._ensure_pool()
        # Phase should match status - don't hardcode 'resolved' when status is 'failed'
        phase = "resolved" if status == "resolved" else status
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._acquire_phase_slot(conn, session_id)
                result = await conn.execute("""
                    UPDATE core.dialectic_sessions
                    SET status = $1, phase = $2, resolution_json = $3, updated_at = now()
                    WHERE session_id = $4
                """, status, phase, json.dumps(resolution), session_id)
                logger.info(f"Resolved session {session_id[:16]}... with status {status}")
                return "UPDATE 1" in result

    async def add_message(
        self,
        session_id: str,
        agent_id: str,
        message_type: str,
        root_cause: str = None,
        proposed_conditions: List[str] = None,
        reasoning: str = None,
        observed_metrics: Dict = None,
        concerns: List[str] = None,
        agrees: bool = None,
        signature: str = None,
    ) -> int:
        """Add a message to a session."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO core.dialectic_messages (
                    session_id, agent_id, message_type,
                    root_cause, proposed_conditions, reasoning,
                    observed_metrics, concerns, agrees, signature
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING message_id
            """,
                session_id,
                agent_id,
                message_type,
                root_cause,
                json.dumps(proposed_conditions) if proposed_conditions else None,
                reasoning,
                json.dumps(observed_metrics) if observed_metrics else None,
                json.dumps(concerns) if concerns else None,
                agrees,
                signature,
            )

            await conn.execute("""
                UPDATE core.dialectic_sessions SET updated_at = now() WHERE session_id = $1
            """, session_id)

            return row["message_id"] if row else 0

    async def is_agent_in_active_session(self, agent_id: str) -> bool:
        """Check if agent is in an active session."""
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM core.dialectic_sessions
                WHERE (paused_agent_id = $1 OR reviewer_agent_id = $1)
                AND status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                LIMIT 1
            """, agent_id)
            return row is not None

    async def has_recently_reviewed(
        self,
        reviewer_id: str,
        paused_agent_id: str,
        hours: int = 24
    ) -> bool:
        """Check if reviewer has recently reviewed this agent.

        Counts ALL session outcomes (resolved, failed, timeout, escalated) to prevent
        a reviewer from bypassing the cooldown by deliberately failing sessions.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM core.dialectic_sessions
                WHERE reviewer_agent_id = $1
                AND paused_agent_id = $2
                AND created_at >= now() - interval '1 hour' * $3
                LIMIT 1
            """, reviewer_id, paused_agent_id, hours)
            return row is not None

    async def get_active_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all active sessions."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM core.dialectic_sessions
                WHERE status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                ORDER BY created_at DESC
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

    async def get_sessions_awaiting_reviewer(self) -> List[Dict[str, Any]]:
        """Get sessions that need a reviewer assigned."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM core.dialectic_sessions
                WHERE status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                AND (reviewer_agent_id IS NULL OR reviewer_agent_id = '')
                ORDER BY created_at ASC
            """)
            return [dict(row) for row in rows]

    async def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            stats = {}

            rows = await conn.fetch("""
                SELECT status, COUNT(*) as count
                FROM core.dialectic_sessions
                GROUP BY status
            """)
            stats["by_status"] = {row["status"]: row["count"] for row in rows}

            rows = await conn.fetch("""
                SELECT session_type, COUNT(*) as count
                FROM core.dialectic_sessions
                GROUP BY session_type
            """)
            stats["by_type"] = {row["session_type"] or "unknown": row["count"] for row in rows}

            row = await conn.fetchrow("SELECT COUNT(*) as count FROM core.dialectic_messages")
            stats["total_messages"] = row["count"] if row else 0

            row = await conn.fetchrow("SELECT COUNT(*) as count FROM core.dialectic_sessions")
            stats["total_sessions"] = row["count"] if row else 0

            return stats

    async def health_check(self) -> Dict[str, Any]:
        """Database health check."""
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            sess = await conn.fetchval("SELECT COUNT(*) FROM core.dialectic_sessions")
            msgs = await conn.fetchval("SELECT COUNT(*) FROM core.dialectic_messages")

            return {
                "backend": "postgres",
                "total_sessions": int(sess) if sess else 0,
                "total_messages": int(msgs) if msgs else 0,
            }


# =============================================================================
# Singleton Instance & Async Wrappers
# =============================================================================

_db_instance: Optional[DialecticDB] = None
_db_lock: Optional[asyncio.Lock] = None


async def get_dialectic_db() -> DialecticDB:
    """Get singleton dialectic database instance."""
    global _db_instance, _db_lock

    if _db_lock is None:
        _db_lock = asyncio.Lock()

    async with _db_lock:
        if _db_instance is None:
            logger.info("Initializing PostgreSQL dialectic backend")
            _db_instance = DialecticDB()
            await _db_instance.init()

        return _db_instance


# Convenience wrappers - call methods directly on singleton
async def create_session_async(**kwargs) -> Dict[str, Any]:
    db = await get_dialectic_db()
    return await db.create_session(**kwargs)


async def get_session_async(session_id: str) -> Optional[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_session(session_id)


async def get_session_by_agent_async(agent_id: str, active_only: bool = True) -> Optional[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_session_by_agent(agent_id, active_only)


async def get_all_sessions_by_agent_async(agent_id: str) -> List[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_all_sessions_by_agent(agent_id)


async def is_agent_in_active_session_async(agent_id: str) -> bool:
    db = await get_dialectic_db()
    return await db.is_agent_in_active_session(agent_id)


async def has_recently_reviewed_async(reviewer_id: str, paused_agent_id: str, hours: int = 24) -> bool:
    db = await get_dialectic_db()
    return await db.has_recently_reviewed(reviewer_id, paused_agent_id, hours)


async def add_message_async(**kwargs) -> int:
    db = await get_dialectic_db()
    return await db.add_message(**kwargs)


async def update_session_phase_async(session_id: str, phase: str) -> bool:
    db = await get_dialectic_db()
    return await db.update_session_phase(session_id, phase)


async def update_session_reviewer_async(session_id: str, reviewer_agent_id: str) -> bool:
    db = await get_dialectic_db()
    return await db.update_session_reviewer(session_id, reviewer_agent_id)


async def update_session_status_async(session_id: str, status: str) -> bool:
    db = await get_dialectic_db()
    return await db.update_session_status(session_id, status)


async def resolve_session_async(session_id: str, resolution: Dict[str, Any], status: str = "resolved") -> bool:
    db = await get_dialectic_db()
    return await db.resolve_session(session_id, resolution, status)


async def get_active_sessions_async(limit: int = 100) -> List[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_active_sessions(limit)


async def get_sessions_awaiting_reviewer_async() -> List[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_sessions_awaiting_reviewer()
