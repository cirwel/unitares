"""Tests for scripts/dev/ship.sh delivery routing."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIP_SOURCE = PROJECT_ROOT / "scripts" / "dev" / "ship.sh"


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


@pytest.fixture
def ship_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    script_path = repo / "scripts" / "dev" / "ship.sh"
    script_path.parent.mkdir(parents=True)
    shutil.copy2(SHIP_SOURCE, script_path)
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)

    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "t@t"], repo)
    run(["git", "config", "user.name", "t"], repo)
    run(["git", "config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("seed\n")
    run(["git", "add", "README.md"], repo)
    run(["git", "commit", "-q", "-m", "seed"], repo)
    return repo


def stage_file(repo: Path, relative_path: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{relative_path}\n")
    run(["git", "add", relative_path], repo)


def ship_plan(repo: Path, *options: str) -> dict[str, str]:
    cmd = [str(repo / "scripts" / "dev" / "ship.sh"), "--plan", *options, "test: change"]
    result = run(cmd, repo)
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def test_auto_routes_runtime_changes_to_draft_pr_branch(ship_repo: Path) -> None:
    stage_file(ship_repo, "src/mcp_server.py")

    plan = ship_plan(ship_repo)

    assert plan["kind"] == "runtime"
    assert plan["branch"] == "main"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "1"


def test_auto_routes_detached_non_runtime_changes_to_draft_pr(ship_repo: Path) -> None:
    run(["git", "checkout", "--detach", "-q"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo)

    assert plan["kind"] == "other"
    assert plan["branch"] == "(detached)"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "1"


def test_auto_routes_feature_branch_docs_to_direct_push(ship_repo: Path) -> None:
    run(["git", "checkout", "-q", "-b", "docs/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo)

    assert plan["kind"] == "other"
    assert plan["branch"] == "docs/workflow-note"
    assert plan["delivery"] == "direct"
    assert plan["force_auto_branch"] == "0"


def test_explicit_draft_pr_uses_current_feature_branch(ship_repo: Path) -> None:
    run(["git", "checkout", "-q", "-b", "codex/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo, "--draft-pr")

    assert plan["kind"] == "other"
    assert plan["branch"] == "codex/workflow-note"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "0"
