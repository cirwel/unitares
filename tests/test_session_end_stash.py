"""Tests for the session-end auto-stash helper.

Problem this module solves: Claude/Codex sessions regularly leave
uncommitted work in the main worktree even when the edits logically
belong to a different branch/worktree. A later session sees the stale
edits, intermingles them with new work, or silently loses track when
branches switch. Over a week this mixes unrelated concerns across
branches and corrupts PR boundaries.

Solution: a ``SessionEnd`` hook that stashes any uncommitted work with
a branch-labeled message so intent survives the session boundary. The
user can ``git stash list`` to see what was captured and ``pop`` on
resume, instead of editing on top of ghost state.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def stash_module():
    project_root = Path(__file__).resolve().parent.parent
    module_path = project_root / "scripts" / "dev" / "session_end_stash.py"
    spec = importlib.util.spec_from_file_location("session_end_stash", module_path)
    assert spec and spec.loader, f"could not load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["session_end_stash"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def git_repo(tmp_path):
    """A minimal git repo with one committed file so stash/status work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    original_cwd = os.getcwd()
    try:
        os.chdir(repo)
        subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "config", "user.name", "t"], check=True)
        # Hermetic: host config may force commit signing (e.g. remote containers)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], check=True)
        (repo / "seed.py").write_text("seed\n")
        subprocess.run(["git", "add", "seed.py"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], check=True)
        yield repo
    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# build_stash_message — the label is the whole point of this feature
# ---------------------------------------------------------------------------


def test_build_stash_message_names_branch_and_file_count(stash_module):
    """The message must identify (a) the branch the work was on so the
    user knows where to pop it back, (b) the file count so idle stashes
    that only hold a typo are distinguishable from big abandoned WIPs,
    and (c) a timestamp so multiple auto-stashes on the same branch
    don't look identical in ``git stash list``."""
    msg = stash_module.build_stash_message(
        branch="feat/orphan-sweep",
        file_count=7,
        timestamp="2026-04-19T09:00:00Z",
    )
    assert "feat/orphan-sweep" in msg
    assert "7" in msg
    assert "2026-04-19T09:00:00Z" in msg
    assert "session-end" in msg.lower() or "auto" in msg.lower()


# ---------------------------------------------------------------------------
# detect_dirty_state — must see modified, staged, AND untracked
# ---------------------------------------------------------------------------


def test_detect_dirty_state_clean_tree_is_not_dirty(stash_module, git_repo):
    """Clean tree → no dirt. Caller must not stash, and log a no-op."""
    state = stash_module.detect_dirty_state(str(git_repo))
    assert state["is_dirty"] is False
    assert state["file_count"] == 0


def test_detect_dirty_state_sees_modified_file(stash_module, git_repo):
    """Modified tracked file counts. This is the most common case —
    an edit left uncommitted."""
    (git_repo / "seed.py").write_text("seed changed\n")
    state = stash_module.detect_dirty_state(str(git_repo))
    assert state["is_dirty"] is True
    assert state["file_count"] == 1


def test_detect_dirty_state_sees_untracked_file(stash_module, git_repo):
    """Untracked files must count — the 2026-04-19 dialectic-respond.md
    case in the unitares repo was an untracked new file that sat in the
    main worktree for hours before anyone noticed it."""
    (git_repo / "new.py").write_text("fresh\n")
    state = stash_module.detect_dirty_state(str(git_repo))
    assert state["is_dirty"] is True
    assert state["file_count"] == 1


def test_detect_dirty_state_sees_staged_file(stash_module, git_repo):
    """Staged-but-not-committed counts too — someone ran ``git add``
    and then the session ended before ``git commit``."""
    (git_repo / "seed.py").write_text("seed changed\n")
    subprocess.run(["git", "add", "seed.py"], check=True, cwd=git_repo)
    state = stash_module.detect_dirty_state(str(git_repo))
    assert state["is_dirty"] is True
    assert state["file_count"] == 1


def test_detect_dirty_state_counts_combined_changes(stash_module, git_repo):
    """Mix of modified, staged, and untracked should produce the total,
    deduped — one physical file should not count twice even if it is
    both staged and has unstaged edits on top."""
    (git_repo / "seed.py").write_text("one\n")
    subprocess.run(["git", "add", "seed.py"], check=True, cwd=git_repo)
    (git_repo / "seed.py").write_text("one staged, now modified on top\n")
    (git_repo / "new.py").write_text("untracked\n")
    state = stash_module.detect_dirty_state(str(git_repo))
    assert state["is_dirty"] is True
    # seed.py counted once, plus new.py = 2
    assert state["file_count"] == 2


def test_detect_dirty_state_returns_branch_name(stash_module, git_repo):
    """We need the branch name in the payload so build_stash_message
    does not have to re-shell into git."""
    state = stash_module.detect_dirty_state(str(git_repo))
    assert state["branch"] == "main"


def test_detect_dirty_state_handles_git_failure(stash_module, tmp_path):
    """Called in a non-git directory → must not raise; is_dirty False,
    branch empty. Hook is best-effort and never breaks session end."""
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    state = stash_module.detect_dirty_state(str(non_repo))
    assert state["is_dirty"] is False
    assert state["branch"] == ""


# ---------------------------------------------------------------------------
# auto_stash — end-to-end: dirty tree → stash created → tree becomes clean
# ---------------------------------------------------------------------------


def test_auto_stash_no_op_on_clean_tree(stash_module, git_repo):
    """Clean tree → no stash created. Nothing in ``git stash list``."""
    result = stash_module.auto_stash(str(git_repo))
    assert result["stashed"] is False
    stash_list = subprocess.run(
        ["git", "stash", "list"], capture_output=True, text=True, cwd=git_repo
    )
    assert stash_list.stdout.strip() == ""


def test_auto_stash_creates_labeled_stash_on_dirty_tree(stash_module, git_repo):
    """Dirty tree → stash created, working tree clean afterwards,
    stash list contains the branch-labeled message."""
    (git_repo / "seed.py").write_text("dirty\n")
    (git_repo / "new.py").write_text("untracked\n")

    result = stash_module.auto_stash(str(git_repo))

    assert result["stashed"] is True
    assert result["file_count"] == 2

    # Working tree must be clean afterwards.
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=git_repo
    )
    assert status.stdout.strip() == "", (
        "auto_stash must leave the working tree clean"
    )

    # Stash list must include the branch name and the file count.
    stash_list = subprocess.run(
        ["git", "stash", "list"], capture_output=True, text=True, cwd=git_repo
    )
    assert "main" in stash_list.stdout
    assert "2" in stash_list.stdout


def test_auto_stash_includes_untracked_files(stash_module, git_repo):
    """The untracked-file case is exactly what this feature was built
    for — the stash must use ``-u`` so new files are preserved, not
    left behind to leak into the next session."""
    (git_repo / "brand_new.py").write_text("was not tracked\n")

    result = stash_module.auto_stash(str(git_repo))

    assert result["stashed"] is True
    # No untracked files remain after stash.
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=git_repo
    )
    assert status.stdout.strip() == ""
    # Popping brings the untracked file back.
    subprocess.run(["git", "stash", "pop"], check=True, cwd=git_repo)
    assert (git_repo / "brand_new.py").exists()


def test_auto_stash_survives_non_git_cwd(stash_module, tmp_path):
    """Called outside a git repo → must not raise, returns ``stashed``
    False. Hook is best-effort; the whole point is that it never
    disrupts session end."""
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    result = stash_module.auto_stash(str(non_repo))

    assert result["stashed"] is False
