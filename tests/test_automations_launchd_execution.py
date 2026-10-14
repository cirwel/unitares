"""Scheduler evidence must not be replaced by configuration or stale starts."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def census(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/ops/unitares-automations"
    loader = importlib.machinery.SourceFileLoader("census_execution", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    monkeypatch.setattr(mod, "HOME", tmp_path)
    monkeypatch.setattr(mod, "OVERRIDES_JSON", tmp_path / "overrides.json")
    monkeypatch.setattr(mod, "LATEST_RUN_DIR", tmp_path / "latest")
    monkeypatch.setattr(mod, "RUNNING_RUN_DIR", tmp_path / "running")
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    for name in ("idle", "live", "missing", "paused"):
        (agents / f"{name}.plist").write_bytes(plistlib.dumps({
            "Label": name, "ProgramArguments": ["/bin/true"], "StartInterval": 60,
        }))
    for collector in ("collect_crontab", "collect_systemd", "collect_remote_host",
                      "collect_codex", "collect_hermes", "collect_claude_tasks",
                      "collect_claude_hooks", "collect_external"):
        monkeypatch.setattr(mod, collector, lambda warnings: [])
    monkeypatch.setattr(mod, "collect_github_actions", lambda *args: [])
    monkeypatch.setattr(mod, "check_own_install_health", lambda warnings: None)
    return mod


def scheduler(monkeypatch, census, *, output="PID\tStatus\tLabel\n-\t2\tidle\n42\t1\tlive\n", disabled='"paused" => disabled'):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 5
        return SimpleNamespace(returncode=0, stdout=output if command[1] == "list" else disabled)

    monkeypatch.setattr(census.subprocess, "run", run)
    return calls


def receipt(census, root, status, timestamp):
    root.mkdir(exist_ok=True)
    (root / "run.json").write_text(json.dumps({
        "automation_id": "launchd:idle", "status": status, "timestamp": timestamp,
    }))


def test_config_is_not_execution(census, monkeypatch):
    calls = scheduler(monkeypatch, census)
    report = census.collect_all(census.HOME)
    rows = {row["name"]: row for row in report["automations"]}
    assert rows["missing"]["status"] == "inactive"
    assert rows["paused"]["status"] == "disabled"
    assert rows["idle"]["status"] == rows["live"]["status"] == "active"
    assert "launchd:idle" in report["summary"]["needs_attention"]
    assert "launchd:live" not in report["summary"]["needs_attention"]
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "malformed"])
def test_unavailable_scheduler_is_unknown(census, monkeypatch, failure):
    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        return SimpleNamespace(returncode=1 if failure == "nonzero" else 0, stdout="bad output")
    monkeypatch.setattr(census.subprocess, "run", run)
    report = census.collect_all(census.HOME)
    assert all(row["status"] == "unknown" for row in report["automations"])
    assert len(report["summary"]["needs_attention"]) == 4
    assert report["summary"]["warnings"]


def test_operator_pause_does_not_hide_execution_evidence(census, monkeypatch):
    scheduler(monkeypatch, census)
    census.OVERRIDES_JSON.write_text(json.dumps({"automations": {
        "launchd:missing": {"status": "paused", "notes": ["intentional pause"]},
    }}))
    report = census.collect_all(census.HOME)
    row = next(row for row in report["automations"] if row["name"] == "missing")
    assert row["status"] == "paused"
    assert "launchd:unloaded" in row["notes"]
    assert "intentional pause" in row["notes"]


def test_stranded_start_is_not_live_work(census, monkeypatch):
    scheduler(monkeypatch, census)
    receipt(census, census.RUNNING_RUN_DIR, "running", "2026-09-29T22:00:00Z")
    report = census.collect_all(census.HOME)
    row = next(row for row in report["automations"] if row["name"] == "idle")
    assert row["last_status"] == "unknown"
    assert "attention:running_receipt_without_launchd_process" in row["notes"]
    assert json.loads((census.RUNNING_RUN_DIR / "run.json").read_text())["status"] == "running"


def test_current_start_remains_running(census, monkeypatch):
    scheduler(monkeypatch, census, output="PID\tStatus\tLabel\n42\t0\tidle\n")
    receipt(census, census.RUNNING_RUN_DIR, "running", "2026-10-14T06:00:00Z")
    report = census.collect_all(census.HOME)
    assert next(row for row in report["automations"] if row["name"] == "idle")["last_status"] == "running"


@pytest.mark.parametrize("running_time", ["2026-10-13T06:00:00Z", "2026-10-14T06:00:00Z", "invalid"])
def test_old_start_cannot_replace_terminal_receipt(census, running_time):
    receipt(census, census.LATEST_RUN_DIR, "ok", "2026-10-14T06:00:00Z")
    receipt(census, census.RUNNING_RUN_DIR, "running", running_time)
    assert census.load_latest_runs()["launchd:idle"]["status"] == "ok"


def test_new_start_replaces_older_completion(census):
    receipt(census, census.LATEST_RUN_DIR, "ok", "2026-10-13T06:00:00Z")
    receipt(census, census.RUNNING_RUN_DIR, "running", "2026-10-14T06:00:00Z")
    assert census.load_latest_runs()["launchd:idle"]["status"] == "running"
