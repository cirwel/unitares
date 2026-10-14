"""launchd configuration is not evidence that a job is loaded or healthy."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def census_mod():
    module_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "ops" / "unitares-automations"
    )
    loader = importlib.machinery.SourceFileLoader("unitares_automations_launchd", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _plist(home: Path, label: str, args: list[str], *, disabled: bool = False) -> None:
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    with (launch_dir / f"{label}.plist").open("wb") as handle:
        plistlib.dump(
            {"Label": label, "ProgramArguments": args, "Disabled": disabled},
            handle,
        )


def test_launchctl_list_and_disabled_overrides_are_parsed(census_mod, monkeypatch):
    monkeypatch.setattr(census_mod.sys, "platform", "darwin")
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv == ["launchctl", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout="PID\tStatus\tLabel\n123\t-9\tservice.running\n-\t2\tjob.failed\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='disabled services = {\n\t"job.disabled" => true\n\t"job.failed" => false\n}',
            stderr="",
        )

    monkeypatch.setattr(census_mod.subprocess, "run", fake_run)
    jobs, disabled = census_mod.launchd_runtime([])
    assert jobs == {
        "service.running": ("123", -9),
        "job.failed": ("-", 2),
    }
    assert disabled == {"job.disabled": True, "job.failed": False}
    assert calls == [
        ["launchctl", "list"],
        ["launchctl", "print-disabled", f"gui/{census_mod.os.getuid()}"],
    ]


def test_collect_launchd_reconciles_loaded_disabled_missing_and_not_loaded(
    census_mod, tmp_path, monkeypatch,
):
    monkeypatch.setattr(census_mod, "HOME", tmp_path)
    existing = tmp_path / "job.sh"
    existing.write_text("#!/bin/sh\n")
    missing = tmp_path / "removed.py"
    _plist(tmp_path, "job.failed", ["/bin/bash", str(existing)])
    _plist(tmp_path, "job.disabled", ["/usr/bin/true"])
    _plist(tmp_path, "job.missing", ["/usr/bin/python3", str(missing)])
    _plist(tmp_path, "job.not-loaded", ["/usr/bin/true"])
    monkeypatch.setattr(
        census_mod,
        "launchd_runtime",
        lambda _warnings: (
            {"job.failed": ("-", 2)},
            {"job.disabled": True},
        ),
    )

    items = {item.name: item for item in census_mod.collect_launchd([])}
    assert (items["job.failed"].status, items["job.failed"].last_status) == (
        "active", "failed",
    )
    assert items["job.failed"].scheduler_last_exit_code == 2
    assert items["job.disabled"].status == "disabled"
    assert items["job.missing"].status == "missing"
    assert f"attention:command_missing:{census_mod.short_path(missing)}" in (
        items["job.missing"].notes
    )
    assert items["job.not-loaded"].status == "inactive"


def test_scheduler_result_supersedes_an_older_wrapper_receipt(census_mod):
    item = census_mod.Automation(
        id="launchd:job",
        name="job",
        source="launchd",
        kind="automation",
        scheduler="launchd",
        runner="shell",
        scheduler_state="loaded",
        scheduler_last_exit_code=0,
    )
    census_mod.apply_run_receipt(
        item,
        {
            "status": "failed",
            "timestamp": "2026-09-11T00:00:00Z",
            "exit_code": 1,
            "message": "old failure",
            "run_id": "old",
        },
    )
    assert (item.last_status, item.last_exit_code) == ("ok", 0)
    assert item.last_run is None and item.last_message is None and item.run_id is None
    assert "attention:receipt_disagrees_with_scheduler" in item.notes


def test_dead_running_receipt_is_stale_when_job_is_not_loaded(census_mod, monkeypatch):
    item = census_mod.Automation(
        id="launchd:job",
        name="job",
        source="launchd",
        kind="automation",
        scheduler="launchd",
        runner="shell",
        status="not_loaded",
        scheduler_state="not_loaded",
    )
    monkeypatch.setattr(census_mod, "pid_is_alive", lambda _pid: False)
    census_mod.apply_run_receipt(
        item,
        {"status": "running", "timestamp": "2026-09-29T00:00:00Z", "pid": 42},
    )
    assert item.last_status == "unknown"
    assert item.last_message == "running receipt is stale; completion outcome is unknown"
    assert "attention:stale_running_receipt" in item.notes


def test_dead_running_receipt_without_definition_becomes_attention_item(census_mod, monkeypatch):
    monkeypatch.setattr(census_mod, "pid_is_alive", lambda _pid: False)
    rows = census_mod.orphaned_stale_receipts(
        [],
        {
            "launchd:gone": {
                "status": "running",
                "pid": 42,
                "name": "gone",
                "source": "launchd",
                "kind": "automation",
                "timestamp": "2026-09-13T00:00:00Z",
            }
        },
    )
    assert len(rows) == 1
    assert (rows[0].status, rows[0].last_status, rows[0].scheduler) == (
        "orphaned", "unknown", "receipt-only",
    )


def test_launchctl_failure_degrades_to_config_only(census_mod, monkeypatch):
    monkeypatch.setattr(census_mod.sys, "platform", "darwin")

    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["launchctl", "list"], 5)

    monkeypatch.setattr(census_mod.subprocess, "run", fail)
    warnings: list[str] = []
    assert census_mod.launchd_runtime(warnings) == (None, {})
    assert warnings and "list unavailable" in warnings[0]
