#!/usr/bin/env python3
"""Offline, label-free ancestry replay of one ordered behavioral stream.

Input JSON: {"agent_id": "...", "records": [eisv.telemetry.v1, ...]}.
The exporter must supply one identity in update order, without presampling.
No DB, policy evaluation, baseline resets, or calibration is performed.
Usage: python scripts/analysis/eisv_provenance_replay.py trace.json --out replay.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.behavioral_state import BOOTSTRAP_UPDATES, BehavioralEISV  # noqa: E402
from src.eisv_telemetry import BEHAVIORAL_SENSOR_FORMULA_VERSION  # noqa: E402

CATEGORIES = (
    "explicit_default", "default_dependent", "fallback_formula", "observed_controller",
    "observed_other", "unknown", "unresolved_formula",
)
DIMENSIONS = ("E", "I", "S", "V")
LANES = ("E", "I", "S", "V_E_term", "V_I_term")
ROUNDING = 0.00005  # behavioral snapshots serialize to four decimal places


def _bucket(name):
    return {key: float(key == name) for key in CATEGORIES}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _category(term):
    if term.get("observed") is False:
        return "explicit_default"
    if term.get("observed") is not True:
        return "unknown"
    return "observed_controller" if term.get("source") in {
        "governance_decisions", "legacy_tanh_v", "regime_history",
    } else "observed_other"


def input_ledger(record, dimension):
    """Allocate only a reconciled convex recipe; retain all other uncertainty."""
    behavior = record["measurement"]["behavioral"]
    observation = behavior["raw_observation"][dimension]
    trace = record.get("derivation") or {}
    source = behavior.get("observation_source")
    if source == "continuity_fallback" and trace.get("kind") == source and trace.get("formula_version") == 1:
        item = (trace.get("selection") or {}).get(dimension) or {}
        value = item.get("value")
        category = item.get("dependency")
        if _finite(value) and 0 <= value <= 1 and abs(value - observation) <= ROUNDING + 1e-12 and category in CATEGORIES:
            return _bucket(category), "whole fallback input dependency (not fractional attribution)"
        return _bucket("unknown"), "fallback selection unavailable or inconsistent"
    if source not in ("behavioral", "behavioral_sensor") or trace.get("kind") != "behavioral_sensor" or trace.get("formula_version") != BEHAVIORAL_SENSOR_FORMULA_VERSION:
        return _bucket("unknown"), "unsupported or absent derivation"
    detail = ((trace.get("components") or {}).get("dimensions") or {}).get(dimension)
    if not isinstance(detail, dict):
        return _bucket("unknown"), "component trace absent"
    weights = dict.fromkeys(CATEGORIES, 0.0)
    value = 0.0
    try:
        for term in detail["components"]:
            weight, contribution = term["weight"], term["weighted_contribution"]
            if not _finite(weight) or not _finite(contribution) or weight < 0:
                raise ValueError("invalid coefficient")
            weights[_category(term)] += weight
            value += contribution
        exact = math.isclose(value, detail["base_value"], abs_tol=1e-9)
        for term in detail["adjustments"]:
            retained, added, incoming = term["retained_weight"], term["input_weight"], term["input_value"]
            if not all(_finite(x) for x in (retained, added, incoming)) or min(retained, added) < 0:
                raise ValueError("invalid adjustment")
            weights = {key: weight * retained for key, weight in weights.items()}
            weights[_category(term)] += added
            value = value * retained + added * incoming
            exact = exact and math.isclose(value, term["output_value"], abs_tol=1e-9)
        exact = exact and math.isclose(value, detail["value"], abs_tol=1e-9)
        exact = exact and abs(value - observation) <= ROUNDING + 1e-12
        if exact and 0 <= value <= 1 and math.isclose(sum(weights.values()), 1, abs_tol=1e-9):
            return weights, "recorded affine convex recipe reconciled"
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return _bucket("unresolved_formula"), "nonlinear, inconsistent or non-unit recipe; no renormalization"


def _advance(old, new, alpha):
    return {key: (1 - alpha) * old[key] + alpha * new[key] for key in CATEGORIES}


def _supported(behavior):
    if not isinstance(behavior, dict) or behavior.get("v_formula_version") != 2:
        return False
    n = behavior.get("updates")
    return (
        isinstance(n, int) and not isinstance(n, bool) and n >= 1
        and all(_finite((behavior.get("smoothed") or {}).get(d)) for d in DIMENSIONS)
        and all(_finite((behavior.get("raw_observation") or {}).get(d)) for d in DIMENSIONS[:3])
        and all(_finite((behavior.get("alphas") or {}).get(d)) for d in DIMENSIONS)
    )


def replay(packet):
    """Anchor first/gapped states as unknown; replay only adjacent v2 updates.

    V's two legs retain their ancestry separately. Their common initial
    unknown anchor is counted once in V, never subtracted into false certainty.
    """
    if not isinstance(packet.get("agent_id"), str) or not packet["agent_id"]:
        raise ValueError("agent_id must identify the single exported stream")
    records = packet["records"]
    seen = set()
    for record in records:
        if record.get("schema") != "eisv.telemetry.v1":
            raise ValueError("unsupported telemetry schema")
        measurement_id = record.get("measurement_id")
        if not measurement_id or measurement_id in seen:
            raise ValueError("unique measurement_id required; duplicate ingestion is not a new update")
        seen.add(measurement_id)
    rows = []
    previous = None
    lanes = {d: _bucket("unknown") for d in LANES}
    shared_anchor = 1.0
    for record in records:
        behavior = (record.get("measurement") or {}).get("behavioral")
        supported = _supported(behavior)
        status = "unknown_anchor"
        reason = "first record, unsupported metadata, gap, reset or reordered count"
        gains, errors, inputs = {}, {}, {}
        adjacent = supported and previous is not None and behavior["updates"] == previous["updates"] + 1
        if adjacent:
            n_before = previous["updates"]
            boost = 0.5 * max(0, 1 - n_before / BOOTSTRAP_UPDATES)
            gains = {d: behavior["alphas"][d] + boost for d in DIMENSIONS}
            if all(0 <= a <= 1 for a in gains.values()):
                state = BehavioralEISV(**previous["smoothed"], alphas=dict(behavior["alphas"]), update_count=n_before)
                state.update(**{f"{d}_obs": behavior["raw_observation"][d] for d in DIMENSIONS[:3]})
                errors = {d: abs(getattr(state, d) - behavior["smoothed"][d]) for d in DIMENSIONS}
                bounds = {d: (2 + (gains[d] if d == "V" else 0)) * ROUNDING for d in DIMENSIONS}
                if all(errors[d] <= bounds[d] + 1e-12 for d in DIMENSIONS):
                    inputs = {d: input_ledger(record, d) for d in DIMENSIONS[:3]}
                    for d in DIMENSIONS[:3]:
                        lanes[d] = _advance(lanes[d], inputs[d][0], gains[d])
                    for d in ("E", "I"):
                        lanes[f"V_{d}_term"] = _advance(lanes[f"V_{d}_term"], inputs[d][0], gains["V"])
                    shared_anchor *= 1 - gains["V"]
                    status, reason = "reconciled", "real BehavioralEISV recurrence within serialized rounding bounds"
                else:
                    reason = "numeric recurrence mismatch; history re-anchored as unknown"
            else:
                reason = "non-convex effective gains; history re-anchored as unknown"
        if status != "reconciled":
            lanes = {d: _bucket("unknown") for d in LANES}
            shared_anchor = 1.0
        rows.append({
            "measurement_id": record["measurement_id"],
            "updates": behavior.get("updates") if isinstance(behavior, dict) else None,
            "status": status, "reason": reason, "effective_gains": gains,
            "absolute_replay_error": errors,
            "input_recipe_status": {d: value[1] for d, value in inputs.items()},
            "coefficients": {d: dict(value) for d, value in lanes.items()},
            "V_shared_anchor_coefficient": shared_anchor,
        })
        previous = behavior if supported else None
    return {
        "schema": "eisv.provenance_replay.v1", "agent_id": packet["agent_id"],
        "mode": "offline_observational", "policy_applied": False,
        "limitations": [
            "coefficients are computational ancestry, not confidence, information or readiness",
            "initial/carry history is unknown; reset/gap boundaries re-anchor, never invent bootstrap ancestry",
            "V legs share one anchor and must not be summed or canceled to claim certainty",
            "fallback default_dependent allocates the whole input, not a fraction of its formula",
            "fallback_formula retains missing/nonpositive-gap formula dependence even for an observed zero report length",
            "observed categories reflect recorded presence at the computation boundary, not recursive upstream/default-free ancestry",
            "baseline provenance and ODE innovations are not replayed; no policy or outcome claims",
            "exporter asserts single identity and ordered records; no population inference",
        ],
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.write_text(json.dumps(replay(json.loads(args.trace.read_text())), indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
