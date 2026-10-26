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
    assert options["env"]["HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK"] == "1"
    log = updater.state_dir / "native-update-homebrew.log"
    assert log.read_text() == "fixture build output\n"
    assert log.stat().st_mode & 0o777 == 0o600


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
    assert "Already" not in output or "match" in output
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
    updater.brew.start = lambda: pytest.fail("previously stopped writers were started")
    assert updater.resume(state=state) == 0
    assert written[-1]["stage"] == "complete"
    assert written[-1]["schema_after"] == 78


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
    monkeypatch.setattr(
        updater, "write_journal", lambda value: calls.append(value["stage"])
    )
    assert updater.resume(state=state) == 0
    assert calls == [
        "stop",
        "quiesced",
        "stopped",
        "quiesced",
        "backed_up",
        "quiesced",
        "packages_installed",
        "quiesced",
        "migrate",
        "migrated",
        "schema-check",
        "complete",
    ]
