"""Agent state operations mixin for PostgresBackend."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..base import AgentStateRecord
from src.logging_utils import get_logger

logger = get_logger(__name__)


class StateMixin:
    """Agent state (EISV) snapshot operations."""

    async def read_state_model_observation_candidates(
        self, agent_id: str, *, observed_before, limit: int = 32,
    ) -> List[Dict[str, Any]]:
        """Model-bearing non-synthetic state, using the identity owner's lookup."""
        identity = await self.get_identity(agent_id)
        if identity is None:
            return []
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT recorded_at AS observed_at, state_id::text AS record_id,
                       'agent_state'::text AS record_kind,
                       state_json#>'{provenance_context,runtime_provenance}' AS provenance
                FROM core.agent_state
                WHERE identity_id = $1 AND COALESCE(synthetic, false) = false
                  AND recorded_at <= $2
                  AND state_json#>>'{provenance_context,runtime_provenance,schema}' = 's22.runtime_provenance.v1'
                  AND jsonb_typeof(state_json#>'{provenance_context,runtime_provenance,model,identifier}') = 'string'
                  AND length(state_json#>>'{provenance_context,runtime_provenance,model,identifier}') BETWEEN 1 AND 160
                ORDER BY recorded_at DESC, state_id DESC LIMIT $3
                """,
                identity.identity_id, observed_before, max(1, min(limit, 32)),
            )
        return [dict(row) for row in rows]

    async def record_agent_state(
        self,
        identity_id: int,
        entropy: float,
        integrity: float,
        void: float,
        regime: str,
        coherence: float,
        state_json: Optional[Dict[str, Any]] = None,
        risk_score: Optional[float] = None,
        epistemic_class: Optional[str] = None,
    ) -> int:
        from config.governance_config import GovernanceConfig
        payload = dict(state_json or {})
        if epistemic_class is not None:
            payload.setdefault("epistemic_class", epistemic_class)
        async with self.acquire() as conn:
            # stability_index is omitted deliberately — retired in 20684dd1,
            # made nullable by migration 058, so the column lands as NULL
            # ("not measured") instead of a hardcoded float that reads as one.
            state_id = await conn.fetchval(
                """
                INSERT INTO core.agent_state
                    (identity_id, entropy, integrity, volatility, regime,
                     coherence, state_json, epoch, risk_score, epistemic_class)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING state_id
                """,
                identity_id, entropy, integrity, void,  # void maps to volatility column
                regime, coherence, json.dumps(payload),
                GovernanceConfig.CURRENT_EPOCH,
                risk_score,
                epistemic_class,
            )
            # Matview refresh moved to periodic_matview_refresh() in background_tasks.py
            return state_id

    async def record_bootstrap_state(
        self,
        identity_id: int,
        entropy: float,
        integrity: float,
        void: float,
        regime: str,
        coherence: float,
        state_json: Dict[str, Any],
    ) -> tuple[int, bool]:
        """Insert a synthetic bootstrap row, idempotent on (identity_id) via the
        unique partial index from migration 018. Returns (state_id, was_written).

        On UniqueViolationError (race lost or already-bootstrapped), looks up
        the existing bootstrap row and returns its state_id with was_written=False.
        Callers use was_written to populate `bootstrap.written` in the response.
        """
        import asyncpg
        from config.governance_config import GovernanceConfig
        payload = dict(state_json)
        payload.setdefault("epistemic_class", "synthetic")
        async with self.acquire() as conn:
            try:
                state_id = await conn.fetchval(
                    """
                    INSERT INTO core.agent_state
                        (identity_id, entropy, integrity, volatility,
                         regime, coherence, state_json, epoch, synthetic, epistemic_class)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, 'synthetic')
                    RETURNING state_id
                    """,
                    identity_id, entropy, integrity, void,
                    regime, coherence, json.dumps(payload),
                    GovernanceConfig.CURRENT_EPOCH,
                )
                return state_id, True
            except asyncpg.UniqueViolationError:
                existing = await conn.fetchval(
                    """
                    SELECT state_id FROM core.agent_state
                    WHERE identity_id = $1 AND synthetic = true
                    """,
                    identity_id,
                )
                return existing, False

    async def get_bootstrap_state(
        self,
        identity_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Return the bootstrap row for an identity, or None if absent.

        Returns {state_id, state_json} — the digest is read from state_json
        by the handler, not pulled out as a separate column.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT state_id, state_json
                FROM core.agent_state
                WHERE identity_id = $1 AND synthetic = true
                """,
                identity_id,
            )
            if row is None:
                return None
            state_json = row["state_json"]
            if isinstance(state_json, str):
                state_json = json.loads(state_json)
            return {"state_id": row["state_id"], "state_json": state_json}

    async def is_substrate_earned(self, agent_id: str) -> bool:
        """Substrate-earned check for the bootstrap-checkin exemption (§3.5).

        True iff the agent_id is registered in core.substrate_claims (S19's
        canonical resident-attestation registry) OR appears in the small
        Pi-resident allowlist for cross-substrate cases like Lumen.
        """
        from src.mcp_handlers.identity.bootstrap_checkin import (
            PI_RESIDENT_ALLOWLIST,
        )
        if agent_id in PI_RESIDENT_ALLOWLIST:
            return True
        async with self.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT 1 FROM core.substrate_claims WHERE agent_id = $1",
                    agent_id,
                )
            )

    async def list_bootstrap_only_agents(
        self,
        min_age_hours: int = 24,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Agents with a bootstrap row but no measured check-in past the age window.

        This is the validation surface for onboard-bootstrap-checkin §6 —
        the population the proposal exists to count. The default 24-hour
        window filters out agents that just onboarded and may genuinely be
        about to check in; a positive count past 24h means the agent
        bootstrapped and went silent.

        Returns most-recent bootstraps first. Row shape:
          {agent_id, identity_id, bootstrap_state_id, bootstrap_recorded_at,
           bootstrap_age_hours, display_name}
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT i.agent_id,
                       i.identity_id,
                       b.state_id AS bootstrap_state_id,
                       b.recorded_at AS bootstrap_recorded_at,
                       EXTRACT(EPOCH FROM (now() - b.recorded_at)) / 3600.0
                           AS bootstrap_age_hours,
                       (i.metadata->>'display_name') AS display_name
                FROM core.identities i
                JOIN core.agent_state b
                  ON b.identity_id = i.identity_id
                  AND b.synthetic = true
                WHERE NOT EXISTS (
                    SELECT 1 FROM core.agent_state m
                    WHERE m.identity_id = i.identity_id
                      AND m.synthetic = false
                )
                  AND b.recorded_at <= now() - ($1 * interval '1 hour')
                ORDER BY b.recorded_at DESC
                LIMIT $2
                """,
                min_age_hours, limit,
            )
            return [dict(r) for r in rows]

    async def count_bootstrap_only_agents(
        self,
        min_age_hours: int = 24,
    ) -> int:
        """Count of agents bootstrapped past the age window with no measured
        check-in. Cheap query for dashboard count badges."""
        async with self.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM core.identities i
                    JOIN core.agent_state b
                      ON b.identity_id = i.identity_id
                      AND b.synthetic = true
                    WHERE NOT EXISTS (
                        SELECT 1 FROM core.agent_state m
                        WHERE m.identity_id = i.identity_id
                          AND m.synthetic = false
                    )
                      AND b.recorded_at <= now() - ($1 * interval '1 hour')
                    """,
                    min_age_hours,
                )
                or 0
            )

    async def get_latest_agent_state(
        self,
        identity_id: int,
    ) -> Optional[AgentStateRecord]:
        """Latest measured state for an identity. Bootstrap (synthetic) rows
        are excluded by default per onboard-bootstrap-checkin §4.1; this is
        the user-visible "what is this agent's current state" question, and
        a synthetic anchor is not the answer."""
        from config.governance_config import GovernanceConfig
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.state_id, s.identity_id, i.agent_id, s.recorded_at,
                       s.entropy, s.integrity, s.volatility,
                       s.regime, s.coherence, s.state_json, s.epistemic_class
                FROM core.agent_state s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE s.identity_id = $1 AND s.epoch = $2
                  AND s.synthetic = false
                ORDER BY s.recorded_at DESC
                LIMIT 1
                """,
                identity_id, GovernanceConfig.CURRENT_EPOCH,
            )
            if not row:
                return None
            return self._row_to_agent_state(row)

    async def get_agent_state_history(
        self,
        identity_id: int,
        limit: int = 100,
        exclude_synthetic: bool = False,
    ) -> List[AgentStateRecord]:
        """State-row history for an identity.

        Per onboard-bootstrap-checkin §4 inclusion rule #2 ("Identity audit /
        lineage queries"), the default INCLUDES synthetic rows — history is
        the audit/lineage record and legitimately wants the full picture.
        Bootstrap rows in the result carry their flag in `state_json` (and
        the underlying row's `synthetic` column is true) so callers can
        introspect.

        Set `exclude_synthetic=True` for measured-only history reads. The
        canonical caller for that mode is `hydrate_from_db_if_fresh` in
        agent_monitor_state.py — the in-memory monitor must NEVER be seeded
        from a synthetic row, because every downstream consumer of
        monitor.state (self-recovery, dialectic, trajectory ODE) treats
        seeded values as measured. See
 sites #5/#6.
        """
        from config.governance_config import GovernanceConfig
        async with self.acquire() as conn:
            base_sql = """
                SELECT s.state_id, s.identity_id, i.agent_id, s.recorded_at,
                       s.entropy, s.integrity, s.volatility,
                       s.regime, s.coherence, s.state_json, s.epistemic_class
                FROM core.agent_state s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE s.identity_id = $1 AND s.epoch = $2
            """
            if exclude_synthetic:
                base_sql += " AND s.synthetic = false"
            base_sql += " ORDER BY s.recorded_at DESC LIMIT $3"
            rows = await conn.fetch(
                base_sql,
                identity_id, GovernanceConfig.CURRENT_EPOCH, limit,
            )
            return [self._row_to_agent_state(r) for r in rows]

    async def get_all_latest_agent_states(self) -> list[AgentStateRecord]:
        """Get latest measured state per identity, using matview with base-table fallback.

        The matview is measured-only by definition (migration 019 bakes
        `WHERE synthetic = false` into the matview SELECT), so the matview
        path needs no query-time filter. The base-table fallback queries
        agent_state directly and adds the filter explicitly. Both paths
        agree: bootstrap rows never appear here. Per onboard-bootstrap-
        checkin §4.1.
        """
        async with self.acquire() as conn:
            try:
                rows = await conn.fetch(
                    """
                    SELECT state_id, identity_id, agent_id, recorded_at,
                           entropy, integrity, volatility,
                           regime, coherence, state_json, epistemic_class
                    FROM core.mv_latest_agent_states
                    """,
                )
            except Exception:
                # Matview may not exist yet, or may still be on the pre-040
                # projection that lacks epistemic_class. Fall back to the base
                # table rather than requiring a DROP/CREATE matview migration.
                # Filter `synthetic = false` here because the base table
                # contains both measured and synthetic rows.
                from config.governance_config import GovernanceConfig
                logger.debug("Matview unavailable, falling back to base table")
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (s.identity_id)
                           s.state_id, s.identity_id, i.agent_id, s.recorded_at,
                           s.entropy, s.integrity, s.volatility,
                           s.regime, s.coherence, s.state_json, s.epistemic_class
                    FROM core.agent_state s
                    JOIN core.identities i ON i.identity_id = s.identity_id
                    WHERE s.epoch = $1
                      AND s.synthetic = false
                    ORDER BY s.identity_id, s.recorded_at DESC
                    """,
                    GovernanceConfig.CURRENT_EPOCH,
                )
            return [self._row_to_agent_state(r) for r in rows]

    async def reconstruct_eisv_series(
        self,
        agent_id: str,
        window: timedelta,
        *,
        epoch: Optional[int] = None,
    ) -> Dict[str, List[float]]:
        """Reconstruct per-dimension EISV series for an agent over a window.

        R1 helper consumed by `score_trajectory_continuity` (PR 2). Returns
        `{'E': [...], 'I': [...], 'S': [...], 'V': [...]}` ordered by
        `recorded_at` ascending. Empty list per dimension when no rows in
        window — caller (the score primitive) handles exclusion-from-average
        per design doc §v3.3-H.C4.

        Filters:
        - `epoch = GovernanceConfig.CURRENT_EPOCH` (default; pass explicitly to
          override for replay/historical analysis). Mirrors the same filter
          shape used in `get_latest_agent_state` and `get_all_latest_agent_states`.
        - `synthetic = false` — bootstrap rows are claimed-genesis anchors, not
          earned trajectory. R1 plausibility is about earned trajectory shape,
          so synthetic anchors must not inflate similarity scores. (Same posture
          as `get_all_latest_agent_states` per onboard-bootstrap-checkin §4.1.)

        Dimension key 'V' maps to SQL column 'volatility' per the project
        convention; see `_row_to_agent_state` for the analogous Python field
        name 'void' ↔ SQL 'volatility'. R1 uses the user-facing 'V' label
        (matches paper terminology and identity.md ontology lexicon).

 See: §v3.1 'New helper',
             §v3.3-A 'audit-only persistence' (consumes this output),
             §v3.3-F 'epoch column reference'.
        """
        if epoch is None:
            from config.governance_config import GovernanceConfig
            epoch = GovernanceConfig.CURRENT_EPOCH

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.entropy, s.integrity, s.volatility, s.state_json,
                       s.recorded_at
                FROM core.agent_state s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE i.agent_id = $1
                  AND s.epoch = $2
                  AND s.synthetic = false
                  AND s.recorded_at >= NOW() - $3::interval
                ORDER BY s.recorded_at ASC
                """,
                agent_id, epoch, window,
            )

        # Column → EISV mapping per db/base.py: state_json.E → E,
        # integrity → I, entropy → S, volatility → V. The stability_index
        # column was retired in commit 20684dd1 (2026-03-26); migration 058
        # made it nullable and nothing writes or reads it any more.
        series: Dict[str, List[float]] = {"E": [], "I": [], "S": [], "V": []}
        for row in rows:
            sj_raw = row["state_json"]
            sj = json.loads(sj_raw) if isinstance(sj_raw, str) else (sj_raw or {})
            series["E"].append(float(sj.get("E", 0.5)))
            series["I"].append(float(row["integrity"]))
            series["S"].append(float(row["entropy"]))
            series["V"].append(float(row["volatility"]))
        return series

    async def get_recent_cross_agent_activity(
        self,
        exclude_identity_id: int,
        minutes: int = 60,
    ) -> list[dict]:
        """Get recent measured activity from other agents, grouped by agent.

        Returns list of dicts with agent_id, recorded_at (most recent), count.
        Bootstrap (synthetic) rows are excluded — the COUNT is "how many real
        check-ins" and a session-start anchor is not activity. Per onboard-
        bootstrap-checkin §4.1.
        """
        from config.governance_config import GovernanceConfig
        window = minutes or GovernanceConfig.TEMPORAL_CROSS_AGENT_MINUTES
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT i.agent_id,
                       MAX(s.recorded_at) as recorded_at,
                       COUNT(*) as count
                FROM core.agent_state s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE s.identity_id != $1
                  AND s.recorded_at > now() - ($2 * interval '1 minute')
                  AND s.epoch = $3
                  AND s.synthetic = false
                GROUP BY i.agent_id
                ORDER BY MAX(s.recorded_at) DESC
                LIMIT 5
                """,
                exclude_identity_id, window, GovernanceConfig.CURRENT_EPOCH,
            )
            return [dict(r) for r in rows]

    def _row_to_agent_state(self, row) -> AgentStateRecord:
        sj = json.loads(row["state_json"]) if isinstance(row["state_json"], str) else (row["state_json"] or {})
        try:
            epistemic_class = row["epistemic_class"]
        except (KeyError, IndexError):
            epistemic_class = None
        if epistemic_class is not None:
            sj.setdefault("epistemic_class", epistemic_class)
        return AgentStateRecord(
            state_id=row["state_id"],
            identity_id=row["identity_id"],
            agent_id=row["agent_id"],
            recorded_at=row["recorded_at"],
            energy=sj.get("E", 0.5),
            entropy=row["entropy"],
            integrity=row["integrity"],
            void=row["volatility"],  # Map database column 'volatility' to 'void' field
            regime=row["regime"],
            coherence=row["coherence"],
            epistemic_class=epistemic_class,
            state_json=sj,
        )


