"""Tests for the Valence-formula validation gate and the _raw_valence seam.

These lock in (a) the seam is behavior-preserving by default, (b) the harness
faithfully A/Bs the two formulas through the real assessment, and (c) the
documented empirical findings from the 2026-06-19 council replay so a future
change to either formula or the band-aids can't silently drift them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.behavioral_state import BehavioralEISV

_HARNESS = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "validate_valence_formula.py"
_spec = importlib.util.spec_from_file_location("validate_valence_formula", _HARNESS)
vvf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vvf)


class TestSeam:
    def test_default_raw_valence_is_double_smoothed(self):
        """Default seam returns the gap of already-smoothed E,I (deployed behavior)."""
        s = BehavioralEISV()
        s.E, s.I = 0.7, 0.55
        assert s._raw_valence(0.9, 0.1) == 0.7 - 0.55  # ignores raw obs, uses smoothed

    def test_candidate_raw_valence_is_raw_imbalance(self):
        """Candidate seam returns the gap of the RAW observations."""
        s = vvf.CandidateV()
        s.E, s.I = 0.7, 0.55
        assert s._raw_valence(0.9, 0.1) == 0.9 - 0.1  # uses raw obs, ignores smoothed

    def test_seam_does_not_change_default_v_trajectory(self):
        """A trace through the default class must match the explicit old formula."""
        obs = [[0.7, 0.8, 0.15], [0.72, 0.78, 0.16], [0.68, 0.82, 0.14]] * 10
        s = BehavioralEISV()
        ref = BehavioralEISV()
        for e, i, sv in obs:
            s.update(e, i, sv)
            # reference: replicate old V update independently
            ae, ai, av = ref.alphas["E"], ref.alphas["I"], ref.alphas["V"]
            boost = 0.5 * (1.0 - ref.update_count / 10) if ref.update_count < 10 else 0.0
            ref.E = (1 - (ae + boost)) * ref.E + (ae + boost) * e
            ref.I = (1 - (ai + boost)) * ref.I + (ai + boost) * i
            ref.V = (1 - (av + boost)) * ref.V + (av + boost) * (ref.E - ref.I)
            ref.V = max(-1.0, min(1.0, ref.V))
            ref.update_count += 1
        assert abs(s.V - ref.V) < 1e-9


class TestHarness:
    def test_report_structure(self):
        report = vvf.build_report(vvf.synthetic_traces(seed=7), sigma_budget=3.0)
        assert set(report) == {"summary", "per_trace", "migration"}
        s = report["summary"]
        assert {"n_traces", "total_verdict_flips", "total_healthy_regressions",
                "migration_reset_required", "gate_pass"} <= set(s)
        assert len(report["per_trace"]) == 5

    def test_candidate_increases_v_variance(self):
        """Seat 3's claim: single-smoothing makes V more responsive (higher std)."""
        report = vvf.build_report(vvf.synthetic_traces(seed=7), sigma_budget=3.0)
        hot = next(t for t in report["per_trace"] if t["label"] == "hot")
        assert hot["V_std_new"] > hot["V_std_old"]
        assert hot["V_std_ratio"] > 1.0

    def test_synthetic_has_no_healthy_regressions(self):
        """Seat 2's claim: candidate is verdict-neutral on healthy trajectories."""
        report = vvf.build_report(vvf.synthetic_traces(seed=7), sigma_budget=3.0)
        assert report["summary"]["total_healthy_regressions"] == 0

    def test_migration_probe_quantifies_discontinuity(self):
        """Seat 3 RISK 2: a no-reset formula switch spikes z; a reset zeroes it."""
        traces = vvf.synthetic_traces(seed=7)
        sentinel = next(t for t in traces if t["label"] == "sentinel")
        m = vvf.migration_probe(sentinel["observations"], sigma_budget=3.0)
        assert m["z_spike_with_reset"] == 0.0
        assert m["z_spike_no_reset"] >= m["z_spike_with_reset"]
        assert m["z_spike_no_reset"] > 1.0  # tight-sigma agent shows a real spike

    def test_gate_fails_when_budget_tightened_below_observed_spike(self):
        """A sigma budget below the observed sentinel spike must trip the gate."""
        traces = [t for t in vvf.synthetic_traces(seed=7) if t["label"] == "sentinel"]
        report = vvf.build_report(traces, sigma_budget=0.5)
        assert report["summary"]["migration_reset_required"] is True
        assert report["summary"]["gate_pass"] is False
