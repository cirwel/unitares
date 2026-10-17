"""
PostgreSQL Backend for Dialectic Sessions

Provides storage for dialectic sessions with PostgreSQL.
"""

import json
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


from src.logging_utils import get_logger
from src.dialectic_session_writes import (
    KIND_CREATE,
    KIND_FACILITATION,
    KIND_MESSAGE,
    KIND_PHASE,
    KIND_REOPEN,
    KIND_RESOLVE,
    KIND_REVIEWER,
    KIND_STATUS,
    note_session_read,
    record_session_write,
    written_outcome,
)
from src.dialectic_protocol import DialecticPhase
from src.dialectic_reviewed_work import (
    CANONICAL_AGENT_UUID_PATTERN,
    MAX_REVIEWED_WORK_AUTHOR_AGENT_IDS,
    REVIEWED_WORK_AUTHORSHIP_SCHEMA,
    REVIEWED_WORK_AUTHORSHIP_STATE_KEY,
    read_reviewed_work_authorship,
)
from src.db.acquire_compat import compatible_acquire

logger = get_logger(__name__)

# Distinguishes "no compare-and-set predicate" from an observed None.
_UNSET: Any = object()


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
        *,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new dialectic session."""
        reviewed_work_authorship = read_reviewed_work_authorship(
            paused_agent_state,
            expected_declared_by_agent_id=paused_agent_id,
        )
        if reviewed_work_authorship.is_invalid:
            raise ValueError(
                "reviewed-work authorship metadata is malformed"
            )
        if reviewed_work_authorship.recuses(
            reviewer_agent_id, paused_agent_id=paused_agent_id
        ):
            raise ValueError(
                "reviewer_recused: declared author cannot review authored work"
            )
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(f"""
                    INSERT INTO core.dialectic_sessions (
                        session_id, paused_agent_id, reviewer_agent_id,
                        phase, status, session_type, topic,
                        reason, discovery_id, dispute_type,
                        max_synthesis_rounds, synthesis_round, paused_agent_state_json,
                        trigger_source
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    RETURNING {self._EFFECT_TS}
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
                self._record_effect(detail, row)
                return {"session_id": session_id, "created": True}
            except Exception as e:
                if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                    logger.warning(f"Session {session_id} already exists: {e}")
                    return {"session_id": session_id, "created": False, "error": "already_exists"}
                raise

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID with all messages."""
        read_at = datetime.now(timezone.utc)
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                SELECT s.*,
                       e.state AS resolution_effect_state,
                       e.attempt_count AS resolution_effect_attempt_count,
                       e.claimed_at AS resolution_effect_claimed_at,
                       e.applied_at AS resolution_effect_applied_at,
                       e.uncertain_at AS resolution_effect_uncertain_at
                FROM core.dialectic_sessions s
                LEFT JOIN coordination.dialectic_resolution_effects e
                  ON e.session_id = s.session_id
                WHERE s.session_id = $1
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
            # The state a later write in this task acts on (its decision time).
            note_session_read(session_id, read_at)
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
        read_at = datetime.now(timezone.utc)
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
            note_session_read([x.get("session_id") for x in sessions], read_at)
            return sessions

    async def update_session_phase(
        self, session_id: str, phase: str, synthesis_round: Optional[int] = None,
        *, detail: Optional[Dict[str, Any]] = None,
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
            row = await conn.fetchrow(f"""
                UPDATE core.dialectic_sessions
                SET phase = $1,
                    synthesis_round = COALESCE($3, synthesis_round),
                    updated_at = now()
                WHERE session_id = $2
                  AND status NOT IN ('resolved', 'failed')
                RETURNING {self._EFFECT_TS}
            """, phase, session_id, synthesis_round)
            self._record_effect(detail, row)
            if row is not None:
                return True
            logger.info(
                f"update_session_phase: {session_id[:16]}... phase sync skipped "
                "(row terminal or missing)"
            )
            return False

    async def reopen_session(
        self, session_id: str, phase: str, *, detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
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
            row = await conn.fetchrow(f"""
                UPDATE core.dialectic_sessions
                SET phase = $1, status = 'active', updated_at = now()
                WHERE session_id = $2
                  AND status = 'failed'
                  AND awaiting_facilitation = true
                RETURNING {self._EFFECT_TS}
            """, phase, session_id)
            self._record_effect(detail, row)
            return row is not None

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

    # The follow-up read every guarded writer below makes after a refused
    # UPDATE. It reads the winner's status (always) and the reason it recorded
    # (when it wrote a resolution), on the same connection, immediately after
    # the refusal, so the attribution cannot race a later reopen.
    _REFUSAL_WINNER_SQL = (
        "SELECT status, paused_agent_id, resolution_json->>'reason' AS reason, "
        "paused_agent_state_json "
        "FROM core.dialectic_sessions WHERE session_id = $1"
    )

    @staticmethod
    def _record_winner(winner: Optional[Dict[str, Any]], existing) -> None:
        """Report who refused a guarded write, without changing the refusal.

        ``winner`` is an optional caller-supplied dict. When given, it is
        filled with ``winner_status`` and ``winner_reason`` (both None for a
        missing row) and ``row_missing``. The helpers' boolean return is
        unchanged: this is observability for the caller, not a new outcome
        (Wave 3 gate council 2026-09-27, finding B3 -- the status was read and
        only logged). Never raises.
        """
        if winner is None:
            return
        try:
            if existing is None:
                winner.update(winner_status=None, winner_reason=None, row_missing=True)
                return
            reason = None
            try:
                reason = existing["reason"]
            except (KeyError, IndexError, TypeError):
                reason = None
            winner.update(
                winner_status=existing["status"],
                winner_reason=reason,
                row_missing=False,
            )
        except Exception:  # pragma: no cover - attribution must never fail a write path
            return

    # Every Python session writer returns its effect time from the SAME
    # statement: `RETURNING clock_timestamp() AS effect_ts`. clock_timestamp()
    # and not now(): now() is the transaction's START (for these single-
    # statement autocommit writes, the moment the statement began), which can
    # precede an unbounded wait for the row lock; clock_timestamp() is read as
    # the RETURNING row is produced, i.e. after the lock is held and the row
    # modified, and before the commit that makes it visible. It is therefore
    # the tightest single database-clock time for when this write took effect.
    # The Wave 3 collision report orders competing writes by it
    # (`dialectic_session_write` responses carry it as `effect_ts`).
    _EFFECT_TS = "clock_timestamp() AS effect_ts"

    @staticmethod
    def _record_effect(detail: Optional[Dict[str, Any]], row) -> None:
        """Fill the caller's ``detail`` with whether THIS statement wrote, and when."""
        if detail is None:
            return
        try:
            detail["written"] = row is not None
            detail["effect_ts"] = row["effect_ts"] if row is not None else None
        except Exception:  # pragma: no cover - observability must not fail a write
            return

    async def update_session_reviewer(
        self,
        session_id: str,
        reviewer_agent_id: str,
        *,
        claim_if_unassigned: bool = False,
        expected_phase: Any = _UNSET,
        expected_updated_at: Any = _UNSET,
        expected_reviewer: Any = _UNSET,
        expected_transcript_revision: Any = _UNSET,
        expected_awaiting_facilitation: Any = _UNSET,
        expected_resolution_saga_revision: Any = _UNSET,
        exclude_inflight_saga: bool = False,
        clear_facilitation: bool = False,
        winner: Optional[Dict[str, Any]] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Assign reviewer to session.

        Refuses terminal sessions: the sweeper picks a replacement reviewer
        across several DB round-trips, and the session can resolve (e.g. via
        the BEAM saga) inside that window. Without the guard the write lands
        on a resolved row. Returns False when refused or missing; pass
        ``winner={}`` to learn which (see `_record_winner`). When
        ``claim_if_unassigned`` is true, the assignment is a NULL-to-reviewer
        compare-and-set; this is the first-responder boundary that prevents two
        reviewers that both read an open slot from overwriting one another.
        Sweeper reassignment also binds the phase, old reviewer, read timestamp,
        and transcript frontier so it cannot overwrite newer participant or
        operator work selected after the sweep read.
        """
        await self._ensure_pool()
        open_slot_guard = (
            " AND reviewer_agent_id IS NULL" if claim_if_unassigned else ""
        )
        args: List[Any] = [
            reviewer_agent_id,
            session_id,
            REVIEWED_WORK_AUTHORSHIP_SCHEMA,
            CANONICAL_AGENT_UUID_PATTERN,
            MAX_REVIEWED_WORK_AUTHOR_AGENT_IDS,
        ]
        cas = ""
        if expected_phase is not _UNSET:
            args.append(expected_phase)
            cas += f" AND phase IS NOT DISTINCT FROM ${len(args)}"
        if expected_updated_at is not _UNSET:
            args.append(expected_updated_at)
            cas += f" AND updated_at IS NOT DISTINCT FROM ${len(args)}"
        if expected_reviewer is not _UNSET:
            args.append(expected_reviewer)
            cas += f" AND reviewer_agent_id IS NOT DISTINCT FROM ${len(args)}"
        if expected_transcript_revision is not _UNSET:
            args.append(expected_transcript_revision)
            cas += f" AND transcript_revision = ${len(args)}"
        if expected_awaiting_facilitation is not _UNSET:
            args.append(expected_awaiting_facilitation)
            cas += (
                " AND awaiting_facilitation IS NOT DISTINCT FROM "
                f"${len(args)}"
            )
        if expected_resolution_saga_revision is not _UNSET:
            args.append(expected_resolution_saga_revision)
            cas += f" AND resolution_saga_revision = ${len(args)}"
        saga_guard = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM coordination.session_resolution_sagas saga
                    WHERE saga.session_id = core.dialectic_sessions.session_id
                      AND saga.state IN ('reserved', 'paused_agent_applied',
                                         'both_agents_applied', 'reverting')
                  )
        """ if exclude_inflight_saga else ""
        clear = ", awaiting_facilitation = false" if clear_facilitation else ""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"""
                UPDATE core.dialectic_sessions
                SET reviewer_agent_id = $1, updated_at = now(){clear}
                WHERE session_id = $2
                  AND status NOT IN ('resolved', 'failed')
                  {open_slot_guard}
                  {cas}
                  {saga_guard}
                  AND (
                    paused_agent_state_json IS NULL
                    OR NOT (paused_agent_state_json ? '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}')
                    OR (
                      jsonb_typeof(paused_agent_state_json -> '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}') = 'object'
                      AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},schema}}' = $3
                      AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},status}}' = 'declared'
                      AND paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},authorship_verified}}' = 'false'::jsonb
                      AND jsonb_typeof(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}') = 'string'
                      AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}' ~ $4
                      AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}' = paused_agent_id
                      AND CASE
                        WHEN jsonb_typeof(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') = 'array'
                        THEN
                          jsonb_array_length(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') BETWEEN 1 AND $5
                          AND NOT EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') AS author(value)
                            WHERE jsonb_typeof(author.value) <> 'string'
                               OR (author.value #>> '{{}}') !~ $4
                          )
                          AND jsonb_array_length(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') = (
                            SELECT count(DISTINCT author.value #>> '{{}}')
                            FROM jsonb_array_elements(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') AS author(value)
                          )
                          AND (
                            $1 = paused_agent_id
                            OR NOT (
                              paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}' ? $1
                            )
                          )
                        ELSE FALSE
                      END
                    )
                  )
                RETURNING {self._EFFECT_TS}
            """, *args)
            self._record_effect(detail, row)
            if row is not None:
                return True
            existing = await conn.fetchrow(self._REFUSAL_WINNER_SQL, session_id)
            self._record_winner(winner, existing)
            if existing is None:
                logger.warning(f"update_session_reviewer: {session_id[:16]}... not found")
            elif read_reviewed_work_authorship(existing).recuses(
                reviewer_agent_id,
                paused_agent_id=(
                    existing.get("paused_agent_id")
                    if hasattr(existing, "get")
                    else None
                ),
            ):
                logger.warning(
                    f"update_session_reviewer: {session_id[:16]}... reviewer "
                    "refused by reviewed-work authorship policy"
                )
            else:
                if existing["status"] in self.TERMINAL_WRITE_GUARD:
                    logger.warning(
                        f"update_session_reviewer: {session_id[:16]}... is terminal as "
                        f"{existing['status']!r}; reviewer write refused"
                    )
                else:
                    logger.info(
                        f"update_session_reviewer: {session_id[:16]}... live-row "
                        "guard changed; reviewer write refused"
                    )
            return False

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        winner: Optional[Dict[str, Any]] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
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
        use ``resolve_session``. Pass ``winner={}`` to learn who refused the
        write (see `_record_winner`); the return value is unchanged.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"""
                UPDATE core.dialectic_sessions
                SET status = $1, phase = $1, updated_at = now()
                WHERE session_id = $2
                  AND status NOT IN ('resolved', 'failed')
                RETURNING {self._EFFECT_TS}
            """, status, session_id)
            self._record_effect(detail, row)
            if row is not None:
                return True
            existing = await conn.fetchrow(self._REFUSAL_WINNER_SQL, session_id)
            self._record_winner(winner, existing)
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

    async def mark_awaiting_facilitation(
        self,
        session_id: str,
        *,
        expected_phase: Any = _UNSET,
        expected_updated_at: Any = _UNSET,
        expected_reviewer: Any = _UNSET,
        expected_transcript_revision: Any = _UNSET,
        expected_resolution_saga_revision: Any = _UNSET,
        exclude_inflight_saga: bool = False,
        touch_updated_at: bool = False,
        winner: Optional[Dict[str, Any]] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record a standing facilitation request on a LIVE session.

        Deliberately separate from `update_session_awaiting_facilitation`,
        whose interactive callers advance the staleness clock and can clear a
        live request. SETTING the flag from the sweeper is the opposite case:
        it only means anything while the session can still be answered, and
        stamping it onto a row another writer has just failed would make an
        ordinary failure revivable —
        `reopen_session` reopens exactly `status='failed' AND
        awaiting_facilitation=true`.

        Carries TERMINAL_WRITE_GUARD like the status/reviewer/phase writers.
        Returns False when refused or missing; the sweeper reads that as
        "another writer finished this session" and skips it, the same way it
        treats a refused reviewer write.

        ⛔Deliberately does NOT touch `updated_at`, which every other writer
        here does. `updated_at` is the sweeper's staleness clock, and the
        sweeper is the only unattended retry of `select_reviewer`: bumping it
        would drop the session out of the stuck set for a full
        STUCK_SESSION_THRESHOLD (2h), so a replacement reviewer that becomes
        available ten minutes after the request would not be picked up for two
        hours. Recording that a session is waiting on a human must not stop it
        being rescued by a machine. The handler path writes the flag through
        `update_session_awaiting_facilitation`, which does bump — it is a
        caller-driven transition on a live session, not a sweep observation.

        Compare-and-set (#2367): when ``expected_phase``,
        ``expected_updated_at`` and/or ``expected_reviewer`` are passed (the
        values the caller read), the write lands only if the row still matches
        them, so a participant message, phase move, or reviewer replacement
        refuses it instead of flagging a session that is no longer stalled.
        The sweeper's CAS only READS `updated_at`; the SET clause does not
        write it unless ``touch_updated_at`` is explicitly requested by an
        interactive handler. Omitted observations add no predicate.

        Pass ``winner={}`` to learn who refused the write (see
        `_record_winner`); the return value is unchanged.
        """
        await self._ensure_pool()
        cas = ""
        args: List[Any] = [session_id]
        if expected_phase is not _UNSET:
            args.append(expected_phase)
            cas += f" AND phase IS NOT DISTINCT FROM ${len(args)}"
        if expected_updated_at is not _UNSET:
            args.append(expected_updated_at)
            cas += f" AND updated_at IS NOT DISTINCT FROM ${len(args)}"
        if expected_reviewer is not _UNSET:
            args.append(expected_reviewer)
            cas += f" AND reviewer_agent_id IS NOT DISTINCT FROM ${len(args)}"
        if expected_transcript_revision is not _UNSET:
            args.append(expected_transcript_revision)
            cas += f" AND transcript_revision = ${len(args)}"
        if expected_resolution_saga_revision is not _UNSET:
            args.append(expected_resolution_saga_revision)
            cas += f" AND resolution_saga_revision = ${len(args)}"
        saga_guard = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM coordination.session_resolution_sagas saga
                    WHERE saga.session_id = core.dialectic_sessions.session_id
                      AND saga.state IN ('reserved', 'paused_agent_applied',
                                         'both_agents_applied', 'reverting')
                  )
        """ if exclude_inflight_saga else ""
        touch = ", updated_at = now()" if touch_updated_at else ""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"""
                UPDATE core.dialectic_sessions
                SET awaiting_facilitation = true{touch}
                WHERE session_id = $1
                  AND status NOT IN ('resolved', 'failed')
                  AND awaiting_facilitation IS DISTINCT FROM true{cas}
                  {saga_guard}
                RETURNING {self._EFFECT_TS}
            """, *args)
            self._record_effect(detail, row)
            if row is not None:
                return True
            existing = await conn.fetchrow(self._REFUSAL_WINNER_SQL, session_id)
            self._record_winner(winner, existing)
            if existing is None:
                logger.warning(f"mark_awaiting_facilitation: {session_id[:16]}... not found")
            elif existing["status"] not in ("resolved", "failed"):
                logger.info(
                    f"mark_awaiting_facilitation: {session_id[:16]}... moved since "
                    "the sweeper read it (phase/updated_at changed); facilitation "
                    "write refused, next sweep re-reads"
                )
            else:
                logger.warning(
                    f"mark_awaiting_facilitation: {session_id[:16]}... is terminal as "
                    f"{existing['status']!r}; facilitation write refused"
                )
            return False

    async def update_session_awaiting_facilitation(
        self,
        session_id: str,
        awaiting: bool,
        *,
        expected_phase: Any = _UNSET,
        expected_reviewer: Any = _UNSET,
        expected_transcript_revision: Any = _UNSET,
        expected_awaiting_facilitation: Any = _UNSET,
        expected_resolution_saga_revision: Any = _UNSET,
        exclude_inflight_saga: bool = False,
        live_only: bool = False,
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Persist the awaiting_facilitation flag (#1167 Ask 2).

        Mirrors the in-memory DialecticSession.awaiting_facilitation attribute so
        dialectic(list) can surface stuck sessions (and they survive restarts).
        """
        await self._ensure_pool()
        args: List[Any] = [awaiting, session_id]
        guards = ""
        if live_only:
            guards += " AND status NOT IN ('resolved', 'failed')"
        if expected_phase is not _UNSET:
            args.append(expected_phase)
            guards += f" AND phase IS NOT DISTINCT FROM ${len(args)}"
        if expected_reviewer is not _UNSET:
            args.append(expected_reviewer)
            guards += f" AND reviewer_agent_id IS NOT DISTINCT FROM ${len(args)}"
        if expected_transcript_revision is not _UNSET:
            args.append(expected_transcript_revision)
            guards += f" AND transcript_revision = ${len(args)}"
        if expected_awaiting_facilitation is not _UNSET:
            args.append(expected_awaiting_facilitation)
            guards += (
                " AND awaiting_facilitation IS NOT DISTINCT FROM "
                f"${len(args)}"
            )
        if expected_resolution_saga_revision is not _UNSET:
            args.append(expected_resolution_saga_revision)
            guards += f" AND resolution_saga_revision = ${len(args)}"
        saga_guard = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM coordination.session_resolution_sagas saga
                    WHERE saga.session_id = core.dialectic_sessions.session_id
                      AND saga.state IN ('reserved', 'paused_agent_applied',
                                         'both_agents_applied', 'reverting')
                  )
        """ if exclude_inflight_saga else ""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"""
                UPDATE core.dialectic_sessions
                SET awaiting_facilitation = $1, updated_at = now()
                WHERE session_id = $2{guards}
                  {saga_guard}
                RETURNING {self._EFFECT_TS}
            """, *args)
            self._record_effect(detail, row)
            return row is not None

    async def resolve_session(
        self,
        session_id: str,
        resolution: Dict[str, Any],
        status: str = "resolved",
        *,
        expected_phase: Any = _UNSET,
        expected_updated_at: Any = _UNSET,
        expected_reviewer: Any = _UNSET,
        expected_transcript_revision: Any = _UNSET,
        expected_awaiting_facilitation: Any = _UNSET,
        expected_resolution_saga_revision: Any = _UNSET,
        exclude_inflight_saga: bool = False,
        clear_facilitation: bool = False,
        detail: Optional[Dict[str, Any]] = None,
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

        ``detail``, when given, is filled with ``written`` (whether THIS call
        mutated the row) and, when it did not, the ``existing_status``: an
        idempotent True wrote nothing, and an instrument must not record it as
        a write. For an idempotent terminal row it also carries the phase,
        participant ids, and decoded resolution from that same follow-up read;
        a caller recovering an ambiguous remote commit can compare the exact
        winner without a second racy query. The return value is unchanged.

        Compare-and-set, as on `mark_awaiting_facilitation`: when
        ``expected_phase``, ``expected_updated_at``, ``expected_reviewer`` and/or
        ``expected_transcript_revision`` and/or
        ``expected_awaiting_facilitation`` are passed (the values a caller
        decided on), the write lands only if the live row still matches them.
        A message, phase move, reassignment, or newly raised human request since
        that read then refuses the write (False) instead of terminating the
        session on stale state. Omitted arguments add no predicate.

        ``clear_facilitation`` also sets ``awaiting_facilitation = false`` in
        the same statement. It is retained for call compatibility; every
        terminal write now clears the flag because a terminal session cannot
        still be awaiting a reviewer, including when a timed-out caller races
        the commit.
        """
        await self._ensure_pool()
        # Phase should match status - don't hardcode 'resolved' when status is 'failed'
        phase = "resolved" if status == "resolved" else status
        args: List[Any] = [status, phase, json.dumps(resolution), session_id]
        cas = ""
        if expected_phase is not _UNSET:
            args.append(expected_phase)
            cas += f" AND phase IS NOT DISTINCT FROM ${len(args)}"
        if expected_updated_at is not _UNSET:
            args.append(expected_updated_at)
            cas += f" AND updated_at IS NOT DISTINCT FROM ${len(args)}"
        if expected_reviewer is not _UNSET:
            args.append(expected_reviewer)
            cas += f" AND reviewer_agent_id IS NOT DISTINCT FROM ${len(args)}"
        if expected_transcript_revision is not _UNSET:
            args.append(expected_transcript_revision)
            cas += f" AND transcript_revision = ${len(args)}"
        if expected_awaiting_facilitation is not _UNSET:
            args.append(expected_awaiting_facilitation)
            cas += (
                " AND awaiting_facilitation IS NOT DISTINCT FROM "
                f"${len(args)}"
            )
        if expected_resolution_saga_revision is not _UNSET:
            args.append(expected_resolution_saga_revision)
            cas += f" AND resolution_saga_revision = ${len(args)}"
        saga_guard = """
                      AND NOT EXISTS (
                        SELECT 1
                        FROM coordination.session_resolution_sagas saga
                        WHERE saga.session_id = core.dialectic_sessions.session_id
                          AND saga.state IN ('reserved', 'paused_agent_applied',
                                             'both_agents_applied', 'reverting')
                      )
        """ if exclude_inflight_saga else ""
        authority_guard = ""
        if status == "resolved":
            args.extend([
                REVIEWED_WORK_AUTHORSHIP_SCHEMA,
                CANONICAL_AGENT_UUID_PATTERN,
                MAX_REVIEWED_WORK_AUTHOR_AGENT_IDS,
            ])
            schema_arg, uuid_arg, max_arg = range(len(args) - 2, len(args) + 1)
            authority_guard = f"""
              AND (
                paused_agent_state_json IS NULL
                OR paused_agent_state_json->'pause_effect_required'
                     IS DISTINCT FROM 'true'::jsonb
                OR reviewer_agent_id IS DISTINCT FROM paused_agent_id
              )
              AND (
                paused_agent_state_json IS NULL
                OR NOT (paused_agent_state_json ? '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}')
                OR (
                  jsonb_typeof(paused_agent_state_json -> '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}') = 'object'
                  AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},schema}}' = ${schema_arg}
                  AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},status}}' = 'declared'
                  AND paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},authorship_verified}}' = 'false'::jsonb
                  AND jsonb_typeof(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}') = 'string'
                  AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}' ~ ${uuid_arg}
                  AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}' = paused_agent_id
                  AND CASE
                    WHEN jsonb_typeof(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') = 'array'
                    THEN
                      jsonb_array_length(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') BETWEEN 1 AND ${max_arg}
                      AND NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') AS author(value)
                        WHERE jsonb_typeof(author.value) <> 'string'
                           OR (author.value #>> '{{}}') !~ ${uuid_arg}
                      )
                      AND jsonb_array_length(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') = (
                        SELECT count(DISTINCT author.value #>> '{{}}')
                        FROM jsonb_array_elements(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') AS author(value)
                      )
                      AND (
                        reviewer_agent_id = paused_agent_id
                        OR NOT (
                          paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}' ? reviewer_agent_id
                        )
                      )
                    ELSE FALSE
                  END
                )
              )
            """
        clear = ", awaiting_facilitation = false"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"""
                WITH terminal AS (
                    UPDATE core.dialectic_sessions
                    SET status = $1, phase = $2, resolution_json = $3,
                        updated_at = now(){clear}
                    WHERE session_id = $4
                      AND status NOT IN ('resolved', 'failed'){cas}{saga_guard}{authority_guard}
                    RETURNING session_id, paused_agent_id, reviewer_agent_id,
                              paused_agent_state_json, resolution_json,
                              {self._EFFECT_TS}
                ), prepared AS (
                    SELECT terminal.*,
                           CASE
                               WHEN COALESCE(
                                   paused_agent_state_json
                                       ? 'pause_effect_required',
                                   FALSE
                               )
                               THEN paused_agent_state_json
                                   ->'pause_effect_required' = 'true'::jsonb
                               ELSE FALSE
                           END AS effect_required,
                           NOT COALESCE(
                               paused_agent_state_json
                                   ? 'pause_effect_required',
                               FALSE
                           ) AS legacy_unbound
                    FROM terminal
                ), enqueued AS (
                    INSERT INTO coordination.dialectic_resolution_effects (
                        session_id, paused_agent_id, reviewer_agent_id,
                        effect_required, pause_token, resolution_payload_json,
                        state, execution_result_json, applied_at
                    )
                    SELECT session_id, paused_agent_id, reviewer_agent_id,
                           effect_required,
                           paused_agent_state_json->>'pause_token',
                           resolution_json,
                           CASE WHEN effect_required THEN 'pending' ELSE 'applied' END,
                           CASE WHEN effect_required THEN NULL ELSE
                               jsonb_build_object(
                                   'success', true,
                                   'no_op', true,
                                   'reason', CASE
                                       WHEN legacy_unbound
                                           THEN 'legacy_unbound_session'
                                       ELSE 'active_at_session_open'
                                   END
                               )
                           END,
                           CASE WHEN effect_required THEN NULL ELSE clock_timestamp() END
                    FROM prepared
                    WHERE $1 = 'resolved'
                    RETURNING session_id
                )
                SELECT terminal.session_id, terminal.effect_ts
                FROM terminal
                LEFT JOIN enqueued USING (session_id)
            """, *args)
            if row is not None:
                logger.info(f"Resolved session {session_id[:16]}... with status {status}")
                self._record_effect(detail, row)
                return True
            # No row written: inspect why (idempotent replay vs conflict vs missing).
            existing = await conn.fetchrow(
                """SELECT status, phase, paused_agent_id, reviewer_agent_id,
                          paused_agent_state_json, resolution_json,
                          transcript_revision
                   FROM core.dialectic_sessions WHERE session_id = $1""",
                session_id,
            )
            if detail is not None:
                detail["written"] = False
                detail["existing_status"] = existing["status"] if existing else None
            if existing is None:
                logger.warning(f"resolve_session: {session_id[:16]}... not found")
                return False
            if existing["status"] == status:
                if detail is not None:
                    stored_resolution = existing.get("resolution_json")
                    if isinstance(stored_resolution, str):
                        try:
                            stored_resolution = json.loads(stored_resolution)
                        except (TypeError, json.JSONDecodeError):
                            stored_resolution = None
                    detail.update(
                        existing_phase=existing.get("phase"),
                        existing_paused_agent_id=existing.get("paused_agent_id"),
                        existing_reviewer_agent_id=existing.get(
                            "reviewer_agent_id"
                        ),
                        existing_resolution=stored_resolution,
                        existing_transcript_revision=existing.get(
                            "transcript_revision"
                        ),
                    )
                logger.info(
                    f"resolve_session: {session_id[:16]}... already {status} "
                    "(idempotent no-op, overwrite prevented)"
                )
                return True
            if status == "resolved" and existing["status"] not in (
                "resolved", "failed"
            ):
                paused_state = existing.get("paused_agent_state_json")
                if isinstance(paused_state, str):
                    try:
                        paused_state = json.loads(paused_state)
                    except (TypeError, json.JSONDecodeError):
                        paused_state = None
                if (
                    isinstance(paused_state, dict)
                    and paused_state.get("pause_effect_required") is True
                    and existing.get("reviewer_agent_id")
                    == existing.get("paused_agent_id")
                ):
                    if detail is not None:
                        detail["refused"] = "self_review_not_authorizing"
                    logger.warning(
                        "resolve_session: %s pause-bound self-review refused",
                        session_id[:16],
                    )
                    return False
                authorship = read_reviewed_work_authorship(existing)
                if authorship.is_invalid or authorship.recuses(
                    existing.get("reviewer_agent_id"),
                    paused_agent_id=existing.get("paused_agent_id"),
                ):
                    if detail is not None:
                        detail["refused"] = (
                            "invalid_review_authorship"
                            if authorship.is_invalid
                            else "reviewer_recused"
                        )
                    logger.warning(
                        "resolve_session: %s reviewed-work authority refused",
                        session_id[:16],
                    )
                    return False
            if existing["status"] not in ("resolved", "failed"):
                logger.info(
                    f"resolve_session: {session_id[:16]}... moved since the caller "
                    "read it (compare-and-set refused); not terminated"
                )
                return False
            logger.warning(
                f"resolve_session: {session_id[:16]}... already terminal as "
                f"{existing['status']!r}; refused overwrite to {status!r}"
            )
            return False

    async def get_resolution_effect(
        self, session_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the durable runtime-effect record for a resolved session."""
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                SELECT session_id, paused_agent_id, reviewer_agent_id,
                       effect_required, pause_token, resolution_payload_json,
                       state, owner_token,
                       attempt_count, last_error, execution_result_json,
                       claimed_at, applied_at, uncertain_at, created_at, updated_at
                FROM coordination.dialectic_resolution_effects
                WHERE session_id = $1
            """, session_id)
        if row is None:
            return None
        result = dict(row)
        for key in ("resolution_payload_json", "execution_result_json"):
            value = result.get(key)
            if isinstance(value, str):
                try:
                    result[key] = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    result[key] = None
        return result

    async def claim_resolution_effect(
        self,
        session_id: str,
        owner_token: str,
        *,
        paused_agent_id: str,
        reviewer_agent_id: str,
        effect_required: bool,
        pause_token: Optional[str],
        resolution: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Claim one pending effect, returning the claim or the live winner.

        The participant and payload predicates bind execution to the exact
        terminal decision the caller inspected.  Losing a concurrent claim is
        not an error: the returned row has ``claimed=False`` and exposes the
        state that now owns reconciliation.
        """
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE coordination.dialectic_resolution_effects AS e
                    SET state = 'applying', owner_token = $2,
                        attempt_count = attempt_count + 1,
                        claimed_at = clock_timestamp(), updated_at = clock_timestamp(),
                        last_error = NULL, uncertain_at = NULL
                    WHERE e.session_id = $1
                      AND e.state = 'pending'
                      AND e.paused_agent_id = $3
                      AND e.reviewer_agent_id = $4
                      AND e.resolution_payload_json = ($5::text)::jsonb
                      AND e.pause_token IS NOT DISTINCT FROM $6
                      AND e.effect_required = $7
                      AND EXISTS (
                          SELECT 1
                          FROM core.dialectic_sessions s
                          WHERE s.session_id = e.session_id
                            AND s.status = 'resolved'
                            AND s.phase = 'resolved'
                            AND s.paused_agent_id = $3
                            AND s.reviewer_agent_id = $4
                            AND s.resolution_json = ($5::text)::jsonb
                            AND s.paused_agent_state_json->>'pause_token'
                                IS NOT DISTINCT FROM $6
                            AND COALESCE(
                                s.paused_agent_state_json->'pause_effect_required'
                                    = 'true'::jsonb,
                                TRUE
                            ) = $7
                      )
                    RETURNING *, true AS claimed
                """, session_id, owner_token, paused_agent_id, reviewer_agent_id,
                    json.dumps(resolution), pause_token, effect_required)
                if row is None:
                    # A second statement gets a fresh READ COMMITTED snapshot.
                    # A losing concurrent claimant therefore observes the
                    # winner's applying/applied state, never the pre-wait
                    # pending row from the first statement's snapshot.
                    row = await conn.fetchrow("""
                        SELECT e.*, false AS claimed
                        FROM coordination.dialectic_resolution_effects e
                        WHERE e.session_id = $1
                    """, session_id)
        if row is None:
            return None
        result = dict(row)
        for key in ("resolution_payload_json", "execution_result_json"):
            value = result.get(key)
            if isinstance(value, str):
                try:
                    result[key] = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    result[key] = None
        return result

    async def complete_resolution_effect(
        self,
        session_id: str,
        owner_token: str,
        execution_result: Dict[str, Any],
    ) -> bool:
        """Mark the effect applied, but only for its current claimant."""
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                UPDATE coordination.dialectic_resolution_effects
                SET state = 'applied',
                    execution_result_json = ($3::text)::jsonb,
                    applied_at = clock_timestamp(), updated_at = clock_timestamp(),
                    last_error = NULL, uncertain_at = NULL
                WHERE session_id = $1 AND state = 'applying' AND owner_token = $2
                RETURNING session_id
            """, session_id, owner_token, json.dumps(execution_result))
        return row is not None

    async def fail_resolution_effect(
        self,
        session_id: str,
        owner_token: str,
        error: str,
        *,
        execution_result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record a claimed effect whose final application is uncertain.

        The worker may already have landed one or more non-idempotent runtime
        mutations.  The row therefore enters ``unknown_partial`` and is never
        automatically retried.
        """
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                UPDATE coordination.dialectic_resolution_effects
                SET state = 'unknown_partial', last_error = $3,
                    execution_result_json = CASE
                        WHEN $4::text IS NULL THEN execution_result_json
                        ELSE ($4::text)::jsonb
                    END,
                    uncertain_at = clock_timestamp(), updated_at = clock_timestamp()
                WHERE session_id = $1 AND state = 'applying' AND owner_token = $2
                RETURNING session_id
            """, session_id, owner_token, error,
                json.dumps(execution_result) if execution_result is not None else None)
        return row is not None

    async def acknowledge_resolution_effect(
        self,
        session_id: str,
        reason: str,
        *,
        expected_updated_at: Any,
    ) -> bool:
        """Close an ambiguous effect after an operator reconciles it manually.

        This never executes the effect. It is deliberately limited to states
        where automatic replay is unsafe because one or more sub-effects may
        already have landed. ``expected_updated_at`` binds the acknowledgement
        to the exact unknown-partial record the operator inspected. Original
        execution evidence is retained; reconciliation metadata is additive.
        """
        await self._ensure_pool()
        result = {
            "manually_reconciled": True,
            "reconciliation_reason": reason,
            "operator_reconciliation": {
                "success": True,
                "reason": reason,
            },
        }
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                UPDATE coordination.dialectic_resolution_effects
                SET state = 'applied',
                    execution_result_json =
                        COALESCE(execution_result_json, '{}'::jsonb)
                        || ($2::text)::jsonb,
                    applied_at = clock_timestamp(), updated_at = clock_timestamp()
                WHERE session_id = $1
                  AND state = 'unknown_partial'
                  AND updated_at IS NOT DISTINCT FROM $3
                RETURNING session_id
            """, session_id, json.dumps(result), expected_updated_at)
        return row is not None

    async def quarantine_resolution_effect(
        self,
        session_id: str,
        owner_token: str,
        reason: str,
    ) -> bool:
        """Fence an abandoned claimant after an operator verifies it stopped.

        This does not execute, retry, or acknowledge the runtime effect.  The
        exact owner token makes the transition a compare-and-set against the
        claim the operator inspected; the resulting ``unknown_partial`` row
        still requires live-state repair before it can be acknowledged.
        """
        await self._ensure_pool()
        async with compatible_acquire(self._pool) as conn:
            row = await conn.fetchrow("""
                UPDATE coordination.dialectic_resolution_effects
                SET state = 'unknown_partial',
                    last_error = $3,
                    uncertain_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                WHERE session_id = $1
                  AND state = 'applying'
                  AND owner_token = $2
                RETURNING session_id
            """, session_id, owner_token, reason)
        return row is not None

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

        ⛔Fail-open is correct for a **write gate** and wrong for an
        **instrument**: a probe that reports "no saga" when it could not look
        manufactures exactly the clean zero the measurement-authority rule
        forbids. `probe_inflight_saga` below is the honest form, and the
        sweeper's post-write instrument uses `probe_saga_since`, which also
        matches sagas that already committed; this stays boolean because its
        caller is deciding whether to skip a session.
        """
        return await self.probe_inflight_saga(session_id) is True

    async def probe_inflight_saga(self, session_id: str) -> Optional[bool]:
        """`has_inflight_saga` without the fail-open, for measurement.

        Returns True (a non-terminal saga exists), False (none exists, and the
        query ran), or **None** (the query could not be answered). The third
        state is the whole point: a caller counting overlaps must be able to
        record "not observed" separately from "observed absent", because
        collapsing them is how an outage becomes evidence of a collision-free
        system.
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
        except Exception as e:
            logger.debug(f"has_inflight_saga check failed for {session_id[:16]}...: {e}")
            return None

    async def probe_saga_since(
        self,
        session_id: str,
        since: Optional[datetime],
    ) -> Optional[Dict[str, Any]]:
        """Time-correlated saga probe for measurement (instrument v2).

        Returns the newest saga on ``session_id`` that was created at or after
        ``since`` in ANY state -- ``pg_committed`` and ``reverted`` included --
        or that is still non-terminal whenever it was created. Returns ``{}``
        when the query ran and found none, and **None** when it could not be
        answered (the same three-state contract as `probe_inflight_saga`).

        Why not a state match: sagas go from ``created_at`` to
        ``pg_committed_at`` in p50 6.5 ms / p99 84 ms (Wave 3 gate council
        2026-09-27, finding B1), so a saga that starts after the sweeper's
        early check and commits before this probe is invisible to any check
        that looks for non-terminal states. Creation time is what places a
        saga inside the interval being measured; its state only says how far
        it got. ``since=None`` degrades to the non-terminal match alone.

        Read-only. Never raises.
        """
        try:
            await self._ensure_pool()
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT saga_id, state, created_at, pg_committed_at, reverted_at
                    FROM coordination.session_resolution_sagas
                    WHERE session_id = $1
                      AND (
                            state IN ('reserved', 'paused_agent_applied',
                                      'both_agents_applied', 'reverting')
                         OR ($2::timestamptz IS NOT NULL AND created_at >= $2::timestamptz)
                      )
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    session_id,
                    since,
                )
                return dict(row) if row is not None else {}
        except Exception as e:
            logger.debug(f"probe_saga_since failed for {session_id[:16]}...: {e}")
            return None

    async def get_session_terminal_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Read a row's ``status`` and ``resolution_json->>'reason'``.

        For attribution only (who made a session terminal, and what reason it
        recorded). Returns None when the row is missing. Raises on a failed
        read so a caller can tell "no reason recorded" from "could not read".
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._REFUSAL_WINNER_SQL, session_id)
            if row is None:
                return None
            return {"status": row["status"], "reason": row["reason"]}

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
        *,
        expected_phase: Any = _UNSET,
        expected_reviewer: Any = _UNSET,
        expected_transcript_revision: Any = _UNSET,
        expected_awaiting_facilitation: Any = _UNSET,
        next_phase: Any = _UNSET,
        next_synthesis_round: Any = _UNSET,
        next_reviewer: Any = _UNSET,
        next_awaiting_facilitation: Any = _UNSET,
        next_status: Any = _UNSET,
        reopen_failed_facilitation: bool = False,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Add a message to a session.

        Two statements in ONE transaction: a guarded session-row revision/state
        advance, then the message INSERT. The advance refuses terminal rows and
        can compare the phase, reviewer, and transcript revision the caller
        loaded. Protocol callers pass the next phase/round (and, for an atomic
        reviewer handoff, the next reviewer) so another process can never load
        a newly committed message with the old protocol phase. Its row lock
        serializes concurrent appends and terminal writes; only the winner
        inserts, and a terminal writer can bind the returned revision.

        The bump runs FIRST so this transaction holds the session row's update
        lock before the message exists. The INSERT alone would take only the
        foreign key's KEY SHARE lock, which a non-key UPDATE does not wait on,
        so a concurrent ``mark_awaiting_facilitation`` could still pass its
        compare-and-set and flag a session that just progressed. Bumping first
        makes that write wait for this commit and then re-check its predicate
        against the new ``updated_at``, which refuses it.

        ``detail``, when given, receives ``message_ts`` (the message row's own
        timestamp), ``effect_ts`` (the database clock at the session-row bump),
        and ``transcript_revision`` (the new durable frontier).
        """
        await self._ensure_pool()
        args: List[Any] = [session_id]
        cas = ""
        if expected_phase is not _UNSET:
            args.append(expected_phase)
            cas += f" AND phase IS NOT DISTINCT FROM ${len(args)}"
        if expected_reviewer is not _UNSET:
            args.append(expected_reviewer)
            cas += f" AND reviewer_agent_id IS NOT DISTINCT FROM ${len(args)}"
        if expected_transcript_revision is not _UNSET:
            args.append(expected_transcript_revision)
            cas += f" AND transcript_revision = ${len(args)}"
        if expected_awaiting_facilitation is not _UNSET:
            args.append(expected_awaiting_facilitation)
            cas += (
                " AND awaiting_facilitation IS NOT DISTINCT FROM "
                f"${len(args)}"
            )
        updates = []
        if next_phase is not _UNSET:
            args.append(next_phase)
            updates.append(f"phase = ${len(args)}")
        if next_synthesis_round is not _UNSET:
            args.append(next_synthesis_round)
            updates.append(f"synthesis_round = ${len(args)}")
        reviewer_authority_guard = ""
        if next_reviewer is not _UNSET:
            args.append(next_reviewer)
            reviewer_arg = len(args)
            updates.append(f"reviewer_agent_id = ${reviewer_arg}")
            args.extend(
                [
                    REVIEWED_WORK_AUTHORSHIP_SCHEMA,
                    CANONICAL_AGENT_UUID_PATTERN,
                    MAX_REVIEWED_WORK_AUTHOR_AGENT_IDS,
                ]
            )
            schema_arg, uuid_arg, max_arg = range(len(args) - 2, len(args) + 1)
            reviewer_authority_guard = f"""
                      AND (
                        paused_agent_state_json IS NULL
                        OR NOT (paused_agent_state_json ? '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}')
                        OR (
                          jsonb_typeof(paused_agent_state_json -> '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}') = 'object'
                          AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},schema}}' = ${schema_arg}
                          AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},status}}' = 'declared'
                          AND paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},authorship_verified}}' = 'false'::jsonb
                          AND jsonb_typeof(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}') = 'string'
                          AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}' ~ ${uuid_arg}
                          AND paused_agent_state_json #>> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},declared_by_agent_id}}' = paused_agent_id
                          AND CASE
                            WHEN jsonb_typeof(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') = 'array'
                            THEN
                              jsonb_array_length(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') BETWEEN 1 AND ${max_arg}
                              AND NOT EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') AS author(value)
                                WHERE jsonb_typeof(author.value) <> 'string'
                                   OR (author.value #>> '{{}}') !~ ${uuid_arg}
                              )
                              AND jsonb_array_length(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') = (
                                SELECT count(DISTINCT author.value #>> '{{}}')
                                FROM jsonb_array_elements(paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}') AS author(value)
                              )
                              AND (
                                ${reviewer_arg} = paused_agent_id
                                OR NOT (
                                  paused_agent_state_json #> '{{{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}' ? ${reviewer_arg}
                                )
                              )
                            ELSE FALSE
                          END
                        )
                      )
            """
        if next_awaiting_facilitation is not _UNSET:
            args.append(next_awaiting_facilitation)
            updates.append(f"awaiting_facilitation = ${len(args)}")
        if next_status is not _UNSET:
            args.append(next_status)
            updates.append(f"status = ${len(args)}")
        optional_update_sql = "".join(
            f",\n                        {update}" for update in updates
        )
        status_guard = (
            "status = 'failed' AND awaiting_facilitation = true"
            if reopen_failed_facilitation
            else "status NOT IN ('resolved', 'failed', 'escalated')"
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                bump = await conn.fetchrow(f"""
                    UPDATE core.dialectic_sessions
                    SET updated_at = now(),
                        transcript_revision = transcript_revision + 1
                        {optional_update_sql}
                    WHERE session_id = $1
                      AND {status_guard}
                      {cas}
                      {reviewer_authority_guard}
                    RETURNING transcript_revision, {self._EFFECT_TS}
                """, *args)

                if bump is None:
                    if detail is not None:
                        detail["written"] = False
                    return 0

                row = await conn.fetchrow("""
                    INSERT INTO core.dialectic_messages (
                        session_id, agent_id, message_type,
                        root_cause, proposed_conditions, reasoning,
                        observed_metrics, concerns, agrees, signature
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING message_id, timestamp
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

                if detail is not None:
                    try:
                        detail["message_ts"] = row["timestamp"] if row else None
                        detail["effect_ts"] = bump["effect_ts"] if bump is not None else None
                        detail["transcript_revision"] = (
                            bump["transcript_revision"]
                            if bump is not None else None
                        )
                        detail["written"] = row is not None
                    except Exception:  # pragma: no cover
                        pass

                return row["message_id"] if row else 0

    async def add_bounded_message(
        self,
        session_id: str,
        agent_id: str,
        message_type: str,
        max_of_type: int,
        root_cause: str = None,
        proposed_conditions: List[str] = None,
        reasoning: str = None,
        observed_metrics: Dict = None,
        concerns: List[str] = None,
        metrics_from_session: Optional[Callable[[Optional[Dict[str, Any]]], Dict]] = None,
    ) -> Optional[int]:
        """Insert a record-only message unless the session already holds
        ``max_of_type`` of that type; return its id, or None when full.

        The count and the insert run in one transaction under a per-session
        advisory lock, so concurrent filers cannot each see room for one more
        and overshoot the bound. The session row is not touched: a record-only
        message is not protocol activity (add_message refreshes updated_at, which the sweeper reads as activity).

        ``metrics_from_session``, when given, builds ``observed_metrics`` from
        the session row (``phase``, ``paused_agent_id``, ``reviewer_agent_id``,
        or None when there is no row) read ``FOR SHARE`` in the same
        transaction, so what the message says about the session is its state
        when the message was filed, not an earlier snapshot a phase move or a
        reassignment has since overtaken. The share lock holds those writers
        off until the insert commits; it takes no write lock of its own.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"dialectic-bounded:{message_type}:{session_id}",
                )
                held = await conn.fetchval(
                    "SELECT count(*) FROM core.dialectic_messages "
                    "WHERE session_id = $1 AND message_type = $2",
                    session_id, message_type,
                )
                if held >= max_of_type:
                    return None
                if metrics_from_session is not None:
                    state = await conn.fetchrow(
                        "SELECT phase, paused_agent_id, reviewer_agent_id "
                        "FROM core.dialectic_sessions WHERE session_id = $1 FOR SHARE",
                        session_id,
                    )
                    observed_metrics = metrics_from_session(dict(state) if state else None)
                row = await conn.fetchrow("""
                    INSERT INTO core.dialectic_messages (
                        session_id, agent_id, message_type,
                        root_cause, proposed_conditions, reasoning,
                        observed_metrics, concerns, agrees, signature
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, NULL)
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
                )
                return row["message_id"] if row else None

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

    async def get_active_sessions(
        self,
        limit: int = 100,
        *,
        least_recently_updated_first: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get active sessions in caller-selected maintenance order.

        Interactive/startup callers retain newest-created-first ordering. The
        stuck-session sweeper opts into least-recently-updated-first so a full
        batch cannot continually hide the oldest, most likely stuck rows behind
        newer active sessions.
        """
        await self._ensure_pool()
        order_by = (
            "COALESCE(updated_at, created_at) ASC, created_at ASC"
            if least_recently_updated_first
            else "created_at DESC"
        )
        async with self._pool.acquire() as conn:
            read_at = datetime.now(timezone.utc)
            rows = await conn.fetch(f"""
                SELECT * FROM core.dialectic_sessions
                WHERE status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                ORDER BY {order_by}
                LIMIT $1
            """, limit)
            out = [dict(row) for row in rows]
            note_session_read([r.get("session_id") for r in out], read_at)
            return out

    async def get_sessions_awaiting_reviewer(self) -> List[Dict[str, Any]]:
        """Get sessions that need a reviewer assigned."""
        read_at = datetime.now(timezone.utc)
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM core.dialectic_sessions
                WHERE status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                AND (reviewer_agent_id IS NULL OR reviewer_agent_id = '')
                ORDER BY created_at ASC
            """)
            out = [dict(row) for row in rows]
            note_session_read([r.get("session_id") for r in out], read_at)
            return out

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
            RESOLVED_EFFECT_PENDING,
            UNRESOLVED_AWAITING_FACILITATION,
            classify_outcome,
            resolution_rate,
        )

        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            # `standing_rejection` is the transcript-derived signal
            # classify_outcome prefers: the session's most recent reviewer
            # synthesis, unsuperseded, carrying agrees=false. Derived here in
            # SQL rather than by loading transcripts, so this stays one query.
            #
            # ⛔The LEFT JOIN on core.agents is deliberate and load-bearing.
            # Sessions exist whose paused_agent_id has no agents row, and an
            # inner join would drop real work while looking like a filter.
            rows = await conn.fetch("""
                SELECT s.status,
                       coalesce(s.awaiting_facilitation, false) AS awaiting_facilitation,
                       a.label AS paused_agent_label,
                       e.state AS resolution_effect_state,
                       -- ⛔Three-valued ON PURPOSE. `r.agrees IS FALSE` would
                       -- collapse "the reviewer never synthesised" into
                       -- "the reviewer agreed", yielding FALSE for both and
                       -- making classify_outcome's documented
                       -- awaiting_facilitation fallback unreachable from its
                       -- only caller. NULL means "no transcript answer", which
                       -- is exactly when the flag should still be consulted.
                       CASE WHEN r.agrees IS NULL THEN NULL
                            ELSE (r.agrees IS FALSE)
                       END AS standing_rejection
                FROM core.dialectic_sessions s
                LEFT JOIN core.agents a ON a.id = s.paused_agent_id
                LEFT JOIN coordination.dialectic_resolution_effects e
                  ON e.session_id = s.session_id
                LEFT JOIN LATERAL (
                    SELECT dm.agrees
                    FROM core.dialectic_messages dm
                    WHERE dm.session_id = s.session_id
                      AND dm.message_type = 'synthesis'
                      AND dm.agent_id = s.reviewer_agent_id
                    ORDER BY dm.timestamp DESC, dm.message_id DESC
                    LIMIT 1
                ) r ON true
                WHERE s.created_at >= now() - interval '1 day' * $1
            """, window_days)

        counts: Dict[str, int] = {
            RESOLVED: 0,
            RESOLVED_EFFECT_PENDING: 0,
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
                standing_rejection=row["standing_rejection"],
                resolution_effect_state=row["resolution_effect_state"],
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
# Every write helper below records an attempt/response pair
# (`dialectic_session_write`, see src/dialectic_session_writes.py) so the Wave 3
# collision report can see every Python-initiated session write, including the
# ones that leave no other trace. The record is taken here, at the chokepoint,
# so no call site can forget it.


def _winner_fields(winner: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not winner:
        return {}
    return {"winner_status": winner.get("winner_status"),
            "winner_reason": winner.get("winner_reason")}


async def create_session_async(**kwargs) -> Dict[str, Any]:
    async with record_session_write(
        kind=KIND_CREATE, session_id=kwargs.get("session_id"),
    ) as rec:
        db = await get_dialectic_db()
        detail: Dict[str, Any] = {}
        result = await db.create_session(**kwargs, detail=detail)
        # A duplicate id answers a truthy dict with created=False: no write.
        created = result.get("created") if isinstance(result, dict) else None
        rec.respond(outcome=("not_written" if created is False
                             else written_outcome(result)),
                    effect_ts=detail.get("effect_ts"))
        return result


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


async def probe_inflight_saga_async(session_id: str) -> Optional[bool]:
    db = await get_dialectic_db()
    return await db.probe_inflight_saga(session_id)


async def probe_saga_since_async(
    session_id: str, since: Optional[datetime]
) -> Optional[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.probe_saga_since(session_id, since)


async def get_session_terminal_state_async(session_id: str) -> Optional[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_session_terminal_state(session_id)


async def has_recently_reviewed_async(reviewer_id: str, paused_agent_id: str, hours: int = 24) -> bool:
    db = await get_dialectic_db()
    return await db.has_recently_reviewed(reviewer_id, paused_agent_id, hours)


async def add_message_async(**kwargs) -> int:
    # A message insert also bumps the session's updated_at -- the sweeper's
    # staleness clock -- so it is a session write by the Wave 3 rule and is
    # recorded like the others, with the database-clock time of that bump.
    async with record_session_write(
        kind=KIND_MESSAGE, session_id=kwargs.get("session_id"),
        requested=kwargs.get("message_type"),
    ) as rec:
        db = await get_dialectic_db()
        caller_detail = kwargs.pop("detail", None)
        detail: Dict[str, Any] = caller_detail if caller_detail is not None else {}
        result = await db.add_message(**kwargs, detail=detail)
        rec.respond(outcome="written" if result else "not_written",
                    effect_ts=detail.get("effect_ts"), message_ts=detail.get("message_ts"))
        return result


async def _recorded_write(rec, write, *, winner: Optional[Dict[str, Any]] = None,
                          outcome=None) -> Any:
    """Run one DB write with a ``detail`` out-param and record its response.

    The response carries ``effect_ts`` -- the database clock read by the write
    statement itself (`DialecticDB._EFFECT_TS`). When the caller passed a
    ``winner`` dict (the sweeper does), the same ``effect_ts`` is copied into
    it, so the sweeper's own per-write row carries the database commit time
    too. The helper's return value is passed through unchanged.
    """
    detail: Dict[str, Any] = {}
    result = await write(detail)
    if winner is not None:
        winner["effect_ts"] = detail.get("effect_ts")
    rec.respond(outcome=outcome(result, detail) if outcome else written_outcome(result),
                effect_ts=detail.get("effect_ts"), **_winner_fields(winner))
    return result


async def add_bounded_message_async(**kwargs) -> Optional[int]:
    db = await get_dialectic_db()
    return await db.add_bounded_message(**kwargs)


async def update_session_phase_async(
    session_id: str, phase: str, synthesis_round: Optional[int] = None
) -> bool:
    async with record_session_write(
        kind=KIND_PHASE, session_id=session_id, requested=phase,
    ) as rec:
        db = await get_dialectic_db()
        return await _recorded_write(rec, lambda d: db.update_session_phase(
            session_id, phase, synthesis_round, detail=d))


async def reopen_session_async(session_id: str, phase: str) -> bool:
    async with record_session_write(
        kind=KIND_REOPEN, session_id=session_id, requested=phase,
    ) as rec:
        db = await get_dialectic_db()
        return await _recorded_write(rec, lambda d: db.reopen_session(
            session_id, phase, detail=d))


async def update_session_reviewer_async(
    session_id: str,
    reviewer_agent_id: str,
    *,
    claim_if_unassigned: bool = False,
    expected_phase: Any = _UNSET,
    expected_updated_at: Any = _UNSET,
    expected_reviewer: Any = _UNSET,
    expected_transcript_revision: Any = _UNSET,
    expected_awaiting_facilitation: Any = _UNSET,
    expected_resolution_saga_revision: Any = _UNSET,
    exclude_inflight_saga: bool = False,
    clear_facilitation: bool = False,
    winner: Optional[Dict[str, Any]] = None,
) -> bool:
    async with record_session_write(
        kind=KIND_REVIEWER, session_id=session_id, requested=reviewer_agent_id,
    ) as rec:
        db = await get_dialectic_db()
        guards: Dict[str, Any] = {}
        for name, value in (
            ("expected_phase", expected_phase),
            ("expected_updated_at", expected_updated_at),
            ("expected_reviewer", expected_reviewer),
            ("expected_transcript_revision", expected_transcript_revision),
            ("expected_awaiting_facilitation", expected_awaiting_facilitation),
            (
                "expected_resolution_saga_revision",
                expected_resolution_saga_revision,
            ),
        ):
            if value is not _UNSET:
                guards[name] = value
        return await _recorded_write(rec, lambda d: db.update_session_reviewer(
            session_id, reviewer_agent_id,
            claim_if_unassigned=claim_if_unassigned,
            exclude_inflight_saga=exclude_inflight_saga,
            clear_facilitation=clear_facilitation,
            winner=winner, detail=d, **guards), winner=winner)


async def update_session_status_async(
    session_id: str,
    status: str,
    *,
    winner: Optional[Dict[str, Any]] = None,
) -> bool:
    async with record_session_write(
        kind=KIND_STATUS, session_id=session_id, requested=status,
    ) as rec:
        db = await get_dialectic_db()
        return await _recorded_write(rec, lambda d: db.update_session_status(
            session_id, status, winner=winner, detail=d), winner=winner)


async def mark_awaiting_facilitation_async(
    session_id: str,
    *,
    expected_phase: Any = _UNSET,
    expected_updated_at: Any = _UNSET,
    expected_reviewer: Any = _UNSET,
    expected_transcript_revision: Any = _UNSET,
    expected_resolution_saga_revision: Any = _UNSET,
    exclude_inflight_saga: bool = False,
    touch_updated_at: bool = False,
    winner: Optional[Dict[str, Any]] = None,
) -> bool:
    async with record_session_write(
        kind=KIND_FACILITATION, session_id=session_id, requested=True,
    ) as rec:
        db = await get_dialectic_db()
        return await _recorded_write(rec, lambda d: db.mark_awaiting_facilitation(
            session_id, expected_phase=expected_phase,
            expected_updated_at=expected_updated_at,
            expected_reviewer=expected_reviewer,
            expected_transcript_revision=expected_transcript_revision,
            expected_resolution_saga_revision=(
                expected_resolution_saga_revision
            ),
            exclude_inflight_saga=exclude_inflight_saga,
            touch_updated_at=touch_updated_at,
            winner=winner, detail=d), winner=winner)


async def update_session_awaiting_facilitation_async(
    session_id: str,
    awaiting: bool,
    *,
    expected_phase: Any = _UNSET,
    expected_reviewer: Any = _UNSET,
    expected_transcript_revision: Any = _UNSET,
    expected_awaiting_facilitation: Any = _UNSET,
    expected_resolution_saga_revision: Any = _UNSET,
    exclude_inflight_saga: bool = False,
    live_only: bool = False,
) -> bool:
    async with record_session_write(
        kind=KIND_FACILITATION, session_id=session_id, requested=bool(awaiting),
    ) as rec:
        db = await get_dialectic_db()
        return await _recorded_write(rec, lambda d: db.update_session_awaiting_facilitation(
            session_id, awaiting,
            expected_phase=expected_phase,
            expected_reviewer=expected_reviewer,
            expected_transcript_revision=expected_transcript_revision,
            expected_awaiting_facilitation=expected_awaiting_facilitation,
            expected_resolution_saga_revision=(
                expected_resolution_saga_revision
            ),
            exclude_inflight_saga=exclude_inflight_saga,
            live_only=live_only,
            detail=d))


async def resolve_session_async(
    session_id: str,
    resolution: Dict[str, Any],
    status: str = "resolved",
    *,
    winner: Optional[Dict[str, Any]] = None,
    **expected: Any,
) -> bool:
    """Guarded terminal write (`DialecticDB.resolve_session`), recorded.

    ``expected`` passes the compare-and-set predicates and
    ``clear_facilitation`` through (see `DialecticDB.resolve_session`).

    ``winner`` selects the sweeper's answer, as on the sweeper's other guarded
    writes: when it is passed, the return is True only when THIS call wrote the
    row, and a refusal fills it with the status found. Without it the return is
    `resolve_session`'s, which is also True for a row already in the requested
    state (an idempotent replay); for the sweeper that would count another
    writer's reap as its own.
    """
    async with record_session_write(
        kind=KIND_RESOLVE, session_id=session_id, requested=status,
    ) as rec:
        db = await get_dialectic_db()
        detail: Dict[str, Any] = {}
        result = await db.resolve_session(session_id, resolution, status, detail=detail,
                                          **expected)
        # An idempotent True (already in the requested state) wrote nothing.
        if detail.get("written") is False:
            outcome = "no_op" if result else "not_written"
        else:
            outcome = written_outcome(result)
        if winner is not None:
            written = detail.get("written") is True
            winner["effect_ts"] = detail.get("effect_ts")
            if not written:
                existing = detail.get("existing_status")
                winner.update(winner_status=existing, winner_reason=None,
                              row_missing=existing is None)
                terminal_snapshot_fields = {
                    "winner_phase": "existing_phase",
                    "winner_paused_agent_id": "existing_paused_agent_id",
                    "winner_reviewer_agent_id": "existing_reviewer_agent_id",
                    "winner_resolution": "existing_resolution",
                    "winner_transcript_revision": (
                        "existing_transcript_revision"
                    ),
                }
                for winner_key, detail_key in terminal_snapshot_fields.items():
                    if detail_key in detail:
                        winner[winner_key] = detail[detail_key]
            result = written
        rec.respond(outcome=outcome, existing_status=detail.get("existing_status"),
                    effect_ts=detail.get("effect_ts"),
                    reason=(resolution or {}).get("reason")
                    if isinstance(resolution, dict) else None,
                    **(_winner_fields(winner) if winner is not None and not result else {}))
        return result


async def get_resolution_effect_async(
    session_id: str,
) -> Optional[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_resolution_effect(session_id)


async def claim_resolution_effect_async(
    session_id: str,
    owner_token: str,
    *,
    paused_agent_id: str,
    reviewer_agent_id: str,
    effect_required: bool,
    pause_token: Optional[str],
    resolution: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.claim_resolution_effect(
        session_id,
        owner_token,
        paused_agent_id=paused_agent_id,
        reviewer_agent_id=reviewer_agent_id,
        effect_required=effect_required,
        pause_token=pause_token,
        resolution=resolution,
    )


async def complete_resolution_effect_async(
    session_id: str,
    owner_token: str,
    execution_result: Dict[str, Any],
) -> bool:
    db = await get_dialectic_db()
    return await db.complete_resolution_effect(
        session_id, owner_token, execution_result
    )


async def fail_resolution_effect_async(
    session_id: str,
    owner_token: str,
    error: str,
    *,
    execution_result: Optional[Dict[str, Any]] = None,
) -> bool:
    db = await get_dialectic_db()
    return await db.fail_resolution_effect(
        session_id,
        owner_token,
        error,
        execution_result=execution_result,
    )


async def acknowledge_resolution_effect_async(
    session_id: str,
    reason: str,
    *,
    expected_updated_at: Any,
) -> bool:
    db = await get_dialectic_db()
    return await db.acknowledge_resolution_effect(
        session_id,
        reason,
        expected_updated_at=expected_updated_at,
    )


async def quarantine_resolution_effect_async(
    session_id: str,
    owner_token: str,
    reason: str,
) -> bool:
    db = await get_dialectic_db()
    return await db.quarantine_resolution_effect(
        session_id, owner_token, reason
    )


async def get_active_sessions_async(
    limit: int = 100,
    *,
    least_recently_updated_first: bool = False,
) -> List[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_active_sessions(
        limit,
        least_recently_updated_first=least_recently_updated_first,
    )


async def get_sessions_awaiting_reviewer_async() -> List[Dict[str, Any]]:
    db = await get_dialectic_db()
    return await db.get_sessions_awaiting_reviewer()