async def fetch_timeline_checkins(
    conn,
    *,
    start,
    identity_ids: Optional[List[int]],
    classes: List[str],
    limit: int,
) -> list:
    """Measured check-ins since `start`, newest first, for GET /v1/timeline.

    `identity_ids` None reads every agent. `classes` filters on the epistemic
    class (agent report vs substrate). Synthetic bootstrap rows are excluded.
    Each row carries its `state_id`, the stored report excerpt and the
    confidence the agent reported, each None where the row has none;
    `has_self_report` says whether the row recorded a self-report at all.
    """
    return await conn.fetch(
        """
        SELECT s.state_id, s.recorded_at, s.identity_id,
               coalesce(s.epistemic_class,
                        s.state_json->>'epistemic_class') AS epistemic_class,
               s.state_json->>'action' AS action,
               s.state_json->>'verdict' AS verdict,
               s.state_json->>'report_excerpt' AS report_excerpt,
               s.state_json #>> '{eisv_telemetry,self_report,confidence_reported}'
                   AS confidence_reported,
               coalesce(jsonb_typeof(s.state_json #> '{eisv_telemetry,self_report}')
                        = 'object', false) AS has_self_report,
               s.risk_score
        FROM core.agent_state s
        WHERE s.recorded_at >= $1 AND s.synthetic = false
          AND ($2::bigint[] IS NULL OR s.identity_id = ANY($2::bigint[]))
          AND coalesce(s.epistemic_class,
                       s.state_json->>'epistemic_class') = ANY($3::text[])
        ORDER BY s.recorded_at DESC
        LIMIT $4
        """,
        start, identity_ids, classes, limit,
    )


