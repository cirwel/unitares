"""Tool usage and outcome event operations mixin for PostgresBackend."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from datetime import datetime

from src.logging_utils import get_logger

logger = get_logger(__name__)


class ToolUsageMixin:
    """Tool usage recording, outcome events, and EISV queries."""

    async def append_tool_usage(
        self,
        agent_id: Optional[str],
        session_id: Optional[str],
        tool_name: str,
        latency_ms: Optional[int],
        success: bool,
        error_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        async with self.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO audit.tool_usage
                        (ts, agent_id, session_id, tool_name, latency_ms, success, error_type, payload)
                    VALUES (now(), $1, $2, $3, $4, $5, $6, $7)
                    """,
                    agent_id, session_id, tool_name, latency_ms, success, error_type,
                    json.dumps(payload or {}),
                )
                return True
            except Exception as e:
                logger.error(f"append_tool_usage failed for agent={agent_id} tool={tool_name}: {e}")
                return False

    async def record_outcome_event(
        self,
        agent_id: str,
        outcome_type: str,
        is_bad: bool,
        outcome_score: Optional[float] = None,
        session_id: Optional[str] = None,
        eisv_e: Optional[float] = None,
        eisv_i: Optional[float] = None,
        eisv_s: Optional[float] = None,
        eisv_v: Optional[float] = None,
        eisv_phi: Optional[float] = None,
        eisv_verdict: Optional[str] = None,
        eisv_coherence: Optional[float] = None,
        eisv_regime: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        verification_source: Optional[str] = None,
    ) -> Optional[str]:
        """Insert one outcome event. Returns outcome_id UUID string or None on failure.

        ``verification_source`` is the v1 enum (agent_reported_tool_result |
        server_observation | external_signal), promoted to a top-level column
        in migration 038. Callers must pass a value matching the enum or NULL;
        the CHECK constraint rejects other strings. Optional for backwards
        compatibility with pre-Phase-1 callers; future migration will require it.
        """
        from config.governance_config import GovernanceConfig
        async with self.acquire() as conn:
            try:
                outcome_id = await conn.fetchval(
                    """
                    INSERT INTO audit.outcome_events
                        (ts, agent_id, session_id, outcome_type, outcome_score, is_bad,
                         eisv_e, eisv_i, eisv_s, eisv_v, eisv_phi, eisv_verdict, eisv_coherence, eisv_regime,
                         detail, epoch, verification_source)
                    VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                    RETURNING outcome_id
                    """,
                    agent_id, session_id, outcome_type, outcome_score, is_bad,
                    eisv_e, eisv_i, eisv_s, eisv_v, eisv_phi, eisv_verdict, eisv_coherence, eisv_regime,
                    json.dumps(detail or {}),
                    GovernanceConfig.CURRENT_EPOCH,
                    verification_source,
                )
                return str(outcome_id)
            except Exception:
                return None

    async def get_recent_outcomes(
        self,
        agent_id: str,
        limit: int = 20,
        since_hours: float = 24.0,
    ) -> List[Dict[str, Any]]:
        """Fetch recent outcome events for an agent."""
        async with self.acquire() as conn:
            try:
                rows = await conn.fetch(
                    """
                    SELECT outcome_type, is_bad, outcome_score, ts
                    FROM audit.outcome_events
                    WHERE agent_id = $1
                      AND ts >= now() - make_interval(hours => $2)
                    ORDER BY ts DESC
                    LIMIT $3
                    """,
                    agent_id, since_hours, limit,
                )
                return [dict(r) for r in rows]
            except Exception:
                return []

    async def get_latest_eisv_by_agent_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Fetch latest measured EISV snapshot for an agent.

        Bootstrap (synthetic) rows are excluded — the outcome correlator
        snapshots EISV at outcome-event time for calibration; correlating
        a real test outcome against a synthetic 0.5/0.5/0.5 anchor would
        inject default-encoded noise into every agent's calibration prior.
        Per onboard-bootstrap-checkin §4.1.
        """
        from config.governance_config import GovernanceConfig
        async with self.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT s.state_json, s.entropy, s.integrity, s.volatility,
                           s.coherence, s.regime
                    FROM core.agent_state s
                    JOIN core.identities i ON i.identity_id = s.identity_id
                    WHERE i.agent_id = $1 AND s.epoch = $2
                      AND s.synthetic = false
                    ORDER BY s.recorded_at DESC
                    LIMIT 1
                    """,
                    agent_id, GovernanceConfig.CURRENT_EPOCH,
                )
                if not row:
                    return None
                state_json = json.loads(row["state_json"]) if isinstance(row["state_json"], str) else row["state_json"]
                return {
                    "E": state_json.get("E"),
                    "I": row["integrity"],
                    "S": row["entropy"],
                    "V": row["volatility"],
                    "phi": state_json.get("phi"),
                    "verdict": state_json.get("verdict"),
                    "coherence": row["coherence"],
                    "regime": row["regime"],
                }
            except Exception:
                return None

    async def query_tool_usage(
        self,
        agent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        conditions = []
        params = []
        param_idx = 1

        if agent_id:
            conditions.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1
        if tool_name:
            conditions.append(f"tool_name = ${param_idx}")
            params.append(tool_name)
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
        params.append(limit)

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT ts, usage_id, agent_id, session_id, tool_name, latency_ms, success, error_type, payload
                FROM audit.tool_usage
                {where_clause}
                ORDER BY ts DESC
                LIMIT ${param_idx}
                """,
                *params,
            )
            return [
                {
                    "ts": r["ts"],
                    "usage_id": str(r["usage_id"]),
                    "agent_id": r["agent_id"],
                    "session_id": r["session_id"],
                    "tool_name": r["tool_name"],
                    "latency_ms": r["latency_ms"],
                    "success": r["success"],
                    "error_type": r["error_type"],
                    "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
                }
                for r in rows
            ]
