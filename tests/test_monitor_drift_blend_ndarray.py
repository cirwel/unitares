"""The self-reported ethical_drift blend must see what the check-in handler sends.

phases.py builds ``agent_state["ethical_drift"] = np.array(ctx.ethical_drift)``.
monitor_drift.compute_drift_vector used to accept only list/tuple, so every
real report read as norm 0.0 and the documented 30% blend never ran on the
live path; only callers passing a plain list (tests, scripts) exercised it.
"""

import uuid

import numpy as np
import pytest

from src.governance_monitor import UNITARESMonitor

TEXT = "Implemented the requested change and ran the focused tests; they pass."


def _drift_components(ethical_drift, warm_updates=5):
    # Unique id per call: the drift baseline cache is keyed by agent_id and
    # shared across monitor instances, so a reused id carries state over.
    monitor = UNITARESMonitor(f"test-drift-blend-{uuid.uuid4().hex[:12]}", load_state=False)
    zero = {
        "parameters": np.array([]),
        "ethical_drift": np.array([0.0, 0.0, 0.0]),
        "response_text": TEXT,
        "complexity": 0.5,
    }
    for _ in range(warm_updates):
        monitor.process_update(dict(zero), confidence=0.7)
    monitor.process_update({**zero, "ethical_drift": ethical_drift}, confidence=0.7)
    return monitor._last_drift_vector


def test_ndarray_report_blends_like_a_list():
    as_array = _drift_components(np.array([1.0, 1.0, 1.0]))
    as_list = _drift_components([1.0, 1.0, 1.0])
    assert as_array.coherence_deviation == pytest.approx(as_list.coherence_deviation)
    assert as_array.norm == pytest.approx(as_list.norm)


def test_nonzero_report_moves_the_drift_vector():
    quiet = _drift_components(np.array([0.0, 0.0, 0.0]))
    loud = _drift_components(np.array([1.0, 1.0, 1.0]))
    # The blend adds 0.3 * min(1, |d|) to three components.
    assert loud.coherence_deviation >= 0.3 - 1e-9
    assert loud.norm > quiet.norm


@pytest.mark.parametrize("bad", [None, 1.0, "high"])
def test_unusable_report_contributes_nothing(bad):
    quiet = _drift_components(np.array([0.0, 0.0, 0.0]))
    odd = _drift_components(bad)
    assert odd.norm == pytest.approx(quiet.norm)
