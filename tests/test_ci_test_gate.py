"""Regression tests for CI/local pytest gate parity."""

from __future__ import annotations

import re
from pathlib import Path


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
