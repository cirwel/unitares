"""Warmup structural grace: suppress cold-ODE structural pauses on the first few
process-local cycles after a restart, but ONLY when the restored behavioral
baseline is established and says 'safe'.

Regression for the 2026-06-03 Lumen restart false-pause: the DB behavioral
restore (#575) fixed the risk-ceiling trigger but a residual void_pause remained
on the cold post-restart state (verified: behavioral risk 0.00, yet void_pause).
This guard closes that, gated on the trustworthy baselined+safe behavioral signal
so a genuinely-degraded agent (high risk / non-safe verdict) is never suppressed.
"""
from unittest.mock import patch
import pytest

from config.governance_config import GovernanceConfig
from src.governance_monitor import UNITARESMonitor


def _mon(label="wsg"):
    return UNITARESMonitor(label, load_state=False)


def _baseline_safe(m):
    """Drive the behavioral state to baselined, and stamp the last verdict safe."""
    for _ in range(30):
        m._behavioral_state.update(0.34, 0.74, 0.20)
    assert m._behavioral_state.is_baselined
    m._last_behavioral_verdict = "safe"


def _struct_pause(sub="void_pause", edge="void"):
    return {"action": "pause", "sub_action": sub, "reason": f"{sub} fired", "nearest_edge": edge}


class TestSuppressesWhenBaselinedSafeInWindow:
    @pytest.mark.parametrize("sub", ["void_pause", "coherence_pause", "basin_pause", "cirs_block"])
    def test_structural_pause_suppressed(self, sub):
        m = _mon(); _baseline_safe(m); m._process_local_updates = 1
        with patch("src.governance_monitor.audit_logger.log_warmup_structural_suppressed"):
            out = m._maybe_warmup_structural_suppress(_struct_pause(sub, "coherence" if sub == "cirs_block" else "void"))
        assert out["action"] == "proceed"
        assert out["warmup_structural_suppressed"] is True
        assert out["original_action"] == "pause"


class TestNeverSuppresses:
    def test_when_not_baselined(self):
        m = _mon(); m._last_behavioral_verdict = "safe"; m._process_local_updates = 1
        assert not m._behavioral_state.is_baselined
        out = m._maybe_warmup_structural_suppress(_struct_pause())
        assert out["action"] == "pause"

    def test_when_behavioral_not_safe(self):
        m = _mon(); _baseline_safe(m); m._last_behavioral_verdict = "caution"; m._process_local_updates = 1
        out = m._maybe_warmup_structural_suppress(_struct_pause())
        assert out["action"] == "pause"

    def test_oscillation_edge(self):
        m = _mon(); _baseline_safe(m); m._process_local_updates = 1
        out = m._maybe_warmup_structural_suppress(_struct_pause("cirs_block", "oscillation"))
        assert out["action"] == "pause"

    def test_risk_pause_not_in_suppressible_set(self):
        m = _mon(); _baseline_safe(m); m._process_local_updates = 1
        out = m._maybe_warmup_structural_suppress(_struct_pause("risk_pause", "risk"))
        assert out["action"] == "pause"

    def test_after_window_closes(self):
        m = _mon(); _baseline_safe(m)
        m._process_local_updates = GovernanceConfig.WARMUP_STRUCTURAL_GRACE_CYCLES + 1
        out = m._maybe_warmup_structural_suppress(_struct_pause())
        assert out["action"] == "pause"

    def test_boundary_last_in_window_still_suppressed(self):
        """cycle == GRACE_CYCLES is the last IN-window cycle (gate is `> CYCLES`).
        Pins the boundary so a future `>=` regression is caught."""
        m = _mon(); _baseline_safe(m)
        m._process_local_updates = GovernanceConfig.WARMUP_STRUCTURAL_GRACE_CYCLES
        with patch("src.governance_monitor.audit_logger.log_warmup_structural_suppressed"):
            out = m._maybe_warmup_structural_suppress(_struct_pause())
        assert out["action"] == "proceed"

    def test_non_pause_untouched(self):
        m = _mon(); _baseline_safe(m); m._process_local_updates = 1
        out = m._maybe_warmup_structural_suppress({"action": "proceed", "sub_action": "approve"})
        assert out["action"] == "proceed"
        assert "warmup_structural_suppressed" not in out

    def test_flag_disabled(self):
        m = _mon(); _baseline_safe(m); m._process_local_updates = 1
        orig = GovernanceConfig.WARMUP_STRUCTURAL_GRACE_ENABLED
        try:
            GovernanceConfig.WARMUP_STRUCTURAL_GRACE_ENABLED = False
            out = m._maybe_warmup_structural_suppress(_struct_pause())
            assert out["action"] == "pause"
        finally:
            GovernanceConfig.WARMUP_STRUCTURAL_GRACE_ENABLED = orig


class TestPerProcessCounter:
    def test_counter_starts_zero_and_increments_per_update(self):
        m = _mon()
        assert m._process_local_updates == 0
        m.process_update({"response_text": "x", "complexity": 0.2})
        assert m._process_local_updates == 1

    def test_counter_not_persisted_in_state(self):
        """The counter lives on the monitor, not GovernanceState — a restored
        state (high lifetime update_count) must not inflate it."""
        m = _mon()
        d = m.state.to_dict()
        assert "_process_local_updates" not in d
        assert "process_local_updates" not in d
