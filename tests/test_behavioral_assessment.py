"""Tests for behavioral assessment: risk thresholds, context sensitivity, verdict mapping."""

import pytest
from src.behavioral_state import BehavioralEISV, BOOTSTRAP_UPDATES
from src.behavioral_assessment import (
    assess_behavioral_state,
    AssessmentResult,
    RISK_SAFE_THRESHOLD,
    RISK_CAUTION_THRESHOLD,
)


def _make_state(E=0.5, I=0.5, S=0.2, updates=20):
    """Helper to create a BehavioralEISV at specific values."""
    state = BehavioralEISV()
    # Use bootstrap period to snap to desired values quickly
    for _ in range(updates):
        state.update(E, I, S)
    return state


class TestHealthyAgent:
    """Healthy agents should get safe verdicts."""

    def test_healthy_state_is_safe(self):
        state = _make_state(E=0.7, I=0.7, S=0.1)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.verdict == "safe"
        assert result.risk < RISK_SAFE_THRESHOLD
        assert result.health in ("healthy", "moderate")

    def test_high_energy_high_integrity(self):
        state = _make_state(E=0.9, I=0.8, S=0.1)
        result = assess_behavioral_state(state, rho=0.7)
        assert result.verdict == "safe"
        assert result.risk < 0.15

    def test_no_guidance_for_healthy(self):
        state = _make_state(E=0.8, I=0.8, S=0.1)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.guidance is None


class TestLowEnergy:
    """Low E should increase risk."""

    def test_low_E_raises_risk(self):
        state = _make_state(E=0.2, I=0.7, S=0.1)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["low_E"] > 0
        assert result.risk > 0.05

    def test_very_low_E_is_concerning(self):
        state = _make_state(E=0.1, I=0.7, S=0.1)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["low_E"] > 0.15
        assert "energy" in (result.guidance or "").lower() or result.risk > RISK_SAFE_THRESHOLD


class TestLowIntegrity:
    """Low I should increase risk."""

    def test_low_I_raises_risk(self):
        state = _make_state(E=0.7, I=0.2, S=0.1)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["low_I"] > 0
        assert result.risk > 0.05

    def test_very_low_I_triggers_caution(self):
        state = _make_state(E=0.7, I=0.1, S=0.1)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["low_I"] > 0.15


class TestHighEntropy:
    """High S should increase risk, with context sensitivity."""

    def test_high_S_raises_risk(self):
        state = _make_state(E=0.7, I=0.7, S=0.8)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["high_S"] > 0

    def test_convergent_task_tolerates_higher_S(self):
        state = _make_state(E=0.7, I=0.7, S=0.55)
        # Default context
        result_mixed = assess_behavioral_state(state, rho=0.5)
        # Convergent context — S threshold is higher (0.6 vs 0.5)
        result_conv = assess_behavioral_state(
            state, rho=0.5, agent_context={"task_type": "convergent"}
        )
        # Convergent should have lower S risk for the same S value
        assert result_conv.components["high_S"] <= result_mixed.components["high_S"]


class TestImbalance:
    """High |V| should increase risk."""

    def test_positive_imbalance(self):
        state = _make_state(E=0.8, I=0.3, S=0.2)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["high_V"] > 0

    def test_small_imbalance_no_risk(self):
        state = _make_state(E=0.55, I=0.5, S=0.2)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["high_V"] == 0.0


def _healthy_baseline_then_override(**overrides):
    """A settled, fully-baselined healthy state (E=I=0.7, S=0.15, past
    BehavioralEISV's ~30-update warmup) with one or more raw fields
    force-set afterward.

    Valid ONLY for fields that genuinely don't covary with the others
    through the real update() pipeline -- S is such a field. Do NOT use
    this to override E or I alone: V = EMA(E - I) is derived, not an
    independent input (governance_monitor.py, the sole producer, never
    sets V directly), so setting E or I without also moving V desyncs the
    state from anything the real pipeline can produce. Overriding E AND I
    together (to the same target) is fine -- that mirrors a real
    synchronized decline, which never opens the E/I gap that drives V.
    """
    state = _make_state(E=0.7, I=0.7, S=0.15, updates=40)
    for field, value in overrides.items():
        setattr(state, field, value)
    return state


