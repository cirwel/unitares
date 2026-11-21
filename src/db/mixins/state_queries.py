"""Standalone read-only ``core.agent_state`` SQL builders and queries.

Sibling to ``src/db/mixins/state.py`` under the ``eisv`` component so callers in
other components (``identity``, ``http``) can delegate ``core.agent_state``
queries to the table's owner without embedding foreign SQL or pulling in the
full ``StateMixin`` / monitor runtime.
"""

from __future__ import annotations


def latest_measured_state_verdict_lateral_sql(identity_alias: str = "i") -> str:
    """Return a LATERAL subquery selecting the latest non-synthetic verdict,
    action, and risk state columns from ``core.agent_state`` for
    ``identity_alias``."""
    return f"""
                    SELECT state_json, risk_score, recorded_at
                    FROM core.agent_state
                    WHERE identity_id = {identity_alias}.identity_id AND synthetic = false
                    ORDER BY recorded_at DESC
                    LIMIT 1
                """


def agent_state_left_join_on_identity_sql(
    state_alias: str = "s",
    identity_alias: str = "i",
) -> str:
    """Return a LEFT JOIN against ``core.agent_state`` keyed on ``identity_id``."""
    return (
        f"LEFT JOIN core.agent_state {state_alias} "
        f"ON {state_alias}.identity_id = {identity_alias}.identity_id"
    )


def latest_state_provenance_lateral_sql(
    identity_alias: str = "i",
    state_alias: str = "s2",
) -> str:
    """Return a LATERAL subquery selecting the latest provenance-context
    routing fields from ``core.agent_state`` for ``identity_alias``."""
    return f"""
        SELECT
            {state_alias}.state_json#>>'{{provenance_context,session_resolution_source}}'
                AS session_resolution_source,
            {state_alias}.state_json#>>'{{provenance_context,transport}}' AS transport,
            {state_alias}.state_json#>>'{{provenance_context,context_source}}' AS context_source
        FROM core.agent_state {state_alias}
        WHERE {state_alias}.identity_id = {identity_alias}.identity_id
        ORDER BY {state_alias}.recorded_at DESC
        LIMIT 1
    """


def _build_model_risk_cohort_sql() -> str:
    from .identity import identity_join_on_id_sql

    identity_join = identity_join_on_id_sql(source_alias="s", identity_alias="i")
    return f"""
WITH measured AS (
    SELECT
        s.state_id,
        s.identity_id,
        i.agent_id AS agent_uuid,
        s.recorded_at,
        s.risk_score,
        s.state_json,
        row_number() OVER (
            PARTITION BY s.identity_id ORDER BY s.recorded_at, s.state_id
        ) AS measured_update_index,
        min(s.recorded_at) OVER (PARTITION BY s.identity_id) AS session_started_at
    FROM core.agent_state s
    {identity_join}
    WHERE COALESCE(s.synthetic, false) = false
)
SELECT
    state_id::TEXT AS state_id,
    agent_uuid,
    recorded_at,
    risk_score,
    state_json->'provenance_context'->'runtime_provenance'
        AS runtime_provenance,
    state_json#>>'{{provenance_context,task_type}}' AS task_type,
    state_json#>>'{{eisv_telemetry,measurement,behavioral,updates}}'
        AS behavioral_updates,
    state_json#>>'{{eisv_telemetry,measurement,behavioral,warmup,is_baselined}}'
        AS is_baselined,
    measured_update_index,
    EXTRACT(EPOCH FROM (recorded_at - session_started_at)) AS exposure_seconds
FROM measured
WHERE recorded_at >= $1
  AND recorded_at < $2
ORDER BY recorded_at, state_id
LIMIT $3
"""


def _build_agent_state_s22_sql() -> str:
    from .identity import identity_join_on_id_sql

    identity_join = identity_join_on_id_sql(source_alias="s", identity_alias="i")
    return f"""
SELECT
    s.state_id::TEXT AS entry_id,
    'agent_state' AS source,
    i.agent_id AS agent_id,
    s.recorded_at AS recorded_at,
    s.state_json->'provenance_context' AS s22_context,
    s.state_json AS state_json
FROM core.agent_state s
{identity_join}
WHERE s.state_json ? 'provenance_context'
  AND (
    $2::TEXT IS NULL
    OR COALESCE(
        s.state_json#>>'{{provenance_context,comparison_key}}',
        s.state_json#>>'{{provenance_context,task_label}}',
        s.state_json#>>'{{provenance_context,task}}'
    ) = $2
  )
ORDER BY s.recorded_at DESC
LIMIT $1
"""


MODEL_RISK_COHORT_SQL = _build_model_risk_cohort_sql()
AGENT_STATE_S22_SQL = _build_agent_state_s22_sql()
