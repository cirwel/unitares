"""Unit tests for the Elixir CI gate machinery (#2040).

Covers the two scripts .github/workflows/elixir-tests.yml runs:
  scripts/ci/elixir_paths_changed.py  -- decides whether the suites are relevant
  scripts/ci/elixir_gate.py           -- the single requirable status context
and pins the workflow wiring those scripts assume.
"""

from __future__ import annotations

import importlib.util
import json
import re
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

DETECTOR = gate.DETECTOR_JOB
SUITES = (
    "unitares_sdk_floor",
    "unitares_sdk",
    "agent_orchestrator",
    "dialectic_live",
    "sentinel",
    "wave3a_handlers",
    "lease_plane",
)


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
        # Cross-runtime fixtures the lease_plane suite reads by relative path.
        (["tests/vectors/effect_payload_canonical.json"], True),
        (["tests/vendored/fermata-governed-effect-ir-v0.schema.json"], True),
        # What the lease_plane job's `docker compose up postgres-age` consumes.
        (["docker-compose.yml"], True),
        (["Dockerfile"], True),
        ([".dockerignore"], True),
        ([".github/workflows/elixir-tests.yml"], True),
        (["scripts/ci/elixir_paths_changed.py"], True),
        (["scripts/ci/elixir_gate.py"], True),
        # Prefix discipline: a sibling that merely shares a prefix string, or a
        # same-named file elsewhere, is not a suite input.
        (["elixir/sentinel_docs/notes.md"], False),
        (["db/postgresql-notes.md"], False),
        (["docs/elixir/sentinel/overview.md"], False),
        (["docs/docker-compose.yml"], False),
        (["dashboard/Dockerfile"], False),
        (["tests/test_lease_plane_proxy.py"], False),
    ],
)
def test_is_relevant(paths, expected):
    assert paths_changed.is_relevant(paths) is expected


def test_shared_fixture_prefixes_referenced_by_elixir_tests_are_relevant():
    """Any `tests/<dir>/` the Elixir suites reach into by relative path must be
    a relevant prefix, or a fixture edit passes the gate without running them."""
    referenced: set[str] = set()
    for exs in (REPO_ROOT / "elixir").glob("*/test/**/*.exs"):
        for match in re.finditer(r"tests/(vectors|vendored)/", exs.read_text(errors="replace")):
            referenced.add(match.group(0))
    assert referenced, "expected the lease_plane suite to reference shared fixtures"
    for prefix in sorted(referenced):
        assert prefix in paths_changed.RELEVANT_PREFIXES, prefix


def test_relevant_lists_match_the_workflow_header():
    """The header comment documents the same surfaces the script gates on."""
    text = WORKFLOW.read_text()
    for prefix in paths_changed.RELEVANT_PREFIXES:
        assert prefix.rstrip("/") in text, prefix
    for rel_file in paths_changed.RELEVANT_FILES:
        assert rel_file in text, rel_file


def test_relevant_prefixes_cover_every_suite_directory_the_workflow_runs_in():
    """Every `working-directory: elixir/<app>` the jobs use is a relevant prefix,
    so a new app job cannot be added without the detector learning about it."""
    text = WORKFLOW.read_text()
    directories = set(re.findall(r"working-directory:\s*(elixir/[A-Za-z0-9_]+)", text))
    assert directories, "expected working-directory lines for the Elixir apps"
    for directory in sorted(directories):
        assert f"{directory}/" in paths_changed.RELEVANT_PREFIXES, directory


def test_docker_compose_usage_implies_docker_files_are_relevant():
    """The lease_plane job builds and runs the compose service; the files that
    define it must count as suite inputs or a compose-only break passes green."""
    text = WORKFLOW.read_text()
    assert "docker compose up" in text
    for rel_file in ("docker-compose.yml", "Dockerfile", ".dockerignore"):
        assert rel_file in paths_changed.RELEVANT_FILES, rel_file


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
    _git(path, "add", "README.md", "elixir/sentinel/a.ex")
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


def test_rename_out_of_a_suite_directory_is_relevant(repo: Path):
    """With rename detection, git would report only the destination path and
    the suite's lost module would be invisible; --no-renames shows the delete."""
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "docs").mkdir()
    _git(repo, "mv", "elixir/sentinel/a.ex", "docs/a.ex")
    _git(repo, "commit", "-q", "-m", "move module out of the suite")
    head = _git(repo, "rev-parse", "HEAD")
    paths = paths_changed.changed_paths(base, head, cwd=str(repo))
    assert paths is not None
    assert "elixir/sentinel/a.ex" in paths
    assert "docs/a.ex" in paths
    relevant, _ = paths_changed.decide("pull_request", base, head, cwd=str(repo))
    assert relevant is True


