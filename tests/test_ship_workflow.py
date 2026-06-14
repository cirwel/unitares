"""Tests for scripts/dev/ship.sh delivery routing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIP_SOURCE = PROJECT_ROOT / "scripts" / "dev" / "ship.sh"
SHIP_WATCHER_HELPER_SOURCE = (
    PROJECT_ROOT / "scripts" / "dev" / "_ship_watcher_fingerprints.py"
)


def run(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
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
    shutil.copy2(
        SHIP_WATCHER_HELPER_SOURCE,
        script_path.parent / "_ship_watcher_fingerprints.py",
    )

    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "t@t"], repo)
    run(["git", "config", "user.name", "t"], repo)
    run(["git", "config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("seed\n")
    run(["git", "add", "README.md"], repo)
    run(["git", "commit", "-q", "-m", "seed"], repo)

    origin = tmp_path / "origin.git"
    run(["git", "init", "--bare", "-q", str(origin)], tmp_path)
    run(["git", "remote", "add", "origin", str(origin)], repo)
    run(["git", "push", "-q", "-u", "origin", "main"], repo)
    return repo


def stage_file(repo: Path, relative_path: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{relative_path}\n")
    run(["git", "add", relative_path], repo)


def ship_plan(repo: Path, *options: str) -> dict[str, str]:
    cmd = [
        str(repo / "scripts" / "dev" / "ship.sh"),
        "--plan",
        *options,
        "test: change",
    ]
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


def test_auto_routes_feature_branch_docs_to_draft_pr(ship_repo: Path) -> None:
    # Draft PR for everything: non-runtime work on a named feature branch opens
    # a draft PR on that branch rather than direct-pushing
    # (docs/operations/github-workflow-conventions.md).
    run(["git", "checkout", "-q", "-b", "docs/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo)

    assert plan["kind"] == "other"
    assert plan["branch"] == "docs/workflow-note"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "0"


def test_explicit_direct_opts_out_on_feature_branch(ship_repo: Path) -> None:
    # --direct is the escape hatch from draft-PR-for-everything, for
    # docs/tests-only pushes on a named feature branch.
    run(["git", "checkout", "-q", "-b", "docs/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo, "--direct")

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


def test_stage_all_plan_classifies_dirty_worktree_without_staging(ship_repo: Path) -> None:
    path = ship_repo / "src" / "mcp_server.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("runtime\n")

    plan = ship_plan(ship_repo, "--stage-all")

    assert plan["kind"] == "runtime"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "1"
    assert plan["stage_all"] == "1"

    status = run(["git", "status", "--porcelain=v1"], ship_repo)
    staged = run(["git", "diff", "--cached", "--name-only"], ship_repo)
    assert "?? src/" in status.stdout
    assert staged.stdout == ""


def test_direct_ship_reads_shared_watcher_dir_for_commit_trailer(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    stage_file(ship_repo, "agents/foo.py")

    watcher_dir = tmp_path / "watcher"
    watcher_dir.mkdir()
    (watcher_dir / "findings.jsonl").write_text(
        json.dumps(
            {
                "file": str(ship_repo / "agents" / "foo.py"),
                "fingerprint": "abc123",
                "status": "surfaced",
            }
        )
        + "\n"
    )

    env = {
        **os.environ,
        "UNITARES_WATCHER_DATA_DIR": str(watcher_dir),
    }
    result = run(
        [
            str(ship_repo / "scripts" / "dev" / "ship.sh"),
            "--direct",
            "test: change",
        ],
        ship_repo,
        env=env,
    )
    message = run(["git", "log", "-1", "--format=%B"], ship_repo).stdout

    assert "[ship] appended Watcher-Findings trailer: abc123" in result.stdout
    assert "Watcher-Findings: abc123" in message
