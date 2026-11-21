"""Tests for scripts/dev/branch_hygiene/clean_check.py."""
from pathlib import Path

from scripts.dev.branch_hygiene.clean_check import check_worktree_clean


class TestCheckWorktreeClean:
    def test_clean_status_no_sentinels(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        result = check_worktree_clean(tmp_path, "")
        assert result.is_clean
        assert result.reason == ""

    def test_dirty_status(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        result = check_worktree_clean(tmp_path, " M src/foo.py\n?? new.py\n")
        assert not result.is_clean
        assert "uncommitted" in result.reason

    def test_paused_rebase_not_clean(self, tmp_path: Path):
        (tmp_path / ".git" / "rebase-merge").mkdir(parents=True)
        (tmp_path / ".git" / "rebase-merge" / "head-name").write_text("refs/heads/foo")
        result = check_worktree_clean(tmp_path, "")
        assert not result.is_clean
        assert "head-name" in result.reason

    def test_paused_cherry_pick_not_clean(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "CHERRY_PICK_HEAD").write_text("abc123")
        result = check_worktree_clean(tmp_path, "")
        assert not result.is_clean
        assert "CHERRY_PICK_HEAD" in result.reason

    def test_paused_merge_not_clean(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "MERGE_HEAD").write_text("abc123")
        result = check_worktree_clean(tmp_path, "")
        assert not result.is_clean
        assert "MERGE_HEAD" in result.reason

    def test_in_progress_bisect_not_clean(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "BISECT_LOG").write_text("...")
        result = check_worktree_clean(tmp_path, "")
        assert not result.is_clean
        assert "BISECT_LOG" in result.reason

    def test_dirty_status_takes_priority_over_sentinel_check(self, tmp_path: Path):
        # Dirty status returned first since the sentinel scan is skipped
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "MERGE_HEAD").write_text("abc")
        result = check_worktree_clean(tmp_path, " M file\n")
        assert not result.is_clean
        assert "uncommitted" in result.reason
