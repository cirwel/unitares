"""Tests for the basin shadow-compare (kernel-split WS1 option b, Phase 0).

The shadow path records what `classify_basin()` would return if fed the
already-computed behavioral EISV (with coherence = C(behavioral_V)) instead of
the ODE-evolved state. It is behavior-neutral measurement gated behind
`UNITARES_BASIN_SHADOW` (default off), used to validate the behavioral-EISV
basin against the live ODE basin before the ODE solve is gated off the
check-in critical path.

Guarantees under test:
  1. Default off — no `basin_shadow` event, no behavior change.
  2. When enabled, warm agents (behavioral confidence >= 0.3) emit a
     `basin_shadow` event with a self-consistent `agree` flag.
  3. Enabling the shadow does NOT perturb the live decision or metrics.
"""

from unittest.mock import MagicMock

import pytest

from src.governance_monitor import UNITARESMonitor
from src import audit_log


def _agent_state():
    # 6 leading params + filler to the expected 128-wide parameter vector,
    # mirroring tests/test_bug_fixes.py. Healthy, low-drift input.
    params = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5] + [0.01] * 122
    return {
        "parameters": params,
        "ethical_drift": [0.1, 0.12, 0.1],
        "response_text": "Test response",
        "complexity": 0.4,
    }


def _run(monitor, n=6):
    last = None
    for _ in range(n):
        last = monitor.process_update(_agent_state())
    return last


def test_basin_shadow_off_by_default(monkeypatch):
    monkeypatch.delenv("UNITARES_BASIN_SHADOW", raising=False)
    spy = MagicMock()
    monkeypatch.setattr(audit_log.audit_logger, "log_basin_shadow", spy)

    monitor = UNITARESMonitor(agent_id="shadow-default")
    _run(monitor)

    spy.assert_not_called()


def test_basin_shadow_emits_when_enabled_and_warm(monkeypatch):
    monkeypatch.setenv("UNITARES_BASIN_SHADOW", "1")
    spy = MagicMock()
    monkeypatch.setattr(audit_log.audit_logger, "log_basin_shadow", spy)

    monitor = UNITARESMonitor(agent_id="shadow-warm")
    _run(monitor, n=6)  # warms behavioral confidence past the 0.3 gate

    assert spy.call_count >= 1, "warm agent should emit at least one basin_shadow event"
    kw = spy.call_args.kwargs
    assert kw["ode_basin"] in ("high", "low", "boundary")
    assert kw["behavioral_basin"] in ("high", "low", "boundary")
    # The agree flag must be self-consistent with the two basins it compares.
    assert kw["agree"] == (kw["ode_basin"] == kw["behavioral_basin"])
    assert kw["confidence"] >= 0.3


def test_basin_shadow_is_behavior_neutral(monkeypatch):
    # Same fresh state + identical inputs, shadow off vs on: the live decision
    # and metrics must be byte-identical — the shadow only observes.
    monkeypatch.delenv("UNITARES_BASIN_SHADOW", raising=False)
    monkeypatch.setattr(audit_log.audit_logger, "log_basin_shadow", MagicMock())
    off = _run(UNITARESMonitor(agent_id="neutral-off"))

    monkeypatch.setenv("UNITARES_BASIN_SHADOW", "1")
    monkeypatch.setattr(audit_log.audit_logger, "log_basin_shadow", MagicMock())
    on = _run(UNITARESMonitor(agent_id="neutral-on"))

    assert on["decision"]["action"] == off["decision"]["action"]
    assert on["decision"].get("basin") == off["decision"].get("basin")
    assert on["decision"].get("sub_action") == off["decision"].get("sub_action")
    assert on["metrics"]["coherence"] == pytest.approx(off["metrics"]["coherence"])
    assert on["metrics"]["E"] == pytest.approx(off["metrics"]["E"])
    assert on["metrics"]["V"] == pytest.approx(off["metrics"]["V"])
