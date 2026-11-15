"""The EISV substitution probe substitutes what it says and undoes cleanly."""

import importlib.util
from pathlib import Path

import pytest

from src.governance_monitor import UNITARESMonitor

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "diagnostics" / "eisv_substitution_probe.py"
_spec = importlib.util.spec_from_file_location("eisv_substitution_probe", _PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

_STATE = {"response_text": "work", "complexity": 0.4}


@pytest.fixture
def installed():
    undos = []

    def _install(mode, on_call=None):
        undos.append(probe.install(mode, on_call=on_call))

    yield _install
    for undo in reversed(undos):
        undo()


def _history(monitor):
    return len(monitor.state.V_history)


def test_templates_are_real_proceed_and_pause_results():
    templates = probe.build_templates(UNITARESMonitor.process_update, UNITARESMonitor)
    assert templates["proceed"]["decision"]["action"] == "proceed"
    assert templates["pause"]["decision"]["action"] == "pause"


@pytest.mark.parametrize("kind", ["proceed", "pause"])
def test_freeze_returns_the_template_and_state_never_advances(installed, kind):
    installed(f"freeze_{kind}")
    monitor = UNITARESMonitor("probe-freeze-subject")
    first = monitor.process_update(dict(_STATE), confidence=0.7)
    second = monitor.process_update(dict(_STATE), confidence=0.7)
    assert first["decision"]["action"] == kind
    assert first == second
    assert _history(monitor) == 0


@pytest.mark.parametrize("kind", ["proceed", "pause"])
def test_out_replaces_the_output_while_state_advances(installed, kind):
    installed(f"out_{kind}")
    monitor = UNITARESMonitor("probe-out-subject")
    result = monitor.process_update(dict(_STATE), confidence=0.7)
    monitor.process_update(dict(_STATE), confidence=0.7)
    assert result["decision"]["action"] == kind
    assert _history(monitor) == 2


def test_count_is_transparent_and_records_each_call(installed):
    calls = []
    real_result = UNITARESMonitor("probe-count-reference").process_update(dict(_STATE), confidence=0.7)
    installed("count", on_call=lambda: calls.append(1))
    result = UNITARESMonitor("probe-count-subject").process_update(dict(_STATE), confidence=0.7)
    assert calls == [1]
    assert result["decision"]["action"] == real_result["decision"]["action"]


def test_undo_restores_the_real_method():
    original = UNITARESMonitor.process_update
    undo = probe.install("freeze_proceed")
    assert UNITARESMonitor.process_update is not original
    undo()
    assert UNITARESMonitor.process_update is original


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        probe.make_substitute("freeze_maybe", UNITARESMonitor.process_update, {})


def test_compare_separates_state_advance_from_verdict_sensitivity():
    results = {
        "count": {"failed": ["t::base"]},
        "freeze_proceed": {"failed": ["t::base", "t::state", "t::value"]},
        "freeze_pause": {"failed": ["t::base", "t::state", "t::value", "t::verdict"]},
        "out_proceed": {"failed": ["t::base", "t::value"]},
        "out_pause": {"failed": ["t::base", "t::value", "t::verdict"]},
    }
    report = probe.compare(results)
    assert report["new_failures"] == {"freeze_proceed": 2, "freeze_pause": 3, "out_proceed": 1, "out_pause": 2}
    assert report["state_advance_only"] == ["t::state"]
    assert report["freeze_verdict_sensitive"] == ["t::verdict"]
    assert report["out_verdict_sensitive"] == ["t::verdict"]
