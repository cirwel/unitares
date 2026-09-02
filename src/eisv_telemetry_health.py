"""Read-only fleet health report for the EISV telemetry envelope.

The report is deliberately observational.  It measures rollout coverage,
provenance completeness, same-row contract consistency, and descriptive
outcome calibration.  It does not feed the monitor, policy, or enforcement
paths and it does not reinterpret EISV as an outcome judgment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
import math
from typing import Any

from src.eisv_telemetry import EISV_TELEMETRY_SCHEMA
from src.grounding.outcome_anchors import (
    DEFAULT_FIXTURE_RULE,
    is_scraped_only_exclusion,
    anchored_outcomes_predicate,
    is_structurally_controlled_fixture,
)


EISV_TELEMETRY_HEALTH_SCHEMA = "eisv.telemetry-health.v1"
CALIBRATION_LEAD_MINUTES = 5.0
MIN_CALIBRATION_BIN_CLUSTERS = 20
MIN_CALIBRATION_COHORT_CLUSTERS = 100

# These are intentionally shown as three distinct vocabularies.  The same
# numeric risk can truthfully occupy different named bands because each surface
# answers a different question.  Surfacing the split is safer than pretending
# one label is canonical without changing the runtime contract.
RISK_VOCABULARIES = (
    {
        "surface": "behavioral_verdict",
        "bands": (
            {"label": "safe", "minimum": 0.0, "maximum_exclusive": 0.35},
            {"label": "caution", "minimum": 0.35, "maximum_exclusive": 0.60},
            {"label": "high-risk", "minimum": 0.60, "maximum_exclusive": None},
        ),
    },
    {
        "surface": "experience_summary",
        "bands": (
            {"label": "low", "minimum": 0.0, "maximum_exclusive": 0.40},
            {"label": "elevated", "minimum": 0.40, "maximum_exclusive": 0.70},
            {"label": "high", "minimum": 0.70, "maximum_exclusive": None},
        ),
    },
    {
        "surface": "health_status",
        "bands": (
            {"label": "healthy", "minimum": 0.0, "maximum_exclusive": 0.45},
            {"label": "moderate", "minimum": 0.45, "maximum_exclusive": 0.70},
            {"label": "critical", "minimum": 0.70, "maximum_exclusive": None},
        ),
        "note": "Coherence and void gates can override the risk-only band.",
    },
)

_CALIBRATION_BINS = (
    ("0.0-0.2", 0.0, 0.2),
    ("0.2-0.4", 0.2, 0.4),
    ("0.4-0.6", 0.4, 0.6),
    ("0.6-0.8", 0.6, 0.8),
    ("0.8-1.0", 0.8, 1.0),
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


_STRICT_ANCHOR_SQL = anchored_outcomes_predicate(table_alias="o")
EISV_TELEMETRY_CALIBRATION_SQL = f"""
SELECT
    o.outcome_id,
    o.ts,
    o.agent_id,
    o.is_bad,
    o.detail,
    ps.recorded_at AS prior_recorded_at,
    ps.risk_score::double precision AS prior_risk,
    ps.state_json #>> '{{eisv_telemetry,schema}}' AS prior_telemetry_schema,
    ps.state_json #>> '{{eisv_telemetry,measurement_id}}' AS prior_measurement_id
FROM audit.outcome_events o
LEFT JOIN LATERAL (
    SELECT s.recorded_at, s.risk_score, s.state_json
    FROM core.identities i
    JOIN core.agent_state s ON s.identity_id = i.identity_id
    WHERE i.agent_id = o.agent_id
      AND s.synthetic IS NOT TRUE
      AND s.recorded_at <= o.ts - ($2::double precision * INTERVAL '1 minute')
    ORDER BY s.recorded_at DESC
    LIMIT 1
) ps ON TRUE
WHERE o.ts >= now() - make_interval(days => $1)
  AND {_STRICT_ANCHOR_SQL}
