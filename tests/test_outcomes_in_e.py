"""The agent-facing outcome account in behavioral E (#2610).

The account restates the derivation the check-in built, so the tests pin it to
the deployed formula: its per-adverse figure must equal the change in the
published observation when one more outcome is adverse, with the later E
blends active.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.behavioral_sensor import compute_behavioral_sensor_eisv
from src.eisv_telemetry import build_behavioral_derivation, build_physical_sensor_derivation
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response
from src.outcomes_in_e import build_outcomes_in_e, evidence_key, mirror_line


def _outcomes(n: int, bad: int) -> list[dict]:
    rows = []
    for i in range(n):
        is_bad = i < bad
        rows.append({
            "outcome_type": "test_failed" if is_bad else "test_passed",
            "is_bad": is_bad,
            "outcome_score": 0.0 if is_bad else 1.0,
            "verification_source": "external_signal" if i % 2 else "agent_reported_tool_result",
        })
    return rows


def _inputs(outcomes: list[dict] | None) -> dict:
    return dict(
        decision_history=["proceed", "guide", "proceed", "proceed"],
        coherence_history=[0.48, 0.49, 0.50, 0.50],
        regime_history=["convergence"] * 4,
        E_history=[0.7, 0.71, 0.72],
        I_history=[0.78, 0.79, 0.79],
        S_history=[0.2, 0.2, 0.2],
        V_history=[0.0, 0.0, 0.0],
        calibration_error=0.1,
        drift_norm=0.1,
        complexity_divergence=0.1,
        # Both later E blends active, so the share is not the nominal weight.
        continuity_E_input=0.6,
        tool_error_rate=0.1,
        outcome_history=outcomes,
    )


def _evidence(outcomes):
    inputs = _inputs(outcomes)
    computed = compute_behavioral_sensor_eisv(**inputs)
    return build_outcomes_in_e(build_behavioral_derivation(**inputs, computed=computed))


def test_account_matches_the_deployed_formula():
    outcomes = _outcomes(6, 2)
    evidence = _evidence(outcomes)

    assert evidence["counted"] == 6
    assert evidence["adverse"] == 2
    assert evidence["adverse_by_type"] == {"test_failed": 2}
    assert evidence["by_verification_source"] == {
        "agent_reported_tool_result": 3,
        "external_signal": 3,
    }
    assert evidence["caused_by_your_change"] == "not_recorded"
    # Nominal 0.20, kept at 0.80 by the continuity blend and 0.85 by tool success.
    assert evidence["share_of_observation"] == pytest.approx(0.20 * 0.80 * 0.85, abs=1e-4)
    assert evidence["term_range"] == pytest.approx([0.136 * 0.3, 0.136 * 0.9], abs=1e-4)

    # Recording one more adverse outcome (a seventh row, not a reclassified
    # sixth) lowers the published observation by exactly next_adverse_outcome.
    before = compute_behavioral_sensor_eisv(**_inputs(outcomes))["E"]
    after = compute_behavioral_sensor_eisv(**_inputs(_outcomes(7, 3)))["E"]
    assert evidence["next_adverse_outcome"]["window_full"] is False
    assert before - after == pytest.approx(
        evidence["next_adverse_outcome"]["lowers_by"], abs=1e-4
    )


def test_next_adverse_is_zero_when_all_are_adverse():
    evidence = _evidence(_outcomes(5, 5))
    assert evidence["next_adverse_outcome"]["lowers_by"] == 0.0
    before = compute_behavioral_sensor_eisv(**_inputs(_outcomes(5, 5)))["E"]
    after = compute_behavioral_sensor_eisv(**_inputs(_outcomes(6, 6)))["E"]
    assert before == pytest.approx(after, abs=1e-9)


def test_next_adverse_at_the_window_cap_is_an_upper_bound():
    # 20 rows, oldest first, the oldest good: the arriving adverse row evicts it.
    outcomes = list(reversed(_outcomes(20, 4)))
    evidence = _evidence(outcomes)
    nxt = evidence["next_adverse_outcome"]
    assert nxt["window_full"] is True
    bad = dict(outcomes[0], is_bad=True, outcome_type="test_failed")
    before = compute_behavioral_sensor_eisv(**_inputs(outcomes))["E"]
    after = compute_behavioral_sensor_eisv(**_inputs(outcomes[1:] + [bad]))["E"]
    assert before - after == pytest.approx(nxt["lowers_by_at_most"], abs=1e-4)


def test_no_account_without_the_outcome_term():
    assert _evidence(_outcomes(2, 2)) is None  # below the formula's 3-outcome gate
    assert _evidence(None) is None
    assert build_outcomes_in_e(build_physical_sensor_derivation({"E": 0.4})) is None
    assert build_outcomes_in_e(None) is None


def test_key_tracks_counts_not_floats():
    a = _evidence(_outcomes(6, 2))
    b = deepcopy(a)
    b["term_contribution"] = 0.0
    assert evidence_key(a) == evidence_key(b)
    assert evidence_key(a) != evidence_key(_evidence(_outcomes(6, 3)))


def test_mirror_line_carries_both_caveats():
    line = mirror_line(_evidence(_outcomes(6, 2)))
    assert line.startswith("Outcomes in E: 2 of 6 recorded outcomes in the last 24h are adverse")
    assert "test_failed x2" in line
    assert "14% of each E observation" in line
    assert "without attribution to your change" in line
    assert "not fault" in line


def _payload(evidence) -> dict:
    payload = {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        "decision": {"action": "proceed", "sub_action": "approve", "margin": "comfortable"},
        "metrics": {"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0, "risk_score": 0.27,
                    "verdict": "safe", "coherence": 0.48},
    }
    if evidence is not None:
        payload["outcomes_in_e"] = evidence
    return payload


@pytest.mark.parametrize("mode", ["mirror", "compact", "standard"])
def test_filtered_modes_show_it_once_per_change(mode):
    evidence = _evidence(_outcomes(6, 2))

    shown = format_response(_payload(dict(evidence, changed=True)), {"response_mode": mode})
    assert shown["outcomes_in_e"]["adverse"] == 2
    if mode == "mirror":
        assert any(s.startswith("Outcomes in E:") for s in shown["mirror"])

    repeat = format_response(_payload(dict(evidence, changed=False)), {"response_mode": mode})
    assert "outcomes_in_e" not in repeat
    assert not any(str(s).startswith("Outcomes in E:") for s in repeat.get("mirror", []))


def test_all_good_window_costs_nothing():
    evidence = dict(_evidence(_outcomes(6, 0)), changed=True)
    assert evidence["adverse"] == 0
    shown = format_response(_payload(evidence), {"response_mode": "compact"})
    assert "outcomes_in_e" not in shown


def test_full_mode_carries_it_always():
    evidence = dict(_evidence(_outcomes(6, 0)), changed=False)
    full = format_response(_payload(evidence), {"response_mode": "full"})
    assert full["outcomes_in_e"]["counted"] == 6


def test_sync_state_envelope_lifts_it():
    evidence = dict(_evidence(_outcomes(6, 2)), changed=True)
    formatted = format_response(_payload(evidence), {"response_mode": "auto"})
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": "auto"}
    )
    assert env["outcomes_in_e"]["adverse"] == 2
    assert "raw_governance" not in env

    quiet = format_response(_payload(dict(evidence, changed=False)), {"response_mode": "auto"})
    env = build_experience_envelope(
        "sync_state", "process_agent_update", quiet, {"response_mode": "auto"}
    )
    assert "outcomes_in_e" not in env


def test_enrichment_marks_change_per_monitor():
    from types import SimpleNamespace

    from src.mcp_handlers.updates.enrichments import enrich_outcomes_in_e

    inputs = _inputs(_outcomes(6, 2))
    derivation = build_behavioral_derivation(
        **inputs, computed=compute_behavioral_sensor_eisv(**inputs)
    )
    monitor = SimpleNamespace()

    def run():
        ctx = SimpleNamespace(
            agent_state={"_eisv_derivation": derivation}, monitor=monitor, response_data={}
        )
        enrich_outcomes_in_e(ctx)
        return ctx.response_data["outcomes_in_e"]

    assert run()["changed"] is True
    assert run()["changed"] is False  # same counts, same process: not news

    ctx = SimpleNamespace(agent_state={}, monitor=monitor, response_data={})
    enrich_outcomes_in_e(ctx)
    assert "outcomes_in_e" not in ctx.response_data
