"""Tests for the agent workspace closeout helper."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def closeout_module():
    project_root = Path(__file__).resolve().parent.parent
    module_path = project_root / "scripts" / "dev" / "workspace_closeout.py"
    spec = importlib.util.spec_from_file_location("workspace_closeout", module_path)
    assert spec and spec.loader, f"could not load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["workspace_closeout"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def git_repo(tmp_path):
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


def test_parse_git_porcelain_splits_dirty_classes(closeout_module):
    state = closeout_module.parse_git_porcelain(
        "M  staged.py\n"
        " M unstaged.py\n"
        "MM both.py\n"
        "?? fresh.py\n"
    )

    assert state.dirty is True
    assert state.staged == ["staged.py", "both.py"]
    assert state.unstaged == ["unstaged.py", "both.py"]
    assert state.untracked == ["fresh.py"]


def test_build_stash_message_is_scannable(closeout_module):
    message = closeout_module.build_stash_message(
        branch="feat/workspace-cleanup",
        file_count=3,
        timestamp="2026-06-03T07:30:00Z",
    )

    assert "workspace-closeout auto-stash" in message
    assert "feat/workspace-cleanup" in message
    assert "3 files" in message
    assert "2026-06-03T07:30:00Z" in message


def test_parse_lsof_field_output_extracts_cwds(closeout_module):
    parsed = closeout_module.parse_lsof_field_output(
        "p123\n"
        "cpython\n"
        "fcwd\n"
        "n/tmp/repo\n"
        "p456\n"
        "ftxt\n"
        "n/ignored\n"
        "fcwd\n"
        "n/tmp/other\n"
    )

    assert parsed == {123: "/tmp/repo", 456: "/tmp/other"}


def test_parse_launchctl_print_maps_pid_to_label(closeout_module):
    parsed = closeout_module.parse_launchctl_print(
        "282:\t\t   84445      0 \tcom.unitares.sentinel-beam\n"
        "437:\t\t   84449      0 \tcom.unitares.wave3a-handlers\n"
        "457:\t\t       0      - \tcom.unitares.oneshot\n"
    )

    assert parsed == {
        84445: "com.unitares.sentinel-beam",
        84449: "com.unitares.wave3a-handlers",
    }


def test_is_under_requires_workspace_containment(closeout_module, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "src" / "file.py"
    inside.parent.mkdir()
    inside.write_text("x\n")
    outside = tmp_path / "other.py"
    outside.write_text("x\n")

    assert closeout_module.is_under(inside, root) is True
    assert closeout_module.is_under(outside, root) is False


def test_process_baseline_filters_existing_processes(closeout_module, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    baseline_proc = closeout_module.ProcessInfo(
        pid=10,
        ppid=1,
        cwd=str(root),
        command="python server.py",
        launch_label="com.example.server",
    )
    new_proc = closeout_module.ProcessInfo(
        pid=11,
        ppid=1,
        cwd=str(root),
        command="python new.py",
    )

    baseline_path = closeout_module.write_process_baseline(root, [baseline_proc])
    keys = closeout_module.read_process_baseline(root)

    assert baseline_path.exists()
    assert closeout_module.process_key(baseline_proc) in keys
    assert closeout_module.process_key(new_proc) not in keys


def test_closeout_stashes_dirty_repo_when_requested(closeout_module, git_repo, monkeypatch):
    monkeypatch.setattr(closeout_module, "repo_rooted_processes", lambda *args, **kwargs: [])
    (git_repo / "seed.py").write_text("dirty\n")
    (git_repo / "new.py").write_text("fresh\n")

    result = closeout_module.closeout(git_repo, stash=True)

    assert result.stashed is True
    assert result.git.dirty is False
    assert "workspace-closeout auto-stash" in result.stash_message

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=git_repo,
        check=True,
    )
    assert status.stdout.strip() == ""

    stash_list = subprocess.run(
        ["git", "stash", "list"],
        capture_output=True,
        text=True,
        cwd=git_repo,
        check=True,
    )
    assert "workspace-closeout auto-stash" in stash_list.stdout


def test_closeout_branch_hygiene_dry_run_surfaces_cleanup_candidates(
    closeout_module, git_repo, monkeypatch
):
    monkeypatch.setattr(closeout_module, "repo_rooted_processes", lambda *a, **k: [])
    calls = []

    def fake_hygiene(root, *, dry_run):
        calls.append((root, dry_run))
        return closeout_module.BranchHygieneSummary(
            dry_run=True,
            branches_prunable=1,
            branches_pruned=0,
            worktrees_removable=1,
            worktrees_removed=0,
            origin_orphans_deletable=0,
            origin_orphans_deleted=0,
            holds_count=0,
            holds=[],
            errors=[],
            log_lines=["DRY-RUN would branch -D 'codex/stale'"],
        )

    monkeypatch.setattr(closeout_module, "run_branch_hygiene", fake_hygiene)

    result = closeout_module.closeout(git_repo, branch_hygiene=True)

    assert calls == [(git_repo, True)]
    assert closeout_module.result_has_issues(result) is True
    rendered = closeout_module.render_text(result)
    assert "branch hygiene: dry-run" in rendered
    assert "would_prune_branches=1" in rendered
    assert "DRY-RUN would branch -D 'codex/stale'" in rendered
    assert closeout_module.to_jsonable(result)["clean"] is False


def test_closeout_branch_hygiene_live_can_clear_without_issue(
    closeout_module, git_repo, monkeypatch
):
    monkeypatch.setattr(closeout_module, "repo_rooted_processes", lambda *a, **k: [])
    calls = []

    def fake_hygiene(root, *, dry_run):
        calls.append((root, dry_run))
        return closeout_module.BranchHygieneSummary(
            dry_run=False,
            branches_prunable=1,
            branches_pruned=1,
            worktrees_removable=1,
            worktrees_removed=1,
            origin_orphans_deletable=0,
            origin_orphans_deleted=0,
            holds_count=0,
            holds=[],
            errors=[],
            log_lines=["deleted local branch: codex/stale"],
        )

    monkeypatch.setattr(closeout_module, "run_branch_hygiene", fake_hygiene)

    result = closeout_module.closeout(git_repo, branch_hygiene_live=True)

    assert calls == [(git_repo, False)]
    assert closeout_module.result_has_issues(result) is False
    rendered = closeout_module.render_text(result)
    assert "branch hygiene: live" in rendered
    assert "branches_pruned=1" in rendered


def test_git_state_marks_dirty_work_as_not_delivered(closeout_module, git_repo):
    (git_repo / "seed.py").write_text("dirty\n")

    state = closeout_module.git_state(git_repo)

    assert state.dirty is True
    assert state.delivery_status == "local_changes"
    assert closeout_module.delivery_needs_attention(state) is True
    assert "--stage-all" in closeout_module.delivery_next_step(state)


def test_git_state_marks_unpushed_commits_as_not_delivered(
    closeout_module, git_repo, tmp_path
):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=git_repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=git_repo, check=True)

    (git_repo / "seed.py").write_text("second\n")
    subprocess.run(["git", "add", "seed.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=git_repo, check=True)

    state = closeout_module.git_state(git_repo)

    assert state.dirty is False
    assert state.upstream == "origin/main"
    assert state.ahead == 1
    assert state.behind == 0
    assert state.delivery_status == "unpushed_commits"
    assert closeout_module.delivery_needs_attention(state) is True
    assert "gh pr create --draft" in closeout_module.delivery_next_step(state)


def test_git_state_marks_synced_default_as_delivered(
    closeout_module, git_repo, tmp_path
):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=git_repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=git_repo, check=True)

    state = closeout_module.git_state(git_repo)

    assert state.dirty is False
    assert state.delivery_status == "synced_default"
    assert closeout_module.delivery_needs_attention(state) is False


def _add_worktree(repo: Path, path: Path, branch: str) -> Path:
    subprocess.run(
        ["git", "worktree", "add", str(path), "-b", branch],
        cwd=str(repo), check=True, capture_output=True,
    )
    return path


def test_classify_main_checkout_is_shared(closeout_module, git_repo):
    iso = closeout_module.classify_workspace(git_repo)
    assert iso.kind == "main_checkout"
    assert iso.shared is True


def test_classify_linked_worktree_is_agent_owned(closeout_module, git_repo, tmp_path):
    wt = _add_worktree(git_repo, tmp_path / "agent_wt", "feat/x")
    iso = closeout_module.classify_workspace(wt)
    assert iso.kind == "agent_worktree"
    assert iso.shared is False


def test_classify_deploy_worktree_by_name(closeout_module, git_repo, tmp_path):
    wt = _add_worktree(git_repo, tmp_path / "unitares-deploy", "deploybr")
    iso = closeout_module.classify_workspace(wt)
    assert iso.kind == "deploy_worktree"
    assert iso.shared is True


def test_classify_non_repo_is_unknown_not_shared(closeout_module, tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    iso = closeout_module.classify_workspace(plain)
    assert iso.kind == "unknown"
    assert iso.shared is False


def test_start_check_shared_checkout_is_advisory_by_default(
    closeout_module, git_repo, monkeypatch
):
    monkeypatch.setattr(closeout_module, "repo_rooted_processes", lambda *a, **k: [])
    result = closeout_module.start_check(git_repo)  # advisory (default)
    assert result.isolation.shared is True
    assert result.isolation_enforced is False
    # Advisory: shared checkout does NOT block a clean start.
    assert closeout_module.isolation_is_issue(result) is False
    assert closeout_module.start_check_has_issues(result) is False


def test_start_check_require_worktree_blocks_shared_checkout(
    closeout_module, git_repo, monkeypatch
):
    monkeypatch.setattr(closeout_module, "repo_rooted_processes", lambda *a, **k: [])
    result = closeout_module.start_check(git_repo, require_worktree=True)
    assert result.isolation_enforced is True
    assert closeout_module.isolation_is_issue(result) is True
    assert closeout_module.start_check_has_issues(result) is True


def test_start_check_agent_worktree_clean_under_strict(
    closeout_module, git_repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(closeout_module, "repo_rooted_processes", lambda *a, **k: [])
    wt = _add_worktree(git_repo, tmp_path / "agent_wt2", "feat/y")
    result = closeout_module.start_check(wt, require_worktree=True)
    assert result.isolation.kind == "agent_worktree"
    assert closeout_module.isolation_is_issue(result) is False
    assert closeout_module.start_check_has_issues(result) is False


def test_start_check_without_existing_baseline_writes_initial_baseline(
    closeout_module, git_repo, monkeypatch
):
    process = closeout_module.ProcessInfo(
        pid=10,
        ppid=1,
        cwd=str(git_repo),
        command="python server.py",
        launch_label="com.example.server",
    )
    monkeypatch.setattr(
        closeout_module,
        "repo_rooted_processes",
        lambda *args, **kwargs: [process],
    )

    result = closeout_module.start_check(git_repo)

    assert result.checked_existing_baseline is False
    assert result.baseline_written is True
    assert result.new_baseline_process_count == 1
    keys = closeout_module.read_process_baseline(git_repo)
    assert closeout_module.process_key(process) in keys


def test_start_check_dirty_repo_blocks_baseline_refresh(
    closeout_module, git_repo, monkeypatch
):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dirty start should not write process baseline")

    monkeypatch.setattr(closeout_module, "repo_rooted_processes", fail_if_called)
    (git_repo / "seed.py").write_text("dirty\n")

    result = closeout_module.start_check(git_repo)

    assert result.baseline_written is False
    assert result.closeout.git.dirty is True
    assert not closeout_module.baseline_path(git_repo).exists()


def test_start_check_existing_baseline_blocks_leftover_process(
    closeout_module, git_repo, monkeypatch
):
    existing = closeout_module.ProcessInfo(
        pid=10,
        ppid=1,
        cwd=str(git_repo),
        command="python server.py",
        launch_label="com.example.server",
    )
    leftover = closeout_module.ProcessInfo(
        pid=11,
        ppid=1,
        cwd=str(git_repo),
        command="python forgotten.py",
    )
    closeout_module.write_process_baseline(git_repo, [existing])

    def fake_repo_rooted_processes(_root, *, baseline_keys=None):
        if baseline_keys:
            return [leftover]
        return [existing, leftover]

    monkeypatch.setattr(
        closeout_module,
        "repo_rooted_processes",
        fake_repo_rooted_processes,
    )

    result = closeout_module.start_check(git_repo)

    assert result.checked_existing_baseline is True
    assert result.baseline_written is False
    assert result.closeout.repo_processes == [leftover]


def test_render_text_includes_delivery_next_step_for_dirty_repo(
    closeout_module, git_repo, monkeypatch,
):
    monkeypatch.setattr(closeout_module, "repo_rooted_processes", lambda *a, **k: [])
    (git_repo / "seed.py").write_text("dirty\n")

    result = closeout_module.closeout(git_repo)
    rendered = closeout_module.render_text(result)

    assert "delivery: local_changes" in rendered
    assert "next=ship with: ./scripts/dev/ship.sh --stage-all" in rendered
