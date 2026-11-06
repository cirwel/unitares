"""
Query, resolution-effect, saga-probe, and analytics helpers for DialecticDB.

Extracted from src/dialectic_db.py to keep session-row writer logic and
read/query/effect helpers modular. All methods and module-level async
functions remain re-exported on src.dialectic_db.
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.db.acquire_compat import compatible_acquire as _default_compatible_acquire
from src.dialectic_session_writes import note_session_read as _default_note_session_read
from src.logging_utils import get_logger

logger = get_logger(__name__)


def _db_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get("src.dialectic_db")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


class DialecticDBQueriesMixin:
    """Read, query, saga-probe, resolution-effect, and analytics methods for DialecticDB."""

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID with all messages."""
        read_at = datetime.now(timezone.utc)
        await self._ensure_pool()
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        note_read = _db_attr("note_session_read", _default_note_session_read)
        async with comp_acquire(self._pool) as conn:
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
            note_read(session_id, read_at)
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
        note_read = _db_attr("note_session_read", _default_note_session_read)
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
            note_read([x.get("session_id") for x in sessions], read_at)
            return sessions

    async def get_resolution_effect(
        self, session_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the durable runtime-effect record for a resolved session."""
        await self._ensure_pool()
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        async with comp_acquire(self._pool) as conn:
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
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        async with comp_acquire(self._pool) as conn:
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
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        async with comp_acquire(self._pool) as conn:
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
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        async with comp_acquire(self._pool) as conn:
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
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        async with comp_acquire(self._pool) as conn:
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
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        async with comp_acquire(self._pool) as conn:
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
        log = _db_attr("logger", logger)
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
            log.debug(f"has_inflight_saga check failed for {session_id[:16]}...: {e}")
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
        log = _db_attr("logger", logger)
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
            log.debug(f"probe_saga_since failed for {session_id[:16]}...: {e}")
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
        comp_acquire = _db_attr("compatible_acquire", _default_compatible_acquire)
        async with comp_acquire(self._pool) as conn:
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
        note_read = _db_attr("note_session_read", _default_note_session_read)
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
            note_read([r.get("session_id") for r in out], read_at)
            return out

    async def get_sessions_awaiting_reviewer(self) -> List[Dict[str, Any]]:
        """Get sessions that need a reviewer assigned."""
        read_at = datetime.now(timezone.utc)
        await self._ensure_pool()
        note_read = _db_attr("note_session_read", _default_note_session_read)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM core.dialectic_sessions
                WHERE status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')
                AND (reviewer_agent_id IS NULL OR reviewer_agent_id = '')
                ORDER BY created_at ASC
            """)
            out = [dict(row) for row in rows]
            note_read([r.get("session_id") for r in out], read_at)
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
# Module-level Async Query & Resolution-Effect Wrappers
# =============================================================================


async def _get_db():
    getter = _db_attr("get_dialectic_db")
    return await getter()


async def get_session_async(session_id: str) -> Optional[Dict[str, Any]]:
    db = await _get_db()
    return await db.get_session(session_id)


async def get_session_by_agent_async(agent_id: str, active_only: bool = True) -> Optional[Dict[str, Any]]:
    db = await _get_db()
    return await db.get_session_by_agent(agent_id, active_only)


async def get_all_sessions_by_agent_async(agent_id: str) -> List[Dict[str, Any]]:
    db = await _get_db()
    return await db.get_all_sessions_by_agent(agent_id)


async def is_agent_in_active_session_async(agent_id: str) -> bool:
    db = await _get_db()
    return await db.is_agent_in_active_session(agent_id)


async def has_inflight_saga_async(session_id: str) -> bool:
    db = await _get_db()
    return await db.has_inflight_saga(session_id)


async def probe_inflight_saga_async(session_id: str) -> Optional[bool]:
    db = await _get_db()
    return await db.probe_inflight_saga(session_id)


async def probe_saga_since_async(
    session_id: str, since: Optional[datetime]
) -> Optional[Dict[str, Any]]:
    db = await _get_db()
    return await db.probe_saga_since(session_id, since)


async def get_session_terminal_state_async(session_id: str) -> Optional[Dict[str, Any]]:
    db = await _get_db()
    return await db.get_session_terminal_state(session_id)


async def has_recently_reviewed_async(reviewer_id: str, paused_agent_id: str, hours: int = 24) -> bool:
    db = await _get_db()
    return await db.has_recently_reviewed(reviewer_id, paused_agent_id, hours)


async def add_bounded_message_async(**kwargs) -> Optional[int]:
    db = await _get_db()
    return await db.add_bounded_message(**kwargs)


async def get_resolution_effect_async(
    session_id: str,
) -> Optional[Dict[str, Any]]:
    db = await _get_db()
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
    db = await _get_db()
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
    db = await _get_db()
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
    db = await _get_db()
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
    db = await _get_db()
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
    db = await _get_db()
    return await db.quarantine_resolution_effect(
        session_id, owner_token, reason
    )


async def get_active_sessions_async(
    limit: int = 100,
    *,
    least_recently_updated_first: bool = False,
) -> List[Dict[str, Any]]:
    db = await _get_db()
    return await db.get_active_sessions(
        limit,
        least_recently_updated_first=least_recently_updated_first,
    )


async def get_sessions_awaiting_reviewer_async() -> List[Dict[str, Any]]:
    db = await _get_db()
    return await db.get_sessions_awaiting_reviewer()


async def fetch_timeline_dialectic_sessions(
    conn, *, start, agent_id: Optional[str], limit: int
) -> list:
    """Sessions updated since `start`, most recently updated first, for
    GET /v1/timeline. `agent_id` matches the paused agent or the reviewer."""
    return await conn.fetch(
        """
        SELECT session_id, paused_agent_id, reviewer_agent_id, status,
               phase, session_type, created_at, updated_at
        FROM core.dialectic_sessions
        WHERE updated_at >= $1
          AND ($2::text IS NULL OR paused_agent_id = $2
               OR reviewer_agent_id = $2)
        ORDER BY updated_at DESC
        LIMIT $3
        """,
        start, agent_id, limit,
    )
