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
                        parent_mature, reasons, class_tag, calibration_status
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
                    event.session_id,
                    event.event_type,
                    event.confidence,
                    json.dumps(event.payload),
                    event.raw_hash,
                )
                return True
            except Exception as e:
                logger.error(f"append_audit_event failed for agent={event.agent_id} type={event.event_type}: {e}")
                return False

    async def query_audit_events(
        self,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        order: str = "asc",
    ) -> List[AuditEvent]:
        conditions = []
        params = []
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

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
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
