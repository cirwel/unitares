"""Tool usage and outcome event operations mixin for PostgresBackend."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from datetime import datetime

from src.logging_utils import get_logger

logger = get_logger(__name__)


def _is_missing_outcome_partition(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "no partition of relation" in msg and "outcome_events" in msg


def _is_missing_verification_source_column(exc: Exception) -> bool:
    msg = str(exc).lower()
    class_name = exc.__class__.__name__.lower()
    return (
        "verification_source" in msg
        and ("does not exist" in msg or "undefinedcolumn" in class_name)
    )


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
        from src.outcome_corroboration import enrich_detail_with_corroboration
        from config.governance_config import GovernanceConfig

        # --- EISV outcome-snapshot bridge (Stage-0 population bridge; roadmap §4a / Appendix B) ---
        # External-signal / test outcomes arrive through callers that do not carry an EISV
        # snapshot, so eisv_* land NULL and the row can never join an agent's state for the
        # residual-vs-Phi falsifiability test (§6.3). When no EISV was supplied and the agent
        # has a real (non-synthetic) measured state, snapshot it at outcome time. Fires only
        # when every channel is None — never overrides an explicit snapshot — and self-limits
        # to agents already in the EISV system (get_latest_eisv_by_agent_id excludes bootstrap
        # rows and returns None otherwise, so non-instrumented agents stay NULL, never faked).
        if (
            eisv_e is None and eisv_i is None and eisv_s is None and eisv_v is None
            and eisv_phi is None and eisv_verdict is None
            and eisv_coherence is None and eisv_regime is None
        ):
            try:
                _snap = await self.get_latest_eisv_by_agent_id(agent_id)
            except Exception:
                _snap = None
            if _snap:
                eisv_e, eisv_i, eisv_s, eisv_v = (
                    _snap.get("E"), _snap.get("I"), _snap.get("S"), _snap.get("V"),
                )
                eisv_phi, eisv_verdict = _snap.get("phi"), _snap.get("verdict")
                eisv_coherence, eisv_regime = _snap.get("coherence"), _snap.get("regime")
                detail = dict(detail or {})
                detail["eisv_snapshot_source"] = "outcome_bridge"

        corroborated_detail = enrich_detail_with_corroboration(
            detail,
            outcome_type=outcome_type,
            verification_source=verification_source,
        )
        async with self.acquire() as conn:
            def _detail_json(*, legacy_verification_source: bool = False) -> str:
                payload = dict(corroborated_detail)
                if legacy_verification_source and verification_source is not None:
                    payload.setdefault("verification_source", verification_source)
                return json.dumps(payload)

            async def _insert(*, include_verification_source: bool = True):
                if include_verification_source:
                    return await conn.fetchval(
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
                        _detail_json(),
                        GovernanceConfig.CURRENT_EPOCH,
                        verification_source,
                    )

                return await conn.fetchval(
                    """
                    INSERT INTO audit.outcome_events
                        (ts, agent_id, session_id, outcome_type, outcome_score, is_bad,
                         eisv_e, eisv_i, eisv_s, eisv_v, eisv_phi, eisv_verdict, eisv_coherence, eisv_regime,
                         detail, epoch)
                    VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    RETURNING outcome_id
                    """,
                    agent_id, session_id, outcome_type, outcome_score, is_bad,
                    eisv_e, eisv_i, eisv_s, eisv_v, eisv_phi, eisv_verdict, eisv_coherence, eisv_regime,
                    _detail_json(legacy_verification_source=True),
                    GovernanceConfig.CURRENT_EPOCH,
                )

            try:
                outcome_id = await _insert()
                return str(outcome_id) if outcome_id else None
            except Exception as exc:
                last_exc = exc

            if _is_missing_outcome_partition(last_exc):
                logger.warning(
                    "record_outcome_event encountered missing outcome_events partition; "
                    "running audit.partition_maintenance() before one retry"
                )
                try:
                    await conn.fetchval("SELECT audit.partition_maintenance()")
                    outcome_id = await _insert()
                    return str(outcome_id) if outcome_id else None
                except Exception as retry_exc:
                    last_exc = retry_exc

            if _is_missing_verification_source_column(last_exc):
                logger.warning(
                    "record_outcome_event found audit.outcome_events without "
                    "verification_source column; retrying legacy insert without "
                    "optional provenance column"
                )
                try:
                    outcome_id = await _insert(include_verification_source=False)
                    return str(outcome_id) if outcome_id else None
                except Exception as retry_exc:
                    last_exc = retry_exc

            logger.error(
                "record_outcome_event failed for agent=%s outcome_type=%s: %s",
                agent_id,
                outcome_type,
                last_exc,
            )
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
