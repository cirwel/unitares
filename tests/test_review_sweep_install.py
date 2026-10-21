"""Exercise the installed launchd configuration without loading a real service."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/install-review-sweep.py"
spec = importlib.util.spec_from_file_location("review_sweep_install", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_write_only_installs_independent_bootstrap_and_registry_wrapper(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(installer.shutil, "which", lambda name: f"/tools/{name}")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    plist = installer.install(repo, reviewers=["claude", "antigravity"], write_only=True)
    config = plistlib.loads(plist.read_bytes())
    assert config["StartInterval"] == 1800 and config["RunAtLoad"] is True
    args = config["ProgramArguments"]
    assert args[:3] == ["/tools/unitares-automation-run", "--id", "launchd:com.unitares.review-sweep"]
    assert args[-6] == "/bin/bash"
    assert args[-4:] == ["--reviewer", "claude", "--reviewer", "antigravity"]
    installed = Path(args[-5])
    assert installed.read_bytes() == SCRIPT.with_name("review-sweep.sh").read_bytes()
    assert not installed.is_relative_to(repo)
    assert config["EnvironmentVariables"]["UNITARES_REVIEW_REPO"] == str(repo)


def test_install_does_not_interrupt_or_overwrite_loaded_service(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    calls = []
    def loaded(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(installer.subprocess, "run", loaded)
    with pytest.raises(SystemExit, match="already loaded"):
        installer.install(tmp_path, reviewers=["claude"])
    assert len(calls) == 1 and calls[0][:2] == ["launchctl", "print"]
    assert not (tmp_path / ".local").exists()


@pytest.mark.parametrize("reviewers", [[], ["not-a-provider"]])
def test_install_requires_explicit_valid_choices_before_mutations(tmp_path, monkeypatch, reviewers):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: pytest.fail("accessed service"))
    with pytest.raises(SystemExit, match="Choose at least one reviewer"):
        installer.install(tmp_path, reviewers=reviewers, write_only=True)


def _bootstrap_repo(tmp_path, slug="unitares"):
    repo = tmp_path / "repo with spaces"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c",
                    "user.email=test@example.test", "commit", "--allow-empty", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    f"https://github.com/cirwel/{slug}.git"], check=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    return repo


@pytest.mark.parametrize("slug", ["unitares", "unitares-governance-plugin"])
def test_bootstrap_routes_fetch_and_review_through_app_in_trusted_checkout(tmp_path, slug):
    repo = _bootstrap_repo(tmp_path, slug)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    runner = bin_dir / "unitares-github"
    runner.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
with Path(os.environ['TEST_CALL_LOG']).open('a') as f:
    f.write(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')
''')
    runner.chmod(0o755)
    lane = tmp_path / "lane"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "UNITARES_REVIEW_REPO": str(repo), "UNITARES_REVIEW_WORKTREES": str(lane),
           "TEST_CALL_LOG": str(log)}
    result = subprocess.run(["bash", str(SCRIPT.with_name("review-sweep.sh")), "--dry-run"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [
        {"args": ["--read-only", "--", "git", "fetch", "--quiet", "origin", "master"], "cwd": str(repo)},
        {"args": ["--", "review", "sweep", "--worktree", str(lane / "unitares-review-bot-pr"), "--dry-run"],
         "cwd": str(lane / "unitares-review-bot")},
    ]


@pytest.mark.parametrize("slug", ["unitares", "unitares-governance-plugin"])
def test_bootstrap_refuses_missing_app_runner_before_fetch(tmp_path, slug):
    repo = _bootstrap_repo(tmp_path, slug)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "tr"):
        (bin_dir / name).symlink_to(shutil.which(name))
    env = {**os.environ, "PATH": str(bin_dir), "UNITARES_REVIEW_REPO": str(repo),
           "UNITARES_REVIEW_WORKTREES": str(tmp_path / "unused")}
    result = subprocess.run([shutil.which("bash"), str(SCRIPT.with_name("review-sweep.sh"))],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "no human-login fallback" in result.stderr
    assert not (tmp_path / "unused").exists()


def test_review_entrypoint_dispatches_sweep_without_starting_author_review(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    entrypoint = SCRIPT.parents[1] / "dev/review.sh"
    result = subprocess.run(["bash", str(entrypoint), "sweep", "--worktree", str(tmp_path), "--dry-run"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["scripts/dev/review_gate.py", "sweep", "--worktree", str(tmp_path), "--dry-run"]
