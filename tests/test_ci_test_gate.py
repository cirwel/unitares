"""Regression tests for CI/local pytest gate parity."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_COVERAGE_FLOOR = 75


def _coverage_floor(text: str) -> int:
    match = re.search(r"--(?:cov-)?fail-under=(\d+)", text)
    assert match, "coverage gate must declare --cov-fail-under"
    return int(match.group(1))


def _ci_shard(path: Path) -> str | None:
    relative = path.relative_to(PROJECT_ROOT).as_posix()

    if relative.startswith("agents/") or relative.count("/") > 1:
        return "agents-and-nested"
    if relative == "tests/smoke_test.py":
        return "agents-and-nested"
    if not relative.startswith("tests/test_"):
        return None

    first_letter = path.name.removeprefix("test_")[:1].lower()
    if "a" <= first_letter <= "b":
        return "tests-a-b"
    if "c" <= first_letter <= "d":
        return "tests-c-d"
    if "e" <= first_letter <= "h":
        return "tests-e-h"
    if "i" <= first_letter <= "l":
        return "tests-i-l"
    if "m" <= first_letter <= "p":
        return "tests-m-p"
    if "q" <= first_letter <= "t":
        return "tests-q-t"
    if "u" <= first_letter <= "z":
        return "tests-u-z"
    return None


def test_github_full_test_jobs_cover_every_test_file() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )

    shard_job = workflow.split("\n  test_shard:", 1)[1].split("\n  test:", 1)[0]
    assert "needs: smoke" not in shard_job
    assert "timeout-minutes: 20" in shard_job
    assert (
        "shard: [tests-a-b, tests-c-d, tests-e-h, tests-i-l, tests-m-p, "
        "tests-q-t, tests-u-z, agents-and-nested]" in shard_job
    )
    assert "targets=(tests/test_[a-b]*.py)" in shard_job
    assert "targets=(tests/test_[c-d]*.py)" in shard_job
    assert "targets=(tests/test_[e-h]*.py)" in shard_job
    assert "targets=(tests/test_[i-l]*.py)" in shard_job
    assert "targets=(tests/test_[m-p]*.py)" in shard_job
    assert "targets=(tests/test_[q-t]*.py)" in shard_job
    assert "targets=(tests/test_[u-z]*.py)" in shard_job
    assert "targets=(agents/ tests/*/ tests/smoke_test.py)" in shard_job
    assert '--health-cmd "pg_isready -U postgres"' in shard_job
    assert 'python -m pytest "${targets[@]}" -q -ra' in shard_job
    assert "--cov=src --cov=agents/sdk/src/unitares_sdk --cov=agents" in shard_job

    test_files = [
        path
        for root in (PROJECT_ROOT / "tests", PROJECT_ROOT / "agents")
        for path in root.rglob("*.py")
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    ]
    unassigned = [
        path.relative_to(PROJECT_ROOT) for path in test_files if not _ci_shard(path)
    ]
    assert not unassigned, f"pytest files missing from CI shards: {unassigned}"

    assert "name: test (3.12)" in workflow
    assert "needs: test_shard" in workflow
    assert "needs.test_shard.result != 'success'" in workflow
    assert "python -m coverage combine coverage-data" in workflow
    assert "cancel-in-progress: true" in workflow
    assert _coverage_floor(workflow) >= MIN_COVERAGE_FLOOR


def test_local_test_entrypoints_keep_realistic_coverage_floor() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    test_cache = (PROJECT_ROOT / "scripts" / "dev" / "test-cache.sh").read_text(
        encoding="utf-8"
    )

    assert _coverage_floor(makefile) >= MIN_COVERAGE_FLOOR
    assert _coverage_floor(test_cache) >= MIN_COVERAGE_FLOOR


# --- Interpreter contract (2026-09-02) -------------------------------------
#
# Until 2026-09-02 the pytest suite ran on Python 3.12 only while production
# (the LaunchAgent, the Docker base image, the launchd automations, and the
# local pre-push hook's `python3`) ran 3.14. These tests pin the contract
# between pyproject's requires-python floor, the `# production-python:` marker
# in constraints.txt, the Dockerfile, the scripts/ops templates, and the
# test_shard matrix, and they sabotage copies of each input to prove the
# checker that enforces it in the smoke job fails closed rather than passing
# while proving nothing.

CHECKER_PATH = PROJECT_ROOT / "scripts" / "diagnostics" / "check_ci_python_matrix_sync.py"
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "tests.yml"
MATRIX_LIST_RE = re.compile(r"python-version:\s*\[([^\]]+)\]")


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_ci_python_matrix_sync", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shard_job(workflow: str) -> str:
    return workflow.split("\n  test_shard:", 1)[1].split("\n  test:", 1)[0]


def _render(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def _with_replacement(tmp_path: Path, source: Path, old: str, new: str) -> Path:
    text = source.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{source.name}: expected one occurrence of {old!r}"
    copy = tmp_path / source.name
    copy.write_text(text.replace(old, new), encoding="utf-8")
    return copy


def test_interpreter_contract_holds_on_the_real_tree(checker):
    floor = checker._read_requires_python()
    production = checker._read_production_python()
    matrix = checker._read_ci_matrix_versions()

    assert production >= floor
    assert floor in matrix
    assert production in matrix
    assert checker._read_dockerfile_python() == production
    assert all(v == production for v in checker._read_template_pythons().values())
    assert checker.main() == 0


def test_every_matrix_version_is_a_declared_classifier(checker):
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for version in checker._read_ci_matrix_versions():
        assert f'"Programming Language :: Python :: {_render(version)}"' in pyproject


def test_shard_legs_stay_distinguishable_and_the_floor_keeps_its_leg(checker):
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    shard_job = _shard_job(workflow_text)
    floor = _render(checker._read_requires_python())

    # Two interpreters share one shard list, so every per-leg artifact carries
    # the interpreter: upload-artifact refuses duplicate names.
    assert "name: test shard (${{ matrix.python-version }}, ${{ matrix.shard }})" in shard_job
    assert "COVERAGE_FILE: .coverage.${{ matrix.python-version }}.${{ matrix.shard }}" in shard_job
    assert "name: coverage-data-${{ matrix.python-version }}-${{ matrix.shard }}" in shard_job
    assert "path: .coverage.${{ matrix.python-version }}.${{ matrix.shard }}" in shard_job

    # The coverage floor is measured on the floor interpreter only, so adding
    # interpreters cannot change what the floor means.
    assert workflow["jobs"]["test_shard"]["env"]["COVERAGE_LEG"] == floor
    assert workflow["jobs"]["test"]["env"]["COVERAGE_LEG"] == floor
    assert 'if [ "${{ matrix.python-version }}" = "$COVERAGE_LEG" ]' in shard_job
    assert "if: ${{ always() && matrix.python-version == env.COVERAGE_LEG }}" in shard_job
    assert "pattern: coverage-data-${{ env.COVERAGE_LEG }}-*" in workflow_text

    # The aggregator checks every leg of the latest attempt by API, and the
    # leg count it expects is the matrix product, not a number that can drift.
    matrix = workflow["jobs"]["test_shard"]["strategy"]["matrix"]
    expected_legs = len(matrix["python-version"]) * len(matrix["shard"])
    test_job = workflow["jobs"]["test"]
    assert test_job["env"]["EXPECTED_SHARD_LEGS"] == str(expected_legs)
    assert test_job["permissions"]["actions"] == "read"
    api_steps = [
        step for step in test_job["steps"]
        if "filter=latest" in str(step.get("run", "")) and 'startswith("test shard (")' in str(step.get("run", ""))
    ]
    assert len(api_steps) == 1
    assert "$EXPECTED_SHARD_LEGS" in api_steps[0]["run"]


def test_checker_accepts_an_annotated_marker(checker, tmp_path, monkeypatch):
    production = _render(checker._read_production_python())
    annotated = _with_replacement(
        tmp_path, checker.CONSTRAINTS_PATH,
        f"# production-python: {production}\n",
        f"# production-python: {production}  # bumped with the 2026-09-02 deploy\n",
    )
    monkeypatch.setattr(checker, "CONSTRAINTS_PATH", annotated)
    assert checker._read_production_python() == checker._parse_version_tuple(production)
    assert checker.main() == 0


@pytest.mark.parametrize("sabotage", [
    "marker_missing",
    "matrix_omits_production",
    "setup_python_ignores_matrix",
    "decoy_list_in_another_job",
    "dockerfile_disagrees",
    "template_disagrees",
])
def test_checker_fails_closed_under_sabotage(checker, tmp_path, monkeypatch, capsys, sabotage):
    production = checker._read_production_python()
    tag = _render(production)
    floor = _render(checker._read_requires_python())
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def _matrix_without_production(match: re.Match[str]) -> str:
        entries = [entry.strip() for entry in match.group(1).split(",")]
        kept = [entry for entry in entries if entry.strip("'\"") != tag]
        return "python-version: [" + ", ".join(kept) + "]"

    if sabotage == "marker_missing":
        path = tmp_path / "constraints.txt"
        path.write_text(
            re.sub(r"^#\s*production-python:.*$", "", checker.CONSTRAINTS_PATH.read_text(encoding="utf-8"), flags=re.M),
            encoding="utf-8",
        )
        monkeypatch.setattr(checker, "CONSTRAINTS_PATH", path)
    elif sabotage == "matrix_omits_production":
        text = MATRIX_LIST_RE.sub(_matrix_without_production, workflow_text, count=1)
        assert text != workflow_text
        path = tmp_path / "tests.yml"; path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(checker, "TEST_WORKFLOW_PATH", path)
    elif sabotage == "setup_python_ignores_matrix":
        # The matrix still lists both interpreters, but every leg would run 3.12.
        path = _with_replacement(
            tmp_path, WORKFLOW_PATH,
            "        python-version: ${{ matrix.python-version }}\n",
            f"        python-version: '{floor}'\n",
        )
        monkeypatch.setattr(checker, "TEST_WORKFLOW_PATH", path)
    elif sabotage == "decoy_list_in_another_job":
        # A bracketed list in the smoke job must not stand in for the shard matrix.
        text = MATRIX_LIST_RE.sub(_matrix_without_production, workflow_text, count=1)
        text = text.replace(
            "    - name: Set up Python 3.12\n",
            f"    # python-version: ['{floor}', '{tag}']\n    - name: Set up Python 3.12\n",
            1,
        )
        path = tmp_path / "tests.yml"; path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(checker, "TEST_WORKFLOW_PATH", path)
    elif sabotage == "dockerfile_disagrees":
        path = tmp_path / "Dockerfile"
        path.write_text(f"FROM python:{floor}-slim@sha256:0000\n", encoding="utf-8")
        monkeypatch.setattr(checker, "DOCKERFILE_PATH", path)
    elif sabotage == "template_disagrees":
        templates = tmp_path / "ops"; templates.mkdir()
        (templates / "com.example.plist.template").write_text(
            f"<string>/Library/Frameworks/Python.framework/Versions/{floor}/bin/python3</string>\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(checker, "OPS_TEMPLATES_DIR", templates)

    assert checker.main() == 1
    assert "❌" in capsys.readouterr().out
