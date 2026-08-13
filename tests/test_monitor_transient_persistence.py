"""Cross-restart persistence of monitor-level transients via the LIVE save path.

`GovernanceMonitor.load_persisted_state` pops four keys off the state file —
`sensor_divergence`, `sensor_divergence_history`, `created_at_iso`,
`last_update_iso` — but the writer that actually runs on the check-in path
(`agent_monitor_state.save_monitor_state[_async]`) never wrote any of them.
`GovernanceMonitor.save_persisted_state` does write them and has no callers, so
the round-trip was only ever exercised in tests.

Measured 2026-08-13 against the live deploy: 0 of 16 state files touched in the
prior 24h carried any of the four, including agents at >22k updates.

The divergence history is the load-bearing one. `UNITARES_SENSOR_COUPLING`
cuts an embodied agent's sensor out of the ODE spring on the stated grounds
that sustained disagreement is itself the signal; with the trend resetting on
every process start, the evidence window was bounded by process uptime rather
than by `SENSOR_DIVERGENCE_HISTORY_MAX`.

These tests target `save_monitor_state` specifically. Asserting against
`save_persisted_state` is what let the gap persist.
"""

import json
import sys
from pathlib import Path

import pytest

project_root_path = Path(__file__).parent.parent
sys.path.insert(0, str(project_root_path))

from src.governance_monitor import UNITARESMonitor
import src.agent_monitor_state as ams


SENSOR_EISV = {"E": 0.36, "I": 0.91, "S": 0.13, "V": -0.54}


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point both the live writer and the monitor's loader at tmp_path."""
    import src._imports

    monkeypatch.setattr(src._imports, "ensure_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(ams, "project_root", str(tmp_path))
    (tmp_path / "data" / "agents").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _update(monitor, n=3):
    """Run updates carrying a sensor EISV so divergence is recorded each cycle."""
    for _ in range(n):
        monitor.process_update(
            {
                "response_text": "seed",
                "complexity": 0.3,
                "parameters": [0.5] * 128,
                "sensor_eisv": dict(SENSOR_EISV),
                "sensor_eisv_source": "physical",
            }
        )


def _saved(tmp_path, agent_id):
    return json.loads((tmp_path / "data" / "agents" / f"{agent_id}_state.json").read_text())


class TestLiveWriterPersistsTransients:

    def test_live_save_writes_all_four_transients(self, isolated_data_dir):
        monitor = UNITARESMonitor(agent_id="transients_live", load_state=False)
        _update(monitor)

        ams.save_monitor_state("transients_live", monitor)
        data = _saved(isolated_data_dir, "transients_live")

        for key in ("sensor_divergence", "sensor_divergence_history",
                    "created_at_iso", "last_update_iso"):
            assert key in data, f"live writer dropped {key}"

    def test_divergence_history_survives_a_restart(self, isolated_data_dir):
        monitor = UNITARESMonitor(agent_id="transients_restart", load_state=False)
        _update(monitor, n=5)
        before = list(monitor._sensor_divergence_history)
        assert before, "fixture did not produce divergence samples"

        ams.save_monitor_state("transients_restart", monitor)

        # A fresh monitor for the same agent = the post-restart path.
        restored = UNITARESMonitor(agent_id="transients_restart")

        assert len(restored._sensor_divergence_history) == len(before)
        assert restored._last_sensor_divergence is not None
        assert restored._last_sensor_divergence["magnitude"] == pytest.approx(
            before[-1]["magnitude"]
        )

    def test_created_at_survives_a_restart(self, isolated_data_dir):
        monitor = UNITARESMonitor(agent_id="transients_age", load_state=False)
        _update(monitor)
        born = monitor.created_at

        ams.save_monitor_state("transients_age", monitor)
        restored = UNITARESMonitor(agent_id="transients_age")

        assert restored.created_at == born

    def test_no_divergence_recorded_leaves_keys_absent(self, isolated_data_dir):
        """An agent that submits no sensor EISV must not gain an empty history."""
        monitor = UNITARESMonitor(agent_id="transients_nosensor", load_state=False)
        monitor.process_update(
            {"response_text": "seed", "complexity": 0.3, "parameters": [0.5] * 128}
        )

        ams.save_monitor_state("transients_nosensor", monitor)
        data = _saved(isolated_data_dir, "transients_nosensor")

        assert "sensor_divergence" not in data
        assert "sensor_divergence_history" not in data

    def test_live_and_monitor_writers_agree_on_transients(self, isolated_data_dir):
        """Pin the two writers together so they cannot drift apart again.

        `save_persisted_state` is currently uncalled. If it is ever revived or
        retired, this fails rather than letting the live path silently diverge.
        """
        monitor = UNITARESMonitor(agent_id="transients_parity", load_state=False)
        _update(monitor)

        ams.save_monitor_state("transients_parity", monitor)
        live = _saved(isolated_data_dir, "transients_parity")

        monitor.save_persisted_state()
        legacy = _saved(isolated_data_dir, "transients_parity")

        for key in ("sensor_divergence", "sensor_divergence_history",
                    "created_at_iso", "last_update_iso"):
            assert (key in live) == (key in legacy), f"writers disagree on {key}"

    @pytest.mark.asyncio
    async def test_async_save_writes_transients_too(self, isolated_data_dir):
        monitor = UNITARESMonitor(agent_id="transients_async", load_state=False)
        _update(monitor)

        await ams.save_monitor_state_async("transients_async", monitor)
        data = _saved(isolated_data_dir, "transients_async")

        assert "sensor_divergence_history" in data
        assert "created_at_iso" in data
