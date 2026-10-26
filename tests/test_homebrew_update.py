"""The native updater keeps Homebrew packaging outside the database boundary."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.install import homebrew_update as update


@pytest.fixture
def updater(tmp_path, monkeypatch):
    brew = SimpleNamespace(
        prefix=tmp_path / "brew",
        app=tmp_path / "brew/opt/unitares/libexec/app",
        brew="brew",
        env={"PATH": "/usr/bin:/bin", "HOMEBREW_NO_AUTO_UPDATE": "1"},
        ports={"unitares": 8767, "unitares-lease-plane": 8788},
        config=tmp_path / "brew/etc/unitares/native.env",
    )
    brew.config.parent.mkdir(parents=True)
    brew.config.write_text("fixture=true\n")
    brew.config.chmod(0o600)
    brew.configure = lambda: None
    brew.database_environment = lambda: (
        "postgresql://probe@localhost:5432/governance",
        {"PATH": "/usr/bin:/bin", "PGPASSWORD": "s3cr3t-value"},
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return update.NativeUpdater(brew)


def _inventory(current="3.5.0_5", target="3.6.0"):
    return {
        name: {"installed": current, "target": target, "pinned": True}
        for name in update.FORMULAE
    }


def _config_hash(updater):
    return hashlib.sha256(updater.brew.config.read_bytes()).hexdigest()


def test_formula_inventory_includes_revisions_and_managed_extension_set(
    updater, monkeypatch
):
    payload = {
        "formulae": [
            {
                "name": name,
                "tap": update.TAP,
                "linked_keg": "3.5.0_5",
                "installed": [{"version": "3.5.0_5"}],
                "versions": {"stable": "3.5.0"},
                "revision": 6,
                "pinned": name in {"unitares", "unitares-lease-plane"},
            }
            for name in update.FORMULAE
        ]
    }
    monkeypatch.setattr(
        updater,
        "_brew_process",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    inventory = updater.inventory()
    assert set(inventory) == {
        "unitares",
        "unitares-lease-plane",
        "apache-age",
        "unitares-pgvector",
    }
    assert inventory["unitares"]["target"] == "3.5.0_6"
    assert inventory["apache-age"]["pinned"] is False


def test_recovery_repairs_managed_formula_left_unlinked(updater, monkeypatch):
    payload = {
        "formulae": [
            {
                "name": name,
                "tap": update.TAP,
                "linked_keg": "" if name == "unitares" else "3.5.0_5",
                "installed": [{"version": "3.5.0_5"}],
            }
            for name in update.FORMULAE
        ]
    }
    calls = []

    def brew_process(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr(updater, "_brew_process", brew_process)
    updater.repair_managed_links()
    assert calls == [
        ("info", "--json=v2", "--formula", *update.QUALIFIED_FORMULAE),
        ("link", "cirwel/unitares/unitares"),
    ]


def test_recovery_refuses_when_no_managed_keg_remains(updater, monkeypatch):
    payload = {
        "formulae": [
            {
                "name": name,
                "tap": update.TAP,
                "linked_keg": "" if name == "unitares" else "3.5.0_5",
                "installed": [] if name == "unitares" else [{"version": "3.5.0_5"}],
            }
            for name in update.FORMULAE
        ]
    }
    monkeypatch.setattr(
        updater,
        "_brew_process",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    with pytest.raises(update.Refusal, match="Reinstall that managed formula"):
        updater.repair_managed_links()


def test_update_journal_is_private_atomic_and_rejects_weak_mode(updater):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "config_sha256": _config_hash(updater),
        "target": _inventory(),
        "writers_were_running": True,
    }
    updater.write_journal(state)
    assert updater.journal_path.stat().st_mode & 0o777 == 0o600
    assert updater.read_journal()["stage"] == "backed_up"
    updater.journal_path.chmod(0o644)
    with pytest.raises(update.Refusal, match="private file"):
        updater.read_journal()


def test_backup_keeps_password_out_of_argv_and_verifies_custom_dump(
    updater, monkeypatch, tmp_path
):
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        if "--format=custom" in args:
            kwargs["stdout"].write(b"PGDMP fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update.subprocess, "run", execute)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(tmp_path / "backups"))
    backup = updater.backup_database("3.6.0")
    assert backup.read_bytes() == b"PGDMP fixture"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert any("--list" in args for args, _ in calls)
    assert all("s3cr3t-value" not in " ".join(map(str, args)) for args, _ in calls)
    assert calls[0][1]["env"]["PGPASSWORD"] == "s3cr3t-value"
    index = updater.read_backup_index()
    assert index["entries"][0]["path"] == str(backup)
    assert index["entries"][0]["sha256"] == hashlib.sha256(
        b"PGDMP fixture"
    ).hexdigest()
    assert updater.backup_index_path.stat().st_mode & 0o777 == 0o600


def test_backup_cleanup_is_dry_run_then_deletes_only_unprotected_indexed_files(
    updater, monkeypatch, tmp_path, capsys
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    paths = []
    for number in range(4):
        path = backup_dir / f"unitares-old-{number}.dump"
        path.write_bytes(f"PGDMP {number}".encode())
        path.chmod(0o600)
        updater.record_backup(path, "3.6.0")
        paths.append(path)
    index = updater.read_backup_index()
    for number, entry in enumerate(index["entries"]):
        entry["verified_at"] = f"2025-01-0{number + 1}T00:00:00+00:00"
    updater._write_private_json(updater.backup_index_path, index)
    monkeypatch.setattr(
        updater, "_verify_indexed_backup", lambda entry, root: Path(entry["path"])
    )

    assert updater.cleanup_backups(retain=2, older_than_days=0) == 0
    assert all(path.exists() for path in paths)
    plan = updater.read_cleanup_journal()
    assert plan["stage"] == "planned"
    assert plan["retain"] == 2
    assert plan["older_than_days"] == 0
    assert "Dry run only" in capsys.readouterr().out

    assert updater.cleanup_backups(retain=2, older_than_days=0, apply=True) == 0
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert paths[2].exists()
    assert paths[3].exists()
    assert [entry["path"] for entry in updater.read_backup_index()["entries"]] == [
        str(paths[2]),
        str(paths[3]),
    ]
    assert updater.read_cleanup_journal()["stage"] == "complete"


def test_backup_cleanup_preserves_current_journal_reference(
    updater, monkeypatch, tmp_path
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    paths = []
    for number in range(3):
        path = backup_dir / f"unitares-protected-{number}.dump"
        path.write_bytes(f"PGDMP {number}".encode())
        path.chmod(0o600)
        updater.record_backup(path, "3.6.0")
        paths.append(path)
    index = updater.read_backup_index()
    for number, entry in enumerate(index["entries"]):
        entry["verified_at"] = f"2025-01-0{number + 1}T00:00:00+00:00"
    updater._write_private_json(updater.backup_index_path, index)
    updater.write_journal(
        {
            "journal_version": update.JOURNAL_VERSION,
            "stage": "complete",
            "backup": str(paths[0]),
        }
    )
    monkeypatch.setattr(
        updater, "_verify_indexed_backup", lambda entry, root: Path(entry["path"])
    )
    assert updater.cleanup_backups(retain=1, older_than_days=0, apply=True) == 0
    assert paths[0].exists()
    assert not paths[1].exists()
    assert paths[2].exists()


def test_backup_cleanup_counts_only_existing_verified_backups_for_retention(
    updater, monkeypatch, tmp_path
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    paths = []
    for number in range(3):
        path = backup_dir / f"unitares-existing-{number}.dump"
        path.write_bytes(f"PGDMP {number}".encode())
        path.chmod(0o600)
        updater.record_backup(path, "3.6.0")
        paths.append(path)
    index = updater.read_backup_index()
    for number, entry in enumerate(index["entries"]):
        entry["verified_at"] = f"2025-01-0{number + 1}T00:00:00+00:00"
    updater._write_private_json(updater.backup_index_path, index)
    paths[2].unlink()
    monkeypatch.setattr(
        updater, "_verify_indexed_backup", lambda entry, root: Path(entry["path"])
    )

    assert updater.cleanup_backups(retain=1, older_than_days=0, apply=True) == 0
    assert not paths[0].exists()
    assert paths[1].exists()
    assert [entry["path"] for entry in updater.read_backup_index()["entries"]] == [
        str(paths[1])
    ]


def test_backup_cleanup_dry_run_then_reconciles_stale_index_entry(
    updater, monkeypatch, tmp_path, capsys
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    existing = backup_dir / "unitares-existing.dump"
    missing = backup_dir / "unitares-missing.dump"
    for path in (existing, missing):
        path.write_bytes(b"PGDMP")
        path.chmod(0o600)
        updater.record_backup(path, "3.6.0")
    missing.unlink()
    monkeypatch.setattr(
        updater, "_verify_indexed_backup", lambda entry, root: Path(entry["path"])
    )

    assert updater.cleanup_backups(retain=1, older_than_days=0) == 0
    assert "Would remove stale index entry" in capsys.readouterr().out
    assert updater.cleanup_backups(retain=1, older_than_days=0, apply=True) == 0
    assert existing.exists()
    assert [entry["path"] for entry in updater.read_backup_index()["entries"]] == [
        str(existing)
    ]


def test_backup_cleanup_apply_refuses_policy_different_from_recorded_plan(
    updater, monkeypatch, tmp_path
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    paths = []
    for number in range(3):
        path = backup_dir / f"unitares-plan-{number}.dump"
        path.write_bytes(f"PGDMP {number}".encode())
        path.chmod(0o600)
        updater.record_backup(path, "3.6.0")
        paths.append(path)
    index = updater.read_backup_index()
    for number, entry in enumerate(index["entries"]):
        entry["verified_at"] = f"2025-01-0{number + 1}T00:00:00+00:00"
    updater._write_private_json(updater.backup_index_path, index)
    monkeypatch.setattr(
        updater, "_verify_indexed_backup", lambda entry, root: Path(entry["path"])
    )

    assert updater.cleanup_backups(retain=1, older_than_days=0) == 0
    planned = updater.read_cleanup_journal()
    assert planned["candidates"] == [str(paths[0]), str(paths[1])]
    with pytest.raises(update.Refusal, match="policy differs"):
        updater.cleanup_backups(retain=3, older_than_days=365, apply=True)
    assert all(path.exists() for path in paths)


def test_backup_cleanup_empty_dry_run_replaces_old_plan_and_applies_nothing(
    updater, monkeypatch, tmp_path
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    paths = []
    for number in range(3):
        path = backup_dir / f"unitares-superseded-{number}.dump"
        path.write_bytes(f"PGDMP {number}".encode())
        path.chmod(0o600)
        updater.record_backup(path, "3.6.0")
        paths.append(path)
    index = updater.read_backup_index()
    for number, entry in enumerate(index["entries"]):
        entry["verified_at"] = f"2025-01-0{number + 1}T00:00:00+00:00"
    updater._write_private_json(updater.backup_index_path, index)
    monkeypatch.setattr(
        updater, "_verify_indexed_backup", lambda entry, root: Path(entry["path"])
    )

    assert updater.cleanup_backups(retain=1, older_than_days=0) == 0
    assert updater.read_cleanup_journal()["stage"] == "planned"
    assert updater.cleanup_backups(retain=3, older_than_days=365) == 0
    replacement = updater.read_cleanup_journal()
    assert replacement["stage"] == "planned"
    assert replacement["candidates"] == []
    index = updater.read_backup_index()
    index["entries"][0]["verified_at"] = "2020-01-01T00:00:00+00:00"
    updater._write_private_json(updater.backup_index_path, index)
    assert updater.cleanup_backups(retain=3, older_than_days=365, apply=True) == 0
    assert all(path.exists() for path in paths)
    assert updater.read_cleanup_journal()["stage"] == "complete"


def test_backup_cleanup_interrupted_policy_mismatch_names_recorded_options(
    updater, monkeypatch, tmp_path
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    updater._write_private_json(
        updater.cleanup_journal_path,
        {
            "journal_version": update.CLEANUP_JOURNAL_VERSION,
            "stage": "deleting",
            "candidates": [],
            "deleted": [],
            "retain": 2,
            "older_than_days": 45,
        },
    )
    with pytest.raises(
        update.Refusal,
        match=r"--retain-backups 2 --older-than-days 45 --yes",
    ):
        updater.cleanup_backups(retain=1, older_than_days=30, apply=True)


def test_backup_cleanup_pg_restore_timeout_is_refusal(updater, monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    path = backup_dir / "unitares-timeout.dump"
    path.write_bytes(b"PGDMP")
    path.chmod(0o600)
    entry = {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": updater._file_sha256(path),
    }

    def timeout(*args, **kwargs):
        raise update.subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(update.subprocess, "run", timeout)
    with pytest.raises(update.Refusal, match="could not be checked"):
        updater._verify_indexed_backup(entry, backup_dir)


def test_backup_cleanup_resumes_after_unlink_before_receipt(
    updater, monkeypatch, tmp_path, capsys
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    paths = []
    for number in range(2):
        path = backup_dir / f"unitares-resume-{number}.dump"
        path.write_bytes(f"PGDMP {number}".encode())
        path.chmod(0o600)
        updater.record_backup(path, "3.6.0")
        paths.append(path)
    index = updater.read_backup_index()
    index["entries"][0]["verified_at"] = "2025-01-01T00:00:00+00:00"
    updater._write_private_json(updater.backup_index_path, index)
    updater._write_private_json(
        updater.cleanup_journal_path,
        {
            "journal_version": update.CLEANUP_JOURNAL_VERSION,
            "stage": "deleting",
            "candidates": [str(paths[0])],
            "deleted": [],
            "retain": 1,
            "older_than_days": 0,
        },
    )
    paths[0].unlink()
    monkeypatch.setattr(
        updater, "_verify_indexed_backup", lambda entry, root: Path(entry["path"])
    )
    assert updater.cleanup_backups(retain=1, older_than_days=0) == 0
    output = capsys.readouterr().out
    assert "dry run shows its remaining work" in output
    assert "Would remove stale index entry" in output
    assert updater.cleanup_backups(retain=1, older_than_days=0, apply=True) == 0
    assert updater.read_cleanup_journal()["stage"] == "complete"
    assert [entry["path"] for entry in updater.read_backup_index()["entries"]] == [
        str(paths[1])
    ]


def test_backup_cleanup_blocks_while_update_is_unfinished(
    updater, monkeypatch, tmp_path
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    updater.write_journal(
        {
            "journal_version": update.JOURNAL_VERSION,
            "stage": "planned",
            "writers_were_running": False,
            "config_sha256": _config_hash(updater),
            "target": _inventory(),
        }
    )
    with pytest.raises(update.Refusal, match="unfinished"):
        updater.cleanup_backups()


def test_backup_name_collision_never_deletes_existing_dump(
    updater, monkeypatch, tmp_path
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(backup_dir))
    real_datetime = update.datetime

    class FixedDateTime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 10, 25, 22, 50, 0)

    monkeypatch.setattr(update, "datetime", FixedDateTime)
    stamp = "20261025-225000"
    existing = backup_dir / f"unitares-{stamp}-before-3.6.0.dump"
    existing.write_bytes(b"existing verified evidence")
    with pytest.raises(update.Refusal, match="backup failed"):
        updater.backup_database("3.6.0")
    assert existing.read_bytes() == b"existing verified evidence"


def test_retry_backup_failure_does_not_claim_packages_are_unchanged(
    updater, monkeypatch, tmp_path
):
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    with pytest.raises(update.Refusal, match="existing update state was preserved"):
        updater.backup_database("3.6.0", pristine=False)


def test_retry_backup_index_failure_does_not_claim_packages_are_unchanged(
    updater, monkeypatch, tmp_path
):
    monkeypatch.setenv("UNITARES_BACKUP_DIR", str(tmp_path / "backups"))

    def execute(args, **kwargs):
        if "--format=custom" in args:
            kwargs["stdout"].write(b"PGDMP fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update.subprocess, "run", execute)
    monkeypatch.setattr(
        updater,
        "record_backup",
        lambda *args: (_ for _ in ()).throw(update.Refusal("index failure")),
    )
    with pytest.raises(update.Refusal, match="existing update state was preserved"):
        updater.backup_database("3.6.0", pristine=False)


def test_package_transaction_disables_cleanup_and_repins_all_components(
    updater, monkeypatch
):
    calls = []
    inventory = _inventory()
    monkeypatch.setattr(updater, "managed_pins", lambda action: calls.append((action,)))
    monkeypatch.setattr(
        updater, "_upgrade_packages", lambda: calls.append(("upgrade",))
    )
    monkeypatch.setattr(
        updater,
        "inventory",
        lambda: {
            name: {**item, "installed": item["target"]}
            for name, item in inventory.items()
        },
    )

    updater.install_packages(inventory)
    assert calls[0] == ("unpin",)
    assert calls[1] == ("upgrade",)
    assert calls[-1] == ("pin",)


def test_package_failure_preserves_original_error_when_repin_also_fails(
    updater, monkeypatch
):
    actions = []

    def pins(action):
        actions.append(action)
        if action == "pin":
            raise update.Refusal("pin failure")

    monkeypatch.setattr(updater, "managed_pins", pins)
    monkeypatch.setattr(
        updater,
        "_upgrade_packages",
        lambda: (_ for _ in ()).throw(update.Refusal("upgrade failure")),
    )
    with pytest.raises(update.Refusal, match="upgrade failure.*pin failure"):
        updater.install_packages(_inventory())
    assert actions == ["unpin", "pin"]


def test_formula_state_records_every_installed_keg(updater, monkeypatch):
    payload = {
        "formulae": [
            {
                "name": "redis",
                "full_name": "redis",
                "linked_keg": "8.2.1",
                "installed": [{"version": "8.2.0"}, {"version": "8.2.1"}],
            },
            {
                "name": "unitares",
                "full_name": "cirwel/unitares/unitares",
                "linked_keg": "3.5.0_6",
                "installed": [{"version": "3.5.0_6"}],
            },
        ]
    }
    monkeypatch.setattr(
        updater,
        "_brew_process",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    assert updater.formula_state() == {
        "redis": {"linked_keg": "8.2.1", "installed": ["8.2.0", "8.2.1"]},
        "cirwel/unitares/unitares": {
            "linked_keg": "3.5.0_6",
            "installed": ["3.5.0_6"],
        },
    }


def test_brew_upgrade_is_logged_privately_without_cleanup(updater, monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        kwargs["stdout"].write("fixture build output\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update.subprocess, "run", execute)
    updater._upgrade_packages()
    command, options = calls[0]
    assert command[:3] == ["brew", "upgrade", "--formula"]
    assert set(command[3:]) == set(update.QUALIFIED_FORMULAE)
    assert options["env"]["HOMEBREW_NO_INSTALL_CLEANUP"] == "1"
    assert "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK" not in options["env"]
    log = updater.state_dir / "native-update-homebrew.log"
    assert log.read_text() == "fixture build output\n"
    assert log.stat().st_mode & 0o777 == 0o600


def test_reexec_flushes_console_output(updater, monkeypatch):
    cli = updater.brew.prefix / "bin/unitares"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o700)
    flushed = []
    monkeypatch.setattr(
        update.sys, "stdout", SimpleNamespace(flush=lambda: flushed.append("stdout"))
    )
    monkeypatch.setattr(
        update.sys, "stderr", SimpleNamespace(flush=lambda: flushed.append("stderr"))
    )

    def execve(path, argv, env):
        assert path == cli
        assert argv == [str(cli), "update", "--resume", "--yes"]
        assert "UNITARES_NATIVE_UPDATE_REEXEC" not in env
        raise SystemExit

    monkeypatch.setattr(update.os, "execve", execve)
    with pytest.raises(SystemExit):
        updater.reexec()
    assert flushed == ["stdout", "stderr"]


def test_quiescence_counts_only_client_backends(updater, monkeypatch):
    updater.brew.info = lambda: {
        name: {"running": False, "loaded": False, "registered": False}
        for name in update.WRITERS
    }
    monkeypatch.setattr(
        update.socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )
    queries = []

    def database_value(sql):
        queries.append(sql)
        return "0"

    monkeypatch.setattr(updater, "_database_value", database_value)
    updater.verify_quiesced()
    assert "backend_type='client backend'" in queries[-1]


def test_check_is_read_only_and_does_not_refresh_tap(updater, monkeypatch, capsys):
    monkeypatch.setattr(updater.brew, "configure", lambda: None)
    monkeypatch.setattr(
        updater, "inventory", lambda: _inventory(current="3.6.0", target="3.6.0")
    )
    monkeypatch.setattr(
        updater, "migration_plan", lambda: (False, "No pending migrations")
    )
    monkeypatch.setattr(
        updater, "refresh_tap", lambda: pytest.fail("--check refreshed the tap")
    )
    assert updater.begin(check=True, assume_yes=False) == 0
    output = capsys.readouterr().out
    assert "locally cached" in output
    assert "match the locally cached tap" in output
    assert not updater.state_dir.exists()


def test_failed_migration_records_resume_stage_and_never_starts(updater, monkeypatch):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "packages_installed",
        "writers_were_running": True,
        "agent_count_before": 4,
        "schema_before": 76,
        "backup": "/private/backup.dump",
        "target": _inventory(),
        "config_sha256": _config_hash(updater),
    }
    written = []
    started = []
    monkeypatch.setattr(updater.brew, "configure", lambda: None)
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(
        updater,
        "apply_migrations",
        lambda: (_ for _ in ()).throw(update.Refusal("migration failed")),
    )
    monkeypatch.setattr(updater, "schema_version", lambda: 77)
    monkeypatch.setattr(
        updater, "write_journal", lambda value: written.append(dict(value))
    )
    updater.brew.start = lambda: started.append(True)
    with pytest.raises(update.Refusal, match="migration failed"):
        updater.resume(state=state)
    assert written[-1]["stage"] == "migration_failed"
    assert written[-1]["schema_after"] == 77
    assert started == []


def test_successful_resume_preserves_identities_and_previous_stopped_state(
    updater, monkeypatch
):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "packages_installed",
        "writers_were_running": False,
        "agent_count_before": 4,
        "schema_before": 76,
        "backup": "/private/backup.dump",
        "target": _inventory(),
        "config_sha256": _config_hash(updater),
    }
    written = []
    monkeypatch.setattr(updater.brew, "configure", lambda: None)
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(updater, "apply_migrations", lambda: None)
    monkeypatch.setattr(updater, "schema_version", lambda: 78)
    monkeypatch.setattr(updater, "agent_count", lambda: 4)
    monkeypatch.setattr(
        updater, "write_journal", lambda value: written.append(dict(value))
    )
    updater.brew.check_schema = lambda: None
    updater.brew.start = lambda **kwargs: pytest.fail(
        "previously stopped writers were started"
    )
    assert updater.resume(state=state) == 0
    assert written[-1]["stage"] == "complete"
    assert written[-1]["schema_after"] == 78


def test_resume_from_backed_up_refreshes_backup_and_baselines(updater, monkeypatch):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "writers_were_running": False,
        "agent_count_before": 3,
        "schema_before": 75,
        "backup": "/private/old.dump",
        "target": _inventory(current="3.6.0", target="3.6.0"),
        "config_sha256": _config_hash(updater),
    }
    written = []
    refreshed = []
    monkeypatch.setattr(updater, "refresh_tap", lambda: refreshed.append(True))
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(
        updater,
        "inventory",
        lambda: _inventory(current="3.6.0", target="3.6.0"),
    )
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(updater, "schema_version", lambda: 76)
    monkeypatch.setattr(updater, "agent_count", lambda: 4)
    monkeypatch.setattr(
        updater,
        "backup_database",
        lambda target, **kwargs: Path("/private/refreshed.dump"),
    )
    monkeypatch.setattr(updater, "apply_migrations", lambda: None)
    monkeypatch.setattr(
        updater, "write_journal", lambda value: written.append(dict(value))
    )
    pins = []
    monkeypatch.setattr(updater, "managed_pins", lambda action: pins.append(action))
    updater.brew.check_schema = lambda: None
    updater.brew.start = lambda **kwargs: pytest.fail("stopped writers were started")
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    assert updater.resume() == 0
    assert refreshed == [True]
    assert written[0]["backup"] == "/private/refreshed.dump"
    assert written[0]["superseded_backups"] == ["/private/old.dump"]
    assert written[0]["agent_count_before"] == 4
    assert pins == ["pin"]


def test_identity_verification_failure_requires_explicit_acceptance(
    updater, monkeypatch
):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "verification_failed",
        "writers_were_running": True,
        "agent_count_before": 3,
        "agent_count_after": 4,
        "schema_before": 75,
        "schema_after": 76,
        "backup": "/private/backup.dump",
        "target": _inventory(),
        "config_sha256": _config_hash(updater),
    }
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(updater, "agent_count", lambda: 4)
    written = []
    monkeypatch.setattr(
        updater, "write_journal", lambda value: written.append(dict(value))
    )
    updater.brew.start = lambda **kwargs: None
    with pytest.raises(update.Refusal, match="accept-identity-change"):
        updater.resume(state=dict(state))
    assert updater.resume(state=state, accept_identity_change=True) == 0
    assert written[0]["stage"] == "migrated"
    assert written[-1]["stage"] == "complete"


def test_abort_restores_prior_writer_state_only_before_package_change(
    updater, monkeypatch
):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "writers_were_running": True,
        "schema_before": 76,
        "backup": "/private/backup.dump",
        "target": _inventory(),
        "config_sha256": _config_hash(updater),
    }
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(updater, "inventory", lambda: _inventory())
    monkeypatch.setattr(updater, "schema_version", lambda: 76)
    written = []
    monkeypatch.setattr(
        updater, "write_journal", lambda value: written.append(dict(value))
    )
    pins = []
    monkeypatch.setattr(updater, "managed_pins", lambda action: pins.append(action))
    starts = []
    updater.brew.start = lambda: starts.append(True)
    assert updater.abort() == 0
    assert written[-1]["stage"] == "aborted"
    assert pins == ["pin"]
    assert starts == [True]


def test_abort_refuses_after_any_managed_package_changed(updater, monkeypatch):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "writers_were_running": True,
        "schema_before": 76,
        "backup": "/private/backup.dump",
        "target": _inventory(),
        "config_sha256": _config_hash(updater),
    }
    live = _inventory()
    live["unitares"]["installed"] = "3.6.0"
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(updater, "managed_pins", lambda action: None)
    monkeypatch.setattr(updater, "inventory", lambda: live)
    with pytest.raises(update.Refusal, match="installed versions changed"):
        updater.abort()


def test_abort_records_dependency_changes_and_restores_writers(updater, monkeypatch):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "writers_were_running": True,
        "schema_before": 76,
        "backup": "/private/backup.dump",
        "target": _inventory(),
        "config_sha256": _config_hash(updater),
        "formula_state_before": {
            "redis": {"linked_keg": "8.2.0", "installed": ["8.2.0"]}
        },
    }
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(updater, "managed_pins", lambda action: None)
    monkeypatch.setattr(updater, "inventory", lambda: _inventory())
    monkeypatch.setattr(
        updater,
        "formula_state",
        lambda: {"redis": {"linked_keg": "8.2.1", "installed": ["8.2.1"]}},
    )
    monkeypatch.setattr(updater, "schema_version", lambda: 76)
    written = []
    monkeypatch.setattr(
        updater, "write_journal", lambda value: written.append(dict(value))
    )
    started = []
    updater.brew.start = lambda: started.append(True)
    assert updater.abort() == 0
    assert written[-1]["stage"] == "aborted"
    assert written[-1]["formula_changes_at_abort"] == ["redis"]
    assert started == [True]


def test_resume_from_backed_up_adopts_repaired_tap_revision(updater, monkeypatch):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "writers_were_running": False,
        "agent_count_before": 3,
        "schema_before": 75,
        "backup": "/private/old.dump",
        "target": _inventory(current="3.5.0_5", target="3.6.0"),
        "config_sha256": _config_hash(updater),
    }
    repaired = _inventory(current="3.5.0_5", target="3.6.0_1")
    written = []
    monkeypatch.setattr(updater, "refresh_tap", lambda: None)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(updater, "inventory", lambda: repaired)
    monkeypatch.setattr(updater, "schema_version", lambda: 75)
    monkeypatch.setattr(updater, "agent_count", lambda: 3)
    monkeypatch.setattr(
        updater,
        "backup_database",
        lambda target, **kwargs: Path(f"/private/before-{target}.dump"),
    )
    monkeypatch.setattr(updater, "formula_state", lambda: {"redis": {}})
    installed = []

    def install(target):
        installed.append(target)

    monkeypatch.setattr(updater, "install_packages", install)
    monkeypatch.setattr(updater, "reexec", lambda: (_ for _ in ()).throw(SystemExit))
    monkeypatch.setattr(
        updater, "write_journal", lambda value: written.append(dict(value))
    )
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    with pytest.raises(SystemExit):
        updater.resume(assume_yes=True)
    assert installed[0]["unitares"]["target"] == "3.6.0_1"
    assert state["superseded_targets"][0]["unitares"] == "3.6.0"
    assert state["target"]["unitares"]["installed"] == "3.5.0_5"
    assert written[-1]["stage"] == "packages_installed"


def test_resume_requires_confirmation_when_tap_target_changes(updater, monkeypatch):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "writers_were_running": False,
        "agent_count_before": 3,
        "schema_before": 75,
        "backup": "/private/old.dump",
        "target": _inventory(current="3.5.0_5", target="3.6.0"),
        "config_sha256": _config_hash(updater),
    }
    monkeypatch.setattr(updater, "refresh_tap", lambda: None)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(
        updater,
        "inventory",
        lambda: _inventory(current="3.5.0_5", target="3.6.0_1"),
    )
    monkeypatch.setattr(updater, "_confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        updater,
        "backup_database",
        lambda *args, **kwargs: pytest.fail("backup ran before target approval"),
    )
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    with pytest.raises(update.Refusal, match="update --abort"):
        updater.resume()
    assert state["target"]["unitares"]["target"] == "3.6.0"


@pytest.mark.parametrize("stage", ["planned", "stopped"])
def test_earlier_resume_stages_also_require_changed_target_confirmation(
    updater, monkeypatch, stage
):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": stage,
        "writers_were_running": False,
        "target": _inventory(current="3.5.0_5", target="3.6.0"),
        "config_sha256": _config_hash(updater),
    }
    updater.brew.stop = lambda: None
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(updater, "schema_version", lambda: 75)
    monkeypatch.setattr(updater, "agent_count", lambda: 3)
    monkeypatch.setattr(
        updater, "backup_database", lambda *args, **kwargs: Path("/private/new.dump")
    )
    monkeypatch.setattr(updater, "refresh_tap", lambda: None)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(
        updater,
        "inventory",
        lambda: _inventory(current="3.5.0_5", target="3.6.0_1"),
    )
    monkeypatch.setattr(updater, "_confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    monkeypatch.setattr(updater, "write_journal", lambda value: None)
    monkeypatch.setattr(
        updater,
        "install_packages",
        lambda target: pytest.fail("packages changed without renewed consent"),
    )
    with pytest.raises(update.Refusal, match="update --abort"):
        updater.resume()
    assert state["target"]["unitares"]["target"] == "3.6.0"


def test_resume_uses_live_versions_when_deciding_to_install(updater, monkeypatch):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "backed_up",
        "writers_were_running": False,
        "agent_count_before": 3,
        "schema_before": 75,
        "backup": "/private/old.dump",
        "target": _inventory(current="3.5.0_5", target="3.6.0"),
        "config_sha256": _config_hash(updater),
    }
    live = _inventory(current="3.5.0_5", target="3.5.0_5")
    live["unitares-lease-plane"]["installed"] = "3.6.0"
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    monkeypatch.setattr(updater, "verify_quiesced", lambda: None)
    monkeypatch.setattr(updater, "refresh_tap", lambda: None)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(updater, "inventory", lambda: live)
    monkeypatch.setattr(updater, "schema_version", lambda: 75)
    monkeypatch.setattr(updater, "agent_count", lambda: 3)
    monkeypatch.setattr(
        updater, "backup_database", lambda *args, **kwargs: Path("/private/new.dump")
    )
    monkeypatch.setattr(updater, "formula_state", lambda: {"redis": {}})
    attempted = []

    def install(target):
        attempted.append(target)
        raise SystemExit

    monkeypatch.setattr(updater, "install_packages", install)
    monkeypatch.setattr(updater, "write_journal", lambda value: None)
    with pytest.raises(SystemExit):
        updater.resume(assume_yes=True)
    assert attempted
    assert state["target"]["unitares"]["target"] == "3.5.0_5"


def test_resume_from_planned_retries_stop_backup_and_finishes(
    updater, monkeypatch, tmp_path
):
    state = {
        "journal_version": update.JOURNAL_VERSION,
        "stage": "planned",
        "writers_were_running": False,
        "config_sha256": _config_hash(updater),
        "target": _inventory(current="3.6.0", target="3.6.0"),
    }
    calls = []
    versions = iter((76, 78))
    counts = iter((4, 4))
    updater.brew.stop = lambda: calls.append("stop")
    updater.brew.check_schema = lambda: calls.append("schema-check")
    monkeypatch.setattr(updater, "verify_quiesced", lambda: calls.append("quiesced"))
    monkeypatch.setattr(updater, "schema_version", lambda: next(versions))
    monkeypatch.setattr(updater, "agent_count", lambda: next(counts))
    monkeypatch.setattr(
        updater, "backup_database", lambda target: tmp_path / f"before-{target}.dump"
    )
    monkeypatch.setattr(updater, "apply_migrations", lambda: calls.append("migrate"))
    monkeypatch.setattr(updater, "managed_pins", lambda action: calls.append(action))
    monkeypatch.setattr(updater, "refresh_tap", lambda: None)
    monkeypatch.setattr(updater, "repair_managed_links", lambda: None)
    monkeypatch.setattr(
        updater,
        "inventory",
        lambda: _inventory(current="3.6.0", target="3.6.0"),
    )
    monkeypatch.setattr(updater, "read_journal", lambda required=True: state)
    monkeypatch.setattr(
        updater, "write_journal", lambda value: calls.append(value["stage"])
    )
    assert updater.resume() == 0
    assert calls == [
        "stop",
        "quiesced",
        "stopped",
        "quiesced",
        "backed_up",
        "quiesced",
        "pin",
        "packages_installed",
        "quiesced",
        "migrate",
        "migrated",
        "schema-check",
        "complete",
    ]
