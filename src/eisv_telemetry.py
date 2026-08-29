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
import re
from typing import Any
from uuid import uuid4


EISV_TELEMETRY_SCHEMA = "eisv.telemetry.v1"
EISV_TELEMETRY_SUMMARY_SCHEMA = "eisv.telemetry.summary.v1"
EISV_SHADOW_ABLATIONS_SCHEMA = "eisv.shadow_ablations.v1"
EISV_AFFERENTS_SCHEMA = "eisv.submitted_afferents.v1"
BEHAVIORAL_SENSOR_FORMULA_VERSION = "behavioral_sensor.v1"
BEHAVIORAL_HISTORY_WINDOW = 10
OUTCOME_WINDOW = 20
AFFERENT_DIMENSION_LIMIT = 16
AFFERENT_KEY_LENGTH_LIMIT = 64
AFFERENT_PROVENANCE_TEXT_LIMIT = 128

_EISV_KEYS = ("E", "I", "S", "V")
_AFFERENT_SOURCE_FIELDS = ("afferents", "body_anima", "anima")
_AFFERENT_PROVENANCE_KEYS = ("schema", "source", "role", "scale", "units")
_AFFERENT_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_SENSITIVE_AFFERENT_KEY_FRAGMENTS = (
    "accesskey",
    "apikey",
    "auth",
    "credential",
    "passcode",
    "password",
    "privatekey",
    "secret",
    "token",
)
_SENSITIVE_AFFERENT_KEY_TOKENS = frozenset({"otp", "pin"})


def _number(value: Any) -> float | None:
    """Return a finite JSON number without guessing from strings."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
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


def _bounded_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:AFFERENT_PROVENANCE_TEXT_LIMIT]


def _is_sensitive_afferent_key(key: str) -> bool:
    """Recognize credential-shaped names across allowed key separators."""
    tokens = [token for token in re.split(r"[^a-z0-9]+", key.lower()) if token]
    compact = "".join(tokens)
    return (
        any(part in compact for part in _SENSITIVE_AFFERENT_KEY_FRAGMENTS)
        or any(token in _SENSITIVE_AFFERENT_KEY_TOKENS for token in tokens)
    )


def _afferent_values(
    values: Mapping[str, Any] | None,
) -> tuple[dict[str, float], int]:
    """Keep finite numeric afferents and report pre-cap valid cardinality."""
    if not isinstance(values, Mapping):
        return {}, 0

    sanitized: dict[str, float] = {}
    valid_count = 0
    for raw_key in sorted(values, key=lambda item: str(item)):
        if not isinstance(raw_key, str) or raw_key != raw_key.strip():
            continue
        key = raw_key
        if (
            not key
            or len(key) > AFFERENT_KEY_LENGTH_LIMIT
            or _AFFERENT_KEY_PATTERN.fullmatch(key) is None
            or _is_sensitive_afferent_key(key)
        ):
            continue
        number = _number(values[raw_key])
        if number is None:
            continue
        valid_count += 1
        if len(sanitized) < AFFERENT_DIMENSION_LIMIT:
            sanitized[key] = number
    return sanitized, valid_count


def _afferent_provenance(
    sensor_data: Mapping[str, Any],
    *,
    source_field: str,
    inline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce caller-declared provenance to a small non-authoritative shape."""
    state_space = sensor_data.get("state_space_provenance")
    state_space = state_space if isinstance(state_space, Mapping) else {}
    outer = state_space.get(source_field)
    outer = outer if isinstance(outer, Mapping) else {}
    inline = inline if isinstance(inline, Mapping) else {}

    reduced: dict[str, str] = {}
    for key in _AFFERENT_PROVENANCE_KEYS:
        text = _bounded_text(inline.get(key)) or _bounded_text(outer.get(key))
        if text is not None:
            reduced[key] = text
    return {
        "status": "caller_declared" if reduced else "undeclared",
        **reduced,
    }


