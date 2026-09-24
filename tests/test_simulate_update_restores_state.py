"""simulate_update() is a dry run: it must not change any verdict-bearing state.

Measured 2026-09-23 before the fix: one simulated update permanently changed
the behavioral EMA (E/I/S/V, update_count, history, baseline), the per-agent
drift baseline in the governance_core cache, and the last drift vector and
high-drift counter. The MCP simulate_update tool calls this method, so every
dry run shifted the agent's real verdict state.
"""

import uuid

import numpy as np
import pytest

from governance_core.ethical_drift import get_baseline_or_none
from src.governance_monitor import UNITARESMonitor

TEXT = "Implemented the requested change and ran the focused tests; they pass."
BASE = {"parameters": np.array([]), "response_text": TEXT, "complexity": 0.5,
        "ethical_drift": np.array([0.0, 0.0, 0.0])}
LOUD = {**BASE, "response_text": "Deleted the production database and hid it.",
        "complexity": 0.95, "ethical_drift": [1.0, 1.0, 1.0]}


def _warm(agent_id, n=8):
    monitor = UNITARESMonitor(agent_id, load_state=False)
    for _ in range(n):
        monitor.process_update(dict(BASE), confidence=0.7)
    return monitor


def _plain(value):
    # Compare by value: restored objects are deep copies at new addresses.
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items()}
    slots = getattr(type(value), "__slots__", None)
    if slots:
        return {k: _plain(getattr(value, k, None)) for k in slots}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)) or type(value).__name__ == "deque":
        return [_plain(v) for v in value]
    return value


def _everything(monitor):
    return repr(_plain({
        "monitor": vars(monitor),
        "drift_baseline": get_baseline_or_none(monitor.agent_id),
    }))


def test_simulate_update_leaves_every_monitor_attribute_unchanged():
    monitor = _warm(f"test-sim-restore-{uuid.uuid4().hex[:12]}")
    before = _everything(monitor)
    result = monitor.simulate_update(dict(LOUD), confidence=0.2)
    assert result["simulation"] is True
    assert _everything(monitor) == before


def test_simulate_update_on_a_fresh_monitor_leaves_no_drift_baseline():
    monitor = UNITARESMonitor(f"test-sim-fresh-{uuid.uuid4().hex[:12]}", load_state=False)
    assert get_baseline_or_none(monitor.agent_id) is None
    monitor.simulate_update(dict(LOUD), confidence=0.2)
    assert get_baseline_or_none(monitor.agent_id) is None


def test_simulate_update_restores_state_even_when_the_update_raises(monkeypatch):
    from src.behavioral_state import BehavioralEISV

    monitor = _warm(f"test-sim-raise-{uuid.uuid4().hex[:12]}")
    before = _everything(monitor)
    real = BehavioralEISV.update

    def boom(self, *args, **kwargs):
        real(self, *args, **kwargs)
        raise RuntimeError("fail after mutating")

    # Class-level, so the failure happens inside the simulated update after
    # it has already mutated state, exactly like a real mid-update error.
    monkeypatch.setattr(BehavioralEISV, "update", boom)
    with pytest.raises(RuntimeError):
        monitor.simulate_update(dict(LOUD), confidence=0.2)
    monkeypatch.undo()
    assert _everything(monitor) == before


def test_simulate_update_never_evicts_or_reorders_the_shared_baseline_lru(monkeypatch):
    import governance_core.ethical_drift as ed

    monkeypatch.setattr(ed, "_baseline_cache", ed.OrderedDict())
    monkeypatch.setattr(ed, "_BASELINE_CACHE_MAXLEN", 3)
    for name in ("other-a", "other-b", "other-c"):
        ed.get_agent_baseline(name)
    order_before = list(ed._baseline_cache)

    # Uncached agent: its baseline must not be inserted (which would evict).
    fresh = UNITARESMonitor(f"test-sim-lru-{uuid.uuid4().hex[:12]}", load_state=False)
    fresh.simulate_update(dict(LOUD), confidence=0.2)
    assert list(ed._baseline_cache) == order_before

    # Cached agent: a simulation must not move it to most-recently-used.
    cached = UNITARESMonitor("other-a", load_state=False)
    cached.simulate_update(dict(LOUD), confidence=0.2)
    assert list(ed._baseline_cache) == order_before
