"""Focused tests for scripts/dev/test-cache.sh.

The script is intentionally exercised in a throwaway git repo with a fake
pytest runner. That keeps the tests fast while still covering the shell-level
cache key behavior agents rely on before commits.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "dev" / "test-cache.sh"


@pytest.fixture
def cache_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()

    script_target = repo / "scripts" / "dev" / "test-cache.sh"
    script_target.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script_target)
    script_target.chmod(0o755)

    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "db" / "postgres" / "migrations").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "from src.app import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    (repo / "db" / "postgres" / "migrations" / "001_seed.sql").write_text(
        "-- seed v1\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("readme\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    fake_python = repo / "fake-python"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-" ]]; then
    exec python3 "$@"
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "pytest" ]]; then
    count_file="${FAKE_PYTEST_COUNT:?}"
    printf "%s\\n" "$*" > "${FAKE_PYTEST_ARGS:?}"
    count=0
    if [[ -f "$count_file" ]]; then
        count="$(cat "$count_file")"
    fi
    count=$((count + 1))
    printf "%s\\n" "$count" > "$count_file"
    echo "1 passed in 0.01s"
    exit 0
fi

echo "unexpected fake python invocation: $*" >&2
exit 99
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["UNITARES_PYTHON"] = str(fake_python)
    env["UNITARES_TEST_CACHE_LOCK_DIR"] = str(repo / "test-cache.lock")
    env["FAKE_PYTEST_COUNT"] = str(repo / "pytest-count.txt")
    env["FAKE_PYTEST_ARGS"] = str(repo / "pytest-args.txt")
    return repo, env


def _run_cache(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/dev/test-cache.sh", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _pytest_count(repo: Path) -> int:
    path = repo / "pytest-count.txt"
    if not path.exists():
        return 0
    return int(path.read_text(encoding="utf-8").strip())


def _pytest_args(repo: Path) -> str:
    return (repo / "pytest-args.txt").read_text(encoding="utf-8")


def test_tracked_sql_change_invalidates_worktree_cache(cache_repo):
    repo, env = cache_repo

    first = _run_cache(repo, env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "[test-cache] MISS" in first.stdout

    second = _run_cache(repo, env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "[test-cache] HIT" in second.stdout
    assert _pytest_count(repo) == 1

    (repo / "db" / "postgres" / "migrations" / "001_seed.sql").write_text(
        "-- seed v2\n",
        encoding="utf-8",
    )

    third = _run_cache(repo, env)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "[test-cache] MISS" in third.stdout
    assert _pytest_count(repo) == 2


def test_untracked_test_file_invalidates_worktree_cache(cache_repo):
    repo, env = cache_repo

    first = _run_cache(repo, env)
    assert first.returncode == 0, first.stdout + first.stderr

    second = _run_cache(repo, env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "[test-cache] HIT" in second.stdout
    assert _pytest_count(repo) == 1

    (repo / "tests" / "test_new_behavior.py").write_text(
        "def test_new_behavior():\n    assert True\n",
        encoding="utf-8",
    )

    third = _run_cache(repo, env)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "[test-cache] MISS" in third.stdout
    assert _pytest_count(repo) == 2


def test_quick_mode_omits_coverage_and_uses_separate_cache(cache_repo):
    repo, env = cache_repo

    quick_first = _run_cache(repo, env, "--quick")
    assert quick_first.returncode == 0, quick_first.stdout + quick_first.stderr
    assert "[test-cache] MISS" in quick_first.stdout
    assert "profile quick" in quick_first.stdout
    assert "--cov=src" not in _pytest_args(repo)
    assert _pytest_count(repo) == 1

    full = _run_cache(repo, env)
    assert full.returncode == 0, full.stdout + full.stderr
    assert "[test-cache] MISS" in full.stdout
    assert "profile coverage" in full.stdout
    assert "--cov=src" in _pytest_args(repo)
    assert _pytest_count(repo) == 2

    quick_second = _run_cache(repo, env, "--quick")
    assert quick_second.returncode == 0, quick_second.stdout + quick_second.stderr
    assert "[test-cache] HIT" in quick_second.stdout
    assert _pytest_count(repo) == 2


def test_staged_mode_refuses_unstaged_tracked_non_python_input(cache_repo):
    repo, env = cache_repo
    (repo / "README.md").write_text("changed but unstaged\n", encoding="utf-8")

    result = _run_cache(repo, env, "--staged")

    assert result.returncode == 4
    assert "unstaged or untracked files would affect pytest" in result.stderr
    assert "README.md" in result.stderr
    assert _pytest_count(repo) == 0