DAILY_GOVERNANCE_TREND_SQL = """
SELECT (recorded_at AT TIME ZONE 'UTC')::date AS day,
       sum(risk_score)   AS risk_sum,
       count(risk_score) AS risk_n,
       count(*) FILTER (WHERE state_json->>'action' = 'guide') AS guide,
       count(*) FILTER (WHERE state_json->>'action' IS NOT NULL
                          AND state_json->>'action' NOT IN ('approve', 'guide')) AS pause
FROM core.agent_state
WHERE recorded_at >= $1 AND synthetic = false
GROUP BY 1
ORDER BY 1
"""


async def fetch_daily_governance_trend_rows(conn, since) -> list:
    """Daily governance trend aggregates over measured agent check-ins."""
    return await conn.fetch(DAILY_GOVERNANCE_TREND_SQL, since)


SYNTHETIC_STATE_SQL = """
SELECT
    synthetic,
    COUNT(*) AS rows,
    COUNT(DISTINCT identity_id) AS identities,
    MIN(recorded_at) AS first_seen,
    MAX(recorded_at) AS last_seen
FROM core.agent_state
GROUP BY synthetic
ORDER BY synthetic
"""


async def count_measured_checkins_by_identity_ids(
    conn, identity_ids: List[int], window
) -> list:
    """Count non-synthetic check-ins per identity_id within `window`."""
    if not identity_ids:
        return []
    return await conn.fetch(
        """
        SELECT identity_id, count(*) AS n
          FROM core.agent_state
         WHERE identity_id = ANY($1::bigint[])
           AND recorded_at > now() - $2::interval
           AND synthetic IS NOT TRUE
         GROUP BY identity_id
        """,
        identity_ids,
        window,
    )


