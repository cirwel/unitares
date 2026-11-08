"""The scheduled sweep scans real edits, not files git merely wrote.

Measured 2026-11-05: one sweep saw 1,256 "changed" files, nearly all from
fresh worktree checkouts, because mtime moves whenever git writes a file.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest

from agents.watcher import sweep_filter as SF

OLD = "2026-01-01T00:00:00+00:00"


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    if date:
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo with space"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "clean.py").write_text("x = 1\n")
    (root / "edited.py").write_text("y = 1\n")
    (root / ".gitignore").write_text("build/\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed", date=OLD)
    return root


def _kept(paths, since=None):
    return set(SF.filter_candidates([str(p) for p in paths], since))


def test_clean_checkout_file_is_skipped(repo):
    assert _kept([repo / "clean.py"], since=time.time() - 3600) == set()


def test_modified_and_untracked_files_are_kept(repo):
    (repo / "edited.py").write_text("y = 2\n")
    (repo / "new.py").write_text("z = 1\n")
    kept = _kept([repo / "clean.py", repo / "edited.py", repo / "new.py"])
    assert kept == {str(repo / "edited.py"), str(repo / "new.py")}


def test_staged_edits_are_kept(repo):
    """Review finding on #3205 (codex, P1): `ls-files -m` omits an edit once it
    is staged, and the watermark then moves past it for good."""
    (repo / "edited.py").write_text("y = 9\n")
    (repo / "added.py").write_text("w = 1\n")
    _git(repo, "add", "edited.py", "added.py")
    kept = _kept([repo / "clean.py", repo / "edited.py", repo / "added.py"],
                 since=time.time() - 60)
    assert kept == {str(repo / "edited.py"), str(repo / "added.py")}


def test_unborn_branch_filters_without_failing_open(tmp_path):
    """Review finding on #3205 (antigravity, google): with a watermark, `git
    log HEAD` failed on a branch with no commits, and the filter failed open
    for the whole tree. The ignored file proves filtering actually ran."""
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("gen/\n")
    (root / "gen").mkdir()
    (root / "gen" / "out.py").write_text("o = 1\n")
    (root / "first.py").write_text("f = 1\n")
    _git(root, "add", "first.py")
    kept = _kept([root / "first.py", root / "gen" / "out.py"], since=time.time() - 60)
    assert kept == {str(root / "first.py")}


def test_conflict_resolution_committed_in_a_merge_is_kept(repo):
    """Review finding on #3205 (codex, P1): `git log --name-only` lists no
    files for a merge commit, so a resolution committed before the sweep was
    missed. Branch commits predate the watermark; only the merge is new."""
    _git(repo, "checkout", "-q", "-b", "topic")
    (repo / "clean.py").write_text("x = 'topic'\n")
    _git(repo, "commit", "-q", "-am", "topic", date=OLD)
    _git(repo, "checkout", "-q", "main")
    (repo / "clean.py").write_text("x = 'main'\n")
    _git(repo, "commit", "-q", "-am", "main", date=OLD)
    watermark = time.time() - 60
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "topic"], capture_output=True)
    (repo / "clean.py").write_text("x = 'resolved'\n")
    _git(repo, "commit", "-q", "-am", "merge topic")  # merge committed now
    kept = _kept([repo / "clean.py", repo / "edited.py"], since=watermark)
    assert kept == {str(repo / "clean.py")}


def test_relative_candidates_resolve_against_the_callers_directory(repo, monkeypatch):
    """Review finding on #3205 (codex, P2): a relative scan root is relative to
    the caller, and the kept path comes back absolute for later steps."""
    (repo / "edited.py").write_text("y = 7\n")
    monkeypatch.chdir(repo.parent)
    kept = SF.filter_candidates([f"{repo.name}/edited.py", f"{repo.name}/clean.py"])
    assert kept == [str(repo / "edited.py")]


def test_ignored_file_is_skipped(repo):
    (repo / "build").mkdir()
    (repo / "build" / "gen.py").write_text("g = 1\n")
    assert _kept([repo / "build" / "gen.py"]) == set()


def test_edit_committed_since_the_watermark_is_kept(repo):
    watermark = time.time() - 60
    (repo / "edited.py").write_text("y = 3\n")
    _git(repo, "commit", "-q", "-am", "edit")  # committed now, after the watermark
    kept = _kept([repo / "clean.py", repo / "edited.py"], since=watermark)
    assert kept == {str(repo / "edited.py")}


def test_commit_before_the_watermark_does_not_count(repo):
    assert _kept([repo / "edited.py"], since=time.time() - 60) == set()


def test_fresh_worktree_checkout_is_skipped(repo, tmp_path):
    """The measured case: a new worktree writes every file with a new mtime."""
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "topic", str(wt))
    assert _kept([wt / "clean.py", wt / "edited.py"], since=time.time() - 3600) == set()
    (wt / "edited.py").write_text("y = 4\n")
    assert _kept([wt / "clean.py", wt / "edited.py"]) == {str(wt / "edited.py")}


def test_file_outside_any_work_tree_is_kept(tmp_path):
    loose = tmp_path / "loose" / "script.py"
    loose.parent.mkdir()
    loose.write_text("print(1)\n")
    assert _kept([loose]) == {str(loose)}


def test_git_failure_keeps_the_candidate(repo, monkeypatch):
    real = SF._git

    def failing(args, cwd):
        return None if args[0] == "ls-files" else real(args, cwd)

    monkeypatch.setattr(SF, "_git", failing)
    assert _kept([repo / "clean.py"]) == {str(repo / "clean.py")}


def test_git_reads_never_take_optional_locks(repo, monkeypatch):
    """A status-style read must not rewrite another session's index."""
    seen = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        seen.append(kwargs.get("env", {}).get("GIT_OPTIONAL_LOCKS"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(SF.subprocess, "run", spy)
    SF.filter_candidates([str(repo / "clean.py")], time.time())
    assert seen and set(seen) == {"0"}


def test_cli_reads_stdin_and_prints_kept(repo, monkeypatch, capsys):
    (repo / "edited.py").write_text("y = 5\n")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        f"{repo / 'clean.py'}\n{repo / 'edited.py'}\n\n"))
    assert SF.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [str(repo / "edited.py")]


def test_cli_reads_the_watermark_from_a_file(repo, tmp_path, monkeypatch, capsys):
    """The engine passes its state file; the filter reads its mtime itself,
    because `stat -f %m` is BSD-only."""
    state = tmp_path / "sweep.state"
    state.touch()
    old = time.time() - 60
    os.utime(state, (old, old))
    (repo / "edited.py").write_text("y = 8\n")
    _git(repo, "commit", "-q", "-am", "edit")  # after the watermark
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        f"{repo / 'clean.py'}\n{repo / 'edited.py'}\n"))
    assert SF.main(["--since-file", str(state)]) == 0
    assert capsys.readouterr().out.splitlines() == [str(repo / "edited.py")]


def test_cli_missing_watermark_file_is_ignored(repo, tmp_path, monkeypatch, capsys):
    (repo / "edited.py").write_text("y = 9\n")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(f"{repo / 'edited.py'}\n"))
    assert SF.main(["--since-file", str(tmp_path / "absent")]) == 0
    assert capsys.readouterr().out.splitlines() == [str(repo / "edited.py")]
