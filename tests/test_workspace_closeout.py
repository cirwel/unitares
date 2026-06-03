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
