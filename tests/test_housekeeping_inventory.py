"""Tests for scripts/dev/housekeeping_inventory.py."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def inventory_module():
    project_root = Path(__file__).resolve().parent.parent
    module_path = project_root / "scripts" / "dev" / "housekeeping_inventory.py"
    spec = importlib.util.spec_from_file_location("housekeeping_inventory", module_path)
    assert spec and spec.loader, f"could not load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["housekeeping_inventory"] = module
    spec.loader.exec_module(module)
    return module


def test_parse_worktree_porcelain_marks_detached_without_branch(inventory_module):
    stdout = """worktree /repo
HEAD abcdef1234567890
branch refs/heads/master

worktree /repo-wt
HEAD 9999999999999999
detached

"""

    worktrees = inventory_module.parse_worktree_porcelain(stdout)

    assert len(worktrees) == 2
    assert worktrees[0].path == "/repo"
    assert worktrees[0].branch == "master"
    assert worktrees[0].detached is False
    assert worktrees[1].path == "/repo-wt"
    assert worktrees[1].branch == ""
    assert worktrees[1].detached is True
    assert worktrees[1].head == "999999999999"


def test_parse_status_short_returns_paths(inventory_module):
    stdout = " M README.md\n?? tests/new_test.py\nA  scripts/dev/new.py\n"

    paths = inventory_module.parse_status_short(stdout)

    assert paths == ["README.md", "tests/new_test.py", "scripts/dev/new.py"]


def test_parse_branch_rows_handles_subject_tabs(inventory_module):
    stdout = (
        "codex/foo\torigin/codex/foo\t[gone]\tabc123\t"
        "2026-06-02 10:00:00 -0600\tfix: keep\ttabbed subject\n"
    )

    rows = inventory_module.parse_branch_rows(stdout)

    assert len(rows) == 1
    assert rows[0].name == "codex/foo"
    assert rows[0].upstream == "origin/codex/foo"
    assert rows[0].track == "[gone]"
    assert rows[0].subject == "fix: keep\ttabbed subject"


def test_protected_branch_keeps_primary_and_archive_refs(inventory_module):
    assert inventory_module.protected_branch("master") is True
    assert inventory_module.protected_branch("main") is True
    assert inventory_module.protected_branch("archive/stash-20260420") is True
    assert inventory_module.protected_branch("backup/foo") is True
    assert inventory_module.protected_branch("codex/foo") is False


def test_parse_stash_rows_computes_age_days(inventory_module):
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    stdout = (
        "stash@{0}\tabcdef\t2026-05-20T12:00:00+00:00\tOn master: old\n"
        "stash@{1}\t999999\t2026-06-01T12:00:00+00:00\tOn master: new\n"
    )

    stashes = inventory_module.parse_stash_rows(stdout, now)

    assert [stash.age_days for stash in stashes] == [13, 1]
    assert stashes[0].subject == "On master: old"


def test_collect_stashes_uses_git_pretty_tab_escape(inventory_module, monkeypatch):
    seen_args: list[str] = []

    def fake_run_cmd(args, cwd):
        nonlocal seen_args
        seen_args = args
        return inventory_module.CommandResult(
            args=args,
            returncode=0,
            stdout=(
                "stash@{0}\tabcdef\t2026-05-20T12:00:00+00:00\t"
                "On master: old\n"
            ),
        )

    monkeypatch.setattr(inventory_module, "run_cmd", fake_run_cmd)

    stashes = inventory_module.collect_stashes(
        Path("/repo"),
        datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert any("%gd%x09%H%x09%cd%x09%gs" in arg for arg in seen_args)
    assert stashes[0].hash == "abcdef"
    assert stashes[0].age_days == 13


def test_github_repo_from_remote_accepts_https_and_ssh(inventory_module):
    assert (
        inventory_module.github_repo_from_remote(
            "https://github.com/CIRWEL/unitares.git"
        )
        == "CIRWEL/unitares"
    )
    assert (
        inventory_module.github_repo_from_remote("git@github.com:CIRWEL/unitares.git")
        == "CIRWEL/unitares"
    )
    assert inventory_module.github_repo_from_remote("file:///tmp/origin.git") == ""


def test_text_report_includes_core_counts(inventory_module, capsys):
    inventory = inventory_module.Inventory(
        repo_root="/repo",
        generated_at="2026-06-02T12:00:00+00:00",
        worktrees=[
            inventory_module.WorktreeInfo(
                path="/repo",
                head="abcdef123456",
                branch="master",
                dirty_paths=["README.md"],
            ),
            inventory_module.WorktreeInfo(
                path="/repo-wt",
                head="999999999999",
                detached=True,
            ),
        ],
        gone_upstream_branches=[],
        merged_branch_candidates=[],
        unmerged_branches=[],
        stashes=[
            inventory_module.StashInfo(
                ref="stash@{0}",
                hash="abc",
                date="2026-05-01T00:00:00+00:00",
                subject="On master: old",
                age_days=32,
            )
        ],
        old_stashes=[
            inventory_module.StashInfo(
                ref="stash@{0}",
                hash="abc",
                date="2026-05-01T00:00:00+00:00",
                subject="On master: old",
                age_days=32,
            )
        ],
        github_prs=inventory_module.ProbeResult(status="skipped", message="test"),
        watcher=inventory_module.ProbeResult(status="ok", stdout="P001 unresolved\n"),
    )

    inventory_module.print_text_report(inventory, limit=10)

    out = capsys.readouterr().out
    assert "worktrees: 2 total, 1 dirty, 1 detached" in out
    assert "README.md" in out
    assert "stashes: 1 total, 1 old" in out
    assert "watcher unresolved output: 1 non-empty line(s)" in out