def build_submitted_afferents(
    sensor_data: Mapping[str, Any] | None,
    *,
    submitted_source: str | None = None,
) -> dict[str, Any] | None:
    """Build a bounded, measurement-only record of raw submitted afferents.

    New callers should use ``sensor_data["afferents"]`` with either a flat
    numeric mapping or ``{"values": {...}, "provenance": {...}}``.  The
    existing ``body_anima`` field and its legacy ``anima`` alias remain accepted
    so embodied check-ins immediately retain their pre-projection dimensions.

    This record is descriptive only.  It is not passed to the behavioral
    estimator, ODE, basin classifier, or policy layer.
    """
    if not isinstance(sensor_data, Mapping):
        return None

    for source_field in _AFFERENT_SOURCE_FIELDS:
        candidate = sensor_data.get(source_field)
        if not isinstance(candidate, Mapping):
            continue

        inline_provenance = None
        values = candidate
        if source_field == "afferents" and isinstance(candidate.get("values"), Mapping):
            values = candidate["values"]
            inline = candidate.get("provenance")
            inline_provenance = inline if isinstance(inline, Mapping) else None

        sanitized, valid_count = _afferent_values(values)
        if not sanitized:
            continue

        return {
            "schema": EISV_AFFERENTS_SCHEMA,
            "source": submitted_source or "untagged_sensor",
            "source_field": source_field,
            "measurement_role": "raw_afferents",
            "policy_applied": False,
            "dimension_limit": AFFERENT_DIMENSION_LIMIT,
            "valid_dimension_count": valid_count,
            "truncated": valid_count > len(sanitized),
            "values": sanitized,
            "provenance": _afferent_provenance(
                sensor_data,
                source_field=source_field,
                inline=inline_provenance,
            ),
        }

    return None


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
    substrate_canaries: Mapping[str, Any] | None = None,
    coherence_source: str | None = None,
    coherence_role: str | None = None,
) -> dict[str, Any]:
    """Capture the bounded inputs used by ``compute_behavioral_sensor_eisv``.

    Histories are capped at ten because every behavioral formula reads only
    that suffix.  Outcome rows are capped at the query limit and stripped of
    free-form detail/text, which the formula never reads.

    ``substrate_canaries`` records, per check-in, which continuity features were
    real measurements and which were defaults or clipped constants. The three
    ``continuity_*`` features above are derived from markdown structure in the
    agent's ``response_text``; that is a valid reading for a natural-language
    assistant turn and a near-constant for anything else. Recording the
    distinction keeps an export consumer from reading a template artifact as
    evidence.
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
            "history_provenance": {
                "coherence": {
                    "source": coherence_source,
                    "role": coherence_role,
                    "usage": ["E_level_term", "I_trend_term"],
                    "health_evidence": False,
                }
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
        "substrate_canaries": dict(substrate_canaries or {}),
        "known_limitations": [
            "coherence_history carries legacy ODE control feedback into the behavioral E/I blend"
        ],
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
    submitted_afferents: Mapping[str, Any] | None = None,
    shadow_ablations: Mapping[str, Any] | None = None,
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
            "coherence": {
                "value": _number(metrics.get("coherence")),
                "source": metrics.get("coherence_source") or "unknown",
                "role": metrics.get("coherence_role") or "unknown",
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
            "submitted_afferents": (
                _json_safe(submitted_afferents)
                if isinstance(submitted_afferents, Mapping)
                else None
            ),
        },
        "derivation": derivation_record,
        "shadow_ablations": {
            "schema": EISV_SHADOW_ABLATIONS_SCHEMA,
            "mode": "measurement_only",
            "policy_applied": False,
            "candidates": _json_safe(dict(shadow_ablations or {})),
        },
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
    coherence = measurement.get("coherence")
    coherence = coherence if isinstance(coherence, Mapping) else {}
    behavioral = measurement.get("behavioral")
    behavioral = behavioral if isinstance(behavioral, Mapping) else {}
    submitted = measurement.get("submitted_sensor")
    submitted = submitted if isinstance(submitted, Mapping) else {}
    afferents = measurement.get("submitted_afferents")
    afferents = afferents if isinstance(afferents, Mapping) else {}
    afferent_values = afferents.get("values")
    afferent_values = afferent_values if isinstance(afferent_values, Mapping) else {}
    derivation = envelope.get("derivation")
    derivation = derivation if isinstance(derivation, Mapping) else {}
    policy = envelope.get("policy_evaluation")
    policy = policy if isinstance(policy, Mapping) else {}
    enforcement = envelope.get("enforcement")
    enforcement = enforcement if isinstance(enforcement, Mapping) else {}
    shadow = envelope.get("shadow_ablations")
    shadow = shadow if isinstance(shadow, Mapping) else {}
    candidates = shadow.get("candidates")
    candidates = candidates if isinstance(candidates, Mapping) else {}
    legacy_candidate = candidates.get("legacy_coherence_neutralized")
    legacy_candidate = (
        legacy_candidate if isinstance(legacy_candidate, Mapping) else {}
    )
    behavioral_shadow = legacy_candidate.get("behavioral_sensor")
    behavioral_shadow = (
        behavioral_shadow if isinstance(behavioral_shadow, Mapping) else {}
    )
    confidence_shadow = legacy_candidate.get("derived_confidence")
    confidence_shadow = (
        confidence_shadow if isinstance(confidence_shadow, Mapping) else {}
    )
    trajectory_shadow = legacy_candidate.get("trajectory_identity")
    trajectory_shadow = (
        trajectory_shadow if isinstance(trajectory_shadow, Mapping) else {}
    )
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
        "coherence_source": coherence.get("source") or "unknown",
        "coherence_role": coherence.get("role") or "unknown",
        "submitted_source": submitted_source,
        "afferents_recorded": bool(afferent_values),
        "afferent_source": afferents.get("source"),
        "afferent_source_field": afferents.get("source_field"),
        "afferent_count": len(afferent_values),
        "afferent_valid_count": (
            afferents.get("valid_dimension_count")
            if isinstance(afferents.get("valid_dimension_count"), int)
            and not isinstance(afferents.get("valid_dimension_count"), bool)
            else None
        ),
        "afferent_truncated": (
            afferents.get("truncated")
            if isinstance(afferents.get("truncated"), bool)
            else None
        ),
        "afferent_keys": sorted(str(key) for key in afferent_values),
        "afferent_policy_applied": (
            afferents.get("policy_applied")
            if isinstance(afferents.get("policy_applied"), bool)
            else None
        ),
        "behavioral_source": behavioral_source,
        "behavioral_confidence": _number(behavioral.get("confidence")),
        "missing_inputs": list(derivation.get("missing_inputs") or ()),
        "shadow_ablation_schema": shadow.get("schema"),
        "legacy_coherence_behavioral_shadow_recorded": bool(behavioral_shadow),
        "legacy_coherence_behavioral_shadow_eligible": (
            behavioral_shadow.get("eligible")
            if isinstance(behavioral_shadow.get("eligible"), bool)
            else None
        ),
        "legacy_coherence_confidence_shadow_recorded": bool(confidence_shadow),
        "legacy_coherence_confidence_shadow_eligible": (
            confidence_shadow.get("eligible")
            if isinstance(confidence_shadow.get("eligible"), bool)
            else None
        ),
        "legacy_coherence_trajectory_shadow_recorded": bool(trajectory_shadow),
        "legacy_coherence_trajectory_shadow_eligible": (
            trajectory_shadow.get("eligible")
            if isinstance(trajectory_shadow.get("eligible"), bool)
            else None
        ),
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
        "actuation_scope": maturity_gate.get("actuation_scope"),
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
        "enforcement_scope": enforcement.get("scope"),
        "enforcement_actuation_id": enforcement.get("actuation_id"),
        "enforcement_applied_at": enforcement.get("applied_at"),
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
        "coherence_source": state.get("coherence_form") or "unknown_legacy",
        "coherence_role": "unknown",
        "submitted_source": submitted_source,
        "afferents_recorded": False,
        "afferent_source": None,
        "afferent_source_field": None,
        "afferent_count": None,
        "afferent_valid_count": None,
        "afferent_truncated": None,
        "afferent_keys": [],
        "afferent_policy_applied": None,
        "behavioral_source": behavioral_source,
        "behavioral_confidence": confidence,
        "missing_inputs": ["eisv_telemetry"],
        "shadow_ablation_schema": None,
        "legacy_coherence_behavioral_shadow_recorded": False,
        "legacy_coherence_behavioral_shadow_eligible": None,
        "legacy_coherence_confidence_shadow_recorded": False,
        "legacy_coherence_confidence_shadow_eligible": None,
        "legacy_coherence_trajectory_shadow_recorded": False,
        "legacy_coherence_trajectory_shadow_eligible": None,
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
        "actuation_scope": None,
        "actuation_enabled": None,
        "actuation_ready": None,
        "actuation_applied": None,
        "actuation_blocker": None,
        "lineage_status": None,
        "epistemic_guard_applied": None,
        "epistemic_guard_class": None,
        "epistemic_guard_ineligibility_reason": None,
        "enforcement_basis": None,
        "enforcement_scope": None,
        "enforcement_actuation_id": None,
        "enforcement_applied_at": None,
        "enforcement_requested": None,
        "enforcement_applied": None,
    }
