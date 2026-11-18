"""The EISV substitution probe substitutes what it says and undoes cleanly."""

import importlib.util
import os
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


def _report(reach, reached=(), failed=(), collection_errors=()):
    """Write the files the plugin leaves for a session that finished reporting."""
    probe.collection_errors_path(reach).write_text("\n".join(collection_errors), encoding="utf-8")
    probe.failed_path(reach).write_text("\n".join(failed), encoding="utf-8")
    reach.write_text("\n".join(reached), encoding="utf-8")
    return reach


def test_parse_run_rejects_a_session_that_did_not_complete(tmp_path):
    reach = _report(tmp_path / "reach.txt")
    with pytest.raises(probe.IncompleteRun, match="exited 3"):
        probe.parse_run("freeze_pause", 3, "INTERNALERROR> RuntimeError", "", reach)


def test_parse_run_rejects_an_interrupted_collection(tmp_path):
    reach = _report(tmp_path / "reach.txt", collection_errors=["tests/test_broken.py"])
    stdout = "ERROR tests/test_broken.py\n!!! Interrupted: 1 error during collection !!!\n"
    with pytest.raises(probe.IncompleteRun, match="exited 2"):
        probe.parse_run("count", 2, stdout, "", reach)


def test_parse_run_rejects_collection_errors_the_plugin_recorded(tmp_path):
    # As at exit 1 under --continue-on-collection-errors, which the runner never passes.
    reach = _report(tmp_path / "reach.txt", reached=["tests/test_ok.py::t"], collection_errors=["tests/test_broken.py"])
    with pytest.raises(probe.IncompleteRun, match="collection errors"):
        probe.parse_run("count", 1, "1 passed, 1 error in 0.1s\n", "", reach)


@pytest.mark.parametrize(
    "printed",
    [
        "ERROR invoices.csv could not be parsed",
        "RuntimeError: 1 error during collection of invoices",
        "FAILED tests/fake.py::imitated - AssertionError",
        "FAILED: 1 imitated session failure",
    ],
)
def test_text_a_test_prints_is_never_read_as_a_pytest_report(tmp_path, printed):
    reach = _report(tmp_path / "reach.txt", reached=["tests/test_a.py::t1"], failed=["tests/test_a.py::t1"])
    stdout = f"----- Captured stdout call -----\n{printed}\nFAILED tests/test_a.py::t1\n1 failed in 0.1s\n"
    run = probe.parse_run("freeze_pause", 1, stdout, "", reach)
    assert run["failed"] == ["tests/test_a.py::t1"]


def test_runner_does_not_inherit_pytest_addopts(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, env, **kwargs):
        seen["env"] = env
        _report(tmp_path / "reach_count.txt")
        return probe.subprocess.CompletedProcess(cmd, 0, "1 passed\n", "")

    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only -x")
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    probe.run_mode("count", ["tests/test_a.py"], tmp_path)
    assert "PYTEST_ADDOPTS" not in seen["env"]


def test_runner_clears_stale_result_files(tmp_path, monkeypatch):
    _report(tmp_path / "reach_count.txt", reached=["tests/old.py::t"], failed=["tests/old.py::t"])
    monkeypatch.setattr(
        probe.subprocess, "run", lambda cmd, **kw: probe.subprocess.CompletedProcess(cmd, 0, "1 passed\n", "")
    )
    with pytest.raises(probe.IncompleteRun, match="did not report"):
        probe.run_mode("count", ["tests/test_a.py"], tmp_path)


@pytest.mark.parametrize(
    "spelling",
    [
        "./tests/test_eisv_substitution_probe.py",
        "tests/../tests/test_eisv_substitution_probe.py",
        "tests/test_eisv_substitution_probe.py::test_unknown_mode_is_refused",
    ],
)
def test_every_spelling_of_a_self_test_is_excluded(spelling, monkeypatch):
    def no_run(*args, **kwargs):
        raise AssertionError("pytest must not run")

    monkeypatch.setattr(probe.subprocess, "run", no_run)
    assert probe.is_self_test(spelling)
    assert probe.main(["--files", spelling]) == 2