def _sustained(E, I, S, updates=60):
    """A state reached by actually calling update() from a healthy
    baseline -- the only way governance_monitor.py ever drives
    BehavioralEISV, so this is what a real settling agent looks like
    (unlike _healthy_baseline_then_override's raw field override)."""
    state = _make_state(E=0.7, I=0.7, S=0.15, updates=60)
    for _ in range(updates):
        state.update(E, I, S)
    return state


class TestAbsoluteFloorsBoundComponentsNotVerdict:
    """Each absolute floor bounds only its own component's contribution to
    composite risk (0.30 max for E/I, 0.20 max for S/|V|), both below
    RISK_SAFE_THRESHOLD (0.35) -- a documented arithmetic property (issue
    #1995), not a defect fixed here.

    That does NOT mean any lone dimension can be driven to its floor while
    the verdict stays "safe". E and I are coupled through V = EMA(E - I):
    a sustained one-sided collapse in E or I widens the gap and typically
    breaches the V ceiling too, reaching "caution" via two components. S is
    genuinely independent and is the one dimension whose floor is provably
    reachable alone. Whether the E/I/V coupling case should escalate
    further is an open calibration question for the operator -- these
    tests document current behavior, they don't decide it.
    """

    def test_S_alone_at_ceiling_stays_safe(self):
        state = _healthy_baseline_then_override(S=1.0)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.verdict == "safe"
        assert result.risk == pytest.approx(0.20, abs=0.01)
        assert result.components["high_V"] == 0.0

    def test_E_collapse_with_I_still_healthy_reaches_caution_via_V(self):
        # NOT a lone-floor-breach case: I holding its own healthy baseline
        # while E collapses opens the E/I gap, so high_V fires too.
        state = _sustained(E=0.0, I=0.7, S=0.15)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.verdict == "caution"
        assert result.components["low_E"] > 0.25
        assert result.components["high_V"] > 0.0

    def test_E_collapse_alone_stays_safe_only_in_the_narrow_I_corridor(self):
        # A real, reachable lone-E-floor-breach case: I settles moderately
        # reduced too (but above its own floor), keeping |E-I| under the V
        # ceiling. Outside roughly I in [0.30, ~0.62] this stops holding.
        state = _sustained(E=0.0, I=0.5, S=0.15)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.verdict == "safe"
        assert result.risk == pytest.approx(0.30, abs=0.02)
        assert result.components["high_V"] == 0.0

    def test_E_and_I_collapse_together_reaches_high_risk(self):
        # Moving together keeps the E/I gap (and so V) at zero, so both
        # floors fire in full and nothing else does.
        state = _healthy_baseline_then_override(E=0.0, I=0.0)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.verdict == "high-risk"
        assert result.risk == pytest.approx(0.60, abs=0.01)
        assert result.components["high_V"] == 0.0


class TestRhoSignals:
    """Update coherence (rho) signals."""

    def test_negative_rho_raises_risk(self):
        state = _make_state(E=0.6, I=0.6, S=0.2)
        result = assess_behavioral_state(state, rho=-0.5)
        assert result.components["adversarial_rho"] > 0

    def test_positive_rho_no_risk(self):
        state = _make_state(E=0.6, I=0.6, S=0.2)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.components["adversarial_rho"] == 0.0

    def test_coherence_from_rho(self):
        state = _make_state(E=0.6, I=0.6, S=0.2)
        result = assess_behavioral_state(state, rho=0.6)
        # rho=0.6 → coherence=(0.6+1)/2=0.8
        assert result.coherence == pytest.approx(0.8, abs=0.01)

    def test_coherence_from_negative_rho(self):
        state = _make_state(E=0.6, I=0.6, S=0.2)
        result = assess_behavioral_state(state, rho=-0.5)
        # rho=-0.5 → coherence=0.25
        assert result.coherence == pytest.approx(0.25, abs=0.01)


