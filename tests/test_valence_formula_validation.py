"""Tests for the Valence-formula flip (V_FORMULA_VERSION 2), the migration
re-seed, and the validation gate.

These lock in (a) the v2 default is single-EMA of the raw imbalance, (b) the
LegacyV seam still reproduces the v1 double-smoothing so the gate stays a live
A/B, (c) loading a v1-persisted mature state re-seeds _baseline_V so the new
formula isn't judged against a stale baseline, and (d) the documented empirical
findings from the 2026-06-19 real-trace replay, so a future change to either
formula or the #686/#689 band-aids can't silently drift them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.behavioral_state import (
    BehavioralEISV,
    V_FORMULA_VERSION,
    BASELINE_WARMUP_UPDATES,
)

_HARNESS = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "validate_valence_formula.py"
_spec = importlib.util.spec_from_file_location("validate_valence_formula", _HARNESS)
vvf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vvf)


class TestSeam:
    def test_default_raw_valence_is_raw_imbalance(self):
        """v2 default returns the gap of the RAW observations."""
        s = BehavioralEISV()
        s.E, s.I = 0.7, 0.55
        assert s._raw_valence(0.9, 0.1) == 0.9 - 0.1  # uses raw obs, ignores smoothed

    def test_legacy_raw_valence_is_double_smoothed(self):
        """LegacyV seam restores v1: the gap of the already-smoothed E,I."""
        s = vvf.LegacyV()
        s.E, s.I = 0.7, 0.55
        assert s._raw_valence(0.9, 0.1) == 0.7 - 0.55  # ignores raw obs, uses smoothed

    def test_default_trajectory_matches_raw_imbalance_formula(self):
        """A trace through the default class must match the explicit v2 formula."""
        obs = [[0.7, 0.8, 0.15], [0.72, 0.78, 0.16], [0.68, 0.82, 0.14]] * 10
        s = BehavioralEISV()
        ref = BehavioralEISV()
        for e, i, sv in obs:
            s.update(e, i, sv)
            ae, ai, av = ref.alphas["E"], ref.alphas["I"], ref.alphas["V"]
            boost = 0.5 * (1.0 - ref.update_count / 10) if ref.update_count < 10 else 0.0
            ref.E = (1 - (ae + boost)) * ref.E + (ae + boost) * e
            ref.I = (1 - (ai + boost)) * ref.I + (ai + boost) * i
            ref.V = (1 - (av + boost)) * ref.V + (av + boost) * (e - i)  # v2: raw imbalance
            ref.V = max(-1.0, min(1.0, ref.V))
            ref.update_count += 1
        assert abs(s.V - ref.V) < 1e-9

    def test_legacy_trajectory_diverges_on_transient(self):
        """The whole point of v2: less lag, so v1/v2 diverge DURING a step change.

        At steady state EMA(E)-EMA(I) -> E_obs-I_obs and the two converge; the
        v2 win is responsiveness to transitions, so the divergence is measured as
        the max gap across a stepped trace, not the final value.
        """
        obs = [[0.70, 0.80, 0.15]] * 20 + [[0.90, 0.50, 0.20]] * 20  # sharp imbalance step
        v2 = BehavioralEISV()
        v1 = vvf.LegacyV()
        max_gap = 0.0
        for e, i, sv in obs:
            v2.update(e, i, sv)
            v1.update(e, i, sv)
            max_gap = max(max_gap, abs(v2.V - v1.V))
        assert max_gap > 1e-2  # v2 leads v1 through the transient


class TestMigrationReseed:
    """The live path: BehavioralEISV.from_dict re-seeds _baseline_V on a v1 load."""

    def _mature_v1_blob(self):
        """A mature state persisted under v1 (double-smoothing, no version stamp)."""
        legacy = vvf.LegacyV()
        for _ in range(80):
            legacy.update(0.78, 0.72, 0.2)  # mild E>I imbalance
        blob = legacy.to_dict_with_history()
        blob.pop("v_formula_version", None)  # simulate a pre-v2 snapshot
        return blob

    def test_v1_load_triggers_reseed(self):
        blob = self._mature_v1_blob()
        stale_mean = blob["baseline_stats"]["V"]["mean"]
        restored = BehavioralEISV.from_dict(blob)
        # Baseline mean should move off the stale v1 value toward the v2 trajectory.
        assert abs(restored._baseline_V.mean - stale_mean) > 1e-6
        # And the live V is realigned to the v2 trajectory it just replayed, so on
        # this steady trace it sits near the freshly re-seeded baseline mean.
        assert abs(restored.V - restored._baseline_V.mean) < 0.1
        assert restored.is_baselined

    def test_reseed_keeps_post_migration_z_in_budget(self):
        blob = self._mature_v1_blob()
        restored = BehavioralEISV.from_dict(blob)
        # The first post-migration deviation must not spike out of a 3-sigma budget.
        assert abs(restored.deviation("V")) <= 3.0

    def test_v2_load_does_not_reseed(self):
        """A state already at the current version must load untouched."""
        s = BehavioralEISV()
        for _ in range(80):
            s.update(0.78, 0.72, 0.2)
        blob = s.to_dict_with_history()
        assert blob["v_formula_version"] == V_FORMULA_VERSION
        restored = BehavioralEISV.from_dict(blob)
        assert abs(restored._baseline_V.mean - blob["baseline_stats"]["V"]["mean"]) < 1e-9

    def test_reseed_fallback_without_history(self):
        """DB-row restore drops obs_history; reseed seeds a single current-V sample."""
        blob = self._mature_v1_blob()
        blob["obs_history"] = []  # mimic to_dict_for_persistence
        restored = BehavioralEISV.from_dict(blob)
        assert restored._baseline_V.count == 1
        assert abs(restored._baseline_V.mean - restored.V) < 1e-9


class TestHarness:
    def test_report_structure(self):
        report = vvf.build_report(vvf.synthetic_traces(seed=7), sigma_budget=3.0)
        assert set(report) == {"summary", "per_trace", "migration"}
        s = report["summary"]
        assert {"n_traces", "total_verdict_flips", "total_healthy_regressions",
                "migration_reset_would_be_needed", "reset_clears_all_spikes",
                "gate_pass"} <= set(s)
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

    def test_migration_probe_reset_reduces_spike(self):
        """Seat 3 RISK 2: the reset cuts the no-reset spike to within budget."""
        traces = vvf.synthetic_traces(seed=7)
        sentinel = next(t for t in traces if t["label"] == "sentinel")
        m = vvf.migration_probe(sentinel["observations"], sigma_budget=3.0)
        assert m["z_spike_no_reset"] > 1.0          # tight-sigma agent shows a real spike
        assert m["z_spike_with_reset"] < m["z_spike_no_reset"]
        assert m["reset_clears_spike"] is True

    def test_gate_fails_when_budget_below_post_reset_spike(self):
        """A sigma budget below even the post-reset spike must trip the gate."""
        traces = [t for t in vvf.synthetic_traces(seed=7) if t["label"] == "sentinel"]
        report = vvf.build_report(traces, sigma_budget=0.1)
        assert report["summary"]["reset_clears_all_spikes"] is False
        assert report["summary"]["gate_pass"] is False

    def test_gate_passes_with_reset_at_default_budget(self):
        report = vvf.build_report(vvf.synthetic_traces(seed=7), sigma_budget=3.0)
        assert report["summary"]["gate_pass"] is True
