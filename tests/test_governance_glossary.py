"""Tests for src/governance_glossary.py — vocabulary embedding helpers (#428)."""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.governance_glossary import (
    VERDICTS,
    BASINS,
    MODES,
    TRAJECTORIES,
    DRIFT_COMPONENTS,
    explain_verdict,
    explain_basin,
    explain_mode,
    explain_trajectory,
    annotate_drift_components,
)


# -----------------------------------------------------------------------------
# Glossary integrity — every shipped value must have a meaning
# -----------------------------------------------------------------------------

class TestGlossaryIntegrity:
    """Every entry in every glossary table must have at minimum a `meaning`."""

    def test_all_verdicts_have_meaning(self):
        for verdict, info in VERDICTS.items():
            assert "meaning" in info, f"Verdict {verdict!r} missing 'meaning'"
            assert info["meaning"], f"Verdict {verdict!r} has empty meaning"

    def test_actionable_verdicts_have_next_action(self):
        # The five primary verdicts an agent will see in normal flow must
        # carry a next_action — agent should never read a verdict and not
        # know what to do.
        for verdict in ("proceed", "guide", "pause", "reject", "uninitialized", "unbound"):
            assert "next_action" in VERDICTS[verdict], (
                f"Primary verdict {verdict!r} must carry next_action so the "
                f"agent can act on it without consulting external docs."
            )

    def test_all_basins_have_meaning(self):
        for basin, info in BASINS.items():
            assert "meaning" in info and info["meaning"]

    def test_all_modes_have_meaning(self):
        for mode, info in MODES.items():
            assert "meaning" in info and info["meaning"]

    def test_modes_table_matches_governance_state(self):
        # Modes must match the patterns table in
        # src/governance_state.py:_interpret_mode. Drift here means the
        # glossary lies to agents about what mode they are in.
        expected = {
            "collaborating", "building_alone", "exploring_together",
            "exploring_alone", "executing_together", "executing_alone",
            "drifting_together", "stalled",
        }
        assert set(MODES.keys()) == expected, (
            "MODES table must exactly match _interpret_mode patterns. "
            "If governance_state.py adds a new mode, add it here too."
        )

    def test_all_trajectories_have_meaning(self):
        for trajectory, info in TRAJECTORIES.items():
            assert "meaning" in info and info["meaning"]

    def test_drift_components_have_range_and_ideal(self):
        for component, info in DRIFT_COMPONENTS.items():
            assert "meaning" in info and info["meaning"]
            assert "range" in info, f"{component!r} missing range"
            assert "ideal" in info, f"{component!r} missing ideal"


# -----------------------------------------------------------------------------
# Helper behavior
# -----------------------------------------------------------------------------

class TestExplainVerdict:

    def test_known_verdict_includes_meaning_and_next_action(self):
        result = explain_verdict("pause")
        assert result["value"] == "pause"
        assert "Needs attention" in result["meaning"]
        assert "next_action" in result

    def test_unknown_verdict_falls_through(self):
        result = explain_verdict("does_not_exist")
        assert result["value"] == "does_not_exist"
        assert "unknown" in result["meaning"].lower()

    def test_none_verdict(self):
        result = explain_verdict(None)
        assert result == {"value": None}


class TestExplainBasin:

    def test_known_basin(self):
        result = explain_basin("high")
        assert result["value"] == "high"
        assert "Healthy" in result["meaning"]

    def test_boundary_basin_mentions_margin(self):
        result = explain_basin("boundary")
        assert "margin" in result["meaning"].lower() or "tight" in result["meaning"].lower()


class TestExplainMode:

    def test_building_alone_explained(self):
        result = explain_mode("building_alone")
        assert result["value"] == "building_alone"
        assert "high E" in result["meaning"]
        assert "high I" in result["meaning"]


class TestExplainTrajectory:

    def test_stuck_recommends_dialectic(self):
        result = explain_trajectory("stuck")
        assert "dialectic" in result["meaning"].lower()


class TestAnnotateDriftComponents:

    def test_annotates_known_components(self):
        drift = {
            "calibration_deviation": 0.05,
            "complexity_divergence": 0.12,
            "coherence_deviation": 0.03,
            "stability_deviation": 0.01,
        }
        result = annotate_drift_components(drift)
        for component, value in drift.items():
            assert result[component]["value"] == value
            assert "meaning" in result[component]
            assert "range" in result[component]
            assert "ideal" in result[component]

    def test_passes_through_unknown_components(self):
        # If a future drift component is added without a glossary entry,
        # the helper passes it through with just `value` rather than
        # raising. New entries should be added to the glossary, but
        # passthrough beats failure.
        drift = {"calibration_deviation": 0.1, "future_component": 0.5}
        result = annotate_drift_components(drift)
        assert "meaning" in result["calibration_deviation"]
        assert result["future_component"] == {"value": 0.5}

    def test_norm_passes_through(self):
        # `norm` and `norm_squared` are emitted alongside drift components
        # in monitor_result.py but aren't drift dimensions themselves —
        # they should pass through cleanly.
        drift = {"norm": 0.42, "norm_squared": 0.176}
        result = annotate_drift_components(drift)
        assert result["norm"] == {"value": 0.42}
        assert result["norm_squared"] == {"value": 0.176}