def test_unusual_filename_under_a_suite_is_relevant(repo: Path):
    """Without -z, git C-quotes a non-ASCII path ("elixir/sentinel/na\\303\\257ve.ex")
    and the leading quote would defeat the prefix match."""
    _git(repo, "config", "core.quotepath", "true")
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "elixir/sentinel/lib/naïve.ex", "x\n", "unusual name")
    paths = paths_changed.changed_paths(base, head, cwd=str(repo))
    assert paths == ["elixir/sentinel/lib/naïve.ex"]
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

def _needs(relevant: str, detector: str = "success", **results: str) -> dict:
    needs = {DETECTOR: {"result": detector, "outputs": {"relevant": relevant}}}
    default = "success" if (relevant == "true" or detector != "success") else "skipped"
    for name in SUITES:
        needs[name] = {"result": results.get(name, default)}
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
    assert any("skipped although the suites had to run" in line for line in lines)


def test_gate_passes_when_detector_failed_but_every_suite_ran_and_passed():
    """The workflow runs every suite when the detector did not succeed; a full
    green run is the strongest evidence there is, so a flaky detector costs a
    run, not a red required check."""
    ok, lines = gate.evaluate(_needs("", detector="failure"))
    assert ok, lines
    assert lines[0].startswith(f"note {DETECTOR}")


def test_gate_fails_when_detector_failed_and_a_suite_was_skipped():
    """Nothing can vouch for a skip when the detector did not succeed."""
    ok, lines = gate.evaluate(_needs("", detector="failure", sentinel="skipped"))
    assert not ok
    assert any("FAIL sentinel: skipped" in line for line in lines)


def test_gate_ignores_a_stale_relevant_false_from_a_failed_detector():
    """`relevant=false` is only trusted when the detector job succeeded."""
    ok, _ = gate.evaluate(_needs("false", detector="cancelled", lease_plane="skipped"))
    assert not ok


def test_gate_fails_when_detector_is_missing():
    ok, lines = gate.evaluate({name: {"result": "success"} for name in SUITES})
    assert not ok
    assert any("miswired" in line for line in lines)


def test_gate_fails_when_no_suites_are_wired():
    ok, lines = gate.evaluate({DETECTOR: {"result": "success", "outputs": {"relevant": "false"}}})
    assert not ok
    assert any("miswired" in line for line in lines)


def test_gate_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setenv("NEEDS_JSON", json.dumps(_needs("false")))
    assert gate.main() == 0
    assert "elixir-gate: PASS" in capsys.readouterr().out

    monkeypatch.setenv("NEEDS_JSON", json.dumps(_needs("true", sentinel="failure")))
    assert gate.main() == 1

    monkeypatch.setenv("NEEDS_JSON", "not json")
    assert gate.main() == 1


# --- workflow wiring --------------------------------------------------------

APP_JOB_IF = (
    "${{ !cancelled() && ("
    f"needs.{DETECTOR}.result != 'success' || needs.{DETECTOR}.outputs.relevant == 'true') }}}}"
)


def test_workflow_wires_detector_gate_and_every_suite():
    data = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML parses the bare `on:` key as boolean True.
    triggers = data.get("on", data.get(True))
    for event in ("pull_request", "push"):
        assert "paths" not in (triggers[event] or {}), (
            f"{event} must not be path-filtered: a filtered workflow reports no "
            "status and a required context then waits forever (#2040)"
        )

    concurrency = data.get("concurrency") or {}
    assert concurrency.get("cancel-in-progress") is True
    assert "github.ref" in str(concurrency.get("group"))

    jobs = data["jobs"]
    assert DETECTOR in jobs and "elixir-gate" in jobs
    assert set(jobs) == {DETECTOR, "elixir-gate", *SUITES}, sorted(jobs)
    assert jobs[DETECTOR]["outputs"]["relevant"]
    for name in SUITES:
        assert jobs[name]["needs"] == DETECTOR, name
        assert jobs[name]["if"] == APP_JOB_IF, (name, jobs[name]["if"])
        # The gate reads one collapsed result per job; a selectively re-run
        # matrix leg can report success for the whole job while another leg's
        # earlier failure stands, so every suite is an explicit job.
        assert "strategy" not in jobs[name], f"{name} must not be a matrix job"

    gate_job = jobs["elixir-gate"]
    assert gate_job["name"] == "elixir-gate"
    assert set(gate_job["needs"]) == {DETECTOR, *SUITES}
    assert str(gate_job["if"]).strip("${} ") == "always()"
    run_steps = [step for step in gate_job["steps"] if "run" in step]
    assert any("scripts/ci/elixir_gate.py" in step["run"] for step in run_steps)
    assert any(
        "NEEDS_JSON" in (step.get("env") or {}) for step in run_steps
    ), "the gate step must receive toJSON(needs) as NEEDS_JSON"
