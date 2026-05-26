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
    EISV_SOURCES,
    ETHICAL_DRIFT_VECTOR_COMPONENTS,
    TRAJECTORY_SIGNATURE_TERMS,
    explain_verdict,
    explain_basin,
    explain_eisv_source,
    explain_mode,
    explain_trajectory,
    explain_ethical_drift_vector,
    annotate_trajectory_signature_terms,
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

    def test_behavioral_verdicts_covered(self):
        # behavioral_assessment.py and monitor_decision.py emit a parallel
        # vocabulary {safe, caution, high-risk} through the same `verdict`
        # field. Agents should not see these without an inline gloss.
        for verdict in ("safe", "caution", "high-risk"):
            assert verdict in VERDICTS, (
                f"Behavioral verdict {verdict!r} must be in the glossary — "
                f"it flows through metrics['verdict'] alongside the decision "
                f"verdicts and would otherwise fall to the unknown branch."
            )
            assert "next_action" in VERDICTS[verdict]

    def test_all_basins_have_meaning(self):
        for basin, info in BASINS.items():
            assert "meaning" in info and info["meaning"]
            assert "thresholds" in info and info["thresholds"]

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

    def test_all_eisv_sources_have_thresholds(self):
        for source, info in EISV_SOURCES.items():
            assert "meaning" in info and info["meaning"]
            assert "thresholds" in info and info["thresholds"]

    def test_positional_ethical_drift_components_have_range_and_ideal(self):
        for component, info in ETHICAL_DRIFT_VECTOR_COMPONENTS.items():
            assert "meaning" in info and info["meaning"]
            assert "range" in info
            assert "ideal" in info

    def test_trajectory_signature_terms_have_meaning(self):
        for term, info in TRAJECTORY_SIGNATURE_TERMS.items():
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


class TestExplainEisvSource:

    def test_ode_fallback_includes_threshold_and_action(self):
        result = explain_eisv_source("ode_fallback")
        assert result["value"] == "ode_fallback"
        assert "Behavioral confidence" in result["meaning"]
        assert result["thresholds"]["behavioral_confidence_below"] == 0.3
        assert "next_action" in result


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

    def test_name_string_resolves_via_reverse_lookup(self):
        # The response formatter previously stored trust_tier as the bare
        # name string. The helper must accept that shape so existing
        # callers compose naturally.
        from src.governance_glossary import explain_trust_tier
        result = explain_trust_tier("established")
        assert result["tier"] == 2
        assert result["name"] == "established"
        assert "meaning" in result
        assert "criteria" in result

    def test_unknown_name_string_marked(self):
        from src.governance_glossary import explain_trust_tier
        result = explain_trust_tier("not_a_tier_name")
        assert result["value"] == "not_a_tier_name"
        assert "unknown" in result["meaning"].lower()

    def test_dict_with_only_name_resolves(self):
        # Some upstream paths build {"name": "established"} without "tier".
        # Helper should resolve tier via reverse lookup.
        from src.governance_glossary import explain_trust_tier
        result = explain_trust_tier({"name": "verified"})
        assert result["name"] == "verified"
        assert result["tier"] == 3
        assert "meaning" in result


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


class TestExplainEthicalDriftVector:

    def test_names_three_positional_components(self):
        result = explain_ethical_drift_vector([0.1, 0.2, 0.3])
        assert result["value"] == [0.1, 0.2, 0.3]
        assert result["order"] == [
            "primary_drift",
            "coherence_loss",
            "complexity_contribution",
        ]
        assert result["components"]["primary_drift"]["value"] == 0.1
        assert "meaning" in result["components"]["coherence_loss"]
        assert "range" in result["components"]["complexity_contribution"]
        assert "ideal" in result["components"]["complexity_contribution"]