async def fetch_latest_durable_eisv_for_identities(
    conn, identity_ids: List[int]
):
    """Fetch the newest non-synthetic EISV state row across `identity_ids`."""
    if not identity_ids:
        return None
    return await conn.fetchrow(
        """
        SELECT s.recorded_at,
               (s.state_json->>'E')::real AS e,
               s.integrity AS i, s.entropy AS s_entropy, s.volatility AS v,
               s.coherence, s.risk_score,
               s.state_json->>'verdict' AS verdict,
               s.state_json->>'action'  AS action
        FROM core.agent_state s
        WHERE s.identity_id = ANY($1::bigint[])
          AND s.synthetic = false
        ORDER BY s.recorded_at DESC
        LIMIT 1
        """,
        identity_ids,
    )


EISV_TELEMETRY_STATE_HEALTH_SQL = """
WITH state_rows AS MATERIALIZED (
    SELECT
        s.identity_id,
        s.recorded_at,
        date_trunc('day', s.recorded_at)::date AS day,
        s.risk_score::double precision AS state_risk,
        s.coherence::double precision AS state_coherence,
        s.state_json,
        s.state_json->'eisv_telemetry' AS envelope
    FROM core.agent_state s
    WHERE s.synthetic IS NOT TRUE
      AND s.recorded_at >= now() - make_interval(days => $1)
),
extracted AS MATERIALIZED (
    SELECT
        *,
        coalesce(jsonb_typeof(envelope) <> 'null', false) AS envelope_present,
        coalesce(
            jsonb_typeof(envelope) = 'object'
            AND envelope->>'schema' = 'eisv.telemetry.v1',
            false
        ) AS envelope_valid,
        envelope #>> '{measurement,primary,source}' AS primary_source,
        CASE
            WHEN envelope #>> '{measurement,primary,source}' = 'behavioral'
            THEN coalesce(
                envelope #>> '{measurement,behavioral,observation_source}',
                envelope #>> '{measurement,submitted_sensor,source}',
                envelope #>> '{measurement,primary,source}',
                'unknown'
            )
            ELSE coalesce(
                envelope #>> '{measurement,primary,source}',
                envelope #>> '{measurement,behavioral,observation_source}',
                envelope #>> '{measurement,submitted_sensor,source}',
                'unknown'
            )
        END AS measurement_source,
        CASE
            WHEN jsonb_typeof(envelope #> '{measurement,behavioral,confidence}') = 'number'
            THEN (envelope #>> '{measurement,behavioral,confidence}')::double precision
        END AS behavioral_confidence,
        envelope #>> '{measurement,behavioral,warmup,phase}' AS warmup_phase,
        CASE
            WHEN jsonb_typeof(envelope #> '{measurement,behavioral,warmup,is_baselined}') = 'boolean'
            THEN (envelope #>> '{measurement,behavioral,warmup,is_baselined}')::boolean
        END AS is_baselined,
        CASE
            WHEN jsonb_typeof(envelope #> '{derivation,missing_inputs}') = 'array'
            THEN envelope #> '{derivation,missing_inputs}'
            ELSE '[]'::jsonb
        END AS missing_inputs,
        envelope #>> '{policy_evaluation,action}' AS policy_action,
        envelope #>> '{policy_evaluation,inputs,verdict}' AS policy_verdict,
        envelope #>> '{policy_evaluation,inputs,primary_eisv_source}' AS policy_source,
        envelope #>> '{policy_evaluation,inputs,verdict_source}' AS policy_verdict_source,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,inputs,risk_score}') = 'number'
            THEN (envelope #>> '{policy_evaluation,inputs,risk_score}')::double precision
        END AS policy_risk,
        envelope #>> '{policy_evaluation,maturity_gate,outcome}' AS maturity_outcome,
        envelope #>> '{policy_evaluation,maturity_gate,schema}' AS maturity_schema,
        envelope #>> '{policy_evaluation,maturity_gate,measurement_phase}' AS measurement_phase,
        envelope #>> '{policy_evaluation,maturity_gate,ineligibility_reason}' AS maturity_ineligibility_reason,
        envelope #>> '{policy_evaluation,maturity_gate,reset_reason}' AS maturity_reset_reason,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,measurement_ready}') = 'boolean'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,measurement_ready}')::boolean
        END AS measurement_ready,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,eligible}') = 'boolean'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,eligible}')::boolean
        END AS maturity_eligible,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,would_defer}') = 'boolean'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,would_defer}')::boolean
        END AS maturity_would_defer,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,confirmation_count}') = 'number'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,confirmation_count}')::integer
        END AS confirmation_count,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,confirmations_required}') = 'number'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,confirmations_required}')::integer
        END AS confirmations_required,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,actuation_enabled}') = 'boolean'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,actuation_enabled}')::boolean
        END AS maturity_actuation_enabled,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,actuation_ready}') = 'boolean'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,actuation_ready}')::boolean
        END AS maturity_actuation_ready,
        CASE
            WHEN jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate,actuation_applied}') = 'boolean'
            THEN (envelope #>> '{policy_evaluation,maturity_gate,actuation_applied}')::boolean
        END AS maturity_actuation_applied,
        envelope #>> '{enforcement,basis}' AS enforcement_basis,
        CASE
            WHEN jsonb_typeof(envelope #> '{enforcement,requested}') = 'boolean'
            THEN (envelope #>> '{enforcement,requested}')::boolean
        END AS enforcement_requested,
        CASE
            WHEN jsonb_typeof(envelope #> '{enforcement,applied}') = 'boolean'
            THEN (envelope #>> '{enforcement,applied}')::boolean
        END AS enforcement_applied,
        state_json->>'verdict' AS state_verdict
    FROM state_rows
),
normalized AS MATERIALIZED (
    SELECT
        *,
        array_remove(ARRAY[
            CASE WHEN envelope_present AND NOT envelope_valid
                 THEN 'unsupported_envelope_schema' END,
            CASE WHEN envelope_valid AND (
                           nullif(envelope->>'measurement_id', '') IS NULL
                        OR nullif(envelope->>'observed_at', '') IS NULL
                        OR nullif(primary_source, '') IS NULL
                        OR jsonb_typeof(envelope #> '{measurement,primary,values}')
                               IS DISTINCT FROM 'object'
                        OR jsonb_typeof(envelope #> '{measurement,primary,values,E}')
                               IS DISTINCT FROM 'number'
                        OR jsonb_typeof(envelope #> '{measurement,primary,values,I}')
                               IS DISTINCT FROM 'number'
                        OR jsonb_typeof(envelope #> '{measurement,primary,values,S}')
                               IS DISTINCT FROM 'number'
                        OR jsonb_typeof(envelope #> '{measurement,primary,values,V}')
                               IS DISTINCT FROM 'number'
                       )
                 THEN 'measurement_contract_missing' END,
            CASE WHEN envelope_valid AND (
                           jsonb_typeof(envelope->'derivation') IS DISTINCT FROM 'object'
                        OR jsonb_typeof(envelope #> '{derivation,missing_inputs}')
                               IS DISTINCT FROM 'array'
                       )
                 THEN 'derivation_contract_missing' END,
            CASE WHEN envelope_valid AND (
                           jsonb_typeof(envelope->'policy_evaluation')
                               IS DISTINCT FROM 'object'
                        OR jsonb_typeof(envelope #> '{policy_evaluation,inputs}')
                               IS DISTINCT FROM 'object'
                        OR nullif(policy_action, '') IS NULL
                        OR nullif(policy_verdict, '') IS NULL
                        OR nullif(policy_source, '') IS NULL
                        OR nullif(policy_verdict_source, '') IS NULL
                        OR policy_risk IS NULL
                       )
                 THEN 'policy_contract_missing' END,
            CASE WHEN envelope_valid AND (
                           jsonb_typeof(envelope #> '{policy_evaluation,maturity_gate}')
                               IS DISTINCT FROM 'object'
                        OR maturity_schema IS DISTINCT FROM 'eisv.cold-start-confirmation.v1'
                        OR nullif(maturity_outcome, '') IS NULL
                        OR nullif(measurement_phase, '') IS NULL
                        OR measurement_ready IS NULL
                        OR maturity_eligible IS NULL
                        OR maturity_would_defer IS NULL
                        OR confirmation_count IS NULL
                        OR confirmations_required IS NULL
                        OR maturity_actuation_enabled IS NULL
                        OR maturity_actuation_ready IS NULL
                        OR maturity_actuation_applied IS NULL
                       )
                 THEN 'maturity_gate_contract_missing' END,
            CASE WHEN envelope_valid AND (
                           enforcement_requested IS NULL
                        OR enforcement_applied IS NULL
                        OR nullif(enforcement_basis, '') IS NULL
                       )
                 THEN 'enforcement_contract_missing' END,
            CASE WHEN envelope_valid AND policy_risk IS NOT NULL AND state_risk IS NOT NULL
                           AND abs(policy_risk - state_risk) > 0.000001
                 THEN 'policy_risk_mismatch' END,
            CASE WHEN envelope_valid AND policy_verdict IS NOT NULL AND state_verdict IS NOT NULL
                           AND policy_verdict <> state_verdict
                 THEN 'policy_verdict_mismatch' END,
            CASE WHEN envelope_valid AND policy_source IS NOT NULL AND primary_source IS NOT NULL
                           AND policy_source <> primary_source
                 THEN 'policy_source_mismatch' END,
            CASE WHEN envelope_valid AND enforcement_applied IS TRUE
                           AND enforcement_requested IS NOT TRUE
                 THEN 'applied_without_request' END,
            CASE WHEN envelope_valid AND enforcement_requested IS NOT NULL
                           AND enforcement_requested <> (policy_action IN ('pause', 'reject'))
                 THEN 'request_policy_mismatch' END,
            CASE WHEN envelope_valid AND behavioral_confidence IS NOT NULL AND (
                           (behavioral_confidence >= 0.30 AND primary_source <> 'behavioral')
                        OR (behavioral_confidence < 0.30 AND primary_source <> 'ode_fallback')
                       )
                 THEN 'source_confidence_gate_mismatch' END,
            CASE WHEN envelope_valid AND behavioral_confidence IS NOT NULL AND (
                           (behavioral_confidence >= 0.30 AND measurement_ready IS NOT TRUE)
                        OR (behavioral_confidence < 0.30 AND measurement_ready IS NOT FALSE)
                       )
                 THEN 'maturity_readiness_mismatch' END,
            CASE WHEN envelope_valid AND maturity_would_defer IS TRUE
                           AND maturity_eligible IS NOT TRUE
                 THEN 'maturity_defer_without_eligibility' END,
            CASE WHEN envelope_valid AND confirmation_count IS NOT NULL
                           AND confirmations_required IS NOT NULL
                           AND (confirmation_count < 0 OR confirmation_count > confirmations_required)
                 THEN 'maturity_confirmation_count_invalid' END,
            CASE WHEN envelope_valid AND maturity_actuation_applied IS TRUE AND (
                           maturity_actuation_enabled IS NOT TRUE
                        OR maturity_actuation_ready IS NOT TRUE
                       )
                 THEN 'maturity_actuation_without_readiness' END
        ]::text[], NULL) AS contract_violations,
        CASE
            WHEN is_baselined IS TRUE THEN 'baselined'
            WHEN warmup_phase IS NOT NULL THEN warmup_phase
            WHEN is_baselined IS FALSE THEN 'warming'
            ELSE 'unknown'
        END AS warmup_label
    FROM extracted
),
summary AS (
    SELECT
        count(*) AS states,
        count(DISTINCT identity_id) AS agents,
        count(*) FILTER (WHERE envelope_present) AS envelope_rows,
        count(*) FILTER (WHERE envelope_valid) AS envelopes,
        count(DISTINCT identity_id) FILTER (WHERE envelope_valid) AS envelope_agents,
        count(*) FILTER (WHERE envelope_present AND NOT envelope_valid) AS invalid_envelopes,
        min(recorded_at) FILTER (WHERE envelope_valid) AS first_envelope_at,
        max(recorded_at) FILTER (WHERE envelope_valid) AS last_envelope_at,
        count(*) FILTER (WHERE envelope_valid AND primary_source = 'behavioral') AS behavioral_primary,
        count(*) FILTER (WHERE envelope_valid AND primary_source = 'ode_fallback') AS ode_fallback,
        count(*) FILTER (WHERE envelope_valid AND is_baselined IS FALSE) AS warmup,
        count(*) FILTER (WHERE envelope_valid AND jsonb_array_length(missing_inputs) > 0) AS missing,
        count(*) FILTER (WHERE envelope_valid AND measurement_ready IS TRUE) AS measurement_ready,
        count(*) FILTER (WHERE envelope_valid AND maturity_eligible IS TRUE) AS maturity_eligible,
        count(*) FILTER (WHERE envelope_valid AND maturity_would_defer IS TRUE) AS maturity_would_defer,
        count(*) FILTER (WHERE envelope_valid AND maturity_outcome = 'shadow_confirmed') AS maturity_confirmed,
        count(*) FILTER (WHERE envelope_valid AND maturity_actuation_enabled IS TRUE) AS maturity_actuation_enabled,
        count(*) FILTER (WHERE envelope_valid AND maturity_actuation_ready IS TRUE) AS maturity_actuation_ready,
        count(*) FILTER (WHERE envelope_valid AND maturity_actuation_applied IS TRUE) AS maturity_actuation_applied,
        count(*) FILTER (WHERE cardinality(contract_violations) > 0) AS contract_violation_rows,
        coalesce(sum(cardinality(contract_violations)), 0) AS contract_violations,
        count(*) FILTER (WHERE envelope_valid AND enforcement_requested IS TRUE) AS enforcement_requested,
        count(*) FILTER (WHERE envelope_valid AND enforcement_applied IS TRUE) AS enforcement_applied,
        count(*) FILTER (
            WHERE envelope_valid
              AND enforcement_requested IS TRUE
              AND enforcement_applied IS TRUE
        ) AS enforcement_delivered
    FROM normalized
),
daily AS (
    SELECT
        day::text AS day,
        count(*) AS states,
        count(*) FILTER (WHERE envelope_valid) AS envelopes,
        count(*) FILTER (WHERE envelope_present AND NOT envelope_valid) AS invalid_envelopes,
        count(*) FILTER (WHERE envelope_valid AND primary_source = 'behavioral') AS behavioral_primary,
        count(*) FILTER (WHERE envelope_valid AND primary_source = 'ode_fallback') AS ode_fallback,
        count(*) FILTER (WHERE envelope_valid AND is_baselined IS FALSE) AS warmup,
        count(*) FILTER (WHERE envelope_valid AND jsonb_array_length(missing_inputs) > 0) AS missing,
        count(*) FILTER (WHERE envelope_valid AND measurement_ready IS TRUE) AS measurement_ready,
        count(*) FILTER (WHERE envelope_valid AND maturity_would_defer IS TRUE) AS maturity_would_defer
    FROM normalized
    GROUP BY day
),
measurement_sources AS (
    SELECT measurement_source AS source, count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY measurement_source
),
primary_sources AS (
    SELECT primary_source AS source, count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY primary_source
),
warmup_distribution AS (
    SELECT warmup_label AS phase, count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY warmup_label
),
missing_distribution AS (
    SELECT missing_input AS input, count(*) AS observations
    FROM normalized
    CROSS JOIN LATERAL jsonb_array_elements_text(missing_inputs) AS missing_input
    WHERE envelope_valid
    GROUP BY missing_input
),
contract_distribution AS (
    SELECT violation AS type, count(*) AS observations
    FROM normalized
    CROSS JOIN LATERAL unnest(contract_violations) AS violation
    GROUP BY violation
),
maturity_distribution AS (
    SELECT
        coalesce(maturity_outcome, 'unknown') AS outcome,
        count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY coalesce(maturity_outcome, 'unknown')
),
maturity_ineligibility_distribution AS (
    SELECT
        coalesce(maturity_ineligibility_reason, 'none') AS reason,
        count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY coalesce(maturity_ineligibility_reason, 'none')
),
maturity_reset_distribution AS (
    SELECT
        coalesce(maturity_reset_reason, 'none') AS reason,
        count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY coalesce(maturity_reset_reason, 'none')
),
enforcement_basis_distribution AS (
    SELECT
        coalesce(enforcement_basis, 'unknown') AS basis,
        count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY coalesce(enforcement_basis, 'unknown')
),
enforcement_distribution AS (
    SELECT
        CASE
            WHEN enforcement_applied IS TRUE AND enforcement_requested IS NOT TRUE
                THEN 'applied_without_request'
            WHEN enforcement_applied IS TRUE THEN 'applied'
            WHEN enforcement_requested IS TRUE THEN 'requested_not_applied'
            WHEN enforcement_requested IS FALSE THEN 'not_requested'
            ELSE 'unknown'
        END AS stratum,
        count(*) AS observations
    FROM normalized
    WHERE envelope_valid
    GROUP BY stratum
)
SELECT jsonb_build_object(
    'generated_at', now(),
    'summary', jsonb_build_object(
        'states', summary.states,
        'agents', summary.agents,
        'envelope_rows', summary.envelope_rows,
        'envelopes', summary.envelopes,
        'envelope_agents', summary.envelope_agents,
        'invalid_envelopes', summary.invalid_envelopes,
        'invalid_envelope_rate', CASE WHEN summary.envelope_rows > 0 THEN summary.invalid_envelopes::double precision / summary.envelope_rows END,
        'coverage_rate', CASE WHEN summary.states > 0 THEN summary.envelopes::double precision / summary.states END,
        'agent_coverage_rate', CASE WHEN summary.agents > 0 THEN summary.envelope_agents::double precision / summary.agents END,
        'first_envelope_at', summary.first_envelope_at,
        'last_envelope_at', summary.last_envelope_at,
        'behavioral_primary', summary.behavioral_primary,
        'behavioral_primary_rate', CASE WHEN summary.envelopes > 0 THEN summary.behavioral_primary::double precision / summary.envelopes END,
        'ode_fallback', summary.ode_fallback,
        'ode_fallback_rate', CASE WHEN summary.envelopes > 0 THEN summary.ode_fallback::double precision / summary.envelopes END,
        'warmup', summary.warmup,
        'warmup_rate', CASE WHEN summary.envelopes > 0 THEN summary.warmup::double precision / summary.envelopes END,
        'missing', summary.missing,
        'missing_rate', CASE WHEN summary.envelopes > 0 THEN summary.missing::double precision / summary.envelopes END,
        'measurement_ready', summary.measurement_ready,
        'measurement_ready_rate', CASE WHEN summary.envelopes > 0 THEN summary.measurement_ready::double precision / summary.envelopes END,
        'maturity_eligible', summary.maturity_eligible,
        'maturity_eligible_rate', CASE WHEN summary.envelopes > 0 THEN summary.maturity_eligible::double precision / summary.envelopes END,
        'maturity_would_defer', summary.maturity_would_defer,
        'maturity_would_defer_rate', CASE WHEN summary.envelopes > 0 THEN summary.maturity_would_defer::double precision / summary.envelopes END,
        'maturity_confirmed', summary.maturity_confirmed,
        'maturity_actuation_enabled', summary.maturity_actuation_enabled,
        'maturity_actuation_ready', summary.maturity_actuation_ready,
        'maturity_actuation_applied', summary.maturity_actuation_applied,
        'contract_violation_rows', summary.contract_violation_rows,
        'contract_violations', summary.contract_violations,
        'contract_checked_rows', summary.envelope_rows,
        'contract_violation_rate', CASE WHEN summary.envelope_rows > 0 THEN summary.contract_violation_rows::double precision / summary.envelope_rows END,
        'enforcement_requested', summary.enforcement_requested,
        'enforcement_applied', summary.enforcement_applied,
        'enforcement_delivered', summary.enforcement_delivered,
        'enforcement_delivery_rate', CASE WHEN summary.enforcement_requested > 0 THEN summary.enforcement_delivered::double precision / summary.enforcement_requested END
    ),
    'timeline', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'day', day,
            'states', states,
            'envelopes', envelopes,
            'invalid_envelopes', invalid_envelopes,
            'coverage_rate', CASE WHEN states > 0 THEN envelopes::double precision / states END,
            'behavioral_primary', behavioral_primary,
            'ode_fallback', ode_fallback,
            'warmup', warmup,
            'missing', missing,
            'measurement_ready', measurement_ready,
            'maturity_would_defer', maturity_would_defer
        ) ORDER BY day) FROM daily
    ), '[]'::jsonb),
    'measurement_sources', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'source', source,
            'observations', observations,
            'rate', CASE WHEN summary.envelopes > 0 THEN observations::double precision / summary.envelopes END
        ) ORDER BY observations DESC, source) FROM measurement_sources
    ), '[]'::jsonb),
    'primary_sources', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'source', source,
            'observations', observations,
            'rate', CASE WHEN summary.envelopes > 0 THEN observations::double precision / summary.envelopes END
        ) ORDER BY observations DESC, source) FROM primary_sources
    ), '[]'::jsonb),
    'warmup', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'phase', phase,
            'observations', observations,
            'rate', CASE WHEN summary.envelopes > 0 THEN observations::double precision / summary.envelopes END
        ) ORDER BY observations DESC, phase) FROM warmup_distribution
    ), '[]'::jsonb),
    'missing_inputs', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'input', input,
            'observations', observations,
            'rate', CASE WHEN summary.envelopes > 0 THEN observations::double precision / summary.envelopes END
        ) ORDER BY observations DESC, input) FROM missing_distribution
    ), '[]'::jsonb),
    'contract_checks', jsonb_build_object(
        'checked_rows', summary.envelope_rows,
        'violation_rows', summary.contract_violation_rows,
        'violations', summary.contract_violations,
        'by_type', coalesce((
            SELECT jsonb_agg(jsonb_build_object('type', type, 'observations', observations)
                             ORDER BY observations DESC, type)
            FROM contract_distribution
        ), '[]'::jsonb),
        'note', 'Same-row serialization invariants only; differing risk vocabularies are shown separately, not counted as violations.'
    ),
    'maturity_gate', jsonb_build_object(
        'strata', coalesce((
            SELECT jsonb_agg(jsonb_build_object('outcome', outcome, 'observations', observations)
                             ORDER BY observations DESC, outcome)
            FROM maturity_distribution
        ), '[]'::jsonb),
        'ineligibility_reasons', coalesce((
            SELECT jsonb_agg(jsonb_build_object('reason', reason, 'observations', observations)
                             ORDER BY observations DESC, reason)
            FROM maturity_ineligibility_distribution
        ), '[]'::jsonb),
        'reset_reasons', coalesce((
            SELECT jsonb_agg(jsonb_build_object('reason', reason, 'observations', observations)
                             ORDER BY observations DESC, reason)
            FROM maturity_reset_distribution
        ), '[]'::jsonb),
        'note', 'Shadow-only confirmation maturity. would_defer is a counterfactual policy observation, never a suppressed pause.'
    ),
    'enforcement', jsonb_build_object(
        'strata', coalesce((
            SELECT jsonb_agg(jsonb_build_object('stratum', stratum, 'observations', observations)
                             ORDER BY observations DESC, stratum)
            FROM enforcement_distribution
        ), '[]'::jsonb),
        'bases', coalesce((
            SELECT jsonb_agg(jsonb_build_object('basis', basis, 'observations', observations)
                             ORDER BY observations DESC, basis)
            FROM enforcement_basis_distribution
        ), '[]'::jsonb),
        'note', 'Intervention-conditioned delivery counts; not a causal estimate of prevention or harm.'
    )
)
FROM summary
"""
