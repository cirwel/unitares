"""Exercise the installed launchd configuration without loading a real service."""
import importlib.util
from pathlib import Path
import plistlib
import subprocess

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
    plist = installer.install(repo, write_only=True)
    config = plistlib.loads(plist.read_bytes())
    assert config["StartInterval"] == 1800 and config["RunAtLoad"] is True
    args = config["ProgramArguments"]
    assert args[:3] == ["/tools/unitares-automation-run", "--id", "launchd:com.unitares.review-sweep"]
    assert args[-2] == "/bin/bash"
    installed = Path(args[-1])
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
        installer.install(tmp_path)
    assert len(calls) == 1 and calls[0][:2] == ["launchctl", "print"]
    assert not (tmp_path / ".local").exists()
