"""Tests for the coherence gate shadow soak reader and its positive control.

The positive control running in CI is the point: it makes "the instrument can
fire" a regression-tested property, so a silent soak can never again be read
as evidence without the control run backing it
(coherence-proprioceptive-thresholds-v0.md section 6, step 4).
"""

import json
import subprocess
import sys
from pathlib import Path

from scripts.analysis.coherence_gate_shadow_read import (
    K_SWEEP,
    positive_control,
    render,
    render_positive_control,
    summarize,
)
from src.coherence_gate_shadow import K_PAUSE, STATISTIC_VERSION


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
    assert len(results) == 7
    failed = [r for r in results if not r["passed"]]
    assert not failed, f"positive control failed: {failed}"
    # Every candidate tier is exercised.
    fired = {r["would_action"] for r in results}
    assert {"coherence_pause", "hard_block", "hard_block_floor"} <= fired
    # The floor-scale path is exercised explicitly.
    assert any(r["scale_source"] == "floor" and r["passed"] for r in results)
    assert "CAN fire" in render_positive_control(results)


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
