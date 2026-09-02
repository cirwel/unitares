"""Unit tests for the Elixir CI gate machinery (#2040).

Covers the two scripts .github/workflows/elixir-tests.yml runs:
  scripts/ci/elixir_paths_changed.py  -- decides whether the suites are relevant
  scripts/ci/elixir_gate.py           -- the single requirable status context
and pins the workflow wiring those scripts assume.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "elixir-tests.yml"


def _load(name: str):
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


paths_changed = _load("elixir_paths_changed")
gate = _load("elixir_gate")


# --- elixir_paths_changed.is_relevant -------------------------------------

@pytest.mark.parametrize(
    "paths, expected",
    [
        ([], False),
        (["README.md", "src/mcp_server.py"], False),
        (["elixir/sentinel/lib/poller.ex"], True),
        (["elixir/lease_plane/test/effects_test.exs"], True),
        (["elixir/unitares_sdk/mix.exs"], True),
        (["elixir/agent_orchestrator/README.md"], True),
        (["elixir/dialectic_live/lib/x.ex"], True),
        (["elixir/wave3a_handlers/lib/router.ex"], True),
        (["db/postgres/migrations/099_x.sql"], True),
        ([".github/workflows/elixir-tests.yml"], True),
        (["scripts/ci/elixir_paths_changed.py"], True),
        (["scripts/ci/elixir_gate.py"], True),
        # Prefix discipline: a sibling directory that merely shares a prefix
        # string is not a suite.
        (["elixir/sentinel_docs/notes.md"], False),
        (["db/postgresql-notes.md"], False),
        (["docs/elixir/sentinel/overview.md"], False),
    ],
)
def test_is_relevant(paths, expected):
    assert paths_changed.is_relevant(paths) is expected


def test_relevant_lists_match_the_workflow_header():
    """The header comment documents the same surfaces the script gates on."""
    text = WORKFLOW.read_text()
    for prefix in paths_changed.RELEVANT_PREFIXES:
        assert prefix.rstrip("/") in text, prefix
    for rel_file in paths_changed.RELEVANT_FILES:
        assert rel_file in text, rel_file


# --- elixir_paths_changed.changed_paths / decide against a real repo -------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "ci@example.invalid")
    _git(path, "config", "user.name", "ci")
    (path / "README.md").write_text("base\n")
    (path / "elixir" / "sentinel").mkdir(parents=True)
    (path / "elixir" / "sentinel" / "a.ex").write_text("a\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


def _commit(repo: Path, rel: str, content: str, message: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_changed_paths_docs_only_is_not_relevant(repo: Path):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "docs/x.md", "x\n", "docs")
    assert paths_changed.changed_paths(base, head, cwd=str(repo)) == ["docs/x.md"]
    relevant, reason = paths_changed.decide("pull_request", base, head, cwd=str(repo))
    assert relevant is False
    assert "none of 1" in reason


def test_changed_paths_elixir_change_is_relevant(repo: Path):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "elixir/sentinel/a.ex", "changed\n", "elixir")
    relevant, _ = paths_changed.decide("pull_request", base, head, cwd=str(repo))
    assert relevant is True


@pytest.mark.parametrize("base", ["", "0000000000000000000000000000000000000000"])
def test_decide_is_fail_safe_without_a_base(repo: Path, base: str):
    head = _git(repo, "rev-parse", "HEAD")
    relevant, reason = paths_changed.decide("push", base, head, cwd=str(repo))
    assert relevant is True
    assert "undeterminable" in reason


def test_decide_is_fail_safe_when_base_is_unreachable(repo: Path):
    # No `origin` remote, so the depth-1 fetch of the missing base fails and
    # the decision must fall through to "run the suites".
    head = _git(repo, "rev-parse", "HEAD")
    missing = "1" * 40
    assert paths_changed.changed_paths(missing, head, cwd=str(repo)) is None
    relevant, _ = paths_changed.decide("push", missing, head, cwd=str(repo))
    assert relevant is True


def test_main_writes_github_output(repo: Path, tmp_path: Path, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "docs/y.md", "y\n", "docs")
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(repo)
    assert paths_changed.main(["--event", "pull_request", "--base", base, "--head", head]) == 0
    lines = output.read_text().splitlines()
    assert lines[0] == "relevant=false"
    assert lines[1].startswith("reason=")


# --- elixir_gate.evaluate ---------------------------------------------------

SUITES = (
    "unitares_sdk",
    "agent_orchestrator",
    "dialectic_live",
    "sentinel",
    "wave3a_handlers",
    "lease_plane",
)


def _needs(relevant: str, detector: str = "success", **results: str) -> dict:
    needs = {"changes": {"result": detector, "outputs": {"relevant": relevant}}}
    for name in SUITES:
        needs[name] = {"result": results.get(name, "success" if relevant == "true" else "skipped")}
    return needs


def test_gate_passes_when_every_suite_succeeds():
    ok, lines = gate.evaluate(_needs("true"))
    assert ok, lines


def test_gate_passes_when_nothing_relevant_changed_and_suites_skipped():
    ok, lines = gate.evaluate(_needs("false"))
    assert ok, lines
    assert all("skipped (nothing relevant changed)" in line for line in lines[1:])


def test_gate_fails_on_any_suite_failure():
    ok, lines = gate.evaluate(_needs("true", lease_plane="failure"))
    assert not ok
    assert any("FAIL lease_plane" in line for line in lines)


def test_gate_fails_on_cancelled():
    ok, _ = gate.evaluate(_needs("true", sentinel="cancelled"))
    assert not ok


def test_gate_fails_when_relevant_but_a_suite_was_skipped():
    """A skipped suite on a relevant change is a wiring defect, not a pass."""
    ok, lines = gate.evaluate(_needs("true", wave3a_handlers="skipped"))
    assert not ok
    assert any("skipped although relevant" in line for line in lines)


def test_gate_fails_when_detector_did_not_succeed():
    ok, lines = gate.evaluate(_needs("false", detector="failure"))
    assert not ok
    assert lines[0].startswith("FAIL changes")


def test_gate_fails_when_detector_is_missing():
    ok, _ = gate.evaluate({name: {"result": "success"} for name in SUITES})
    assert not ok


def test_gate_fails_when_no_suites_are_wired():
    ok, lines = gate.evaluate({"changes": {"result": "success", "outputs": {"relevant": "false"}}})
    assert not ok
    assert any("miswired" in line for line in lines)


def test_gate_main_exit_codes(monkeypatch, capsys):
    import json

    monkeypatch.setenv("NEEDS_JSON", json.dumps(_needs("false")))
    assert gate.main() == 0
    assert "elixir-gate: PASS" in capsys.readouterr().out

    monkeypatch.setenv("NEEDS_JSON", json.dumps(_needs("true", sentinel="failure")))
    assert gate.main() == 1

    monkeypatch.setenv("NEEDS_JSON", "not json")
    assert gate.main() == 1


# --- workflow wiring --------------------------------------------------------

def test_workflow_wires_detector_gate_and_every_suite():
    data = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML parses the bare `on:` key as boolean True.
    triggers = data.get("on", data.get(True))
    for event in ("pull_request", "push"):
        assert "paths" not in (triggers[event] or {}), (
            f"{event} must not be path-filtered: a filtered workflow reports no "
            "status and a required context then waits forever (#2040)"
        )

    jobs = data["jobs"]
    assert "changes" in jobs and "elixir-gate" in jobs
    assert jobs["changes"]["outputs"]["relevant"]
    for name in SUITES:
        assert jobs[name]["needs"] == "changes", name
        assert jobs[name]["if"] == "needs.changes.outputs.relevant == 'true'", name

    gate_job = jobs["elixir-gate"]
    assert gate_job["name"] == "elixir-gate"
    assert set(gate_job["needs"]) == {"changes", *SUITES}
    assert str(gate_job["if"]).strip("${} ") == "always()"
    run_steps = [step for step in gate_job["steps"] if "run" in step]
    assert any("scripts/ci/elixir_gate.py" in step["run"] for step in run_steps)
    assert any(
        "NEEDS_JSON" in (step.get("env") or {}) for step in run_steps
    ), "the gate step must receive toJSON(needs) as NEEDS_JSON"
