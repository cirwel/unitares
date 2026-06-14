"""Regression tests for CI/local pytest gate parity."""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_COVERAGE_FLOOR = 75


def _coverage_floor(text: str) -> int:
    match = re.search(r"--cov-fail-under=(\d+)", text)
    assert match, "coverage gate must declare --cov-fail-under"
    return int(match.group(1))


def test_github_full_test_job_includes_agent_tests() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )

    assert "python -m pytest tests/ agents/ -v" in workflow
    assert _coverage_floor(workflow) >= MIN_COVERAGE_FLOOR


def test_local_test_entrypoints_keep_realistic_coverage_floor() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    test_cache = (PROJECT_ROOT / "scripts" / "dev" / "test-cache.sh").read_text(
        encoding="utf-8"
    )

    assert _coverage_floor(makefile) >= MIN_COVERAGE_FLOOR
    assert _coverage_floor(test_cache) >= MIN_COVERAGE_FLOOR
