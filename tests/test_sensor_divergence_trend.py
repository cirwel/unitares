"""
Tests for the model<->body sensor-divergence trend ("compare, don't couple").

The monitor records per-axis divergence (sensor - ODE) on every check-in that
carries a sensor_eisv, retains a bounded history, persists it across restarts,
and surfaces the latest value + a recent slice in the governance-metrics read.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.governance_monitor import UNITARESMonitor, SENSOR_DIVERGENCE_HISTORY_MAX


SENSOR = {"E": 0.4, "I": 0.7, "S": 0.2, "V": 0.1}
BASE_STATE = {"response_text": "x", "complexity": 0.5, "parameters": [0.5] * 128}


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Redirect ensure_project_root to tmp_path so state files are isolated."""
    import src._imports
    monkeypatch.setattr(src._imports, "ensure_project_root", lambda: str(tmp_path))
    (tmp_path / "data" / "agents").mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestSensorDivergenceTrend:

    def test_history_accumulates_with_sensor(self):
        monitor = UNITARESMonitor(agent_id="div_accum", load_state=False)
        for _ in range(3):
            monitor.process_update({**BASE_STATE, "sensor_eisv": dict(SENSOR)})

        assert monitor._last_sensor_divergence is not None
        assert len(monitor._sensor_divergence_history) == 3
        for d in monitor._sensor_divergence_history:
            assert {"dE", "dI", "dS", "dV", "magnitude", "at"} <= set(d.keys())

    def test_no_sensor_means_no_divergence(self):
        monitor = UNITARESMonitor(agent_id="div_none", load_state=False)
        monitor.process_update(dict(BASE_STATE))
        assert monitor._last_sensor_divergence is None
        assert len(monitor._sensor_divergence_history) == 0

    def test_history_is_bounded(self):
        monitor = UNITARESMonitor(agent_id="div_bound", load_state=False)
        for _ in range(SENSOR_DIVERGENCE_HISTORY_MAX + 5):
            monitor.process_update({**BASE_STATE, "sensor_eisv": dict(SENSOR)})
        assert len(monitor._sensor_divergence_history) == SENSOR_DIVERGENCE_HISTORY_MAX

    def test_history_persists_across_save_load(self, isolated_data_dir):
        monitor = UNITARESMonitor(agent_id="div_persist", load_state=False)
        for _ in range(4):
            monitor.process_update({**BASE_STATE, "sensor_eisv": dict(SENSOR)})
        monitor.save_persisted_state()

        restored = UNITARESMonitor(agent_id="div_persist", load_state=True)
        assert len(restored._sensor_divergence_history) == 4
        assert restored._last_sensor_divergence is not None
        # Bound is preserved on the restored deque.
        assert restored._sensor_divergence_history.maxlen == SENSOR_DIVERGENCE_HISTORY_MAX

    def test_loads_pre780_state_file_without_divergence_keys(self, isolated_data_dir):
        """Root-cause regression (incident 2026-06-16): an established agent whose
        persisted state file predates the divergence fields loads via the
        load_state branch, which SKIPS _initialize_fresh_state(). Before the fix
        the attributes were set only in that method, so the monitor came up
        WITHOUT them and rejected the next sensor-carrying check-in. They must now
        be initialized unconditionally in __init__.
        """
        import json

        # Seed a real save, then strip the divergence keys to mimic a pre-#780 file.
        seed = UNITARESMonitor(agent_id="div_pre780", load_state=False)
        seed.process_update(dict(BASE_STATE))
        seed.save_persisted_state()

        path = isolated_data_dir / "data" / "agents" / "div_pre780_state.json"
        data = json.loads(path.read_text())
        data.pop("sensor_divergence", None)
        data.pop("sensor_divergence_history", None)
        path.write_text(json.dumps(data))

        # Load via the persisted-state branch — must still have the attributes.
        restored = UNITARESMonitor(agent_id="div_pre780", load_state=True)
        assert hasattr(restored, "_sensor_divergence_history")
        assert hasattr(restored, "_last_sensor_divergence")
        # And a sensor-carrying check-in records cleanly (no AttributeError).
        restored.process_update({**BASE_STATE, "sensor_eisv": dict(SENSOR)})
        assert len(restored._sensor_divergence_history) == 1

    def test_self_heals_when_attr_missing(self):
        """A monitor restored bypassing __init__ (a pickle/cache instance from
        before this attribute existed, or the Pi plugin's older build) lacks
        _sensor_divergence_history. A check-in carrying sensor_eisv must NOT
        raise AttributeError — it self-heals and records the divergence.

        Regression for the live 'UNITARESMonitor object has no attribute
        _sensor_divergence_history' crash on process_agent_update (2026-06-16).
        """
        monitor = UNITARESMonitor(agent_id="div_heal", load_state=False)
        del monitor._sensor_divergence_history
        assert not hasattr(monitor, "_sensor_divergence_history")
        # Must not raise — self-heals lazily at the write site.
        monitor.process_update({**BASE_STATE, "sensor_eisv": dict(SENSOR)})
        assert len(monitor._sensor_divergence_history) == 1
        assert monitor._sensor_divergence_history.maxlen == SENSOR_DIVERGENCE_HISTORY_MAX


class TestDivergenceInMetrics:

    def test_metrics_expose_divergence_when_present(self):
        from src.services.runtime_queries import _build_eisv_semantics
        class FakeMonitor:
            _behavioral_state = None
            _last_sensor_divergence = {
                "dE": -0.1, "dI": 0.05, "dS": 0.0, "dV": 0.6,
                "magnitude": 0.61, "at": "2026-06-16T00:00:00",
            }
            _sensor_divergence_history = [
                {"dE": -0.1, "dI": 0.05, "dS": 0.0, "dV": 0.6, "magnitude": 0.61, "at": "t"},
            ]

        out = _build_eisv_semantics({"E": 0.6, "I": 0.7, "S": 0.2, "V": 0.0}, FakeMonitor())
        assert out["sensor_divergence"]["dV"] == 0.6
        assert isinstance(out["sensor_divergence_recent"], list)
        assert len(out["sensor_divergence_recent"]) == 1
        assert "sensor_divergence_role" in out["state_semantics"]

    def test_metrics_divergence_null_when_absent(self):
        from src.services.runtime_queries import _build_eisv_semantics
        class FakeMonitor:
            _behavioral_state = None
            _last_sensor_divergence = None
            _sensor_divergence_history = None

        out = _build_eisv_semantics({"E": 0.6, "I": 0.7, "S": 0.2, "V": 0.0}, FakeMonitor())
        assert out["sensor_divergence"] is None
        assert out["sensor_divergence_recent"] is None
