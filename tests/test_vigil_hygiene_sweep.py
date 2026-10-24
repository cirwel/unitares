"""Integration tests for agents/vigil_hygiene/agent.py sweep against an
ephemeral local git repo. Avoids network by stubbing list_open_pr_branches.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from agents.vigil_hygiene import agent as agent_mod
from agents.vigil_hygiene.agent import is_keepalive, sweep
from agents.vigil_hygiene.cherry import CherryVerdict


@pytest.fixture(autouse=True)
def isolated_lease_plane(monkeypatch):
    # A developer shell may carry real lease credentials. Tests must exercise
    # the advisory wrapper without sending synthetic sweeps to that service.
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=env,
    )


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Local git repo on master with one commit and a bare origin remote so
    fetch/push/cherry against `origin/*` works without network."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(bare)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(bare))
    (repo / "README").write_text("hello\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "push", "-u", "origin", "master")
    return repo


class TestKeepaliveLogic:
    def test_master_always_keepalive(self):
        keep, reason = is_keepalive("master", set(), None, 1000.0)
        assert keep
        assert "name" in reason

    def test_main_always_keepalive(self):
        keep, _ = is_keepalive("main", set(), None, 1000.0)
        assert keep

    def test_open_pr_branch_keepalive(self):
        keep, reason = is_keepalive("foo", {"foo"}, None, 1000.0)
        assert keep
        assert "PR" in reason

    def test_newer_than_24h_keepalive(self):
        now = 1_000_000.0
        keep, reason = is_keepalive("foo", set(), int(now - 3600), now)
        assert keep
        assert "24h" in reason

    def test_older_than_24h_not_keepalive(self):
        now = 1_000_000.0
        keep, _ = is_keepalive("foo", set(), int(now - 25 * 3600), now)
        assert not keep

    def test_unknown_age_not_keepalive(self):
        # ts None means we couldn't measure age; default to non-keepalive (sweep-eligible)
        keep, _ = is_keepalive("foo", set(), None, 1000.0)
        assert not keep


class TestSweepDryRun:
    def test_sweep_invokes_lease_advisory_with_expected_surface(
        self, fake_repo: Path, monkeypatch
    ):
        from unitares_sdk.lease_plane import advisory as advisory_module

        captured: dict = {}
        events: list[str] = []

        @contextlib.contextmanager
        def fake_scope(**kwargs):
            captured.update(kwargs)
            events.append("enter")
            yield ("held_by_other", None)
            events.append("exit")

        monkeypatch.setattr(advisory_module, "lease_advisory_scope", fake_scope)
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(fake_repo, dry_run=True)

        assert report.dry_run is True
        assert events == ["enter", "exit"]
        assert captured["surface_id"] == "maintenance:/vigil_hygiene_sweep"
        assert captured["ttl_s"] == 900
        assert "dry-run branch/worktree sweep" in captured["intent"]
        assert str(fake_repo) in captured["intent"]

    def test_dry_run_does_not_delete_worktree(self, fake_repo: Path, monkeypatch):
        wt_dir = fake_repo / ".worktrees" / "wt1"
        _git(fake_repo, "worktree", "add", "-b", "feature/x", str(wt_dir))
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(fake_repo, dry_run=True)

        assert report.dry_run is True
        assert wt_dir.exists()
        assert (wt_dir / ".git").exists()
        assert report.worktrees_removed == 0

    def test_dry_run_reports_no_errors_on_clean_repo(self, fake_repo: Path, monkeypatch):
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(fake_repo, dry_run=True)

        assert report.errors == []
        assert report.duration_s > 0


class TestSweepGoneBranchSalvageGuard:
    def test_empty_local_cherry_is_delete_candidate(self):
        result = agent_mod.classify_local_gone_cherry("")
        assert result.verdict == CherryVerdict.DELETE
        assert "no commits ahead" in result.reason

    def test_live_holds_gone_branch_with_unique_commits(self, fake_repo: Path, monkeypatch):
        _git(fake_repo, "switch", "-c", "codex/unique-local")
        (fake_repo / "unique.txt").write_text("unique work\n")
        _git(fake_repo, "add", "unique.txt")
        _git(fake_repo, "commit", "-m", "unique local work")
        _git(fake_repo, "push", "-u", "origin", "codex/unique-local")
        _git(fake_repo, "switch", "master")
        _git(fake_repo, "push", "origin", "--delete", "codex/unique-local")
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(fake_repo, dry_run=False)

        branches = _git(fake_repo, "branch", "--list", "codex/unique-local").stdout
        assert "codex/unique-local" in branches
        assert "codex/unique-local" in report.holds
        assert report.branches_pruned == 0

    def test_live_holds_gone_worktree_branch_with_unique_commits(self, fake_repo: Path, monkeypatch):
        wt_dir = fake_repo / ".worktrees" / "wt-unique"
        _git(fake_repo, "worktree", "add", "-b", "codex/wt-unique", str(wt_dir))
        (wt_dir / "wt-unique.txt").write_text("unique worktree work\n")
        _git(wt_dir, "add", "wt-unique.txt")
        _git(wt_dir, "commit", "-m", "unique worktree work")
        _git(wt_dir, "push", "-u", "origin", "codex/wt-unique")
        _git(fake_repo, "push", "origin", "--delete", "codex/wt-unique")
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(fake_repo, dry_run=False)

        branches = _git(fake_repo, "branch", "--list", "codex/wt-unique").stdout
        assert "codex/wt-unique" in branches
        assert wt_dir.exists()
        assert "codex/wt-unique" in report.holds
        assert report.worktrees_removed == 0

    def test_live_deletes_gone_branch_when_patch_equivalent(self, fake_repo: Path, monkeypatch):
        _git(fake_repo, "switch", "-c", "codex/squash-merged")
        (fake_repo / "squash.txt").write_text("squash-equivalent work\n")
        _git(fake_repo, "add", "squash.txt")
        _git(fake_repo, "commit", "-m", "squash-equivalent work")
        feature_sha = _git(fake_repo, "rev-parse", "HEAD").stdout.strip()
        _git(fake_repo, "push", "-u", "origin", "codex/squash-merged")
        _git(fake_repo, "switch", "master")
        _git(fake_repo, "cherry-pick", feature_sha)
        _git(fake_repo, "push", "origin", "master")
        _git(fake_repo, "push", "origin", "--delete", "codex/squash-merged")
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(fake_repo, dry_run=False)

        branches = _git(fake_repo, "branch", "--list", "codex/squash-merged").stdout
        assert "codex/squash-merged" not in branches
        assert report.holds == []
        assert report.branches_prunable == 1
        assert report.branches_pruned == 1

    def test_live_holds_current_gone_branch_in_sweep_repo(
        self, fake_repo: Path, monkeypatch
    ):
        _git(fake_repo, "switch", "-c", "codex/current-merged")
        (fake_repo / "current.txt").write_text("current branch work\n")
        _git(fake_repo, "add", "current.txt")
        _git(fake_repo, "commit", "-m", "current branch work")
        feature_sha = _git(fake_repo, "rev-parse", "HEAD").stdout.strip()
        _git(fake_repo, "push", "-u", "origin", "codex/current-merged")
        _git(fake_repo, "switch", "master")
        _git(fake_repo, "cherry-pick", feature_sha)
        _git(fake_repo, "push", "origin", "master")
        _git(fake_repo, "push", "origin", "--delete", "codex/current-merged")
        _git(fake_repo, "switch", "codex/current-merged")
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(fake_repo, dry_run=False)

        branches = _git(fake_repo, "branch", "--list", "codex/current-merged").stdout
        assert "codex/current-merged" in branches
        assert "codex/current-merged" in report.holds
        assert report.branches_pruned == 0

    def test_branchless_primary_checkout_is_not_removable_from_linked_worktree(
        self, fake_repo: Path, monkeypatch
    ):
        linked = fake_repo / ".worktrees" / "linked"
        _git(fake_repo, "worktree", "add", "-b", "codex/linked", str(linked))
        _git(fake_repo, "switch", "--detach")
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())

        report = sweep(linked, dry_run=True)

        assert fake_repo.exists()
        assert report.worktrees_removable == 0


class TestSweepHelpers:
    def test_list_worktrees_includes_main_and_added(self, fake_repo: Path):
        wt_dir = fake_repo / ".worktrees" / "alpha"
        _git(fake_repo, "worktree", "add", "-b", "feat/alpha", str(wt_dir))

        worktrees = agent_mod.list_worktrees(fake_repo)

        paths = {p for p, _ in worktrees}
        branches = {b for _, b in worktrees if b}
        assert fake_repo in paths
        assert wt_dir in paths
        assert "master" in branches
        assert "feat/alpha" in branches

    def test_list_origin_branches_returns_pushed_branches(self, fake_repo: Path):
        result = agent_mod.list_origin_branches(fake_repo)
        assert "master" in result


class TestOpenPRProtection:
    def test_queries_selected_origin_and_all_pages(self, fake_repo, monkeypatch):
        import json

        real_run = agent_mod.subprocess.run
        calls = []
        monkeypatch.setenv("GH_REPO", "unrelated/project")

        def run(cmd, **kwargs):
            if cmd[0] != "gh":
                return real_run(cmd, **kwargs)
            calls.append((cmd, kwargs))
            payload = (
                {"nameWithOwner": "example/selected", "url": "https://github.example/example/selected"}
                if cmd[1:3] == ["repo", "view"] else
                [[{"head": {"ref": "first-page"}}], [{"head": {"ref": "later-page"}}]]
            )
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

        monkeypatch.setattr(agent_mod.subprocess, "run", run)
        assert agent_mod.list_open_pr_branches(fake_repo) == {"first-page", "later-page"}
        assert calls[0][0][3] == _git(fake_repo, "remote", "get-url", "origin").stdout.strip()
        assert calls[0][1]["cwd"] == str(fake_repo)
        assert calls[1][0] == [
            "gh", "api", "--hostname", "github.example", "--paginate", "--slurp",
            "repos/example/selected/pulls?state=open&per_page=100",
        ]

    @pytest.mark.parametrize("payload", ["", "null", "[]", "{}", '[[{"head": {}}]]', '["bad-page"]'])
    def test_incomplete_or_malformed_response_is_not_an_empty_protection_set(
        self, fake_repo, monkeypatch, payload,
    ):
        real_run = agent_mod.subprocess.run

        def run(cmd, **kwargs):
            if cmd[0] != "gh":
                return real_run(cmd, **kwargs)
            data = ('{"nameWithOwner":"example/repo","url":"https://github.com/example/repo"}'
                    if cmd[1:3] == ["repo", "view"] else payload)
            return subprocess.CompletedProcess(cmd, 0, data, "")

        monkeypatch.setattr(agent_mod.subprocess, "run", run)
        with pytest.raises(agent_mod.ProtectionLookupError):
            agent_mod.list_open_pr_branches(fake_repo)

    def test_protection_failure_holds_entire_live_sweep_before_git_mutation(self, fake_repo, monkeypatch):
        def unavailable(_repo):
            raise agent_mod.ProtectionLookupError("no GitHub response")

        monkeypatch.setattr(agent_mod, "list_open_pr_branches", unavailable)
        monkeypatch.setattr(agent_mod, "run_git", lambda *a, **kw: pytest.fail("sweep ran git before protection"))
        report = agent_mod._sweep_inner(fake_repo, dry_run=False)
        assert report.errors == ["no GitHub response"]
        assert report.branches_pruned == report.worktrees_removed == report.origin_orphans_deleted == 0

    def test_gone_local_branch_with_open_pr_is_held(self, fake_repo, monkeypatch):
        _git(fake_repo, "branch", "feature/active-review")
        _git(fake_repo, "push", "-u", "origin", "feature/active-review")
        _git(fake_repo, "push", "origin", "--delete", "feature/active-review")
        monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: {"feature/active-review"})
        report = sweep(fake_repo, dry_run=False)
        assert report.branches_pruned == 0
        assert "feature/active-review" in _git(fake_repo, "branch", "--list", "feature/active-review").stdout


def test_selected_main_repository_uses_its_default_base(fake_repo, monkeypatch):
    _git(fake_repo, "branch", "-m", "master", "main")
    _git(fake_repo, "push", "-u", "origin", "main")
    # Explicit symbolic remote default supports repositories retaining another
    # historical branch named master without guessing the base.
    _git(fake_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert agent_mod.default_base(fake_repo) == "origin/main"
    monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())
    assert sweep(fake_repo, dry_run=True).errors == []


def test_ambiguous_default_branch_holds_cleanup(fake_repo, monkeypatch):
    _git(fake_repo, "branch", "main")
    _git(fake_repo, "push", "origin", "main")
    monkeypatch.setattr(agent_mod, "list_open_pr_branches", lambda repo: set())
    agent_mod.run_git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", repo=fake_repo, check=False)
    with pytest.raises(agent_mod.ProtectionLookupError, match="cannot establish origin's default branch"):
        agent_mod.default_base(fake_repo)
