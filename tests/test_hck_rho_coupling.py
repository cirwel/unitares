"""F4 regression tests: HCK ρ(t) must not fire into a void.

Two decouplings the dogfood probe found (v2.13.0):
  1. ``hck.gains_modulated`` was always False even when ρ(t) reduced the PI
     gains — the local flag in update_lambda1 was never propagated.
  2. The behavioral assessment read the *previous* cycle's ρ (and continuity
     energy) because it ran before update_dynamics, so an adversarial ρ spike
     surfaced in ``hck.rho`` while ``adversarial_rho`` stayed 0.0.
"""

import numpy as np
from unittest.mock import patch

import src.governance_monitor as gm
from src.governance_monitor import UNITARESMonitor


class TestGainsModulatedPropagation:
    def test_low_rho_marks_gains_modulated(self):
        mon = UNITARESMonitor("test-f4-gains-low", load_state=False)
        mon.state.current_rho = -0.5  # below neutral → gain factor floors at 0.5
        mon.update_lambda1()
        assert mon._gains_modulated is True

    def test_rho_one_leaves_gains_unmodulated(self):
        mon = UNITARESMonitor("test-f4-gains-one", load_state=False)
        mon.state.current_rho = 1.0  # factor 1.0 → gains unchanged
        mon.update_lambda1()
        assert mon._gains_modulated is False

    def test_flag_resets_each_cycle(self):
        # A cycle that does not run update_lambda1 must not carry a stale True.
        mon = UNITARESMonitor("test-f4-gains-reset", load_state=False)
        mon._gains_modulated = True  # simulate a prior modulated cycle
        # update_count starts at 0; a check-in that skips lambda1 (low confidence
        # on a non-multiple-of-5 cycle) should reset the flag to False.
        mon.state.update_count = 3  # 3 % 5 != 0 → lambda1 block skipped
        mon.process_update(
            {"response_text": "x", "complexity": 0.3, "ethical_drift": [0.0, 0.0, 0.0]},
            confidence=0.9,
        )
        assert mon._gains_modulated is False


class TestBehavioralAssessmentSeesCurrentRho:
    def test_assessment_consumes_current_cycle_rho(self):
        """The ρ passed to the behavioral assessment on cycle N must equal the ρ
        produced by cycle N's dynamics — i.e. dynamics runs first (reorder)."""
        mon = UNITARESMonitor("test-f4-rho-order", load_state=False)
        captured = {}
        real = gm.assess_behavioral_state

        def spy(*args, **kwargs):
            captured["rho"] = kwargs.get("rho")
            return real(*args, **kwargs)

        # Distinct inputs per cycle so the ODE moves and ρ changes cycle-to-cycle.
        with patch.object(gm, "assess_behavioral_state", side_effect=spy):
            for i in range(4):
                mon.process_update(
                    {
                        "parameters": (np.arange(10) * (i + 1) * 0.01).tolist(),
                        "ethical_drift": [0.1 * (i + 1), 0.05, 0.02],
                        "response_text": "varying input " * (i + 1),
                        "complexity": 0.2 + 0.15 * i,
                    },
                    confidence=0.8,
                )

        # With dynamics-first, the consumed ρ is the freshly computed one.
        assert captured["rho"] == mon.state.current_rho
