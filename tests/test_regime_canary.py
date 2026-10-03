"""Canary: detect_regime's output set stays inside agent_storage's allowed_regimes.

A regime outside allowed_regimes is silently coerced to 'unknown' on persist
(regime_raw preserved). This pins the producer side only; the restore path is
deliberately untouched (council rec 2026-08-14).
"""

import ast
import itertools
import re
from pathlib import Path
from types import SimpleNamespace

from src.monitor_regime import detect_regime

ROOT = Path(__file__).resolve().parent.parent


def _allowed_regimes():
    tree = ast.parse((ROOT / "src" / "agent_storage.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "allowed_regimes" for t in node.targets)
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError("allowed_regimes not found in src/agent_storage.py")


def _state(I, S, V, s_hist, i_hist, persist=0):
    return SimpleNamespace(
        I=I, S=S, V=V, S_history=s_hist, I_history=i_hist,
        locked_persistence_count=persist,
    )


def test_detect_regime_outputs_stay_inside_allowed_regimes():
    produced = set()
    for I, S, V, dS, dI, persist in itertools.product(
        (0.5, 0.75, 0.9), (0.05, 0.2, 0.4), (0.0, 0.2),
        (-0.01, 0.0, 0.01), (-0.01, 0.0, 0.01), (0, 5),
    ):
        for hist in (True, False):
            s_hist = [S - dS, S] if hist else []
            i_hist = [I - dI, I] if hist else []
            produced.add(detect_regime(_state(I, S, V, s_hist, i_hist, persist)))
    assert produced <= _allowed_regimes()
    assert {"EXPLORATION", "STABLE", "DIVERGENCE", "TRANSITION", "CONVERGENCE"} <= produced


def test_every_returned_literal_is_allowed():
    src = (ROOT / "src" / "monitor_regime.py").read_text()
    literals = set(re.findall(r'return "([A-Za-z_]+)"', src))
    assert literals <= _allowed_regimes()


def test_nominal_is_not_a_detect_regime_output():
    # 'nominal' is a bootstrap/coercion-sink value, never a detected regime.
    src = (ROOT / "src" / "monitor_regime.py").read_text()
    assert "nominal" not in re.findall(r'return "([A-Za-z_]+)"', src)
