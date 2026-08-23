"""Tests for the coherence gate shadow soak reader and its positive control.

The positive control running in CI is the point: it makes "the instrument can
fire" a regression-tested property, so a silent soak can never again be read
as evidence without the control run backing it
(coherence-proprioceptive-thresholds-v0.md section 6, step 4).

A control that cannot fail is not a control, so the failure modes are tested
here as directly as the passing ones. See
`docs/operations/positive-control-validity-2026-08-23.md`.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.analysis.coherence_gate_shadow_read as reader
import src.coherence_gate_shadow as gate
from scripts.analysis.coherence_gate_shadow_read import (
    K_SWEEP,
    positive_control,
    reachability_report,
    render,
    render_positive_control,
    summarize,
)
from src.behavioral_state import V_MAX, V_MIN
from src.coherence_gate_shadow import (
    K_PAUSE,
    STATISTIC_VERSION,
    deviation_required_for,
    k_reachability,
    max_attainable_magnitude,
)


def _set_tiers(monkeypatch, pause, block, floor):
    """Retarget the tiers everywhere they are read.

    ``evaluate`` and the reachability helpers read the gate module's globals at
    call time; the reader binds its own names at import. Both must move or the
    scenario is built against one k and scored against another.
    """
    for module in (gate, reader):
        monkeypatch.setattr(module, "K_PAUSE", pause, raising=False)
        monkeypatch.setattr(module, "K_BLOCK", block, raising=False)
        monkeypatch.setattr(module, "K_FLOOR", floor, raising=False)


def _row(**overrides):
    base = {
        "agent_id": "agent-a",
        "statistic_version": STATISTIC_VERSION,
        "eligible": True,
        "eligibility_reason": None,
        "scale_source": "sample_std",
        "v_deviation_magnitude": 0.8,
        "would_action": "proceed",
        "agrees": None,
        "fleet_gate_family": None,
    }
    base.update(overrides)
    return base


def test_positive_control_all_scenarios_pass():
    results = positive_control()
    failed = [r for r in results if not r["passed"]]
    assert not failed, f"positive control failed: {failed}"
    # Every candidate tier is exercised.
    fired = {r["would_action"] for r in results}
    assert {"coherence_pause", "hard_block", "hard_block_floor"} <= fired
    assert "CAN fire" in render_positive_control(results)


def test_every_control_scenario_stays_inside_v_domain():
    """The defect this control was repaired for.

    The former `floor_tier` scenario certified `hard_block_floor` from V = 1.10
    while `BehavioralEISV.update` clamps V to [-1, 1] -- a tier validated from a
    state the deployed system cannot reach.
    """
    for result in positive_control():
        assert result["in_domain"], f"{result['scenario']}: {result['reason']}"


def test_each_tier_is_reachable_in_the_floor_scale_regime():
    """Floor scale is where nearly all observed eligible rows live.

    Showing a tier fires only against an empirical sd leaves the dominant
    regime untested, which is what the six-day read's 6,343/6,510 floor-scale
    rows made load-bearing.
    """
    by_name = {r["scenario"]: r for r in positive_control()}
    for tier, expected in (
        ("pause_tier_floor_scale", "coherence_pause"),
        ("block_tier_floor_scale", "hard_block"),
        ("floor_tier_floor_scale", "hard_block_floor"),
    ):
        row = by_name[tier]
        assert row["passed"], row["reason"]
        assert row["scale_source"] == "floor"
        assert row["would_action"] == expected


def test_control_fails_when_a_tier_can_only_fire_out_of_domain(monkeypatch):
    """A k reachable only by leaving V's domain must FAIL, not pass quietly.

    The pre-repair table passed at every one of these.
    """
    _set_tiers(monkeypatch, 3.0, 4.0, 25.0)
    results = positive_control()
    failed = {r["scenario"] for r in results if not r["passed"]}
    assert "floor_tier" in failed
    assert "floor_tier_floor_scale" in failed
    assert "NOT interpretable" in render_positive_control(results)
    offending = next(r for r in results if r["scenario"] == "floor_tier")
    assert offending["in_domain"] is False
    assert "leaves V's domain" in offending["reason"]


def test_control_fails_when_k_exceeds_the_attainable_ceiling(monkeypatch):
    """Above (V_MAX - V_MIN) / floor no state can reach the tier at all."""
    ceiling = max_attainable_magnitude()
    _set_tiers(monkeypatch, 3.0, 4.0, ceiling + 5.0)
    results = positive_control()
    row = next(r for r in results if r["scenario"] == "reachable::k_floor")
    assert row["passed"] is False
    assert "exceeds the maximum attainable magnitude" in row["reason"]


def test_the_attainable_ceiling_is_actually_attained():
    """The ceiling is a tight bound, so `k <= ceiling` is the right comparison.

    A prior pinned at V_MIN with the current value at V_MAX is a legal in-domain
    state: its sample sd is zero, so the floor supplies the scale and the
    magnitude lands exactly on the claimed ceiling. If the bound were merely an
    unreachable upper limit, the boundary case would be wrong by one tier.
    """
    extreme = reader._SyntheticBehavioral(
        V_history=[V_MIN] * 60 + [V_MAX], V=V_MAX
    )
    outcome = gate.evaluate(extreme, fleet_action="proceed")
    assert outcome["scale_source"] == "floor"
    assert outcome["v_deviation_magnitude"] == max_attainable_magnitude()


def test_reachability_is_bounded_by_v_domain_over_the_floor():
    report = reachability_report()
    # V is clamped to [-1, 1] and the calibrated V floor is 0.05.
    assert report["max_attainable_magnitude"] == (V_MAX - V_MIN) / 0.05
    assert report["all_attainable"] is True
    assert k_reachability(report["max_attainable_magnitude"])["attainable"] is True
    assert k_reachability(report["max_attainable_magnitude"] + 1)["attainable"] is False


@pytest.mark.parametrize(
    "k,expected_deviation",
    [(3.0, 0.15), (4.0, 0.20), (5.0, 0.25)],
)
def test_selected_k_states_its_meaning_in_v_units(k, expected_deviation):
    """The operator's k-policy call stated these by hand; the code derives them.

    At the default V alpha, k_pause = 3.0 means
    |V_current - mean(V_recent_prior)| >= 0.15 at floor scale.
    """
    assert deviation_required_for(k) == pytest.approx(expected_deviation)


def test_render_refuses_to_let_a_pass_read_as_efficacy():
    text = render_positive_control(positive_control())
    assert "necessary, never sufficient" in text
    assert "selected for quietness will still" in text


def test_summarize_never_pools_statistic_versions():
    rows = [
        _row(v_deviation_magnitude=6.0, would_action="hard_block_floor"),
        _row(statistic_version="behavioral_v_welford_v1", v_deviation_magnitude=9.9),
    ]
    summary = summarize(rows)
    assert summary["n_rows"] == 2
    assert summary["n_v2"] == 1
    # The v1 row's magnitude must not leak into v2 statistics.
    assert summary["magnitude_percentiles"]["max"] == 6.0


def test_summarize_agreement_excludes_tristate_none():
    rows = [
        _row(agrees=True, fleet_gate_family="fleet_proceeded"),
        _row(agrees=False, fleet_gate_family="cirs_coherence_floor"),
        _row(agrees=None),
        _row(eligible=False, eligibility_reason="behavioral_baseline_immature",
             agrees=None, v_deviation_magnitude=None),
    ]
    summary = summarize(rows)
    agr = summary["agreement"]
    assert agr["n_attributable"] == 2
    assert agr["agree"] == 1
    assert agr["rate"] == 0.5
    assert agr["divergence_by_fleet_family"] == {"cirs_coherence_floor": 1}
    assert summary["ineligible_reasons"] == {"behavioral_baseline_immature": 1}


def test_summarize_k_sweep_is_monotone_and_flags_unfair_zero():
    quiet = [_row(v_deviation_magnitude=m / 10.0) for m in range(1, 21)]
    summary = summarize(quiet)
    counts = [summary["k_sweep"][k] for k in K_SWEEP]
    assert counts == sorted(counts, reverse=True)
    assert summary["fired_any_at_k_pause"] is False
    assert "UNFAIR ZERO GUARD" in render(summary)

    loud = quiet + [_row(v_deviation_magnitude=K_PAUSE + 1.0,
                         would_action="coherence_pause")]
    summary_loud = summarize(loud)
    assert summary_loud["fired_any_at_k_pause"] is True
    assert "UNFAIR ZERO GUARD" not in render(summary_loud)


def test_summarize_per_agent_segmentation():
    rows = [
        _row(agent_id="agent-a", v_deviation_magnitude=1.0),
        _row(agent_id="agent-b", v_deviation_magnitude=5.5,
             would_action="hard_block_floor"),
    ]
    per_agent = summarize(rows)["per_agent"]
    assert per_agent["agent-a"]["fired_floor"] == 0
    assert per_agent["agent-b"]["fired_floor"] == 1


def test_cli_positive_control_and_jsonl_input(tmp_path: Path):
    script = "scripts/analysis/coherence_gate_shadow_read.py"
    proc = subprocess.run(
        [sys.executable, script, "--positive-control", "--json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert all(r["passed"] for r in json.loads(proc.stdout))

    export = tmp_path / "shadow.jsonl"
    export.write_text("\n".join(json.dumps(_row()) for _ in range(3)) + "\n")
    proc = subprocess.run(
        [sys.executable, script, "--input", str(export), "--json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["n_eligible"] == 3
