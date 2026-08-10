"""Versioned provenance envelopes for EISV observations.

This module is deliberately observational.  It serializes the measurements,
derivation inputs, policy evaluation, and actuator result that already exist;
none of its helpers participate in scoring or policy decisions.

The append-only PostgreSQL path stores one full ``eisv.telemetry.v1`` envelope
per check-in.  WebSocket/dashboard surfaces use the bounded summary so they do
not replicate the behavioral input windows into the broadcaster ring buffer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import math
from numbers import Real
from typing import Any
from uuid import uuid4


EISV_TELEMETRY_SCHEMA = "eisv.telemetry.v1"
EISV_TELEMETRY_SUMMARY_SCHEMA = "eisv.telemetry.summary.v1"
BEHAVIORAL_SENSOR_FORMULA_VERSION = "behavioral_sensor.v1"
BEHAVIORAL_HISTORY_WINDOW = 10
OUTCOME_WINDOW = 20

_EISV_KEYS = ("E", "I", "S", "V")


def _number(value: Any) -> float | None:
    """Return a finite JSON number without guessing from strings."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    """Convert known telemetry shapes to JSON-native values.

    Unknown objects become ``None`` rather than a repr: telemetry must not
    accidentally serialize credentials or implementation internals carried by
    an unrelated object.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Real):
        return _number(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return None


def _history(values: Sequence[Any] | None, *, text: bool = False) -> list[Any]:
    window = list(values or ())[-BEHAVIORAL_HISTORY_WINDOW:]
    if text:
        return [str(value) for value in window]
    return [_number(value) for value in window]


def _eisv_values(values: Mapping[str, Any] | None) -> dict[str, float | None]:
    source = values if isinstance(values, Mapping) else {}
    return {key: _number(source.get(key)) for key in _EISV_KEYS}


def _outcome_value(outcome: Any, key: str) -> Any:
    if isinstance(outcome, Mapping):
        return outcome.get(key)
    try:
        return outcome[key]
    except (KeyError, TypeError, IndexError):
        return None


def _sanitize_outcomes(outcomes: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Keep only the fields the behavioral formula actually consumes or gates on."""
    sanitized: list[dict[str, Any]] = []
    for outcome in list(outcomes or ())[-OUTCOME_WINDOW:]:
        row = {
            "outcome_type": _outcome_value(outcome, "outcome_type"),
            "is_bad": bool(_outcome_value(outcome, "is_bad")),
            "outcome_score": _number(_outcome_value(outcome, "outcome_score")),
            "verification_source": _outcome_value(outcome, "verification_source"),
        }
        sanitized.append(_json_safe(row))
    return sanitized


def build_behavioral_derivation(
    *,
    decision_history: Sequence[Any],
    coherence_history: Sequence[Any],
    regime_history: Sequence[Any],
    E_history: Sequence[Any],
    I_history: Sequence[Any],
    calibration_error: Any = None,
    drift_norm: Any = None,
    complexity_divergence: Any = None,
    continuity_E_input: Any = None,
    continuity_I_input: Any = None,
    continuity_S_input: Any = None,
    outcome_history: Sequence[Any] | None = None,
    tool_error_rate: Any = None,
    tool_call_velocity: Any = None,
    unique_tools_ratio: Any = None,
    computed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture the bounded inputs used by ``compute_behavioral_sensor_eisv``.

    Histories are capped at ten because every behavioral formula reads only
    that suffix.  Outcome rows are capped at the query limit and stripped of
    free-form detail/text, which the formula never reads.
    """
    features = {
        "calibration_error": _number(calibration_error),
        "drift_norm": _number(drift_norm),
        "complexity_divergence": _number(complexity_divergence),
        "continuity_E_input": _number(continuity_E_input),
        "continuity_I_input": _number(continuity_I_input),
        "continuity_S_input": _number(continuity_S_input),
        "tool_error_rate": _number(tool_error_rate),
        "tool_call_velocity": _number(tool_call_velocity),
        "unique_tools_ratio": _number(unique_tools_ratio),
    }
    missing_inputs = sorted(key for key, value in features.items() if value is None)
    if outcome_history is None:
        missing_inputs.append("outcome_history")

    return {
        "kind": "behavioral_sensor",
        "formula": "src.behavioral_sensor.compute_behavioral_sensor_eisv",
        "formula_version": BEHAVIORAL_SENSOR_FORMULA_VERSION,
        "history_window": BEHAVIORAL_HISTORY_WINDOW,
        "inputs": {
            "history": {
                "decision": _history(decision_history, text=True),
                "coherence": _history(coherence_history),
                "regime": _history(regime_history, text=True),
                "E": _history(E_history),
                "I": _history(I_history),
            },
            "features": features,
            "outcomes": _sanitize_outcomes(outcome_history),
        },
        # S_history and V_history remain accepted by the legacy function
        # signature but are not read by its current formulas.  Naming that fact
        # prevents an export consumer from assuming every supplied argument was
        # evidence for the observation.
        "unused_legacy_parameters": ["S_history", "V_history"],
        "missing_inputs": missing_inputs,
        "computed_observation": _eisv_values(computed),
    }


def build_physical_sensor_derivation(sensor_eisv: Mapping[str, Any]) -> dict[str, Any]:
    """Describe a caller-published sensor without treating it as verified truth."""
    return {
        "kind": "caller_published_sensor",
        "formula": None,
        "formula_version": None,
        "inputs": {"sensor_eisv": _eisv_values(sensor_eisv)},
        "missing_inputs": [],
        "computed_observation": _eisv_values(sensor_eisv),
    }


def build_eisv_telemetry_envelope(
    *,
    metrics: Mapping[str, Any],
    behavioral_snapshot: Mapping[str, Any] | None,
    submitted_sensor: Mapping[str, Any] | None,
    submitted_source: str | None,
    derivation: Mapping[str, Any] | None,
    policy_evaluation: Mapping[str, Any] | None,
    enforcement: Mapping[str, Any] | None,
    observed_at: str | None = None,
    measurement_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the full append-only EISV telemetry record."""
    behavior = behavioral_snapshot if isinstance(behavioral_snapshot, Mapping) else None
    raw_obs = list(behavior.get("raw_obs") or ()) if behavior else []
    behavioral = None
    if behavior is not None:
        behavioral = {
            "observation_source": behavior.get("obs_source"),
            "raw_observation": {
                "E": _number(raw_obs[0]) if len(raw_obs) > 0 else None,
                "I": _number(raw_obs[1]) if len(raw_obs) > 1 else None,
                "S": _number(raw_obs[2]) if len(raw_obs) > 2 else None,
            },
            "smoothed": _eisv_values(behavior),
            "confidence": _number(behavior.get("confidence")),
            "updates": behavior.get("updates") if isinstance(behavior.get("updates"), int) else None,
            "warmup": _json_safe(behavior.get("warmup")),
            "alphas": _json_safe(behavior.get("alphas")),
            "v_formula_version": behavior.get("v_formula_version"),
        }

    primary_source = metrics.get("primary_eisv_source") or "unknown"
    ode_values = metrics.get("ode") if isinstance(metrics.get("ode"), Mapping) else None
    derivation_record = _json_safe(derivation) if isinstance(derivation, Mapping) else {
        "kind": (behavioral or {}).get("observation_source") or submitted_source or primary_source,
        "formula": None,
        "formula_version": None,
        "inputs": {},
        "missing_inputs": ["derivation_trace"],
        "computed_observation": None,
    }

    return {
        "schema": EISV_TELEMETRY_SCHEMA,
        "measurement_id": measurement_id or str(uuid4()),
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "measurement": {
            "primary": {
                "source": primary_source,
                "values": _eisv_values(metrics),
            },
            "behavioral": behavioral,
            "ode": {
                "source": "ode_diagnostic",
                "values": _eisv_values(ode_values or metrics),
            },
            "submitted_sensor": (
                {
                    "source": submitted_source or "untagged_sensor",
                    "values": _eisv_values(submitted_sensor),
                }
                if isinstance(submitted_sensor, Mapping)
                else None
            ),
        },
        "derivation": derivation_record,
        "policy_evaluation": _json_safe(policy_evaluation or {}),
        "enforcement": _json_safe(enforcement or {}),
        "semantics": {
            "measurement": "Proprioceptive state estimate; not an outcome judgment or actuator.",
            "policy": "Interpretation that consumes measurements and requests guidance/action.",
            "enforcement": "Authenticated runtime effect, recorded separately from the policy request.",
        },
    }


def summarize_eisv_telemetry(envelope: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the compact event/dashboard projection of a full envelope."""
    if not isinstance(envelope, Mapping):
        return None
    measurement = envelope.get("measurement")
    measurement = measurement if isinstance(measurement, Mapping) else {}
    primary = measurement.get("primary")
    primary = primary if isinstance(primary, Mapping) else {}
    behavioral = measurement.get("behavioral")
    behavioral = behavioral if isinstance(behavioral, Mapping) else {}
    submitted = measurement.get("submitted_sensor")
    submitted = submitted if isinstance(submitted, Mapping) else {}
    derivation = envelope.get("derivation")
    derivation = derivation if isinstance(derivation, Mapping) else {}
    policy = envelope.get("policy_evaluation")
    policy = policy if isinstance(policy, Mapping) else {}
    enforcement = envelope.get("enforcement")
    enforcement = enforcement if isinstance(enforcement, Mapping) else {}
    maturity_gate = policy.get("maturity_gate")
    maturity_gate = maturity_gate if isinstance(maturity_gate, Mapping) else {}
    epistemic_gate = policy.get("epistemic_gate")
    epistemic_gate = epistemic_gate if isinstance(epistemic_gate, Mapping) else {}

    behavioral_source = behavioral.get("observation_source")
    submitted_source = submitted.get("source")
    primary_source = primary.get("source")
    # When the primary vector is the warmed behavioral EMA, expose the
    # instrument that fed that estimator (physical, behavioral_sensor, ...).
    # During warm-up the presented values are still the ODE fallback, so naming
    # the latent behavioral input here would mislabel the numbers on the chart.
    measurement_source = (
        behavioral_source or submitted_source or primary_source or "unknown"
        if primary_source == "behavioral"
        else primary_source or behavioral_source or submitted_source or "unknown"
    )
    return {
        "schema": EISV_TELEMETRY_SUMMARY_SCHEMA,
        "envelope_schema": envelope.get("schema"),
        "measurement_id": envelope.get("measurement_id"),
        "measurement_source": measurement_source,
        "primary_source": primary_source or "unknown",
        "submitted_source": submitted_source,
        "behavioral_source": behavioral_source,
        "behavioral_confidence": _number(behavioral.get("confidence")),
        "missing_inputs": list(derivation.get("missing_inputs") or ()),
        "policy_action": policy.get("action"),
        "policy_sub_action": policy.get("sub_action"),
        "verdict_source": (
            (policy.get("inputs") or {}).get("verdict_source")
            if isinstance(policy.get("inputs"), Mapping)
            else maturity_gate.get("primary_driver")
        ),
        "measurement_phase": maturity_gate.get("measurement_phase"),
        "measurement_ready": maturity_gate.get("measurement_ready"),
        "maturity_gate_outcome": maturity_gate.get("outcome"),
        "maturity_gate_eligible": maturity_gate.get("eligible"),
        "maturity_gate_would_defer": maturity_gate.get("would_defer"),
        "maturity_ineligibility_reason": maturity_gate.get("ineligibility_reason"),
        "maturity_reset_reason": maturity_gate.get("reset_reason"),
        "maturity_independent_override": maturity_gate.get("independent_override"),
        "confirmation_count": maturity_gate.get("confirmation_count"),
        "confirmations_required": maturity_gate.get("confirmations_required"),
        "actuation_enabled": maturity_gate.get("actuation_enabled"),
        "actuation_ready": maturity_gate.get("actuation_ready"),
        "actuation_applied": maturity_gate.get("actuation_applied"),
        "actuation_blocker": maturity_gate.get("actuation_blocker"),
        "lineage_status": maturity_gate.get("lineage_status"),
        "epistemic_guard_applied": epistemic_gate.get("applied"),
        "epistemic_guard_class": epistemic_gate.get("epistemic_class"),
        "epistemic_guard_ineligibility_reason": epistemic_gate.get(
            "ineligibility_reason"
        ),
        "enforcement_basis": enforcement.get("basis"),
        "enforcement_requested": bool(enforcement.get("requested", False)),
        "enforcement_applied": bool(enforcement.get("applied", False)),
    }


def summarize_state_eisv_telemetry(state_json: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize a state row, including an honest legacy fallback."""
    state = state_json if isinstance(state_json, Mapping) else {}
    summary = summarize_eisv_telemetry(state.get("eisv_telemetry"))
    if summary is not None:
        return summary

    behavioral = state.get("behavioral_eisv")
    behavioral = behavioral if isinstance(behavioral, Mapping) else {}
    behavioral_source = behavioral.get("obs_source")
    submitted_source = state.get("sensor_eisv_source")
    confidence = _number(behavioral.get("confidence"))
    primary_source = "behavioral" if confidence is not None and confidence >= 0.3 else "ode_fallback"
    measurement_source = (
        behavioral_source or submitted_source or primary_source
        if primary_source == "behavioral"
        else primary_source
    )
    return {
        "schema": "eisv.telemetry.summary.legacy",
        "envelope_schema": None,
        "measurement_id": None,
        "measurement_source": measurement_source,
        "primary_source": primary_source,
        "submitted_source": submitted_source,
        "behavioral_source": behavioral_source,
        "behavioral_confidence": confidence,
        "missing_inputs": ["eisv_telemetry"],
        "policy_action": state.get("action"),
        "policy_sub_action": None,
        "verdict_source": None,
        "measurement_phase": None,
        "measurement_ready": None,
        "maturity_gate_outcome": None,
        "maturity_gate_eligible": None,
        "maturity_gate_would_defer": None,
        "maturity_ineligibility_reason": None,
        "maturity_reset_reason": None,
        "maturity_independent_override": None,
        "confirmation_count": None,
        "confirmations_required": None,
        "actuation_enabled": None,
        "actuation_ready": None,
        "actuation_applied": None,
        "actuation_blocker": None,
        "lineage_status": None,
        "epistemic_guard_applied": None,
        "epistemic_guard_class": None,
        "epistemic_guard_ineligibility_reason": None,
        "enforcement_basis": None,
        "enforcement_requested": None,
        "enforcement_applied": None,
    }
