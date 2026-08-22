"""Tests for CIRS v2 AdaptiveGovernor.

Covers Tasks 1-2 of the implementation plan:
- Dataclasses, initialization, config defaults
- PID controller update cycle (P, I, D terms)
- Verdict classification with adaptive thresholds
- Phase detection integration
- Threshold decay toward defaults
- Oscillation convergence
- Fuzz testing for hard bounds
"""

import random

import pytest

from governance_core.adaptive_governor import (
    AdaptiveGovernor,
    GovernorConfig,
    GovernorState,
    Verdict,
    _clamp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable_histories():
    """Return EISV histories that detect_phase classifies as 'integration'."""
    return dict(
        E_history=[0.5] * 6,
        I_history=[0.5] * 6,
        S_history=[0.5] * 6,
        complexity_history=[0.3] * 6,
    )


def _exploration_histories():
    """Return EISV histories that detect_phase classifies as 'exploration'.

    I growing, S declining, high complexity -> 3/3 exploration signals.
    Need window+1 = 6 samples for detect_phase with default window=5.
    """
    return dict(
        E_history=[0.30, 0.35, 0.40, 0.45, 0.50, 0.55],
        I_history=[0.30, 0.40, 0.50, 0.60, 0.70, 0.80],
        S_history=[0.70, 0.60, 0.50, 0.40, 0.30, 0.20],
        complexity_history=[0.60, 0.70, 0.80, 0.85, 0.90, 0.95],
    )


# ===========================================================================
# TestInitialization
# ===========================================================================


class TestInitialization:
    """Task 1 -- dataclasses, config, initial state."""

    def test_default_initialization(self):
        gov = AdaptiveGovernor()
        state = gov.state
        assert state.tau == pytest.approx(0.44)
        assert state.beta == pytest.approx(0.70)
        assert state.phase == "integration"
        assert state.error_integral_tau == 0.0
        assert state.error_integral_beta == 0.0
        assert state.prev_error_tau == 0.0
        assert state.prev_error_beta == 0.0
        assert state.oi == 0.0
        assert state.flips == 0

    def test_custom_config(self):
        config = GovernorConfig(tau_default=0.45, beta_default=0.55)
        gov = AdaptiveGovernor(config=config)
        assert gov.state.tau == pytest.approx(0.45)
        assert gov.state.beta == pytest.approx(0.55)

    def test_hard_bounds_in_config(self):
        config = GovernorConfig()
        assert config.tau_floor == 0.25
        assert config.tau_ceiling == 0.75
        assert config.beta_floor == 0.20
        assert config.beta_ceiling == 0.80

    def test_pid_gains_in_config(self):
        config = GovernorConfig()
        assert config.K_p == 0.05
        assert config.K_i == 0.005
        assert config.K_d == 0.10
        assert config.integral_max == 0.10

    def test_phase_references_in_config(self):
        config = GovernorConfig()
        assert config.exploration_tau_ref == 0.38
        assert config.exploration_beta_ref == 0.55
        assert config.integration_tau_ref == 0.44
        assert config.integration_beta_ref == 0.70

    def test_governor_state_defaults(self):
        state = GovernorState()
        assert state.tau == 0.44
        assert state.beta == 0.70
        assert state.history == []
        assert state.resonant is False
        assert state.trigger is None

    def test_verdict_constants(self):
        assert Verdict.SAFE == "safe"
        assert Verdict.CAUTION == "caution"
        assert Verdict.HIGH_RISK == "high-risk"
        assert Verdict.HARD_BLOCK == "hard_block"


# ===========================================================================
# TestPIDUpdate
# ===========================================================================


class TestPIDUpdate:
    """Task 2 -- core PID threshold adaptation."""

    def test_stable_input_no_change(self):
        """When coherence/risk match the phase target, no PID adaptation occurs."""
        gov = AdaptiveGovernor()
        # Integration phase target is (0.44, 0.70).
        result = gov.update(
            coherence=0.44, risk=0.70, verdict="high-risk",
            **_stable_histories(),
        )
        # Thresholds should stay near defaults (tiny float noise OK).
        assert gov.state.tau == pytest.approx(0.44, abs=0.01)
        assert gov.state.beta == pytest.approx(0.70, abs=0.01)
        # Result dict must include key fields.
        assert "verdict" in result
        assert "tau" in result
        assert "beta" in result
        assert "controller" in result
        assert "phase" in result

    def test_live_signals_change_thresholds(self):
        """Severe and healthy inputs should drive different threshold updates."""
        severe = AdaptiveGovernor()
        severe.update(
            coherence=0.20, risk=0.95, verdict="high-risk",
            **_stable_histories(),
        )

        healthy = AdaptiveGovernor()
        healthy.update(
            coherence=0.90, risk=0.01, verdict="safe",
            **_stable_histories(),
        )

        assert severe.state.tau > healthy.state.tau
        assert severe.state.beta < healthy.state.beta

    def test_p_term_moves_toward_reference(self):
        """P-term nudges thresholds toward phase reference."""
        config = GovernorConfig(K_p=0.10, K_i=0.0, K_d=0.0, decay_rate=0.0)
        gov = AdaptiveGovernor(config=config)
        # Force tau away from integration reference of 0.44.
        gov.state.tau = 0.50
        gov.update(
            coherence=0.65, risk=0.30, verdict="safe",
            **_stable_histories(),
        )
        # P-term should pull tau back toward 0.44.
        assert gov.state.tau < 0.50

    def test_p_term_direction_for_beta(self):
        """P-term nudges beta toward phase reference too."""
        config = GovernorConfig(K_p=0.10, K_i=0.0, K_d=0.0, decay_rate=0.0)
        gov = AdaptiveGovernor(config=config)
        # Force beta away from integration reference of 0.70.
        gov.state.beta = 0.50  # below ref
        gov.update(
            coherence=0.65, risk=0.30, verdict="safe",
            **_stable_histories(),
        )
        # P-term should push beta back toward 0.70.
        assert gov.state.beta > 0.50

    def test_d_term_damps_oscillation(self):
        """D-term resists rapid error changes (oscillation damping)."""
        config = GovernorConfig(K_p=0.0, K_i=0.0, K_d=0.20, decay_rate=0.0)
        gov = AdaptiveGovernor(config=config)
        histories = _stable_histories()

        # First update: establish a smaller negative coherence error.
        gov.update(coherence=0.50, risk=0.30, verdict="safe", **histories)
        tau_after_first = gov.state.tau

        # Second update: worsen coherence further below the integration target.
        gov.update(coherence=0.55, risk=0.30, verdict="safe", **histories)
        # D-term = K_d * d_factor * (e_new - e_prev).
        # e_new = 0.44 - 0.55 = -0.11, e_prev = 0.44 - 0.50 = -0.06.
        # D-term = 0.20 * 1.0 * (-0.11 - (-0.06)) = 0.20 * (-0.05) = -0.01.
        # So adjustment is negative, pulling tau down relative to the prior step.
        assert gov.state.tau < tau_after_first

    def test_i_term_accumulates(self):
        """I-term accumulates under sustained deviation."""
        config = GovernorConfig(K_p=0.0, K_i=0.05, K_d=0.0, decay_rate=0.0)
        gov = AdaptiveGovernor(config=config)
        gov.state.tau = 0.50  # Sustained deviation from ref 0.44
        histories = _stable_histories()

        # Multiple updates should accumulate integral.
        for _ in range(5):
            gov.update(coherence=0.65, risk=0.30, verdict="safe", **histories)

        # Integral should have pulled tau toward ref.
        assert gov.state.tau < 0.50
        # The integral itself should be non-zero.
        assert gov.state.error_integral_tau != 0.0

    def test_hard_bounds_enforced(self):
        """Thresholds cannot exceed hard safety bounds."""
        gov = AdaptiveGovernor()
        gov.state.tau = 0.20  # Below floor
        gov.state.beta = 0.90  # Above ceiling
        gov.update(
            coherence=0.65, risk=0.30, verdict="safe",
            **_stable_histories(),
        )
        assert gov.state.tau >= gov.config.tau_floor
        assert gov.state.beta <= gov.config.beta_ceiling

    def test_integral_windup_protection(self):
        """Integral contribution is clamped to prevent runaway."""
        config = GovernorConfig(K_i=1.0, K_p=0.0, K_d=0.0, integral_max=0.10)
        gov = AdaptiveGovernor(config=config)
        gov.state.tau = 0.50  # Large deviation
        histories = _stable_histories()

        for _ in range(100):
            gov.update(coherence=0.65, risk=0.30, verdict="safe", **histories)

        # Integral clamped -- tau should not have gone below floor.
        assert gov.state.tau >= gov.config.tau_floor
        assert abs(gov.state.error_integral_tau) <= config.integral_max + 1e-9
        assert abs(gov.state.error_integral_beta) <= config.integral_max + 1e-9

    def test_update_returns_result_dict(self):
        """update() returns a well-formed result dictionary."""
        gov = AdaptiveGovernor()
        result = gov.update(
            coherence=0.65, risk=0.30, verdict="safe",
            **_stable_histories(),
        )
        expected_keys = {
            "verdict", "tau", "beta", "tau_default", "beta_default",
            "phase", "controller", "oi", "flips", "resonant", "trigger",
            "response_tier",
        }
        assert expected_keys.issubset(set(result.keys()))
        # Controller sub-dict should have PID components.
        ctrl = result["controller"]
        assert "p_tau" in ctrl
        assert "i_tau" in ctrl
        assert "d_tau" in ctrl
        assert "p_beta" in ctrl
        assert "i_beta" in ctrl
        assert "d_beta" in ctrl

# ===========================================================================
# TestVerdict
# ===========================================================================


class TestVerdict:
    """Verdict classification with adaptive thresholds."""

    def test_safe_verdict(self):
        gov = AdaptiveGovernor()
        # coherence >= tau(0.44) AND risk < beta(0.70) → safe (two-tier, no epsilon)
        assert gov.make_verdict(coherence=0.70, risk=0.20) == Verdict.SAFE

    def test_safe_verdict_mid_risk(self):
        gov = AdaptiveGovernor()
        # coherence >= tau(0.44) AND risk < beta(0.70) → still safe (no caution band)
        assert gov.make_verdict(coherence=0.70, risk=0.55) == Verdict.SAFE

    def test_high_risk_verdict(self):
        gov = AdaptiveGovernor()
        # coherence below tau -> high risk (or risk >= beta)
        assert gov.make_verdict(coherence=0.35, risk=0.50) == Verdict.HIGH_RISK

    def test_high_risk_via_risk(self):
        gov = AdaptiveGovernor()
        # coherence OK but risk >= beta(0.70), but not > beta_ceiling(0.80)
        assert gov.make_verdict(coherence=0.70, risk=0.72) == Verdict.HIGH_RISK

    def test_hard_block_low_coherence(self):
        gov = AdaptiveGovernor()
        assert gov.make_verdict(coherence=0.20, risk=0.20) == Verdict.HARD_BLOCK

    def test_hard_block_high_risk(self):
        gov = AdaptiveGovernor()
        assert gov.make_verdict(coherence=0.70, risk=0.85) == Verdict.HARD_BLOCK

    def test_update_records_exact_hard_stop_thresholds_and_triggers(self):
        gov = AdaptiveGovernor()
        result = gov.update(
            coherence=0.70,
            risk=0.85,
            verdict="high-risk",
            **_stable_histories(),
        )

        provenance = result["hard_stop_provenance"]
        assert result["verdict"] == Verdict.HARD_BLOCK
        assert provenance == {
            "schema": "cirs.hard-stop-provenance.v1",
            "complete": True,
            "mode": "adaptive_v2",
            "observed": {
                "coherence": 0.70,
                "risk_score": 0.85,
                "oscillation_index": result["oi"],
                "flips": result["flips"],
            },
            "thresholds": {
                "coherence_floor": 0.25,
                "risk_ceiling": 0.80,
                "oscillation_index": 2.5,
                "flips": 4,
            },
            "conditions": {
                "coherence_floor": False,
                "risk_ceiling": True,
                "resonance": result["resonant"],
            },
            "satisfied": ["risk_ceiling"],
        }

    def test_adaptive_threshold_changes_verdict(self):
        """Lowering tau makes marginal coherence safe."""
        gov = AdaptiveGovernor()
        # With default tau=0.44, coherence 0.42 is below tau -> not safe.
        v1 = gov.make_verdict(coherence=0.42, risk=0.30)
        assert v1 != Verdict.SAFE

        # Lower tau to 0.38 (exploration ref) -- now 0.42 >= tau -> safe.
        gov.state.tau = 0.38
        v2 = gov.make_verdict(coherence=0.42, risk=0.30)
        assert v2 == Verdict.SAFE

    def test_hard_block_coherence_exactly_at_floor(self):
        """Coherence exactly at floor should NOT hard-block (it's >=)."""
        gov = AdaptiveGovernor()
        # coherence == tau_floor (0.25) -> not < floor -> no hard block.
        v = gov.make_verdict(coherence=0.25, risk=0.30)
        assert v != Verdict.HARD_BLOCK

    def test_hard_block_risk_exactly_at_ceiling(self):
        """Risk exactly at ceiling should NOT hard-block (it's <=)."""
        gov = AdaptiveGovernor()
        # risk == beta_ceiling (0.80) -> not > ceiling -> no hard block.
        v = gov.make_verdict(coherence=0.70, risk=0.80)
        assert v != Verdict.HARD_BLOCK


# ===========================================================================
# TestPhaseDetection
# ===========================================================================


class TestPhaseDetection:
    """Phase detection integration -- detect_phase drives reference selection."""

    def test_exploration_phase(self):
        """I growing, S declining, high complexity -> 'exploration'."""
        gov = AdaptiveGovernor()
        gov.update(
            coherence=0.65, risk=0.30, verdict="safe",
            **_exploration_histories(),
        )
        assert gov.state.phase == "exploration"

    def test_integration_phase(self):
        """Stable I, stable S, low complexity -> 'integration'."""
        gov = AdaptiveGovernor()
        gov.update(
            coherence=0.65, risk=0.30, verdict="safe",
            **_stable_histories(),
        )
        assert gov.state.phase == "integration"

    def test_exploration_shifts_tau_reference_lower(self):
        """In exploration, tau reference is 0.38 (lower than integration 0.44).

        So if tau starts at 0.44, P-term should pull it down.
        """
        config = GovernorConfig(K_p=0.10, K_i=0.0, K_d=0.0, decay_rate=0.0)
        gov = AdaptiveGovernor(config=config)
        # tau starts at default 0.44, exploration ref is 0.38
        gov.update(
            coherence=0.65, risk=0.30, verdict="safe",
            **_exploration_histories(),
        )
        assert gov.state.phase == "exploration"
        # P-term should push tau toward 0.38 (down from 0.44).
        assert gov.state.tau < 0.44


# ===========================================================================
# TestThresholdDecay
# ===========================================================================


class TestThresholdDecay:
    """Threshold decay when stable (OI < threshold and flips == 0)."""

    def test_decay_toward_defaults(self):
        """With P=I=D=0 and tau above default, tau decays back."""
        config = GovernorConfig(K_p=0.0, K_i=0.0, K_d=0.0, decay_rate=0.05)
        gov = AdaptiveGovernor(config=config)
        gov.state.tau = 0.55  # Above default 0.44
        histories = _stable_histories()

        for _ in range(40):
            gov.update(coherence=0.65, risk=0.30, verdict="safe", **histories)

        # Should have decayed toward 0.44.
        assert gov.state.tau < 0.55
        assert gov.state.tau == pytest.approx(0.44, abs=0.02)

    def test_decay_toward_defaults_beta(self):
        """Beta also decays toward default when stable."""
        config = GovernorConfig(K_p=0.0, K_i=0.0, K_d=0.0, decay_rate=0.05)
        gov = AdaptiveGovernor(config=config)
        gov.state.beta = 0.60  # Below default 0.70
        histories = _stable_histories()

        for _ in range(40):
            gov.update(coherence=0.65, risk=0.30, verdict="safe", **histories)

        assert gov.state.beta > 0.50
        assert gov.state.beta == pytest.approx(0.70, abs=0.02)


# ===========================================================================
# TestOscillationConvergence
# ===========================================================================


class TestOscillationConvergence:
    """Oscillating agents produce non-zero OI and D-term damping."""

    def test_oscillating_agent(self):
        """Alternating safe/high-risk verdicts produce non-zero OI or flips.

        Note: OI tracks sign-transition correlation between coherence and risk.
        When they oscillate in perfect anti-phase (coherence up = risk down),
        the EMA components cancel. Flips are the primary oscillation measure
        in this case, and resonance detection uses both OI and flips.
        """
        gov = AdaptiveGovernor()
        histories = _stable_histories()

        for i in range(20):
            v = "safe" if i % 2 == 0 else "high-risk"
            c = 0.65 if i % 2 == 0 else 0.30
            r = 0.20 if i % 2 == 0 else 0.65
            gov.update(coherence=c, risk=r, verdict=v, **histories)

        # Oscillation should be detected via flips or OI.
        assert gov.state.flips > 0 or abs(gov.state.oi) > 0

    def test_oscillation_tracks_flips(self):
        """Verdict flips are counted in the window."""
        gov = AdaptiveGovernor()
        histories = _stable_histories()

        for i in range(10):
            v = "safe" if i % 2 == 0 else "high-risk"
            c = 0.65 if i % 2 == 0 else 0.30
            r = 0.20 if i % 2 == 0 else 0.65
            gov.update(coherence=c, risk=r, verdict=v, **histories)

        assert gov.state.flips > 0

    def test_resonance_detection(self):
        """Sustained oscillation triggers resonance flag."""
        # Use low flip threshold to trigger more easily.
        config = GovernorConfig(flip_threshold=3)
        gov = AdaptiveGovernor(config=config)
        histories = _stable_histories()

        for i in range(10):
            v = "safe" if i % 2 == 0 else "high-risk"
            c = 0.65 if i % 2 == 0 else 0.30
            r = 0.20 if i % 2 == 0 else 0.65
            gov.update(coherence=c, risk=r, verdict=v, **histories)

        assert gov.state.resonant is True
        assert gov.state.trigger in ("oi", "flips")

    def test_oi_based_resonance(self):
        """Resonance triggered specifically by OI threshold (not just flips)."""
        # Use high flip_threshold so flips won't trigger, only OI can.
        # Use low oi_threshold so OI can realistically trigger.
        config = GovernorConfig(flip_threshold=100, oi_threshold=0.3)
        gov = AdaptiveGovernor(config=config)
        histories = _stable_histories()

        # Oscillate coherence around tau while keeping risk stable.
        # This creates coherence sign transitions without cancelling risk transitions.
        # OI = ema_coherence + ema_risk; if only coherence oscillates,
        # ema_coherence grows while ema_risk stays near 0 → OI > 0.
        for i in range(15):
            c = 0.65 if i % 2 == 0 else 0.30  # oscillates around tau
            r = 0.30  # stable below beta
            v = "safe"  # same verdict each time → no flips
            gov.update(coherence=c, risk=r, verdict=v, **histories)

        # OI should be elevated via coherence oscillation alone
        assert abs(gov.state.oi) >= 0.3
        assert gov.state.resonant is True
        assert gov.state.trigger == "oi"


# ===========================================================================
# TestWasResonantTracking
# ===========================================================================


class TestWasResonantTracking:
    """was_resonant tracks previous resonance state for transition detection."""

    def test_was_resonant_starts_false(self):
        """Initial state: was_resonant is False."""
        gov = AdaptiveGovernor()
        assert gov.state.was_resonant is False

    def test_was_resonant_set_after_resonance_detected(self):
        """After resonance detected, was_resonant is True on next update."""
        config = GovernorConfig(flip_threshold=3)
        gov = AdaptiveGovernor(config=config)
        histories = _stable_histories()

        # Drive into resonance via oscillation
        for i in range(10):
            v = "safe" if i % 2 == 0 else "high-risk"
            c = 0.65 if i % 2 == 0 else 0.30
            r = 0.20 if i % 2 == 0 else 0.65
            gov.update(coherence=c, risk=r, verdict=v, **histories)

        assert gov.state.resonant is True
        # was_resonant should reflect the state BEFORE this update
        # After resonance is detected, the NEXT call should have was_resonant=True
        gov.update(coherence=0.65, risk=0.20, verdict="safe", **histories)
        assert gov.state.was_resonant is True

    def test_was_resonant_false_to_true_transition(self):
        """Detect the exact transition from not-resonant to resonant."""
        config = GovernorConfig(flip_threshold=3)
        gov = AdaptiveGovernor(config=config)
        histories = _stable_histories()

        # First few updates: not resonant
        for _ in range(3):
            gov.update(coherence=0.65, risk=0.20, verdict="safe", **histories)
        assert gov.state.resonant is False
        assert gov.state.was_resonant is False

        # Oscillate to trigger resonance
        for i in range(10):
            v = "safe" if i % 2 == 0 else "high-risk"
            c = 0.65 if i % 2 == 0 else 0.30
            r = 0.20 if i % 2 == 0 else 0.65
            gov.update(coherence=c, risk=r, verdict=v, **histories)

        # Now resonant=True but was_resonant should reflect pre-transition
        # The transition happens within the oscillation loop
        assert gov.state.resonant is True
        assert gov.state.was_resonant is True  # was_resonant captured resonant from prior cycle


# ===========================================================================
# TestFuzzBounds
# ===========================================================================


class TestFuzzBounds:
    """Fuzz test: extreme random inputs never push tau/beta past bounds."""

    def test_extreme_inputs(self):
        """200 random updates never violate hard bounds."""
        random.seed(42)
        gov = AdaptiveGovernor()

        for _ in range(200):
            gov.update(
                coherence=random.uniform(-1, 2),
                risk=random.uniform(-1, 2),
                verdict=random.choice(["safe", "caution", "high-risk"]),
                E_history=[random.uniform(0, 1) for _ in range(6)],
                I_history=[random.uniform(0, 1) for _ in range(6)],
                S_history=[random.uniform(0, 1) for _ in range(6)],
                complexity_history=[random.uniform(0, 1) for _ in range(6)],
            )
            assert gov.state.tau >= gov.config.tau_floor, (
                f"tau {gov.state.tau} < floor {gov.config.tau_floor}"
            )
            assert gov.state.tau <= gov.config.tau_ceiling, (
                f"tau {gov.state.tau} > ceiling {gov.config.tau_ceiling}"
            )
            assert gov.state.beta >= gov.config.beta_floor, (
                f"beta {gov.state.beta} < floor {gov.config.beta_floor}"
            )
            assert gov.state.beta <= gov.config.beta_ceiling, (
                f"beta {gov.state.beta} > ceiling {gov.config.beta_ceiling}"
            )


# ===========================================================================
# TestClamp
# ===========================================================================


class TestClamp:
    """Unit tests for the _clamp helper."""

    def test_clamp_within_range(self):
        assert _clamp(0.5, 0.0, 1.0) == 0.5

    def test_clamp_below_floor(self):
        assert _clamp(-0.5, 0.0, 1.0) == 0.0

    def test_clamp_above_ceiling(self):
        assert _clamp(1.5, 0.0, 1.0) == 1.0

    def test_clamp_at_boundary(self):
        assert _clamp(0.0, 0.0, 1.0) == 0.0
        assert _clamp(1.0, 0.0, 1.0) == 1.0
