"""Audit operations mixin for PostgresBackend."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..base import AuditEvent
from src.logging_utils import get_logger

logger = get_logger(__name__)




class AuditMixin:
    """Audit event operations."""

    async def read_audit_model_observation_candidates(
        self, agent_id: str, *, event_types: List[str],
        observed_before: datetime, limit: int = 32,
    ) -> List[Dict[str, Any]]:
        """Model-bearing host observations, scoped to UUID and observation time."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts AS observed_at, event_id::text AS record_id,
                       'audit_event'::text AS record_kind,
                       payload->'runtime_provenance' AS provenance
                FROM audit.events
                WHERE agent_id = $1 AND event_type = ANY($2::text[]) AND ts <= $3
                  AND payload#>>'{runtime_provenance,schema}' = 's22.runtime_provenance.v1'
                  AND jsonb_typeof(payload#>'{runtime_provenance,model,identifier}') = 'string'
                  AND length(payload#>>'{runtime_provenance,model,identifier}') BETWEEN 1 AND 160
                ORDER BY ts DESC, event_id DESC LIMIT $4
                """,
                agent_id, event_types, observed_before, max(1, min(limit, 32)),
            )
        return [dict(row) for row in rows]

    async def record_r1_score_audit(self, record: Dict[str, Any]) -> bool:
        """R1 v3.3-A: persist the full score record to audit.r1_score_audit.

        The public KG carries only the redacted projection (verdict +
        calibration_status + n_dims_used + score_id); this writer holds the
        full record (plausibility, components, observations, parent_mature,
        reasons, class_tag, calibration_status). `score_id` from the input
        record is used as the public-↔-audit join key per v3.3-A.

        Awaited (not fire-and-forget): callers depend on `score_id` being
        present in the audit table before publishing the redacted KG payload
        that references it. Per-call cost is one INSERT; calls fire only on
        explicit `score_trajectory_continuity` invocations (not on every
        check-in).
        """
        async with self.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO audit.r1_score_audit (
                        score_id, parent_id, successor_id, recorded_at,
                        plausibility, components, observations,
                        parent_mature, reasons, class_tag, calibration_status,
                        verdict, raw_verdict
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """,
                    record["score_id"],
                    record["parent_id"],
                    record["successor_id"],
                    record["recorded_at"],
                    record["plausibility"],
                    json.dumps(record["components"]),
                    json.dumps(record["observations"]),
                    record["parent_mature"],
                    list(record["reasons"]),
                    record.get("class_tag"),
                    record["calibration_status"],
                    record.get("verdict"),
                    record.get("raw_verdict"),
                )
                return True
            except Exception as e:
                logger.error(
                    f"record_r1_score_audit failed for score_id={record.get('score_id')}: {e}"
                )
                return False

    async def append_audit_event(self, event: AuditEvent) -> bool:
        async with self.acquire() as conn:
            try:
                event_id_uuid: uuid.UUID
                if event.event_id:
                    try:
                        event_id_uuid = uuid.UUID(event.event_id)
                    except (ValueError, AttributeError):
                        event_id_uuid = uuid.uuid4()
                else:
                    event_id_uuid = uuid.uuid4()

                await conn.execute(
                    """
                    INSERT INTO audit.events (ts, event_id, agent_id, session_id, event_type, confidence, payload, raw_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT DO NOTHING
                    """,
                    event.ts or datetime.now(timezone.utc),
                    event_id_uuid,
                    event.agent_id,
                    # A keyed session id is a credential; store its digest
                    # (identity/stable_session.audit_reference).
                    _audit_session_ref(event.session_id),
                    event.event_type,
                    event.confidence,
                    json.dumps(event.payload),
                    event.raw_hash,
                )
                return True
            except Exception as e:
                logger.error(f"append_audit_event failed for agent={event.agent_id} type={event.event_type}: {e}")
                return False

    @staticmethod
    def _audit_event_filters(
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        event_id: Optional[str] = None,
    ) -> tuple[str, list, int]:
        """Build the WHERE clause shared by the row query and the aggregate.

        Factored out deliberately: if the two filtered differently, the summary
        would describe a different set of events than the ones returned beside
        it, and nothing in the response would reveal the mismatch.

        Returns (where_clause, params, next_param_idx).
        """
        conditions = []
        params: list = []
        param_idx = 1

        if agent_id:
            conditions.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1
        if event_type:
            conditions.append(f"event_type = ${param_idx}")
            params.append(event_type)
            param_idx += 1
        elif event_types:
            conditions.append(f"event_type = ANY(${param_idx}::text[])")
            params.append(list(event_types))
            param_idx += 1
        if start_time:
            conditions.append(f"ts >= ${param_idx}")
            params.append(start_time)
            param_idx += 1
        if end_time:
            conditions.append(f"ts <= ${param_idx}")
            params.append(end_time)
            param_idx += 1
        if event_id:
            conditions.append(f"event_id = ${param_idx}")
            params.append(uuid.UUID(str(event_id)))
            param_idx += 1

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where_clause, params, param_idx

    async def aggregate_audit_events(
        self,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Aggregate matching audit events over the WHOLE window, no LIMIT.

        Callers summarising a window must not compute their totals by iterating
        a LIMIT-ed page: a small limit then yields a total that is really a page
        size, and -- because rows come back in ts order -- a "last" timestamp
        that is actually the oldest row in the window.

        Grouping keeps the result small regardless of how many rows match: one
        row per (agent_id, event_type) pair, not per event.
        """
        where_clause, params, _ = self._audit_event_filters(
            agent_id, event_type, event_types, start_time, end_time
        )

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    agent_id,
                    event_type,
                    COUNT(*)  AS cnt,
                    MIN(ts)   AS first_ts,
                    MAX(ts)   AS last_ts
                FROM audit.events
                {where_clause}
                GROUP BY agent_id, event_type
                """,
                *params,
            )
            # first_ts/last_ts stay as datetimes. Callers combine group bounds
            # to get the window bounds, and comparing ISO strings would be
            # wrong the moment two rows carry different UTC offsets.
            return [
                {
                    "agent_id": r["agent_id"],
                    "event_type": r["event_type"],
                    "count": int(r["cnt"]),
                    "first_ts": r["first_ts"],
                    "last_ts": r["last_ts"],
                }
                for r in rows
            ]

    async def query_audit_events(
        self,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        order: str = "asc",
        event_id: Optional[str] = None,
    ) -> List[AuditEvent]:
        where_clause, params, param_idx = self._audit_event_filters(
            agent_id, event_type, event_types, start_time, end_time, event_id
        )
        order_clause = "ASC" if order.lower() == "asc" else "DESC"

        params.append(limit)

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT ts, event_id, agent_id, session_id, event_type, confidence, payload, raw_hash
                FROM audit.events
                {where_clause}
                ORDER BY ts {order_clause}
                LIMIT ${param_idx}
                """,
                *params,
            )
            return [self._row_to_audit_event(r) for r in rows]

    async def search_audit_events(
        self,
        query: str,
        agent_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[AuditEvent]:
        async with self.acquire() as conn:
            if agent_id:
                rows = await conn.fetch(
                    """
                    SELECT ts, event_id, agent_id, session_id, event_type, confidence, payload, raw_hash
                    FROM audit.events
                    WHERE payload::text ILIKE '%' || $1 || '%' AND agent_id = $2
                    ORDER BY ts DESC
                    LIMIT $3
                    """,
                    query, agent_id, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT ts, event_id, agent_id, session_id, event_type, confidence, payload, raw_hash
                    FROM audit.events
                    WHERE payload::text ILIKE '%' || $1 || '%'
                    ORDER BY ts DESC
                    LIMIT $2
                    """,
                    query, limit,
                )
            return [self._row_to_audit_event(r) for r in rows]

    async def get_latest_confidence_before(
        self,
        before_ts: Optional[datetime] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[float]:
        """Return the most recent non-null confidence from audit events.

        Used as a fallback when outcome_event cannot resolve confidence from
        the in-memory monitor (e.g. REST callers without MCP session context).

        If agent_id is given, only that agent is searched. Cross-agent fallback
        is intentionally forbidden because it can attach another agent's
        confidence to the wrong outcome event and poison calibration evidence.
        Omitting agent_id queries the most recent confidence across all agents.
        """
        async with self.acquire() as conn:
            ts = before_ts or datetime.now(timezone.utc)

            if agent_id:
                row = await conn.fetchrow(
                    """
                    SELECT confidence FROM audit.events
                    WHERE agent_id = $1
                      AND confidence IS NOT NULL
                      AND confidence > 0
                      AND ts <= $2
                    ORDER BY ts DESC LIMIT 1
                    """,
                    agent_id, ts,
                )
                return float(row["confidence"]) if row else None

            row = await conn.fetchrow(
                """
                SELECT confidence FROM audit.events
                WHERE confidence IS NOT NULL
                  AND confidence > 0
                  AND agent_id NOT IN ('system', 'eisv-sync-task')
                  AND ts <= $1
                ORDER BY ts DESC LIMIT 1
                """,
                ts,
            )
            return float(row["confidence"]) if row else None

    def _row_to_audit_event(self, row) -> AuditEvent:
        return AuditEvent(
            ts=row["ts"],
            event_id=str(row["event_id"]),
            event_type=row["event_type"],
            agent_id=row["agent_id"],
            session_id=row["session_id"],
            confidence=row["confidence"],
            payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
            raw_hash=row["raw_hash"],
        )


def _audit_session_ref(session_id):
    from src.mcp_handlers.identity.stable_session import audit_reference

    return audit_reference(session_id)


async def count_eisv_sync_events_by_agents(
    conn, resident_uuids: List[str], window
) -> list:
    """Count `eisv_sync` audit.events rows per agent_id within `window`."""
    if not resident_uuids:
        return []
    return await conn.fetch(
        """
        SELECT agent_id::text AS uuid, count(*) AS n
        FROM audit.events
        WHERE event_type = 'eisv_sync'
          AND agent_id = ANY($1::text[])
          AND ts > now() - $2::interval
        GROUP BY agent_id
        """,
        resident_uuids,
        window,
    )


async def count_lifecycle_pauses_since(conn, epoch_started_at) -> int:
    """Count `lifecycle_paused` audit events since `epoch_started_at`."""
    pause_sql = """
        SELECT count(*)::int FROM audit.events
        WHERE ts >= $1 AND event_type = 'lifecycle_paused'
    """
    return int((await conn.fetchval(pause_sql, epoch_started_at)) or 0)


# Product-neutral finding counts grouped by event_type. Uses `right(..., 8)`
# rather than `LIKE '%_finding'` because `_` is a single-character wildcard in
# SQL LIKE patterns.
FINDING_COUNTS_7D_SQL = (
    "SELECT event_type, count(*) AS cnt FROM audit.events "
    "WHERE right(event_type, 8) = '_finding' "
    "AND ts > now() - interval '7 days' "
    "GROUP BY event_type"
)


async def fetch_finding_counts_7d(conn) -> Dict[str, int]:
    """Count `_finding` audit events over the trailing 7 days grouped by `event_type`."""
    rows = await conn.fetch(FINDING_COUNTS_7D_SQL)
    counts: Dict[str, int] = {}
    for row in rows:
        etype = row["event_type"]
        if isinstance(etype, str) and etype:
            counts[etype] = int(row["cnt"] or 0)
    return counts


async def count_service_down_events_within(conn, window_delta) -> int:
    """Count `lease_plane_down` / `service_down` audit events within `window_delta`."""
    n_down = await conn.fetchval(
        """
        SELECT count(*) AS n
        FROM audit.events
        WHERE event_type IN ('lease_plane_down', 'service_down')
          AND ts > now() - $1::interval
        """,
        window_delta,
    )
    return int(n_down or 0)


AUDIT_VOLUME_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '6s'"

# Mirrors `UnitaresSentinel.AuditVolumeWatch.volume_sql` so remote Sentinel
# deployments can query Core over HTTP (`GET /v1/audit/volume-summary`) without
# opening PostgreSQL port 5432 directly.
AUDIT_VOLUME_SUMMARY_SQL = """
WITH e AS (
  SELECT event_type,
         coalesce(nullif(agent_id, ''), nullif(session_id, ''), '') AS source,
         ts,
         coalesce(payload->>'resolution_source' = 'throttle_flush', false) AS flush,
         CASE WHEN jsonb_typeof(payload->'suppressed_since_last') = 'number'
              THEN greatest((payload->>'suppressed_since_last')::numeric, 0)::bigint
              ELSE 0 END AS suppressed,
         CASE WHEN payload->>'resolution_source' = 'throttle_flush'
                   AND pg_input_is_valid(payload->>'suppressed_last_at', 'timestamptz')
              THEN (payload->>'suppressed_last_at')::timestamptz
              ELSE ts END AS suppressed_at
  FROM audit.events
  WHERE ts > now() - interval '24 hours'
),
w AS (
  SELECT event_type,
         source,
         CASE WHEN flush THEN suppressed_at ELSE ts END AS at,
         CASE WHEN flush THEN suppressed ELSE 1 + suppressed END AS weight
  FROM e
)
SELECT event_type,
       source,
       coalesce(sum(weight) FILTER (WHERE at > now() - interval '1 hour'), 0) AS recent,
       coalesce(sum(weight) FILTER (WHERE at > now() - interval '24 hours'
                                      AND at <= now() - interval '1 hour'), 0) AS prior
FROM w
GROUP BY event_type, source
HAVING coalesce(sum(weight) FILTER (WHERE at > now() - interval '1 hour'), 0) >= $1::bigint
ORDER BY recent DESC, event_type, source
"""


async def fetch_audit_volume_summary(
    conn: Any,
    *,
    floor: int = 300,
) -> List[Dict[str, Any]]:
    """Run the 24h audit volume summary query on an acquired connection."""
    rows = await conn.fetch(AUDIT_VOLUME_SUMMARY_SQL, int(floor))
    return [
        {
            "event_type": str(row["event_type"] or ""),
            "source": str(row["source"] or ""),
            "recent": int(row["recent"] or 0),
            "prior": int(row["prior"] or 0),
        }
        for row in rows
    ]


async def query_audit_volume_summary(*, floor: int = 300) -> List[Dict[str, Any]]:
    """Acquire a read-only pool snapshot and compute the 24h audit volume summary."""
    from src import agent_storage

    db = agent_storage.get_db()
    async with db.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute(AUDIT_VOLUME_STATEMENT_TIMEOUT_SQL)
            return await fetch_audit_volume_summary(conn, floor=floor)