ORDER BY o.ts
"""


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cluster_key(row: Any) -> str | None:
    measurement_id = _get(row, "prior_measurement_id")
    if measurement_id:
        return f"measurement:{measurement_id}"
    recorded_at = _get(row, "prior_recorded_at")
    agent_id = _get(row, "agent_id")
    if recorded_at is None or not agent_id:
        return None
    timestamp = recorded_at.isoformat() if isinstance(recorded_at, datetime) else str(recorded_at)
    return f"state:{agent_id}:{timestamp}"


def _risk_bin_index(risk: float) -> int | None:
    if risk < 0.0 or risk > 1.0:
        return None
    if risk == 1.0:
        return len(_CALIBRATION_BINS) - 1
    return min(int(risk * len(_CALIBRATION_BINS)), len(_CALIBRATION_BINS) - 1)


def build_calibration_summary(
    rows: Sequence[Any],
    *,
    min_bin_clusters: int = MIN_CALIBRATION_BIN_CLUSTERS,
    min_cohort_clusters: int = MIN_CALIBRATION_COHORT_CLUSTERS,
    fixture_rule: str = DEFAULT_FIXTURE_RULE,
) -> dict[str, Any]:
    """Aggregate strict outcome-linked risk into clustered descriptive bins.

    ``fixture_rule`` selects how a server-stamped ``calibration_excluded`` is
    read; ``scraped_only_rows`` counts rows whose only exclusion is a scraped
    confidence, which the registered rule drops and the corrected rule keeps.
    """
    fixtures_excluded = 0
    scraped_only_rows = 0
    strict_outcomes = 0
    with_prior_state = 0
    with_envelope = 0
    invalid_risk = 0
    bins: list[dict[str, Any]] = [
        {
            "band": label,
            "minimum": low,
            "maximum": high,
            "outcomes": 0,
            "bad_outcomes": 0,
            "clusters": {},
        }
        for label, low, high in _CALIBRATION_BINS
    ]

    for row in rows:
        detail = _get(row, "detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError:
                detail = {}
        if is_scraped_only_exclusion(detail):
            scraped_only_rows += 1
        if is_structurally_controlled_fixture(detail, rule=fixture_rule):
            fixtures_excluded += 1
            continue

        strict_outcomes += 1
        prior_recorded_at = _get(row, "prior_recorded_at")
        if prior_recorded_at is None:
            continue
        with_prior_state += 1
        if _get(row, "prior_telemetry_schema") != EISV_TELEMETRY_SCHEMA:
            continue
        with_envelope += 1

        risk = _as_float(_get(row, "prior_risk"))
        index = _risk_bin_index(risk) if risk is not None else None
        cluster = _cluster_key(row)
        if index is None or cluster is None:
            invalid_risk += 1
            continue
        bad = bool(_get(row, "is_bad"))
        item = bins[index]
        item["outcomes"] += 1
        item["bad_outcomes"] += int(bad)
        item["clusters"][cluster] = item["clusters"].get(cluster, False) or bad

    rendered_bins: list[dict[str, Any]] = []
    cohort_clusters: set[str] = set()
    cohort_bad_clusters: set[str] = set()
    for item in bins:
        clusters = item.pop("clusters")
        cluster_count = len(clusters)
        bad_clusters = sum(1 for bad in clusters.values() if bad)
        cohort_clusters.update(clusters)
        cohort_bad_clusters.update(cluster for cluster, bad in clusters.items() if bad)
        if cluster_count < min_bin_clusters:
            evidence_status = "sparse"
        elif bad_clusters in (0, cluster_count):
            evidence_status = "single_class"
        else:
            evidence_status = "descriptive"
        rendered_bins.append({
            **item,
            "clusters": cluster_count,
            "bad_clusters": bad_clusters,
            "bad_cluster_rate": bad_clusters / cluster_count if cluster_count else None,
            "evidence_status": evidence_status,
        })

    cluster_count = len(cohort_clusters)
    bad_cluster_count = len(cohort_bad_clusters)
    if with_envelope == 0:
        status = "awaiting_envelope"
    elif cluster_count < min_cohort_clusters:
        status = "inconclusive"
    elif bad_cluster_count in (0, cluster_count):
        status = "single_class"
    else:
        status = "descriptive"

    return {
        "status": status,
        "anchor_scope": "strict_external",
        "lead_minutes": CALIBRATION_LEAD_MINUTES,
        "strict_outcomes": strict_outcomes,
        "fixtures_excluded": fixtures_excluded,
        "fixture_rule": fixture_rule,
        "scraped_only_rows": scraped_only_rows,
        "with_prior_state": with_prior_state,
        "with_envelope": with_envelope,
        "envelope_coverage_rate": with_envelope / strict_outcomes if strict_outcomes else None,
        "clusters": cluster_count,
        "bad_clusters": bad_cluster_count,
        "invalid_risk_rows": invalid_risk,
        "minimum_bin_clusters": min_bin_clusters,
        "minimum_cohort_clusters": min_cohort_clusters,
        "bins": rendered_bins,
        "note": (
            "Strict external outcomes only. Rates are descriptive and clustered by the "
            "prior measurement; they do not establish predictive lift or causality."
        ),
    }


def _normalize_json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise TypeError("telemetry health query did not return a JSON object")


async def query_eisv_telemetry_health(
    conn: Any,
    *,
    window_days: int,
) -> dict[str, Any]:
    """Query durable state aggregates and strict outcome-linked calibration."""
    state_report = _normalize_json(
        await conn.fetchval(EISV_TELEMETRY_STATE_HEALTH_SQL, window_days)
    )
    outcome_rows = await conn.fetch(
        EISV_TELEMETRY_CALIBRATION_SQL,
        window_days,
        CALIBRATION_LEAD_MINUTES,
    )
    return {
        "success": True,
        "schema": EISV_TELEMETRY_HEALTH_SCHEMA,
        "window_days": window_days,
        **state_report,
        "risk_vocabularies": [
            {
                **vocabulary,
                "bands": [dict(band) for band in vocabulary["bands"]],
            }
            for vocabulary in RISK_VOCABULARIES
        ],
        "calibration": build_calibration_summary(outcome_rows),
        "semantics": {
            "measurement": "EISV is a proprioceptive estimate, not an outcome judgment.",
            "contract_checks": "Cross-field serialization invariants, not quality scores.",
            "maturity_gate": "Counterfactual shadow evaluation; no pause is suppressed.",
            "enforcement": "Intervention-conditioned delivery accounting, not a causal estimate.",
        },
    }
