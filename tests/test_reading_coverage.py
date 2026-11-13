"""The agent-facing account of which behavioral axes were measured.

The account restates the observed flags of the derivation the check-in built,
so the tests build real derivations and pin the account to them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.behavioral_sensor import compute_behavioral_sensor_eisv
from src.eisv_telemetry import build_behavioral_derivation, build_physical_sensor_derivation
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response
from src.reading_coverage import build_reading_coverage, evidence_key, mirror_line


def _inputs(**overrides) -> dict:
    inputs = dict(
        decision_history=["proceed"] * 4,
        coherence_history=[0.48, 0.49, 0.50, 0.50],
        regime_history=["convergence"] * 4,
        E_history=[0.7, 0.71, 0.72],
        I_history=[0.78, 0.79, 0.79],
        S_history=[0.2, 0.2, 0.2],
        V_history=[0.0, 0.0, 0.0],
    )
    inputs.update(overrides)
    return inputs


def _coverage(**overrides):
    inputs = _inputs(**overrides)
    computed = compute_behavioral_sensor_eisv(**inputs)
    return build_reading_coverage(build_behavioral_derivation(**inputs, computed=computed))


def _good_outcomes(n: int = 4) -> list[dict]:
    return [
        {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 0.9,
         "verification_source": "external_signal"}
        for _ in range(n)
    ]


def test_only_drift_supplied_leaves_e_and_i_as_defaults():
    coverage = _coverage(drift_norm=0.3, continuity_E_input=0.3, unique_tools_ratio=0.02)

    assert coverage["measured"] == ["S"]
    assert coverage["defaulted"] == {
        "E": ["complexity_calibration"],
        "I": ["uncalibrated_base_fallback"],
    }
    assert coverage["v_rests_on_defaults"] is True
    # Carried from the derivation's own unobserved_share, not recomputed.
    assert coverage["unobserved_share"] == {"E": 0.8, "I": 0.9, "S": 0.0}


def test_later_adjustments_alone_do_not_make_an_axis_measured():
    # continuity_E_input and tool success are blends over a defaulted base.
    coverage = _coverage(drift_norm=0.3, continuity_E_input=0.6, tool_error_rate=0.1)

    assert "E" in coverage["defaulted"]


def test_complexity_gap_measures_e_but_not_i():
    coverage = _coverage(drift_norm=0.3, complexity_divergence=0.13)

    assert coverage["measured"] == ["E", "S"]
    assert set(coverage["defaulted"]) == {"I"}
    assert coverage["v_rests_on_defaults"] is True


def test_outcomes_and_inputs_measure_every_axis():
    coverage = _coverage(
        drift_norm=0.3, complexity_divergence=0.13, outcome_history=_good_outcomes()
    )

    assert coverage["measured"] == ["E", "I", "S"]
    assert coverage["defaulted"] == {}
    assert coverage["v_rests_on_defaults"] is False


def test_nothing_supplied_defaults_every_axis():
    coverage = _coverage()

    assert coverage["measured"] == []
    assert set(coverage["defaulted"]) == {"E", "I", "S"}


def test_none_without_a_behavioral_component_record():
    assert build_reading_coverage(None) is None
    assert build_reading_coverage(build_physical_sensor_derivation({"E": 0.5})) is None
    short = _inputs(decision_history=["proceed"], coherence_history=[0.5])
    assert build_reading_coverage(build_behavioral_derivation(**short, computed=None)) is None


def test_evidence_key_tracks_the_defaulted_set_only():
    only_drift = _coverage(drift_norm=0.3)
    same_set = _coverage(drift_norm=0.9, unique_tools_ratio=0.5)
    with_gap = _coverage(drift_norm=0.3, complexity_divergence=0.13)

    assert evidence_key(only_drift) == evidence_key(same_set)
    assert evidence_key(only_drift) != evidence_key(with_gap)


def test_mirror_line_names_defaults_readings_and_v():
    line = mirror_line(_coverage(drift_norm=0.3))

    assert line.startswith("Reading coverage: E and I in this check-in are defaults, not a reading.")
    assert "E reads the gap between the complexity you report" in line
    assert "I reads your recent confidence-and-outcome pairs" in line
    assert "S was measured." in line
    assert "V is derived from E and I" in line


def test_mirror_line_for_one_defaulted_axis():
    line = mirror_line(_coverage(drift_norm=0.3, complexity_divergence=0.13))

    assert line.startswith("Reading coverage: I in this check-in is a default, not a reading.")
    assert "E and S were measured." in line


def _payload(coverage) -> dict:
    payload = {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        "decision": {"action": "proceed", "sub_action": "approve", "margin": "comfortable"},
        "metrics": {"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0, "risk_score": 0.27,
                    "verdict": "safe", "coherence": 0.48},
    }
    if coverage is not None:
        payload["reading_coverage"] = coverage
    return payload


@pytest.mark.parametrize("mode", ["mirror", "compact", "standard"])
def test_filtered_modes_show_it_once_per_change(mode):
    coverage = _coverage(drift_norm=0.3)

    shown = format_response(_payload(dict(coverage, changed=True)), {"response_mode": mode})
    assert shown["reading_coverage"]["measured"] == ["S"]
    if mode == "mirror":
        assert any(s.startswith("Reading coverage:") for s in shown["mirror"])

    repeat = format_response(_payload(dict(coverage, changed=False)), {"response_mode": mode})
    assert "reading_coverage" not in repeat
    assert not any(str(s).startswith("Reading coverage:") for s in repeat.get("mirror", []))


def test_fully_measured_reading_costs_nothing():
    coverage = dict(
        _coverage(drift_norm=0.3, complexity_divergence=0.13, outcome_history=_good_outcomes()),
        changed=True,
    )
    shown = format_response(_payload(coverage), {"response_mode": "compact"})
    assert "reading_coverage" not in shown


def test_full_mode_carries_it_always():
    coverage = dict(_coverage(drift_norm=0.3), changed=False)
    full = format_response(_payload(coverage), {"response_mode": "full"})
    assert full["reading_coverage"]["measured"] == ["S"]


def test_sync_state_envelope_lifts_it():
    coverage = dict(_coverage(drift_norm=0.3), changed=True)
    formatted = format_response(_payload(coverage), {"response_mode": "auto"})
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": "auto"}
    )
    assert env["reading_coverage"]["measured"] == ["S"]

    quiet = format_response(_payload(dict(coverage, changed=False)), {"response_mode": "auto"})
    env = build_experience_envelope(
        "sync_state", "process_agent_update", quiet, {"response_mode": "auto"}
    )
    assert "reading_coverage" not in env


def test_enrichment_marks_change_per_monitor():
    from src.mcp_handlers.updates.enrichments import enrich_reading_coverage

    def derivation(**overrides):
        inputs = _inputs(**overrides)
        return build_behavioral_derivation(
            **inputs, computed=compute_behavioral_sensor_eisv(**inputs)
        )

    monitor = SimpleNamespace()

    def run(d):
        ctx = SimpleNamespace(agent_state={"_eisv_derivation": d}, monitor=monitor, response_data={})
        enrich_reading_coverage(ctx)
        return ctx.response_data.get("reading_coverage")

    assert run(derivation(drift_norm=0.3))["changed"] is True
    assert run(derivation(drift_norm=0.4))["changed"] is False  # same defaulted set
    assert run(derivation(drift_norm=0.3, complexity_divergence=0.13))["changed"] is True
    assert run(None) is None
