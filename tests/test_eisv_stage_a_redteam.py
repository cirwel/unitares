"""The Stage A red-team's basin check covers the live decision-path consumer.

Its check A3 used to call ``governance_core.check_basin``, which reads only I,
so it could not see the S conjunct of ``config.classify_basin`` that pinned the
live action (contract row 50). It now classifies with the live function and the
same σ Φ is detrended by. Offline: no database, no outcome data.
"""
import importlib.util
from pathlib import Path

from governance_core.dynamics import State
from config.governance_config import get_s_setpoint

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "analysis" / "eisv_stage_a_redteam.py"


def _redteam():
    spec = importlib.util.spec_from_file_location("eisv_stage_a_redteam", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a3_uses_the_live_classifier_with_sigma():
    redteam = _redteam()
    sigma = get_s_setpoint("default")
    # Raw S above S_max = 0.25, detrended S inside it; E/I comfortably HIGH.
    state = State(E=0.80, I=0.82, S=0.30, V=0.0)

    detrended = redteam.evaluate(state, "default", s_detrend=sigma)
    raw = redteam.evaluate(state, "default", s_detrend=0.0)

    assert raw["basin_i_only"] == "high", "the I-only check cannot see the S bound"
    assert raw["basin"] == "boundary"
    assert detrended["basin"] == "high"
