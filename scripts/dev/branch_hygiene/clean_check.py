"""Determine if a worktree is safe to remove.

A worktree is safe iff `git status --porcelain` is empty AND none of the
in-progress git operation sentinel files exist. status --porcelain alone
returns empty during a paused rebase if the working tree files have not
been touched yet — load-bearing oversight without the sentinel checks.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class CleanCheckResult(NamedTuple):
    is_clean: bool
    reason: str


SENTINEL_FILES = (
    ".git/rebase-merge/head-name",
    ".git/CHERRY_PICK_HEAD",
    ".git/MERGE_HEAD",
    ".git/BISECT_LOG",
)


def check_worktree_clean(worktree_path: Path, status_porcelain: str) -> CleanCheckResult:
    if status_porcelain.strip():
        return CleanCheckResult(False, "uncommitted changes")

    for sentinel in SENTINEL_FILES:
        if (worktree_path / sentinel).exists():
            op = sentinel.rsplit("/", 1)[-1]
            return CleanCheckResult(False, f"in-progress git operation: {op}")

    return CleanCheckResult(True, "")