class TestContinuityEnergy:
    """High CE signals state volatility."""

    def test_high_CE_raises_risk(self):
        state = _make_state(E=0.6, I=0.6, S=0.2)
        result = assess_behavioral_state(state, rho=0.5, continuity_energy=1.5)
        assert result.components["high_CE"] > 0

    def test_low_CE_no_risk(self):
        state = _make_state(E=0.6, I=0.6, S=0.2)
        result = assess_behavioral_state(state, rho=0.5, continuity_energy=0.3)
        assert result.components["high_CE"] == 0.0


class TestTrendBonus:
    """Improving trends should slightly reduce risk."""

    def test_improving_trends_reduce_risk(self):
        state = BehavioralEISV()
        # Create improving E and I trends
        for i in range(15):
            state.update(0.3 + i * 0.03, 0.3 + i * 0.03, 0.3)
        result_improving = assess_behavioral_state(state, rho=0.5)

        # Compare with flat state at same final values
        state_flat = _make_state(E=state.E, I=state.I, S=state.S, updates=15)
        result_flat = assess_behavioral_state(state_flat, rho=0.5)

        # Improving should have slightly lower risk (or equal if both are already low)
        assert result_improving.risk <= result_flat.risk + 0.01


class TestVerdictMapping:
    """Verify verdict thresholds."""

    def test_safe_verdict(self):
        state = _make_state(E=0.8, I=0.8, S=0.1)
        result = assess_behavioral_state(state, rho=0.5)
        assert result.verdict == "safe"

    def test_caution_verdict(self):
        # Low E + low I + high S + negative rho → caution range
        state = _make_state(E=0.12, I=0.12, S=0.85, updates=50)
        result = assess_behavioral_state(state, rho=-0.5, continuity_energy=1.0)
        # Risk should be in caution range
        assert result.risk >= RISK_SAFE_THRESHOLD

    def test_high_risk_verdict(self):
        # Everything bad
        state = _make_state(E=0.1, I=0.1, S=0.9)
        result = assess_behavioral_state(state, rho=-0.8, continuity_energy=2.0)
        assert result.verdict == "high-risk"
        assert result.health == "critical"
        assert result.risk >= RISK_CAUTION_THRESHOLD


class TestAssessmentResult:
    """AssessmentResult dataclass."""

    def test_result_has_all_fields(self):
        state = _make_state(E=0.6, I=0.6, S=0.2)
        result = assess_behavioral_state(state, rho=0.5)
        assert isinstance(result, AssessmentResult)
        assert isinstance(result.health, str)
        assert isinstance(result.verdict, str)
        assert isinstance(result.risk, float)
        assert isinstance(result.coherence, float)
        assert isinstance(result.components, dict)

    def test_risk_is_bounded(self):
        """Risk should always be in [0, 1]."""
        for E in [0.0, 0.3, 0.5, 0.8, 1.0]:
            for I in [0.0, 0.3, 0.5, 0.8, 1.0]:
                for S in [0.0, 0.3, 0.5, 0.8, 1.0]:
                    state = _make_state(E=E, I=I, S=S)
                    result = assess_behavioral_state(state, rho=0.0)
                    assert 0.0 <= result.risk <= 1.0, f"Risk {result.risk} out of bounds for E={E}, I={I}, S={S}"

    def test_coherence_is_bounded(self):
        """Coherence should always be in [0, 1]."""
        for rho in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            state = _make_state()
            result = assess_behavioral_state(state, rho=rho)
            assert 0.0 <= result.coherence <= 1.0


class TestDifferentiation:
    """Core test: behavioral state should differentiate agents where ODE doesn't."""

    def test_different_inputs_give_different_states(self):
        """Two agents with different behavior should have different states."""
        # Agent A: productive, calibrated
        agent_a = _make_state(E=0.8, I=0.8, S=0.1)
        result_a = assess_behavioral_state(agent_a, rho=0.6)

        # Agent B: struggling, uncalibrated
        agent_b = _make_state(E=0.3, I=0.3, S=0.7)
        result_b = assess_behavioral_state(agent_b, rho=-0.3)

        # Should have meaningfully different risk scores
        assert abs(result_a.risk - result_b.risk) > 0.2
        assert result_a.verdict != result_b.verdict or result_a.risk < result_b.risk - 0.1
