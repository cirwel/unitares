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
    ) -> List[IdentityRecord]:
        async with self.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """
                    SELECT identity_id, agent_id, api_key_hash, created_at, updated_at,
                           status, parent_agent_id, spawn_reason, disabled_at, last_activity_at, metadata
                    FROM core.identities
                    WHERE status = $1
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    status, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT identity_id, agent_id, api_key_hash, created_at, updated_at,
                           status, parent_agent_id, spawn_reason, disabled_at, last_activity_at, metadata
                    FROM core.identities
                    ORDER BY created_at DESC
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
                """,
                successor_id,
            )
            try:
                rows = int((result or "UPDATE 0").split()[-1])
            except Exception:
                rows = 0
            return rows > 0

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
