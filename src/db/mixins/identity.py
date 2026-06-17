"""Identity operations mixin for PostgresBackend."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..base import IdentityRecord
from src.logging_utils import get_logger

logger = get_logger(__name__)


class IdentityMixin:
    """Identity CRUD operations."""

    async def upsert_identity(
        self,
        agent_id: str,
        api_key_hash: str,
        parent_agent_id: Optional[str] = None,
        spawn_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at=None,
    ) -> int:
        async with self.acquire() as conn:
            identity_id = await conn.fetchval(
                """
                INSERT INTO core.identities (
                    agent_id, api_key_hash, parent_agent_id, spawn_reason, metadata, created_at
                )
                VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()))
                ON CONFLICT (agent_id) DO UPDATE SET
                    parent_agent_id = COALESCE(EXCLUDED.parent_agent_id, core.identities.parent_agent_id),
                    spawn_reason = COALESCE(EXCLUDED.spawn_reason, core.identities.spawn_reason),
                    metadata = core.identities.metadata || COALESCE($5, '{}'::jsonb),
                    updated_at = now()
                RETURNING identity_id
                """,
                agent_id,
                api_key_hash,
                parent_agent_id,
                spawn_reason,
                json.dumps(metadata or {}),
                created_at,
            )
            return identity_id

    async def get_identity(self, agent_id: str) -> Optional[IdentityRecord]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT identity_id, agent_id, api_key_hash, created_at, updated_at,
                       status, parent_agent_id, spawn_reason, disabled_at, last_activity_at, metadata
                FROM core.identities
                WHERE agent_id = $1
                """,
                agent_id,
            )
            if not row:
                return None
            return self._row_to_identity(row)

    async def get_identities_batch(self, agent_ids: list[str]) -> dict[str, Optional[IdentityRecord]]:
        """Load identities for multiple agent IDs in a single query."""
        if not agent_ids:
            return {}
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT identity_id, agent_id, api_key_hash, created_at, updated_at,
                       status, parent_agent_id, spawn_reason, disabled_at, last_activity_at, metadata
                FROM core.identities
                WHERE agent_id = ANY($1::text[])
                """,
                agent_ids,
            )
            result = {}
            for row in rows:
                identity = self._row_to_identity(row)
                result[identity.agent_id] = identity
            return result

    async def get_identity_by_id(self, identity_id: int) -> Optional[IdentityRecord]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT identity_id, agent_id, api_key_hash, created_at, updated_at,
                       status, parent_agent_id, spawn_reason, disabled_at, last_activity_at, metadata
                FROM core.identities
                WHERE identity_id = $1
                """,
                identity_id,
            )
            if not row:
                return None
            return self._row_to_identity(row)

    async def list_identities(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        participated_only: bool = False,
    ) -> List[IdentityRecord]:
        """List identities with optional filtering.

        ``participated_only=True`` restricts the result to identities that
        have actually checked in — i.e. those with at least one *measured*
        (``synthetic = false``) row in ``core.agent_state``. This matches the
        existing measured-only convention: substrate_interpretation rows
        count as participation, bootstrap synthetic anchors do not. The
        filter is a derived EXISTS predicate; it is **view-only** and never
        archives, deletes, or mutates any row. Default ``False`` preserves
        the historic "show every onboarded identity" behavior for callers
        that opt out (operator audit / orphan classification / cache loads).
        """
        # core.identities is aliased ``i`` so the EXISTS subquery can
        # correlate on i.identity_id without column ambiguity.
        participated_clause = (
            " AND EXISTS (SELECT 1 FROM core.agent_state s "
            "WHERE s.identity_id = i.identity_id AND s.synthetic = false)"
            if participated_only
            else ""
        )
        async with self.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    f"""
                    SELECT i.identity_id, i.agent_id, i.api_key_hash, i.created_at, i.updated_at,
                           i.status, i.parent_agent_id, i.spawn_reason, i.disabled_at, i.last_activity_at, i.metadata
                    FROM core.identities i
                    WHERE i.status = $1{participated_clause}
                    ORDER BY i.created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    status, limit, offset,
                )
            else:
                # No status filter: a WHERE clause may still be needed for the
                # participated predicate. Build it so the EXISTS lands under
                # WHERE (not a dangling AND) when there is no status.
                no_status_where = (
                    " WHERE" + participated_clause[len(" AND"):]
                    if participated_only
                    else ""
                )
                rows = await conn.fetch(
                    f"""
                    SELECT i.identity_id, i.agent_id, i.api_key_hash, i.created_at, i.updated_at,
                           i.status, i.parent_agent_id, i.spawn_reason, i.disabled_at, i.last_activity_at, i.metadata
                    FROM core.identities i{no_status_where}
                    ORDER BY i.created_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
            return [self._row_to_identity(r) for r in rows]

    async def list_recently_active_identities(
        self,
        cutoff,
        limit: int = 500,
    ) -> List[IdentityRecord]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT identity_id, agent_id, api_key_hash, created_at, updated_at,
                       status, parent_agent_id, spawn_reason, disabled_at, last_activity_at, metadata
                FROM core.identities
                WHERE status = 'active' AND last_activity_at > $1
                ORDER BY last_activity_at DESC
                LIMIT $2
                """,
                cutoff, limit,
            )
            return [self._row_to_identity(r) for r in rows]

    async def update_identity_status(
        self,
        agent_id: str,
        status: str,
        disabled_at=None,
    ) -> bool:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                SET status = $2, disabled_at = $3, updated_at = now()
                WHERE agent_id = $1
                """,
                agent_id, status, disabled_at,
            )
            return result == "UPDATE 1"

    async def update_identity_metadata(
        self,
        agent_id: str,
        metadata: Dict[str, Any],
        merge: bool = True,
    ) -> bool:
        async with self.acquire() as conn:
            if merge:
                result = await conn.execute(
                    """
                    UPDATE core.identities
                    SET metadata = metadata || $2::jsonb, updated_at = now()
                    WHERE agent_id = $1
                    """,
                    agent_id, json.dumps(metadata),
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE core.identities
                    SET metadata = $2::jsonb, updated_at = now()
                    WHERE agent_id = $1
                    """,
                    agent_id, json.dumps(metadata),
                )
            return "UPDATE 1" in result

    async def increment_update_count(
        self,
        agent_id: str,
        extra_metadata: Dict[str, Any] | None = None,
    ) -> int:
        """Atomically increment total_updates in PostgreSQL and return the new value."""
        async with self.acquire() as conn:
            if extra_metadata:
                new_count = await conn.fetchval(
                    """
                    UPDATE core.identities
                    SET metadata = jsonb_set(
                            metadata || $2::jsonb,
                            '{total_updates}',
                            (COALESCE((metadata->>'total_updates')::int, 0) + 1)::text::jsonb
                        ),
                        updated_at = now(),
                        last_activity_at = now()
                    WHERE agent_id = $1
                    RETURNING (metadata->>'total_updates')::int
                    """,
                    agent_id, json.dumps(extra_metadata),
                )
            else:
                new_count = await conn.fetchval(
                    """
                    UPDATE core.identities
                    SET metadata = jsonb_set(
                            metadata,
                            '{total_updates}',
                            (COALESCE((metadata->>'total_updates')::int, 0) + 1)::text::jsonb
                        ),
                        updated_at = now(),
                        last_activity_at = now()
                    WHERE agent_id = $1
                    RETURNING (metadata->>'total_updates')::int
                    """,
                    agent_id,
                )
            return new_count or 0

    async def verify_api_key(self, agent_id: str, api_key: str) -> bool:
        async with self.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT core.verify_api_key($2, api_key_hash)
                FROM core.identities
                WHERE agent_id = $1
                """,
                agent_id, api_key,
            )
            return bool(result)

    # ------------------------------------------------------------------
    # R1 v3.3-D: provisional-lineage helpers + v3.3-C calibration_state
    # ------------------------------------------------------------------

    async def mark_lineage_provisional(
        self,
        successor_id: str,
        score_id: str,
    ) -> bool:
        """Stamp a successor's lineage as provisional after an inconclusive score.

        Per v3.3-D: callers using `marks` policy (onboard-time scoring) invoke
        this to record that the lineage edge is unconfirmed. Trust-tier (S6),
        R3 baselines, KG provenance, and R2 (PR 4) read this flag and
        exclude provisional rows from their respective aggregations.

        score_id references the most recent audit.r1_score_audit row that
        justified this state. provisional_recorded_at stamps now.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                SET provisional_lineage = TRUE,
                    provisional_score_id = $1,
                    provisional_recorded_at = now(),
                    confirmed_at = NULL,
                    updated_at = now()
                WHERE agent_id = $2
                """,
                score_id, successor_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

    async def confirm_lineage(self, successor_id: str) -> bool:
        """Promote provisional → confirmed.

        Called by the promotion policy site (per v3.1 §"Caller policy" —
        promotion uses `blocks`; the promotion gate only fires on a re-score
        returning `plausible`). Stamps confirmed_at, clears the provisional
        flag and score_id reference.

        `provisional_recorded_at` is INTENTIONALLY preserved as an audit
        anchor — it records *when the provisional window began*, which
        survives confirmation. Consumers reading
        `(provisional_lineage=False AND provisional_recorded_at IS NOT NULL
        AND confirmed_at IS NOT NULL)` see "this lineage was once
        provisional, was confirmed at confirmed_at, originally provisional
        at provisional_recorded_at." Clearing it would erase that audit
        trail. (PR 3 council code-reviewer flag — the explicit decision.)

        WHERE guard `lineage_archived_at IS NULL AND lineage_demoted_at
        IS NULL` prevents flipping a row already in a terminal state into
        a corrupt dual-stamped (e.g. archived AND confirmed) shape. This
        mirrors the guards on `demote_lineage` and `archive_lineage` —
        terminal states are absorbing on the storage layer regardless
        of which path the FSM takes to reach them. Returns False (rows=0)
        on a no-op so the FSM caller can distinguish a successful confirm
        from a guard-skip and degrade its outcome accordingly.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                SET provisional_lineage = FALSE,
                    provisional_score_id = NULL,
                    confirmed_at = now(),
                    updated_at = now()
                WHERE agent_id = $1
                  AND lineage_archived_at IS NULL
                  AND lineage_demoted_at IS NULL
                """,
                successor_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

    async def is_lineage_provisional(self, agent_id: str) -> bool:
        """R1 v3.3-D: read-only check of provisional_lineage on core.identities.

        Returns False when the row doesn't exist. Consumers (trust-tier
        gate, R3 baselines, KG provenance) call this to filter without
        loading the full record. Stubbable in tests via conftest's
        `_isolate_db_backend` (no `async with self.acquire()` exposure).
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT provisional_lineage FROM core.identities WHERE agent_id = $1",
                agent_id,
            )
            if row is None:
                return False
            return bool(row["provisional_lineage"])

    async def get_provisional_lineage_set(self, agent_ids: list[str]) -> set[str]:
        """Batch counterpart of is_lineage_provisional.

        Returns the subset of agent_ids whose `provisional_lineage = TRUE`.
        One query for N agents instead of N queries — used by the
        cold-start metadata load path so trust-tier resolution can pass
        `prefetched_provisional` and skip per-agent fetchrow.
        """
        if not agent_ids:
            return set()
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id FROM core.identities
                WHERE agent_id = ANY($1::text[])
                  AND provisional_lineage = TRUE
                """,
                agent_ids,
            )
            return {row["agent_id"] for row in rows}

    async def read_r1_calibration_state(self) -> Dict[str, Any]:
        """Read the R1 calibration_state singleton (v3.3-C).

        Returns the current `calibration_status` and lifecycle timestamps.
        The score primitive snapshots `calibration_status` onto every audit
        record at write time; consumers under `calibration_failed` MUST
        degrade verdict to `inconclusive` (degradation at the consumer
        layer, but the state read here is what gates it).
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, calibration_status, seeded_since, earned_at,
                       failed_at, updated_at
                FROM core.r1_calibration_state
                WHERE id = 1
                """
            )
            if row is None:
                # Pre-migration-032 caller fallback. After 032 is in
                # production this branch is dead; keeping it avoids surprise
                # crashes if a fresh DB is missing the seeded singleton.
                # Logged at WARNING because in steady state this should not
                # fire — if it does, an operator should investigate
                # (singleton row deleted by hand, or migration 032 not
                # applied).
                logger.warning(
                    "[R1] core.r1_calibration_state singleton missing — "
                    "synthesizing fallback 'seeded' state. Either migration "
                    "032 has not been applied or the singleton row was "
                    "removed."
                )
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                return {
                    "calibration_status": "seeded",
                    "seeded_since": now,
                    "earned_at": None,
                    "failed_at": None,
                    "updated_at": now,
                }
            return dict(row)

    async def transition_r1_calibration_state(self, new_status: str) -> Dict[str, Any]:
        """Operator-only: transition the R1 calibration_state singleton.

        Per v3.3-C: transitions are explicit operator actions. This method
        does not validate operator authority — gate that at the call site
        (e.g. an admin handler with `X-Anima-Admin` header check).

        Timestamp semantics:
        - `earned_at` is **idempotent** — stamped only on the first
          `seeded → earned` transition (`earned_at IS NULL` guard). A later
          rollback through seeded and back to earned does NOT re-stamp.
        - `failed_at` is **last-wins** — re-stamped on every transition
          INTO `calibration_failed`. Recurring calibration loops are
          expected and the most-recent failure is more useful than the
          first; this matches the v3.3-C runbook framing of
          calibration_failed as a recoverable state.
        - Rollback paths (`earned → seeded`, `calibration_failed →
          seeded`) update `calibration_status` + `updated_at` only;
          `earned_at` and `failed_at` are append-only forensic anchors and
          are never cleared by a rollback. (PR 3 council architect flag —
          rollback is operator-decided, not in the spec, but defensible.)

        Atomic write+read via `RETURNING *`: a separate read-back would
        introduce a TOCTOU window where a concurrent operator transition
        could replace the state between the UPDATE and the SELECT. The
        returned dict is exactly what was written.

        Returns the post-transition state.
        """
        if new_status not in {"seeded", "earned", "calibration_failed"}:
            raise ValueError(
                f"invalid calibration_status: {new_status!r} "
                f"(must be one of: seeded, earned, calibration_failed)"
            )
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE core.r1_calibration_state
                SET calibration_status = $1,
                    earned_at = CASE
                        WHEN $1 = 'earned' AND earned_at IS NULL THEN now()
                        ELSE earned_at
                    END,
                    failed_at = CASE
                        WHEN $1 = 'calibration_failed' THEN now()
                        ELSE failed_at
                    END,
                    updated_at = now()
                WHERE id = 1
                RETURNING id, calibration_status, seeded_since, earned_at,
                          failed_at, updated_at
                """,
                new_status,
            )
        if row is None:
            # Singleton missing — defer to the read fallback (which logs).
            return await self.read_r1_calibration_state()
        return dict(row)

    # ------------------------------------------------------------------
    # R2: lineage lifecycle helpers (migration 036)
    #
    # Extends the R1 helpers above with the demote / archive / eval-stamp
    # transitions and the forward-only chain counter. The state machine
    # itself lives in src/identity/lineage_lifecycle.py (PR 2); these
    # helpers are the storage primitives it composes.
    #
    # Convention matches the R1 block: each method inlines the asyncpg
    # "UPDATE N" parsing rather than calling out to a shared helper, so
    # the row-affected semantics are obvious at the call site.
    # ------------------------------------------------------------------

    async def declare_lineage(self, successor_id: str) -> bool:
        """R2: stamp lineage_declared_at when parent_agent_id is first set.

        Idempotent — only stamps if NULL. Caller is the onboard handler
        (PR 3) after parent_agent_id is written and the cross-role
        pre-check has passed.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                SET lineage_declared_at = COALESCE(lineage_declared_at, now()),
                    updated_at = now()
                WHERE agent_id = $1
                """,
                successor_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

    async def reset_lineage_for_redeclaration(self, successor_id: str) -> bool:
        """R2 PR 3 council fix: clear terminal-state markers
        (``lineage_archived_at``, ``lineage_demoted_at``, ``confirmed_at``,
        ``provisional_lineage``, ``provisional_score_id``,
        ``chain_obs_count``, ``lineage_last_eval_at``,
        ``lineage_declared_at``) so a fresh declaration can re-enter the FSM.

        Called by ``_r2_pre_check_and_declare`` when a successor is being
        re-declared (i.e., the row already has ``lineage_archived_at`` OR
        ``lineage_demoted_at`` set). Without this reset, the FSM's
        terminal-state short-circuit (PR 2) would permanently skip the
        row even though ``parent_agent_id`` was just freshly set —
        the lineage would be silently dead while the response surfaces
        ``provisional``.

        The audit anchor for the prior terminal state is preserved in
        ``audit.events`` via the prior ``lineage_grace_expired`` /
        ``lineage_demoted`` event — the column-level history is
        intentionally cleared so the new lineage starts from a clean
        state. This is the operational analogue of the "fork in v1.1"
        open question: we don't model a fork edge — the new declaration
        simply starts fresh.

        Returns True if the row was in a terminal state and was reset;
        False otherwise (no-op for active or non-existent rows).
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                   SET lineage_archived_at = NULL,
                       lineage_demoted_at = NULL,
                       confirmed_at = NULL,
                       provisional_lineage = FALSE,
                       provisional_score_id = NULL,
                       chain_obs_count = 0,
                       lineage_last_eval_at = NULL,
                       lineage_declared_at = NULL,
                       updated_at = now()
                 WHERE agent_id = $1
                   AND (lineage_archived_at IS NOT NULL OR lineage_demoted_at IS NOT NULL)
                """,
                successor_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

    async def clear_lineage_declaration(self, agent_id: str) -> bool:
        """R2 PR 3 council fix: cross-role rejection helper.

        Clears ``parent_agent_id`` AND ``spawn_reason`` from
        ``core.identities`` (symmetric clear, per the S8c convention
        that these two columns move together). Called by
        ``_r2_pre_check_and_declare`` when the cross-role envelope check
        rejects a declaration; replaces the prior inline ``UPDATE`` so
        the rejection surface stays consistent with the rest of the
        lineage helpers in this mixin.

        PR 4 council fix: also clears all lineage-state fields
        (``provisional_lineage``, ``provisional_score_id``,
        ``confirmed_at``, ``lineage_declared_at``) so a cross-role-
        rejected row produces a clean fresh-identity row with no
        residual lineage state. Without this, a row with
        ``parent_agent_id=NULL AND provisional_lineage=TRUE`` would
        match the sweeper's candidate WHERE filter every cycle, and
        the FSM's ``no_parent`` short-circuit returns without
        stamping ``lineage_last_eval_at`` — a hot loop forever. Full
        reset makes the sweeper's filter naturally exclude the row
        (``provisional_lineage=FALSE AND confirmed_at IS NULL``).

        Returns True if a row was updated, False otherwise.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                SET parent_agent_id = NULL,
                    spawn_reason = NULL,
                    provisional_lineage = FALSE,
                    provisional_score_id = NULL,
                    confirmed_at = NULL,
                    lineage_declared_at = NULL,
                    updated_at = now()
                WHERE agent_id = $1
                """,
                agent_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

    async def demote_lineage(self, successor_id: str, *, reason: str) -> bool:
        """R2: provisional/confirmed → demoted.

        Clears parent_agent_id, stamps lineage_demoted_at, clears the
        provisional flag and confirmed_at, and resets chain_obs_count
        to 0 (clawback for the confirmed → demoted path). `reason` is
        accepted by the caller for the audit event payload — the column
        carries timestamps, not free-text.

        Also clears lineage_declared_at and lineage_last_eval_at so a
        subsequent re-declaration (PR 3) starts a fresh grace window
        and a fresh cadence cycle. Without these clears, a demoted-then-
        re-declared row would inherit the prior declaration's grace
        clock and the sweeper's prior eval stamp, causing the new
        lineage to expire prematurely or skip its first eval.

        WHERE guard `lineage_archived_at IS NULL AND lineage_demoted_at
        IS NULL` prevents re-demoting a row already in a terminal state
        (forcing item from PR 1 council review).
        """
        # `reason` is intentionally consumed by the caller's audit
        # emission — kept in the signature so call sites must name a
        # reason explicitly.
        del reason
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                SET parent_agent_id = NULL,
                    provisional_lineage = FALSE,
                    provisional_score_id = NULL,
                    confirmed_at = NULL,
                    lineage_demoted_at = now(),
                    lineage_declared_at = NULL,
                    lineage_last_eval_at = NULL,
                    chain_obs_count = 0,
                    updated_at = now()
                WHERE agent_id = $1
                  AND lineage_archived_at IS NULL
                  AND lineage_demoted_at IS NULL
                """,
                successor_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

    async def archive_lineage(self, successor_id: str) -> bool:
        """R2: grace-window expiration.

        Stamps lineage_archived_at, clears the provisional flag and
        provisional_score_id, but **retains parent_agent_id** as an
        inert audit anchor (the declaration happened; we just stopped
        being able to verify it before the grace window closed).

        Also clears confirmed_at, lineage_declared_at, and
        lineage_last_eval_at. Clearing confirmed_at prevents a zombie
        "archived but confirmed" combination if the helper is invoked
        on a confirmed row (the FSM never archives confirmed rows in
        practice, but the helper's contract should not leave that
        invariant to the caller). Clearing the declaration/eval
        timestamps mirrors `demote_lineage`: a future re-declaration
        starts a fresh grace and cadence cycle.

        WHERE guard `lineage_archived_at IS NULL AND lineage_demoted_at
        IS NULL` prevents re-archiving a row already in a terminal
        state.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.identities
                SET provisional_lineage = FALSE,
                    provisional_score_id = NULL,
                    confirmed_at = NULL,
                    lineage_archived_at = now(),
                    lineage_declared_at = NULL,
                    lineage_last_eval_at = NULL,
                    updated_at = now()
                WHERE agent_id = $1
                  AND lineage_archived_at IS NULL
                  AND lineage_demoted_at IS NULL
                """,
                successor_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

    async def increment_chain_obs_count(self, successor_id: str) -> int:
        """R2: post-promotion forward-only counter.

        Returns the new value after the increment. No-op (returns 0)
        when the row is not in the confirmed state — the counter only
        advances for lineage edges that have cleared promotion. The
        confirmed-state guard lives in the WHERE clause so this is one
        atomic UPDATE; no read-modify-write window.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE core.identities
                SET chain_obs_count = chain_obs_count + 1,
                    updated_at = now()
                WHERE agent_id = $1
                  AND confirmed_at IS NOT NULL
                  AND provisional_lineage = FALSE
                RETURNING chain_obs_count
                """,
                successor_id,
            )
            return int(row["chain_obs_count"]) if row is not None else 0

    async def stamp_lineage_eval(self, successor_id: str) -> None:
        """R2: cadence guard. Called after every R1 eval (PR 2 FSM
        + PR 4 sweeper) to avoid hot-loop re-evaluation of the same
        edge inside the sweeper cycle window."""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE core.identities "
                "SET lineage_last_eval_at = now() "
                "WHERE agent_id = $1",
                successor_id,
            )

    async def select_lineage_eval_candidates(
        self,
        *,
        sweep_cadence_hours: int = 6,
        limit: int = 100,
    ) -> List[str]:
        """R2 PR 4: agents with provisional or confirmed lineage whose
        ``lineage_last_eval_at`` is older than ``sweep_cadence_hours``
        (or NULL).

        Excludes terminal-state rows (``lineage_archived_at IS NOT NULL``
        or ``lineage_demoted_at IS NOT NULL``) — the FSM's terminal-state
        guard would skip them anyway, but excluding here saves the
        round-trip and matches the partial-index predicate from
        migration 036 so the planner can use
        ``idx_identities_provisional_eval``.

        Ordering: ``NULLS FIRST`` so never-evaluated rows are picked up
        first. ``LIMIT`` prevents one cycle from doing unbounded work;
        remaining candidates are picked up on the next tick.

        Uses ``make_interval(hours => $1)`` rather than
        ``($1 || ' hours')::interval`` — the former is type-safe (integer
        parameter, no string concatenation).

        PR 4 council fix: uses ``FOR UPDATE SKIP LOCKED`` so two
        concurrent sweeper instances (e.g., during a deploy overlap)
        don't double-evaluate the same row and produce duplicate
        ``audit.r1_score_audit`` entries plus duplicate
        ``lineage_promoted``/``lineage_demoted`` audit events. Matches
        the ``class_promotion_sweeper_task`` precedent in
        ``background_tasks.py``. ``FOR UPDATE`` requires a transaction;
        the lock is released when the ``async with conn.transaction()``
        block exits (right after ``fetch()`` returns) — short-lived,
        just the SELECT. The actual eval happens after the lock is
        released, so two instances racing in the same micro-window can
        still both run R1 on the same row, but the first instance will
        stamp ``lineage_last_eval_at`` before the second instance's
        next sweeper tick. This is a meaningful collision-risk
        reduction matching the existing precedent; holding the lock
        through eval would require restructuring the sweeper's
        per-candidate loop and is deferred.
        """
        async with self.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT agent_id
                      FROM core.identities
                     WHERE parent_agent_id IS NOT NULL
                       AND lineage_archived_at IS NULL
                       AND lineage_demoted_at IS NULL
                       AND (provisional_lineage = TRUE OR confirmed_at IS NOT NULL)
                       AND (
                           lineage_last_eval_at IS NULL
                        OR lineage_last_eval_at < now() - make_interval(hours => $1)
                       )
                     ORDER BY lineage_last_eval_at NULLS FIRST
                     LIMIT $2
                     FOR UPDATE SKIP LOCKED
                    """,
                    int(sweep_cadence_hours), int(limit),
                )
        return [r["agent_id"] for r in rows]

    async def read_lineage_state(
        self, successor_id: str
    ) -> Optional[Dict[str, Any]]:
        """R2 PR 2: single-query read of the columns the lineage FSM
        needs to compute the next transition.

        Returns a dict with keys:
            parent_agent_id, provisional_lineage, confirmed_at,
            lineage_declared_at, lineage_demoted_at,
            lineage_archived_at, lineage_last_eval_at, chain_obs_count
        or ``None`` if no row exists for ``successor_id``.

        Single-query so the FSM driver in
        `src/identity/lineage_lifecycle.py` reads a consistent snapshot
        before deciding whether to skip on cadence, short-circuit on
        grace expiration, score, promote, or demote.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT parent_agent_id,
                       provisional_lineage,
                       confirmed_at,
                       lineage_declared_at,
                       lineage_demoted_at,
                       lineage_archived_at,
                       lineage_last_eval_at,
                       chain_obs_count
                  FROM core.identities
                 WHERE agent_id = $1
                """,
                successor_id,
            )
        if row is None:
            return None
        return {
            "parent_agent_id": row["parent_agent_id"],
            "provisional_lineage": bool(row["provisional_lineage"]),
            "confirmed_at": row["confirmed_at"],
            "lineage_declared_at": row["lineage_declared_at"],
            "lineage_demoted_at": row["lineage_demoted_at"],
            "lineage_archived_at": row["lineage_archived_at"],
            "lineage_last_eval_at": row["lineage_last_eval_at"],
            "chain_obs_count": int(row["chain_obs_count"] or 0),
        }

    async def read_class_tag(self, agent_id: str) -> Optional[str]:
        """R2 PR 3: read primary class tag from `metadata.tags[0]`.

        Returns ``None`` if the row is missing, ``metadata`` is null,
        or ``tags`` is empty/absent. Used by R2's cross-role pre-check
        in ``src.identity.lineage_lifecycle.pre_check_cross_role`` —
        consumers of class info elsewhere already use the
        ``agent_metadata`` cache.

        The value returned is the *first* element of ``tags``. The
        S8a convention is that the class is stamped first
        (``ephemeral``, ``persistent``, ...); modifier tags
        (``autonomous``, ``cadence.24hr``) follow. Two identities
        whose first tags match are considered same-role for the
        cross-role envelope check.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT metadata FROM core.identities WHERE agent_id = $1",
                agent_id,
            )
        if row is None:
            return None
        metadata = row["metadata"]
        if metadata is None:
            return None
        # asyncpg here returns JSONB as a str (no custom codec is
        # registered on the pool — see src/db/postgres_backend.py).
        # The PR 3 council live verifier called this dead code, but
        # `test_read_class_tag_live_db_returns_first_tag` proves the
        # str branch is the live path. Decode defensively.
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                return None
        if not isinstance(metadata, dict):
            return None
        tags = metadata.get("tags")
        if not tags or not isinstance(tags, list):
            return None
        return str(tags[0])

    async def are_lineages_provisional(
        self, agent_ids: List[str]
    ) -> Dict[str, bool]:
        """R2: batch read of provisional_lineage for many agents.

        Architect-flagged in the R1 PR 4a council as the primitive that
        keeps the sweeper and chain-walker from running N+1
        is_lineage_provisional() calls. Missing agents are reported as
        not-provisional (matches the single-agent helper's contract).
        """
        if not agent_ids:
            return {}
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT agent_id, provisional_lineage "
                "FROM core.identities "
                "WHERE agent_id = ANY($1::text[])",
                agent_ids,
            )
        out: Dict[str, bool] = {
            row["agent_id"]: bool(row["provisional_lineage"]) for row in rows
        }
        for aid in agent_ids:
            out.setdefault(aid, False)
        return out

    def _row_to_identity(self, row) -> IdentityRecord:
        return IdentityRecord(
            identity_id=row["identity_id"],
            agent_id=row["agent_id"],
            api_key_hash=row["api_key_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            parent_agent_id=row["parent_agent_id"],
            spawn_reason=row["spawn_reason"],
            disabled_at=row["disabled_at"],
            last_activity_at=row.get("last_activity_at"),
            metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"],
        )
