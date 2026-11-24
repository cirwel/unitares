"""Determine if a worktree is safe to remove.

A worktree is safe iff `git status --porcelain` is empty AND none of the
in-progress git operation or lock sentinel files exist in its administrative
git directory. `status --porcelain` alone returns empty during a paused rebase
or bisect if working-tree files are clean, and linked worktrees store `.git` as
a pointer file (`gitdir: <path>`) rather than a directory (#3360).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple


class CleanCheckResult(NamedTuple):
    is_clean: bool
    reason: str


SENTINEL_ENTRIES = (
    ("locked", "locked worktree"),
    ("rebase-merge/head-name", "in-progress git operation: head-name"),
    ("rebase-merge", "in-progress git operation: rebase-merge"),
    ("rebase-apply", "in-progress git operation: rebase-apply"),
    ("CHERRY_PICK_HEAD", "in-progress git operation: CHERRY_PICK_HEAD"),
    ("MERGE_HEAD", "in-progress git operation: MERGE_HEAD"),
    ("REVERT_HEAD", "in-progress git operation: REVERT_HEAD"),
    ("BISECT_LOG", "in-progress git operation: BISECT_LOG"),
)

# Backwards-compatible `.git/<rel>` tuple retained alongside `SENTINEL_ENTRIES`.
SENTINEL_FILES = tuple(f".git/{rel}" for rel, _ in SENTINEL_ENTRIES)


def _resolve_git_dir(worktree_path: Path) -> Path | None:
    dot_git = worktree_path / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            first = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        except (IndexError, OSError):
            first = ""
        if first.startswith("gitdir:"):
            raw_target = first[len("gitdir:") :].strip()
            if raw_target:
                resolved = (worktree_path / raw_target).resolve()
                if resolved.is_dir():
                    return resolved
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        resolved = (worktree_path / proc.stdout.strip()).resolve()
        if resolved.is_dir():
            return resolved
    return None


def check_worktree_clean(worktree_path: Path, status_porcelain: str) -> CleanCheckResult:
    if status_porcelain.strip():
        return CleanCheckResult(False, "uncommitted changes")

    git_dir = _resolve_git_dir(worktree_path)
    if git_dir is None:
        return CleanCheckResult(False, "cannot locate git directory")

    for rel_sentinel, reason in SENTINEL_ENTRIES:
        if (git_dir / rel_sentinel).exists():
            return CleanCheckResult(False, reason)

    return CleanCheckResult(True, "")
