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


class TestExplainTrustTier:

    def test_int_tier_returns_full_glossary_entry(self):
        from src.governance_glossary import explain_trust_tier
        result = explain_trust_tier(1)
        assert result["tier"] == 1
        assert result["name"] == "emerging"
        assert "forming" in result["meaning"].lower() or "consistency" in result["meaning"].lower()
        assert "50" in result["criteria"]  # observation threshold

    def test_existing_dict_preserved_and_annotated(self):
        """compute_trust_tier emits {tier, name, reason, ...}.
        explain_trust_tier must preserve all existing keys and ADD meaning + criteria."""
        from src.governance_glossary import explain_trust_tier
        existing = {
            "tier": 2,
            "name": "established",
            "reason": "60 observations, confidence 0.6, lineage 0.8",
            "observation_count": 60,
        }
        result = explain_trust_tier(existing)
        assert result["tier"] == 2
        assert result["name"] == "established"
        assert result["reason"] == existing["reason"]
        assert result["observation_count"] == 60  # extra fields survive
        assert "meaning" in result
        assert "criteria" in result

    def test_unknown_tier_marked(self):
        from src.governance_glossary import explain_trust_tier
        result = explain_trust_tier(99)
        assert result["tier"] == 99
        assert "unknown" in result["meaning"].lower()

    def test_none_returns_value_none(self):
        from src.governance_glossary import explain_trust_tier
        assert explain_trust_tier(None) == {"value": None}

    def test_verified_tier_mentions_long_running(self):
        from src.governance_glossary import explain_trust_tier
        result = explain_trust_tier(3)
        assert result["name"] == "verified"
        assert "200" in result["criteria"]  # higher observation threshold


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


class TestGlossaryAppliedToMonitorResult:
    """Source-level guards that monitor_result.py uses the glossary helpers
    to wrap ethical_drift and behavioral.assessment.verdict. Regression
    catch if a future refactor removes the wrapping and bare values leak
    back into agent-facing payloads.
    """

    @staticmethod
    def _read(rel_path: str) -> str:
        from pathlib import Path
        return (Path(__file__).parent.parent / rel_path).read_text()

    def test_ethical_drift_wrapped_via_helper(self):
        source = self._read("src/monitor_result.py")
        # The build_result block constructing ethical_drift must call
        # annotate_drift_components — bare per-component float values
        # would lose the meaning/range/ideal context.
        idx = source.find("'ethical_drift'")
        assert idx != -1
        window = source[max(0, idx - 200):idx + 600]
        assert "annotate_drift_components" in window, (
            "monitor_result.py must wrap ethical_drift via annotate_drift_components(). "
            "Bare per-component float values surface to agents without explanation."
        )

    def test_behavioral_verdict_wrapped_via_helper(self):
        source = self._read("src/monitor_result.py")
        idx = source.find("'verdict': ")
        assert idx != -1
        window = source[max(0, idx - 200):idx + 200]
        assert "explain_verdict" in window, (
            "monitor_result.py must wrap behavioral_assessment.verdict via "
            "explain_verdict() so the agent gets meaning + next_action with "
            "the verdict label."
        )


class TestGlossaryAppliedToResponseFormatter:
    """Source-level guards that the agent-facing response surface
    (mirror, compact, unbound branch) wraps the verdict via the helper.
    Most agent-noticeable surface for #428 — every check-in returns a
    verdict; without wrapping the agent has to consult docs to act on it.
    """

    @staticmethod
    def _read(rel_path: str) -> str:
        from pathlib import Path
        return (Path(__file__).parent.parent / rel_path).read_text()

    def test_mirror_format_wraps_verdict(self):
        source = self._read("src/mcp_handlers/response_formatter.py")
        idx = source.find("def _format_mirror")
        assert idx != -1
        end = source.find("\ndef ", idx + 1)
        window = source[idx:end if end != -1 else len(source)]
        assert "explain_verdict" in window, (
            "response_formatter._format_mirror must wrap verdict via "
            "explain_verdict() at the response surface — mirror is the "
            "highest-traffic agent-facing path."
        )

    def test_compact_metrics_wraps_verdict(self):
        source = self._read("src/mcp_handlers/response_formatter.py")
        idx = source.find("compact_metrics = {")
        assert idx != -1
        window = source[max(0, idx - 300):idx + 600]
        assert "explain_verdict" in window, (
            "compact_metrics must wrap verdict via explain_verdict() — "
            "this is the response_mode='compact' agent surface."
        )

    def test_unbound_verdict_wrapped_in_core(self):
        # The "unbound" verdict surfaces when get_governance_metrics is
        # called against a session with no bound identity.
        source = self._read("src/mcp_handlers/core.py")
        idx = source.find('"verdict": ')
        assert idx != -1
        window = source[max(0, idx - 300):idx + 200]
        assert "explain_verdict" in window, (
            "core.py unbound branch must wrap the bare 'unbound' verdict "
            "via explain_verdict() so an unbound agent gets the next-action "
            "hint without consulting docs."
        )
