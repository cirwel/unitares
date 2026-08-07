"""
Per-source sensor coupling policy ("cut Lumen's spring, keep the fleet").

UNITARES_SENSOR_COUPLING gains two fine-grained modes — behavioral_only /
physical_only — resolved by sensor_coupling_mode()/sensor_coupling_allows().
The monitor applies the decision where the sensor source is known: only
`coupling_sensor` (possibly None) reaches the ODE, while the full submitted
sensor is always kept for divergence.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from governance_core.parameters import sensor_coupling_mode, sensor_coupling_allows
from src.governance_monitor import UNITARESMonitor

BASE = {"response_text": "x", "complexity": 0.5, "parameters": [0.5] * 128}
SENSOR = {"E": 0.4, "I": 0.7, "S": 0.2, "V": 0.1}


class TestCouplingPolicy:
    @pytest.mark.parametrize("env,expected", [
        (None, "on"), ("on", "on"), ("1", "on"), ("true", "on"), ("yes", "on"),
        ("off", "off"), ("0", "off"), ("no", "off"), ("false", "off"),
        ("behavioral_only", "behavioral_only"), ("physical_only", "physical_only"),
        ("garbage", "on"),
    ])
    def test_mode_resolution(self, env, expected, monkeypatch):
        if env is None:
            monkeypatch.delenv("UNITARES_SENSOR_COUPLING", raising=False)
        else:
            monkeypatch.setenv("UNITARES_SENSOR_COUPLING", env)
        assert sensor_coupling_mode() == expected

    @pytest.mark.parametrize("mode,source,allows", [
        ("on", "physical", True), ("on", "behavioral", True), ("on", None, True),
        ("off", "physical", False), ("off", "behavioral", False),
        ("behavioral_only", "behavioral", True),
        ("behavioral_only", "physical", False),
        ("behavioral_only", None, False),   # unknown treated as physical
        ("physical_only", "physical", True),
        ("physical_only", None, True),
        ("physical_only", "behavioral", False),
    ])
    def test_allows(self, mode, source, allows, monkeypatch):
        monkeypatch.setenv("UNITARES_SENSOR_COUPLING", mode)
        assert sensor_coupling_allows(source) is allows


def _run(monitor, source):
    st = dict(BASE)
    st["sensor_eisv"] = dict(SENSOR)
    if source is not None:
        st["sensor_eisv_source"] = source
    return monitor.process_update(st)


def _spy_step_state():
    """Wrap the real step_state, capturing the sensor_eisv kwarg it receives."""
    import src.governance_monitor as gm
    captured = {}
    real = gm.step_state

    def spy(*args, **kwargs):
        captured["sensor_eisv"] = kwargs.get("sensor_eisv")
        return real(*args, **kwargs)

    return captured, spy


class TestMonitorPerSourceGate:

    def test_physical_spring_cut_under_behavioral_only(self, monkeypatch):
        monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "behavioral_only")
        captured, spy = _spy_step_state()
        m = UNITARESMonitor(agent_id="ps_phys_cut", load_state=False)
        with patch("src.governance_monitor.step_state", spy):
            _run(m, "physical")
        # Lumen's (physical) spring is cut: nothing reaches the ODE...
        assert captured["sensor_eisv"] is None
        # ...but divergence is still recorded from the full submitted sensor.
        assert m._last_sensor_divergence is not None

    def test_behavioral_still_couples_under_behavioral_only(self, monkeypatch):
        monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "behavioral_only")
        captured, spy = _spy_step_state()
        m = UNITARESMonitor(agent_id="ps_beh_couple", load_state=False)
        with patch("src.governance_monitor.step_state", spy):
            _run(m, "behavioral")
        assert captured["sensor_eisv"] is not None

    def test_default_on_couples_physical(self, monkeypatch):
        monkeypatch.delenv("UNITARES_SENSOR_COUPLING", raising=False)
        captured, spy = _spy_step_state()
        m = UNITARESMonitor(agent_id="ps_default", load_state=False)
        with patch("src.governance_monitor.step_state", spy):
            _run(m, "physical")
        assert captured["sensor_eisv"] is not None  # no behavior change by default

    def test_off_cuts_everything(self, monkeypatch):
        monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "off")
        captured, spy = _spy_step_state()
        m = UNITARESMonitor(agent_id="ps_off", load_state=False)
        with patch("src.governance_monitor.step_state", spy):
            _run(m, "behavioral")
        assert captured["sensor_eisv"] is None
        assert m._last_sensor_divergence is not None


class TestBehavioralObsSourceDisclosure:
    """The gate covers the ODE spring, not the behavioral observation intake.

    `sensor_coupling_allows()` decides what reaches `step_state`. The
    behavioral estimator — which holds verdict authority under
    BEHAVIORAL_VERDICT_ENABLED — reads the submitted `sensor_eisv` directly
    and is deliberately not gated. So under `behavioral_only` a physical
    sensor is cut from the diagnostic ODE while still driving verdicts.

    That asymmetry is intended but was invisible: the row-level
    `sensor_eisv_source` says what was submitted, and nothing said what the
    authoritative estimator consumed. These pin the disclosure.
    """

    def test_physical_source_recorded_when_it_feeds_behavioral(self, monkeypatch):
        monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "behavioral_only")
        captured, spy = _spy_step_state()
        m = UNITARESMonitor(agent_id="obs_src_phys", load_state=False)
        with patch("src.governance_monitor.step_state", spy):
            _run(m, "physical")
        # The spring is cut ...
        assert captured["sensor_eisv"] is None
        # ... and yet the physical sensor is what the authoritative estimator
        # observed. This is the pair that was previously only inferable by
        # comparing raw_obs magnitudes against the agent's own sensor feed.
        assert m._behavioral_obs_source == "physical"

    def test_behavioral_source_recorded(self, monkeypatch):
        monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "behavioral_only")
        m = UNITARESMonitor(agent_id="obs_src_beh", load_state=False)
        _run(m, "behavioral")
        assert m._behavioral_obs_source == "behavioral"

    def test_untagged_sensor_still_records_a_source(self, monkeypatch):
        """An untagged submitted sensor must not read as 'no sensor involved'."""
        monkeypatch.delenv("UNITARES_SENSOR_COUPLING", raising=False)
        m = UNITARESMonitor(agent_id="obs_src_untagged", load_state=False)
        _run(m, None)
        assert m._behavioral_obs_source == "sensor"

    def test_no_sensor_falls_back_to_a_non_sensor_source(self, monkeypatch):
        """Disembodied agents must report a source too, not an absent field."""
        monkeypatch.delenv("UNITARES_SENSOR_COUPLING", raising=False)
        m = UNITARESMonitor(agent_id="obs_src_none", load_state=False)
        m.process_update(dict(BASE))
        assert m._behavioral_obs_source in ("behavioral_sensor", "continuity_fallback")

    def test_source_survives_a_save_load_round_trip(self, monkeypatch):
        """The label must reach the persisted snapshot, not just live memory.

        Exercises save_persisted_state() rather than re-implementing the
        tagging in the test — a snapshot that loses the source is a snapshot
        that comes back from a restart unable to say what it observed.
        """
        import json
        from pathlib import Path
        from src._imports import ensure_project_root

        monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "behavioral_only")
        m = UNITARESMonitor(agent_id="obs_src_persist", load_state=False)
        _run(m, "physical")
        m.save_persisted_state()

        state_file = (Path(ensure_project_root()) / "data" / "agents"
                      / "obs_src_persist_state.json")
        try:
            written = json.loads(state_file.read_text())
            assert written["behavioral_eisv"]["obs_source"] == "physical"
        finally:
            state_file.unlink(missing_ok=True)
