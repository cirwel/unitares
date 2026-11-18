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


def test_pause_template_differs_from_proceed_only_in_verdict_keys():
    templates = probe.build_templates(UNITARESMonitor.process_update, UNITARESMonitor)
    proceed, pause = templates["proceed"], templates["pause"]
    assert set(proceed) == set(pause)
    differing = {key for key in proceed if proceed[key] != pause[key]}
    assert differing and differing <= set(probe.VERDICT_KEYS) | {"behavioral"}
    assert pause["metrics"] == proceed["metrics"]
    if "behavioral" in differing:
        rest = lambda r: {k: v for k, v in r["behavioral"].items() if k != "assessment"}
        assert rest(pause) == rest(proceed)
    for field in ("basin", "margin", "nearest_edge"):
        assert pause["decision"].get(field) == proceed["decision"].get(field)


def test_pause_template_is_consistent_in_every_verdict_field():
    pause = probe.build_templates(UNITARESMonitor.process_update, UNITARESMonitor)["pause"]
    assert pause["decision"]["action"] == "pause"
    assert pause["policy_evaluation"]["action"] == "pause"
    assert pause["enforcement"]["requested"] is True
    verdict = ((pause.get("behavioral") or {}).get("assessment") or {}).get("verdict")
    if isinstance(verdict, dict) and "decision_action" in verdict:
        assert verdict["decision_action"] == "pause"


def test_rebuilding_a_result_with_its_own_verdict_reproduces_it():
    proceed = probe.build_templates(UNITARESMonitor.process_update, UNITARESMonitor)["proceed"]
    assert probe.with_verdict(proceed, proceed) == proceed


def test_an_empty_selection_is_refused_without_running_pytest(monkeypatch):
    def no_run(*args, **kwargs):
        raise AssertionError("pytest must not be launched for an empty selection")

    monkeypatch.setattr(probe.subprocess, "run", no_run)
    assert probe.main(["--files", *probe.SELF_TESTS]) == 2
    with pytest.raises(probe.IncompleteRun):
        probe.run_mode("count", [], Path("."))


@pytest.mark.parametrize("outer", ["freeze_proceed", "freeze_pause", "out_proceed", "out_pause"])
def test_nested_install_builds_templates_from_the_real_method(installed, outer):
    installed(outer)
    templates = probe.build_templates(UNITARESMonitor.process_update, UNITARESMonitor)
    assert templates["proceed"]["decision"]["action"] == "proceed"
    assert templates["pause"]["decision"]["action"] == "pause"
    inner = "freeze_pause" if outer.endswith("proceed") else "freeze_proceed"
    installed(inner)
    result = UNITARESMonitor("probe-nested-subject").process_update(dict(_STATE), confidence=0.7)
    assert result["decision"]["action"] == inner.split("_", 1)[1]


def test_undo_of_a_nested_install_restores_the_outer_substitute():
    outer_undo = probe.install("freeze_pause")
    try:
        outer = UNITARESMonitor.process_update
        probe.install("freeze_proceed")()
        assert UNITARESMonitor.process_update is outer
    finally:
        outer_undo()


def test_parse_run_rejects_a_session_that_did_not_complete(tmp_path):
    reach = tmp_path / "reach.txt"
    reach.write_text("", encoding="utf-8")
    with pytest.raises(probe.IncompleteRun, match="exited 3"):
        probe.parse_run("freeze_pause", 3, "INTERNALERROR> RuntimeError", "", reach)


@pytest.mark.parametrize(
    "returncode, stdout",
    [
        (2, "ERROR tests/test_broken.py\n!!! Interrupted: 1 error during collection !!!\n1 error in 0.2s\n"),
        (1, "ERROR tests/test_broken.py - ImportError\n1 passed, 1 error in 0.2s\n"),
    ],
)
def test_parse_run_rejects_collection_errors(tmp_path, returncode, stdout):
    reach = tmp_path / "reach.txt"
    reach.write_text("tests/test_ok.py::t", encoding="utf-8")
    with pytest.raises(probe.IncompleteRun):
        probe.parse_run("count", returncode, stdout, "", reach)


def test_parse_run_rejects_a_nonzero_session_with_no_attributable_failure(tmp_path):
    reach = tmp_path / "reach.txt"
    reach.write_text("tests/test_a.py::t1", encoding="utf-8")
    with pytest.raises(probe.IncompleteRun, match="no attributable test failure"):
        probe.parse_run("freeze_pause", 1, "1 passed in 0.1s\n", "", reach)


@pytest.mark.parametrize("failed_tests", ["", "FAILED tests/test_a.py::t1 - AssertionError\n"])
def test_parse_run_rejects_a_session_level_failure(tmp_path, failed_tests):
    reach = tmp_path / "reach.txt"
    reach.write_text("tests/test_a.py::t1", encoding="utf-8")
    stderr = "\n\nFAILED: 1 unawaited AsyncMock coroutine leak(s):\n  - tests/test_a.py::t1\n"
    with pytest.raises(probe.IncompleteRun, match="session-level failure"):
        probe.parse_run("out_pause", 1, failed_tests + "1 passed in 0.1s\n", stderr, reach)


def test_parse_run_keeps_a_test_setup_error_as_a_measured_failure(tmp_path):
    reach = tmp_path / "reach.txt"
    reach.write_text("tests/test_a.py::t1", encoding="utf-8")
    stdout = "ERROR tests/test_a.py::t1 - fixture failed\n1 error in 0.1s\n"
    run = probe.parse_run("freeze_pause", 1, stdout, "", reach)
    assert run["failed"] == ["tests/test_a.py::t1"]


def test_runner_does_not_continue_past_collection_errors(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        (tmp_path / "reach_count.txt").write_text("", encoding="utf-8")
        return probe.subprocess.CompletedProcess(cmd, 0, "1 passed\n", "")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    probe.run_mode("count", ["tests/test_a.py"], tmp_path)
    assert "--continue-on-collection-errors" not in seen["cmd"]


def test_parse_run_rejects_a_session_where_the_plugin_did_not_report(tmp_path):
    with pytest.raises(probe.IncompleteRun, match="did not report"):
        probe.parse_run("count", 0, "1 passed", "", tmp_path / "missing.txt")


def test_parse_run_reads_failures_and_reach_from_a_completed_session(tmp_path):
    reach = tmp_path / "reach.txt"
    reach.write_text("tests/test_a.py::t1\ntests/test_a.py::t2", encoding="utf-8")
    stdout = "FAILED tests/test_a.py::t1 - AssertionError\n1 failed, 1 passed in 0.1s\n"
    run = probe.parse_run("out_pause", 1, stdout, "", reach)
    assert run["failed"] == ["tests/test_a.py::t1"]
    assert run["reached"] == ["tests/test_a.py::t1", "tests/test_a.py::t2"]
    assert run["summary"] == "1 failed, 1 passed in 0.1s"


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