class TestAnnotateTrajectorySignatureTerms:

    def test_returns_path_keyed_known_terms_without_mutating_signature(self):
        signature = {
            "state": {"mode": "building_alone"},
            "source": "ode_fallback",
            "projection": {"phase": "settling"},
        }
        result = annotate_trajectory_signature_terms(signature)
        assert result["state.mode"]["value"] == "building_alone"
        assert result["source"]["value"] == "ode_fallback"
        assert result["projection.phase"]["value"] == "settling"
        assert signature["source"] == "ode_fallback"


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

    def test_mirror_format_wraps_trust_tier(self):
        # #428: trust_tier was a bare name string ("established"); mirror
        # now emits it through explain_trust_tier so meaning + criteria
        # appear inline.
        source = self._read("src/mcp_handlers/response_formatter.py")
        idx = source.find("def _format_mirror")
        end = source.find("\ndef ", idx + 1)
        window = source[idx:end if end != -1 else len(source)]
        assert "explain_trust_tier" in window, (
            "response_formatter._format_mirror must wrap trust_tier via "
            "explain_trust_tier() so the agent sees the tier scale inline."
        )

    def test_compact_format_wraps_trust_tier(self):
        source = self._read("src/mcp_handlers/response_formatter.py")
        idx = source.find("def _format_compact")
        end = source.find("\ndef ", idx + 1)
        window = source[idx:end if end != -1 else len(source)]
        assert "explain_trust_tier" in window, (
            "response_formatter._format_compact must wrap trust_tier via "
            "explain_trust_tier()."
        )

    def test_standard_format_wraps_state_glossary(self):
        source = self._read("src/mcp_handlers/response_formatter.py")
        idx = source.find("def _format_standard")
        end = source.find("\ndef ", idx + 1)
        window = source[idx:end if end != -1 else len(source)]
        assert "state_glossary" in window
        assert "explain_basin" in window
        assert "explain_mode" in window
        assert "explain_trajectory" in window

    def test_simulate_update_wraps_input_ethical_drift(self):
        source = self._read("src/mcp_handlers/core.py")
        idx = source.find("def handle_simulate_update")
        end = source.find("\n@mcp_tool(\"process_agent_update\"", idx + 1)
        window = source[idx:end if end != -1 else len(source)]
        assert "explain_ethical_drift_vector" in window


class TestGlossaryAppliedToObservabilityHandlers:
    """Regression guard: observe_agent's current_state must wrap verdict via
    the glossary helper. observe_agent is the primary tool an agent uses to
    inspect itself or peers; a bare verdict here forces a docs lookup.
    """

    @staticmethod
    def _read(rel_path: str) -> str:
        from pathlib import Path
        return (Path(__file__).parent.parent / rel_path).read_text()

    def test_observe_agent_wraps_verdict(self):
        source = self._read("src/mcp_handlers/observability/handlers.py")
        idx = source.find('"verdict": ')
        assert idx != -1
        window = source[max(0, idx - 300):idx + 300]
        assert "explain_verdict" in window, (
            "observe_agent's current_state must wrap verdict via "
            "explain_verdict() — it's the primary self-observation surface."
        )


class TestGlossaryAppliedToRuntimeQueries:
    """Read APIs should keep raw values and add peer glossary metadata."""

    @staticmethod
    def _read(rel_path: str) -> str:
        from pathlib import Path
        return (Path(__file__).parent.parent / rel_path).read_text()

    def test_primary_eisv_source_has_meta(self):
        source = self._read("src/services/runtime_queries.py")
        assert "primary_eisv_source_meta" in source
        assert "explain_eisv_source" in source


class TestGlossaryAppliedToUpdateEnrichments:
    """process_agent_update full payload should annotate raw state/signature fields."""

    @staticmethod
    def _read(rel_path: str) -> str:
        from pathlib import Path
        return (Path(__file__).parent.parent / rel_path).read_text()

    def test_state_interpretation_adds_state_glossary(self):
        source = self._read("src/mcp_handlers/updates/enrichments.py")
        idx = source.find("def enrich_state_interpretation")
        end = source.find("\n@enrichment", idx + 1)
        window = source[idx:end if end != -1 else len(source)]
        assert "state_glossary" in window
        assert "explain_basin" in window
        assert "explain_mode" in window
        assert "explain_trajectory" in window

    def test_trajectory_identity_adds_signature_glossary_and_wrapped_trust_tier(self):
        source = self._read("src/mcp_handlers/updates/enrichments.py")
        idx = source.find("def enrich_trajectory_identity")
        end = source.find("\n@enrichment", idx + 1)
        window = source[idx:end if end != -1 else len(source)]
        assert "annotate_trajectory_signature_terms" in window
        assert "signature_glossary" in window
        assert "explain_trust_tier" in window