def test_other_targets_are_not_mistaken_for_self_tests():
    assert not probe.is_self_test("tests/test_governance_monitor.py")
    assert not probe.is_self_test("tests")


def test_parse_run_rejects_a_nonzero_session_with_no_attributable_failure(tmp_path):
    reach = _report(tmp_path / "reach.txt", reached=["tests/test_a.py::t1"])
    with pytest.raises(probe.IncompleteRun, match="no attributable test failure"):
        probe.parse_run("freeze_pause", 1, "1 passed in 0.1s\n", "", reach)


@pytest.mark.parametrize("failed_tests", [(), ("tests/test_a.py::t1",)])
def test_parse_run_rejects_a_session_level_failure(tmp_path, failed_tests):
    reach = _report(tmp_path / "reach.txt", reached=["tests/test_a.py::t1"], failed=failed_tests)
    stderr = "\n\nFAILED: 1 unawaited AsyncMock coroutine leak(s):\n  - tests/test_a.py::t1\n"
    with pytest.raises(probe.IncompleteRun, match="session-level failure"):
        probe.parse_run("out_pause", 1, "1 passed in 0.1s\n", stderr, reach)


def test_parse_run_keeps_a_test_setup_error_as_a_measured_failure(tmp_path):
    reach = _report(tmp_path / "reach.txt", reached=["tests/test_a.py::t1"], failed=["tests/test_a.py::t1"])
    run = probe.parse_run("freeze_pause", 1, "ERROR tests/test_a.py::t1 - fixture failed\n1 error in 0.1s\n", "", reach)
    assert run["failed"] == ["tests/test_a.py::t1"]


# These start a real probe session. Inside one (this file reached through a
# path the probe failed to exclude) they would start another, recursively.
_real_session = pytest.mark.skipif(
    bool(os.environ.get("EISV_PROBE_MODE")), reason="never start a probe session inside one"
)

_IMITATING_TESTS = """
def test_prints_lines_that_look_like_pytest_reports():
    print("ERROR invoices.csv could not be parsed")
    print("FAILED fake/node.py::imitated - AssertionError")
    print("FAILED: 1 imitated session failure")
    raise RuntimeError("1 error during collection of invoices")


def test_passes():
    pass
"""


@_real_session
def test_a_real_session_reports_only_what_pytest_recorded(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "test_imitating.py").write_text(_IMITATING_TESTS, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    run = probe.run_mode("count", [str(target)], work)
    assert len(run["failed"]) == 1
    assert run["failed"][0].endswith("test_imitating.py::test_prints_lines_that_look_like_pytest_reports")


@_real_session
def test_a_real_session_never_collects_a_self_test_through_a_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "test_passes.py").write_text("def test_passes():\n    pass\n", encoding="utf-8")
    (target / "test_linked_probe.py").symlink_to(probe.REPO_ROOT / probe.SELF_TESTS[0])
    work = tmp_path / "work"
    work.mkdir()
    run = probe.run_mode("count", [str(target)], work)
    assert run["failed"] == []
    assert run["summary"].lstrip("= ").startswith("1 passed"), run["summary"]


def test_runner_does_not_continue_past_collection_errors(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        _report(tmp_path / "reach_count.txt")
        return probe.subprocess.CompletedProcess(cmd, 0, "1 passed\n", "")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    probe.run_mode("count", ["tests/test_a.py"], tmp_path)
    assert "--continue-on-collection-errors" not in seen["cmd"]


def test_parse_run_rejects_a_session_where_the_plugin_did_not_report(tmp_path):
    with pytest.raises(probe.IncompleteRun, match="did not report"):
        probe.parse_run("count", 0, "1 passed", "", tmp_path / "missing.txt")


def test_parse_run_reads_failures_and_reach_from_a_completed_session(tmp_path):
    reach = _report(
        tmp_path / "reach.txt",
        reached=["tests/test_a.py::t1", "tests/test_a.py::t2"],
        failed=["tests/test_a.py::t1"],
    )
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
