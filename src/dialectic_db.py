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

    async def update_session_phase(
        self, session_id: str, phase: str, synthesis_round: Optional[int] = None
    ) -> bool:
        """Update session phase, and the synthesis round when one is supplied.

        ``synthesis_round=None`` leaves the stored value untouched via COALESCE,
        so callers that do not track rounds are unaffected. Before 2026-08-16
        this wrote phase only, which is why every row read ``synthesis_round=0``
        regardless of how many synthesis messages a session actually carried.

        Carries the same TERMINAL_WRITE_GUARD as the status/reviewer writers:
        a phase sync racing a concurrent resolution used to stamp e.g.
        ``phase='failed'`` onto a ``status='resolved'`` row, and rehydration
        trusts ``phase``. A refused sync returns False; every caller is a
        best-effort mirror of in-memory state, so skipping on a terminal row
        is the correct outcome, not an error.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE core.dialectic_sessions
                SET phase = $1,
                    synthesis_round = COALESCE($3, synthesis_round),
                    updated_at = now()
                WHERE session_id = $2
                  AND status NOT IN ('resolved', 'failed')
            """, phase, session_id, synthesis_round)
            if "UPDATE 1" in result:
                return True
            logger.info(
                f"update_session_phase: {session_id[:16]}... phase sync skipped "
                "(row terminal or missing)"
            )
            return False

    async def reopen_session(self, session_id: str, phase: str) -> bool:
        """Return a swept session to `active` at a workable phase.

        The ONLY path that un-terminalises a session, and deliberately narrow:
        it fires solely when a reviewer is assigned to a session whose
        facilitation request was still standing when the sweeper marked it
        failed. `update_session_phase` sets phase but not status, so without
        this the revived row keeps `status='failed'` and the sweeper
        re-terminates it on the next cycle.

        Guarded on `awaiting_facilitation` in SQL as well as at the call site —
        a resolved session, or a failed one that never asked for a human, is
        never reopened. `resolution_json` is left untouched: reopening does not
        rewrite history.
        """
        if phase in ("resolved", "failed"):
            return False
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE core.dialectic_sessions
                SET phase = $1, status = 'active', updated_at = now()
                WHERE session_id = $2
                  AND status = 'failed'
                  AND awaiting_facilitation = true
            """, phase, session_id)
            return "UPDATE 1" in result

    # Row-statuses no in-place writer may modify. Exactly the set
    # `resolve_session` and DialecticSaga.commit_session_row (dialectic_saga.ex
    # "BEAM is the sole writer for both terminal transitions") already guard,
    # and the only two values the live `dialectic_sessions_status_check`
    # CHECK constraint permits that are terminal: 'timeout'/'abandoned' are
    # not storable at all, and 'escalated' is storable but council-retired
    # with zero writers — deliberately NOT guarded so a stray escalated row
    # stays reapable by the sweeper instead of becoming immortal.
    # Named distinctly from dialectic_outcomes.TERMINAL_STATUSES, which is an
    # analytics classifier with different membership, not a write gate.
    # `reopen_session` is the only sanctioned path out of a terminal state.
    TERMINAL_WRITE_GUARD = ("resolved", "failed")

    async def update_session_reviewer(self, session_id: str, reviewer_agent_id: str) -> bool:
        """Assign reviewer to session.

        Refuses terminal sessions: the sweeper picks a replacement reviewer
        across several DB round-trips, and the session can resolve (e.g. via
        the BEAM saga) inside that window. Without the guard the write lands
        on a resolved row. Returns False when refused or missing.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE core.dialectic_sessions
                SET reviewer_agent_id = $1, updated_at = now()
                WHERE session_id = $2
                  AND status NOT IN ('resolved', 'failed')
            """, reviewer_agent_id, session_id)
            if "UPDATE 1" in result:
                return True
            existing = await conn.fetchrow(
                "SELECT status FROM core.dialectic_sessions WHERE session_id = $1",
                session_id,
            )
            if existing is None:
                logger.warning(f"update_session_reviewer: {session_id[:16]}... not found")
            else:
                logger.warning(
                    f"update_session_reviewer: {session_id[:16]}... is terminal as "
                    f"{existing['status']!r}; reviewer write refused"
                )
            return False

    async def update_session_status(self, session_id: str, status: str) -> bool:
        """Update session status (e.g., to 'failed' for auto-resolve).

        Same cross-process defense as ``resolve_session``: a bare
        ``WHERE session_id`` let the sweeper overwrite a session another
        writer (the BEAM saga, a concurrent resolve) had already finished —
        the in-process lock in ``session.py`` cannot see other processes.

        Returns True ONLY when this call performed the transition. A no-op
        returns False even when the row already holds the requested status:
        "another writer got there first with the same value" is still their
        outcome, not this caller's — the sweeper must not narrate a reap it
        did not perform (BEAM liveness also writes 'failed', with its own
        resolution payload). Callers that want idempotent-replay semantics
        use ``resolve_session``.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE core.dialectic_sessions
                SET status = $1, phase = $1, updated_at = now()
                WHERE session_id = $2
                  AND status NOT IN ('resolved', 'failed')
            """, status, session_id)
            if "UPDATE 1" in result:
                return True
            existing = await conn.fetchrow(
                "SELECT status FROM core.dialectic_sessions WHERE session_id = $1",
                session_id,
            )
            if existing is None:
                logger.warning(f"update_session_status: {session_id[:16]}... not found")
            elif existing["status"] == status:
                logger.info(
                    f"update_session_status: {session_id[:16]}... already {status} "
                    "(another writer won; not this caller's transition)"
                )
            else:
                logger.warning(
                    f"update_session_status: {session_id[:16]}... already terminal as "
                    f"{existing['status']!r}; refused overwrite to {status!r}"
                )
            return False

    async def mark_awaiting_facilitation(self, session_id: str) -> bool:
        """Record a standing facilitation request on a LIVE session.

        Deliberately separate from `update_session_awaiting_facilitation`,
        which is unguarded because its callers include the clear-on-resolve
        path — a write that by definition lands as the row goes terminal.
        SETTING the flag is the opposite case: it only means anything while
        the session can still be answered, and stamping it onto a row another
        writer has just failed would make an ordinary failure revivable —
        `reopen_session` reopens exactly `status='failed' AND
        awaiting_facilitation=true`.

        Carries TERMINAL_WRITE_GUARD like the status/reviewer/phase writers.
        Returns False when refused or missing; the sweeper reads that as
        "another writer finished this session" and skips it, the same way it
        treats a refused reviewer write.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE core.dialectic_sessions
                SET awaiting_facilitation = true, updated_at = now()
                WHERE session_id = $1
                  AND status NOT IN ('resolved', 'failed')
            """, session_id)
            if "UPDATE 1" in result:
                return True
            existing = await conn.fetchrow(
                "SELECT status FROM core.dialectic_sessions WHERE session_id = $1",
                session_id,
            )
            if existing is None:
                logger.warning(f"mark_awaiting_facilitation: {session_id[:16]}... not found")
            else:
                logger.warning(
                    f"mark_awaiting_facilitation: {session_id[:16]}... is terminal as "
                    f"{existing['status']!r}; facilitation write refused"
                )
            return False

    async def update_session_awaiting_facilitation(self, session_id: str, awaiting: bool) -> bool:
        """Persist the awaiting_facilitation flag (#1167 Ask 2).

        Mirrors the in-memory DialecticSession.awaiting_facilitation attribute so
        dialectic(list) can surface stuck sessions (and they survive restarts).
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE core.dialectic_sessions
                SET awaiting_facilitation = $1, updated_at = now()
                WHERE session_id = $2
            """, awaiting, session_id)
            return "UPDATE 1" in result

    async def resolve_session(
        self,
        session_id: str,
        resolution: Dict[str, Any],
        status: str = "resolved"
    ) -> bool:
        """Mark session as resolved or failed with resolution data.

        Idempotent terminal-transition guard (council 2026-06-28, "B-4"): the
        UPDATE refuses to write a session that is already in a terminal state
        (``resolved``/``failed``). This is the database-layer defense that makes
        the transition safe across *processes* — the in-process asyncio.Lock in
        ``mcp_handlers/dialectic/session.py`` only serializes within one Python
        process, so a crash-recovery re-drive or a second writer (e.g. the
        forthcoming BEAM session owner) could otherwise overwrite a committed
        ``resolution_json``. Return semantics:
          * True  — this call performed the terminal transition, OR the session
                    is already in the *requested* terminal state (idempotent).
          * False — the session is missing, or is already in a *different*
                    terminal state (conflict; the existing resolution is kept).
        """
        await self._ensure_pool()
        # Phase should match status - don't hardcode 'resolved' when status is 'failed'
        phase = "resolved" if status == "resolved" else status
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE core.dialectic_sessions
                SET status = $1, phase = $2, resolution_json = $3, updated_at = now()
                WHERE session_id = $4 AND status NOT IN ('resolved', 'failed')
                RETURNING session_id
            """, status, phase, json.dumps(resolution), session_id)
            if row is not None:
                logger.info(f"Resolved session {session_id[:16]}... with status {status}")
                return True
            # No row written: inspect why (idempotent replay vs conflict vs missing).
            existing = await conn.fetchrow(
                "SELECT status FROM core.dialectic_sessions WHERE session_id = $1",
                session_id,
            )
            if existing is None:
                logger.warning(f"resolve_session: {session_id[:16]}... not found")
                return False
            if existing["status"] == status:
                logger.info(
                    f"resolve_session: {session_id[:16]}... already {status} "
                    "(idempotent no-op, overwrite prevented)"
                )
                return True
            logger.warning(
                f"resolve_session: {session_id[:16]}... already terminal as "
                f"{existing['status']!r}; refused overwrite to {status!r}"
            )
            return False

    async def has_inflight_saga(self, session_id: str) -> bool:
        """True if a non-terminal resolution saga is in flight for this session.

        The BEAM session owner (forthcoming) claims a row in
        ``coordination.session_resolution_sagas`` for the lifetime of a
        SYNTHESIS->RESOLVED resolution. The Python auto-resolve sweeper must NOT
        mark such a session ``failed`` or reassign its reviewer mid-resolution —
        doing so would race the saga and corrupt the outcome. Non-terminal saga
        states are: reserved, paused_agent_applied, both_agents_applied,
        reverting (pg_committed / reverted are terminal and do not block).

        Fail-open: any error (e.g. the saga table absent in a bare test schema)
        returns False. That is safe — if no saga infrastructure is live, BEAM is
        not writing sagas, so there is nothing to race.
        """
        await self._ensure_pool()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1 FROM coordination.session_resolution_sagas
                    WHERE session_id = $1
                      AND state IN ('reserved', 'paused_agent_applied',
                                    'both_agents_applied', 'reverting')
                    LIMIT 1
                    """,
                    session_id,
                )
                return row is not None
        except Exception as e:  # pragma: no cover - defensive fail-open
            logger.debug(f"has_inflight_saga check failed for {session_id[:16]}...: {e}")
            return False

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
        """Check if agent is in an active session.

        Terminal on EITHER column. `status` alone is not sufficient here:
        Python may not write terminal status at all — `TERMINAL_WRITE_GUARD`
        and dialectic_saga.ex reserve both terminal transitions for BEAM — so
        the lazy synthesis-timeout path calls `update_session_phase('failed')`,
        which by design sets `phase` and leaves `status` untouched. That leaves
        a real row shape of `status='active', phase='failed'`: a session that
        is over, whose status write has not landed yet.

        Reading `status` alone treated that shape as active and refused the
        agent a new session with SESSION_EXISTS. The auto-resolve sweeper only
        considers sessions inactive for more than two hours, so the refusal
        persisted for up to that long after a 1.4h synthesis timeout had
        already ended the session — roughly 3.4h of lockout per abandonment.
        `self_recovery` does not clear it (it resumes the agent, not the
        session) and `reopen_session` cannot (it requires
        `awaiting_facilitation`, false for manually requested reviews).

        Adding the phase predicate cannot hide a live session: `reopen_session`
        is the only path out of a terminal state and it refuses to set a
        terminal phase, so a revived row always carries a workable one.
        """
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM core.dialectic_sessions
                WHERE (paused_agent_id = $1 OR reviewer_agent_id = $1)
                AND status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                AND phase NOT IN ('resolved', 'failed')
                LIMIT 1
            """, agent_id)
            return row is not None

    async def has_recently_reviewed(
        self,
        reviewer_id: str,
        paused_agent_id: str,
        hours: int = 24
    ) -> bool:
        """Check whether this reviewer pair appeared in either direction.

        Counts ALL session outcomes (resolved, failed, timeout, escalated) to prevent
        a reviewer from bypassing the cooldown by deliberately failing sessions.
        The direction reversal is load-bearing: after A reviews B, B reviewing
        A inside the window is the reciprocal pattern this policy forbids.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM core.dialectic_sessions
                WHERE (
                    (reviewer_agent_id = $1 AND paused_agent_id = $2)
                    OR
                    (reviewer_agent_id = $2 AND paused_agent_id = $1)
                )
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
        """Get operational database statistics.

        ⛔Deliberately does NOT return a ``status`` breakdown. ``status``
        conflates three endings — protocol failure, canary traffic (which ends
        ``failed`` BY DESIGN), and a standing unfacilitated objection (the
        dialectic working exactly as intended) — so a per-status count is not
        outcome data and reading one as dialectic quality gets the answer
        wrong. It did, on 2026-08-22: a Wave-3 decision artifact reported an
        eligible criterion-10 cohort of 11 by counting ``failed`` rows, where
        the true figure through the classifier is 5 and ``failed`` is in fact
        **zero** across all non-canary traffic.

        A ``by_status`` key was removed here on 2026-08-22 rather than
        annotated. It had no production caller, and leaving a correctly-shaped
        wrong number one dictionary key away from an operational stats call is
        the footgun itself. Use :meth:`get_outcome_breakdown` — which routes
        through ``src/dialectic_outcomes.py::classify_outcome`` — for anything
        that answers "how is the dialectic doing". See RFC §0(F), §11 criterion
        10, and issue #1689.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            stats = {}

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

    async def get_outcome_breakdown(
        self,
        window_days: int = 30,
        min_volume: int = 30,
    ) -> Dict[str, Any]:
        """Terminal-outcome breakdown that does not read `status` as quality.

        Issue #1689. A raw resolution rate off `status` counts two things as
        failures that are not: canary probes, which end `failed` by design, and
        sessions holding a standing reviewer objection that nobody facilitated,
        which is the dialectic working. This is the reader every rate should go
        through -- see src/dialectic_outcomes.py for why.

        `sufficient_volume` is reported rather than assumed. Excluding canary
        and unresolved sessions shrinks the denominator sharply, and a rate
        pinned below the volume floor is a number with no power behind it.
        """
        from src.dialectic_outcomes import (
            CANARY,
            FAILED,
            OPEN,
            RESOLVED,
            UNRESOLVED_AWAITING_FACILITATION,
            classify_outcome,
            resolution_rate,
        )

        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT s.status,
                       coalesce(s.awaiting_facilitation, false) AS awaiting_facilitation,
                       a.label AS paused_agent_label
                FROM core.dialectic_sessions s
                LEFT JOIN core.agents a ON a.id = s.paused_agent_id
                WHERE s.created_at >= now() - interval '1 day' * $1
            """, window_days)

        counts: Dict[str, int] = {
            RESOLVED: 0,
            UNRESOLVED_AWAITING_FACILITATION: 0,
            FAILED: 0,
            CANARY: 0,
            OPEN: 0,
        }
        for row in rows:
            outcome = classify_outcome(
                row["status"],
                row["awaiting_facilitation"],
                row["paused_agent_label"],
            )
            counts[outcome] = counts.get(outcome, 0) + 1

        rate = resolution_rate(counts)
        denominator = counts[RESOLVED] + counts[FAILED]
        return {
            "window_days": window_days,
            "total_sessions": len(rows),
            "counts": counts,
            "resolution_rate": rate,
            "resolution_rate_denominator": denominator,
            "sufficient_volume": denominator >= min_volume,
            "min_volume": min_volume,
        }

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


async def has_inflight_saga_async(session_id: str) -> bool:
    db = await get_dialectic_db()
    return await db.has_inflight_saga(session_id)


async def has_recently_reviewed_async(reviewer_id: str, paused_agent_id: str, hours: int = 24) -> bool:
    db = await get_dialectic_db()
    return await db.has_recently_reviewed(reviewer_id, paused_agent_id, hours)


async def add_message_async(**kwargs) -> int:
    db = await get_dialectic_db()
    return await db.add_message(**kwargs)


async def update_session_phase_async(
    session_id: str, phase: str, synthesis_round: Optional[int] = None
) -> bool:
    db = await get_dialectic_db()
    return await db.update_session_phase(session_id, phase, synthesis_round)


async def reopen_session_async(session_id: str, phase: str) -> bool:
    db = await get_dialectic_db()
    return await db.reopen_session(session_id, phase)


async def update_session_reviewer_async(session_id: str, reviewer_agent_id: str) -> bool:
    db = await get_dialectic_db()
    return await db.update_session_reviewer(session_id, reviewer_agent_id)


async def update_session_status_async(session_id: str, status: str) -> bool:
    db = await get_dialectic_db()
    return await db.update_session_status(session_id, status)


async def mark_awaiting_facilitation_async(session_id: str) -> bool:
    db = await get_dialectic_db()
    return await db.mark_awaiting_facilitation(session_id)


async def update_session_awaiting_facilitation_async(session_id: str, awaiting: bool) -> bool:
    db = await get_dialectic_db()
    return await db.update_session_awaiting_facilitation(session_id, awaiting)


async def resolve_session_async(session_id: str, resolution: Dict[str, Any], status: str = "resolved") -> bool:
    db = await get_dialectic_db()
    return await db.resolve_session(session_id, resolution, status)


async def get_active_sessions_async(limit: int = 100) -> List[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_active_sessions(limit)


async def get_sessions_awaiting_reviewer_async() -> List[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_sessions_awaiting_reviewer()
