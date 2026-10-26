#!/usr/bin/env python3
"""Coordinate a safe update of the packaged macOS Homebrew installation.

Homebrew installs immutable application kegs, but it does not own the UNITARES
database transition.  This command is the explicit operator boundary between
those two jobs: refresh and install the reviewed formulae, while UNITARES owns
writer quiescence, backup, migrations, restart and recovery evidence.

Formula hooks must never call this module.  It is intentionally interactive
unless ``--yes`` is supplied, and its journal makes an interrupted update
resumable by the newly installed keg.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile

try:  # Imported as a package in tests and as a script from an installed keg.
    from .homebrew_services import Homebrew, Refusal, WRITERS, run
except ImportError:  # pragma: no cover - exercised by installed-package CI
    from homebrew_services import Homebrew, Refusal, WRITERS, run


TAP = "cirwel/unitares"
FORMULAE = (
    "unitares",
    "unitares-lease-plane",
    "apache-age",
    "unitares-pgvector",
)
QUALIFIED_FORMULAE = tuple(f"{TAP}/{name}" for name in FORMULAE)
JOURNAL_VERSION = 1
BACKUP_INDEX_VERSION = 1
CLEANUP_JOURNAL_VERSION = 1
FINAL_STAGES = frozenset({"complete", "aborted"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target_version(formula: dict) -> str:
    stable = str(formula.get("versions", {}).get("stable") or "")
    revision = int(formula.get("revision") or 0)
    if not stable:
        raise Refusal("Homebrew returned a formula without a stable version.")
    return f"{stable}_{revision}" if revision else stable


class NativeUpdater:
    def __init__(self, brew: Homebrew):
        self.brew = brew
        self.state_dir = brew.prefix / "var/unitares"
        self.journal_path = self.state_dir / "native-update.json"
        self.backup_index_path = self.state_dir / "native-update-backups.json"
        self.cleanup_journal_path = self.state_dir / "native-update-cleanup.json"

    def _brew_process(self, *args: str, timeout: int = 1800, refresh: bool = False):
        env = dict(self.brew.env, HOMEBREW_NO_ANALYTICS="1")
        if refresh:
            env.pop("HOMEBREW_NO_AUTO_UPDATE", None)
        result = run([self.brew.brew, *args], env=env, timeout=timeout)
        if result.returncode:
            diagnostics = (result.stderr or result.stdout).strip()
            if diagnostics:
                print(f"Homebrew diagnostics:\n{diagnostics}", file=sys.stderr)
            raise Refusal(
                f"Homebrew {' '.join(args)} failed. Inspect the diagnostics above; "
                "if an update journal was created, continue with `unitares update --resume`."
            )
        return result

    def refresh_tap(self) -> None:
        print("… Refreshing Homebrew and tap metadata")
        self._brew_process("update", timeout=900, refresh=True)

    def inventory(self) -> dict[str, dict[str, object]]:
        result = self._brew_process(
            "info", "--json=v2", "--formula", *QUALIFIED_FORMULAE
        )
        try:
            formulae = json.loads(result.stdout)["formulae"]
            by_name = {item["name"]: item for item in formulae}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise Refusal(
                "Homebrew returned an unreadable UNITARES package inventory."
            ) from exc
        if set(by_name) != set(FORMULAE):
            raise Refusal(
                "Homebrew did not return the complete managed UNITARES package set."
            )
        inventory: dict[str, dict[str, object]] = {}
        for name in FORMULAE:
            formula = by_name[name]
            if formula.get("tap") != TAP:
                raise Refusal(f"{name} is not owned by the {TAP} tap.")
            installed = formula.get("installed") or []
            linked = str(formula.get("linked_keg") or "")
            if not installed or not linked:
                raise Refusal(
                    f"The managed {name} formula is not installed and linked."
                )
            inventory[name] = {
                "installed": linked,
                "target": _target_version(formula),
                "pinned": bool(formula.get("pinned")),
            }
        return inventory

    def repair_managed_links(self) -> None:
        """Repair the narrow interruption window between Homebrew unlink/link."""
        result = self._brew_process(
            "info", "--json=v2", "--formula", *QUALIFIED_FORMULAE
        )
        try:
            formulae = json.loads(result.stdout)["formulae"]
            by_name = {item["name"]: item for item in formulae}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise Refusal(
                "Homebrew returned an unreadable UNITARES package inventory."
            ) from exc
        if set(by_name) != set(FORMULAE):
            raise Refusal(
                "Homebrew did not return the complete managed UNITARES package set."
            )
        if len(FORMULAE) != len(QUALIFIED_FORMULAE):
            raise Refusal("The managed Homebrew formula configuration is inconsistent.")
        for name, qualified in zip(FORMULAE, QUALIFIED_FORMULAE):
            formula = by_name[name]
            if formula.get("tap") != TAP:
                raise Refusal(f"{name} is not owned by the {TAP} tap.")
            if not formula.get("installed"):
                raise Refusal(
                    f"The interrupted update left no installed {name} keg. Reinstall that managed formula, "
                    "then run `unitares update --resume`."
                )
            if not formula.get("linked_keg"):
                print(f"… Repairing interrupted Homebrew link for {name}")
                self._brew_process("link", qualified, timeout=300)

    @staticmethod
    def changes(inventory: dict[str, dict[str, object]]) -> dict[str, tuple[str, str]]:
        return {
            name: (str(item["installed"]), str(item["target"]))
            for name, item in inventory.items()
            if item["installed"] != item["target"]
        }

    def _secure_state_dir(self, *, create: bool) -> bool:
        if not os.path.lexists(self.state_dir):
            if not create:
                return False
            self.state_dir.mkdir(mode=0o700, parents=True)
        info = self.state_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise Refusal(f"{self.state_dir} must be a private directory owned by you.")
        return True

    def _write_private_json(self, path: Path, value: dict) -> None:
        self._secure_state_dir(create=True)
        fd, temporary = tempfile.mkstemp(prefix=".native-update-", dir=self.state_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(value, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def write_journal(self, state: dict) -> None:
        state["updated_at"] = _utc_now()
        self._write_private_json(self.journal_path, state)

    def read_journal(self, *, required: bool = True) -> dict | None:
        if not self._secure_state_dir(create=False):
            if required:
                raise Refusal("No interrupted native update is available to resume.")
            return None
        if not os.path.lexists(self.journal_path):
            if required:
                raise Refusal("No interrupted native update is available to resume.")
            return None
        info = self.journal_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise Refusal(
                f"{self.journal_path} must be a regular private file owned by you."
            )
        try:
            state = json.loads(self.journal_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise Refusal(
                "The native update journal is unreadable; preserve it and inspect before continuing."
            ) from exc
        if (
            not isinstance(state, dict)
            or state.get("journal_version") != JOURNAL_VERSION
            or not isinstance(state.get("stage"), str)
        ):
            raise Refusal("The native update journal has an unsupported format.")
        if state["stage"] not in FINAL_STAGES:
            required_fields = {
                "config_sha256": str,
                "target": dict,
                "writers_were_running": bool,
            }
            if any(
                not isinstance(state.get(key), kind)
                for key, kind in required_fields.items()
            ):
                raise Refusal(
                    "The native update journal is incomplete; preserve and inspect it."
                )
            target = state["target"]
            if set(target) != set(FORMULAE) or any(
                not isinstance(target[name], dict)
                or not isinstance(target[name].get("target"), str)
                or not isinstance(target[name].get("installed"), str)
                for name in FORMULAE
            ):
                raise Refusal("The native update journal has an invalid package plan.")
        return state

    def read_backup_index(self) -> dict:
        if not self._secure_state_dir(create=False) or not os.path.lexists(
            self.backup_index_path
        ):
            return {"index_version": BACKUP_INDEX_VERSION, "entries": []}
        info = self.backup_index_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise Refusal(
                f"{self.backup_index_path} must be a regular private file owned by you."
            )
        try:
            index = json.loads(self.backup_index_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise Refusal("The native backup index is unreadable; preserve and inspect it.") from exc
        entries = index.get("entries") if isinstance(index, dict) else None
        if (
            not isinstance(index, dict)
            or index.get("index_version") != BACKUP_INDEX_VERSION
            or not isinstance(entries, list)
        ):
            raise Refusal("The native backup index has an unsupported format.")
        required = {
            "path": str,
            "sha256": str,
            "size": int,
            "target": str,
            "verified_at": str,
        }
        if any(
            not isinstance(entry, dict)
            or any(not isinstance(entry.get(key), kind) for key, kind in required.items())
            or len(entry["sha256"]) != 64
            for entry in entries
        ):
            raise Refusal("The native backup index contains an invalid entry.")
        return index

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def record_backup(self, path: Path, target: str) -> None:
        index = self.read_backup_index()
        value = str(path)
        index["entries"] = [
            entry for entry in index["entries"] if entry["path"] != value
        ]
        index["entries"].append(
            {
                "path": value,
                "sha256": self._file_sha256(path),
                "size": path.stat().st_size,
                "target": target,
                "verified_at": _utc_now(),
            }
        )
        index["updated_at"] = _utc_now()
        self._write_private_json(self.backup_index_path, index)

    def read_cleanup_journal(self) -> dict | None:
        if not self._secure_state_dir(create=False) or not os.path.lexists(
            self.cleanup_journal_path
        ):
            return None
        info = self.cleanup_journal_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise Refusal(
                f"{self.cleanup_journal_path} must be a regular private file owned by you."
            )
        try:
            cleanup = json.loads(self.cleanup_journal_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise Refusal(
                "The native backup-cleanup journal is unreadable; preserve and inspect it."
            ) from exc
        if (
            not isinstance(cleanup, dict)
            or cleanup.get("journal_version") != CLEANUP_JOURNAL_VERSION
            or cleanup.get("stage") not in {"planned", "deleting", "complete"}
            or not isinstance(cleanup.get("candidates"), list)
            or not all(isinstance(value, str) for value in cleanup["candidates"])
            or not isinstance(cleanup.get("deleted", []), list)
            or not all(isinstance(value, str) for value in cleanup.get("deleted", []))
            or not isinstance(cleanup.get("retain"), int)
            or cleanup["retain"] < 1
            or not isinstance(cleanup.get("older_than_days"), int)
            or cleanup["older_than_days"] < 0
        ):
            raise Refusal("The native backup-cleanup journal has an invalid format.")
        return cleanup

    @staticmethod
    def _entry_time(entry: dict) -> datetime:
        try:
            value = datetime.fromisoformat(entry["verified_at"])
        except ValueError as exc:
            raise Refusal("The native backup index contains an invalid timestamp.") from exc
        if value.tzinfo is None:
            raise Refusal("The native backup index contains a timezone-free timestamp.")
        return value

    def _protected_backup_paths(self, *, retain: int, index: dict) -> set[str]:
        ordered = sorted(index["entries"], key=self._entry_time, reverse=True)
        protected = {entry["path"] for entry in ordered[:retain]}
        journal = self.read_journal(required=False)
        if journal:
            backup = journal.get("backup")
            if isinstance(backup, str):
                protected.add(backup)
            superseded = journal.get("superseded_backups", [])
            if isinstance(superseded, list):
                protected.update(value for value in superseded if isinstance(value, str))
        return protected

    def _verify_indexed_backup(self, entry: dict, backup_dir: Path) -> Path:
        path = Path(entry["path"])
        try:
            root = backup_dir.resolve(strict=True)
            resolved = path.resolve(strict=True)
            info = path.lstat()
        except OSError as exc:
            raise Refusal(f"Indexed backup is missing or inaccessible: {path}") from exc
        if resolved.parent != root:
            raise Refusal(f"Indexed backup escapes the configured backup directory: {path}")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size != entry["size"]
            or self._file_sha256(path) != entry["sha256"]
        ):
            raise Refusal(f"Indexed backup no longer matches its verified record: {path}")
        pg_restore = self.brew.prefix / "opt/postgresql@17/bin/pg_restore"
        try:
            result = subprocess.run(
                [str(pg_restore), "--list", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refusal(
                f"Indexed backup could not be checked with pg_restore: {path}"
            ) from exc
        if result.returncode:
            raise Refusal(f"Indexed backup no longer passes pg_restore verification: {path}")
        return path

    def cleanup_backups(
        self, *, retain: int = 3, older_than_days: int = 30, apply: bool = False
    ) -> int:
        if retain < 1:
            raise Refusal("Backup cleanup must retain at least one verified backup.")
        if older_than_days < 0:
            raise Refusal("Backup cleanup age cannot be negative.")
        update = self.read_journal(required=False)
        if update and update["stage"] not in FINAL_STAGES:
            raise Refusal(
                "Backup cleanup is blocked while a native update is unfinished. Resume or safely abort it first."
            )
        backup_dir = Path(
            os.environ.get(
                "UNITARES_BACKUP_DIR", str(Path.home() / ".unitares/backups")
            )
        ).expanduser()
        if not backup_dir.exists():
            print(f"No native backup directory exists at {backup_dir}.")
            return 0
        directory_info = backup_dir.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or directory_info.st_mode & 0o022
        ):
            raise Refusal(f"{backup_dir} must be a private directory owned by you.")

        index = self.read_backup_index()
        entries = {entry["path"]: entry for entry in index["entries"]}
        root = backup_dir.resolve(strict=True)
        verified_entries = []
        stale_names = set()
        outside_names = set()
        for entry in index["entries"]:
            path = Path(entry["path"])
            if not os.path.lexists(path):
                stale_names.add(entry["path"])
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise Refusal(
                    f"Indexed backup is inaccessible and cannot be classified safely: {path}"
                ) from exc
            if resolved.parent != root:
                outside_names.add(entry["path"])
                continue
            self._verify_indexed_backup(entry, backup_dir)
            verified_entries.append(entry)
        cleanup = self.read_cleanup_journal()
        if (
            apply
            and cleanup
            and cleanup["stage"] != "complete"
            and (
                retain != cleanup["retain"]
                or older_than_days != cleanup["older_than_days"]
            )
        ):
            if cleanup["stage"] == "deleting":
                raise Refusal(
                    "An interrupted backup cleanup must resume with its recorded "
                    f"policy: --retain-backups {cleanup['retain']} "
                    f"--older-than-days {cleanup['older_than_days']} --yes."
                )
            raise Refusal(
                "Backup-cleanup policy differs from the recorded plan; re-run the "
                "dry run with these options, then repeat it with --yes."
            )
        resume_plan = bool(
            cleanup
            and cleanup["stage"] != "complete"
            and (apply or cleanup["stage"] == "deleting")
        )
        policy_retain = cleanup["retain"] if resume_plan else retain
        policy_age = cleanup["older_than_days"] if resume_plan else older_than_days
        verified_index = {"entries": verified_entries}
        protected = self._protected_backup_paths(
            retain=policy_retain, index=verified_index
        )
        if resume_plan:
            assert cleanup is not None
            candidate_names = cleanup["candidates"]
            print(
                f"Using recorded cleanup policy: retain {policy_retain}, older than {policy_age} day(s)."
            )
            if cleanup["stage"] == "deleting" and not apply:
                print(
                    "An interrupted backup-cleanup plan is waiting; this dry run shows its remaining work."
                )
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=policy_age)
            candidate_names = [
                entry["path"]
                for entry in verified_entries
                if entry["path"] not in protected and self._entry_time(entry) <= cutoff
            ]
            candidate_names.extend(
                sorted(value for value in stale_names if value not in protected)
            )

        for value in candidate_names:
            if value not in entries:
                if not cleanup or value not in cleanup.get("deleted", []):
                    raise Refusal(
                        f"Cleanup journal references an unindexed backup: {value}"
                    )
                continue
            if value in protected:
                raise Refusal(f"Cleanup plan now names a protected backup: {value}")
            if not os.path.lexists(value):
                # A hard interruption can land after unlink but before the
                # deletion receipt is durably rewritten. A fresh stale index
                # record is also safe to reconcile. Existing changed files
                # still fail closed through full verification above.
                continue
            if value in outside_names:
                raise Refusal(
                    f"Cleanup plan names a backup outside the configured directory: {value}"
                )

        if not candidate_names:
            if not apply and not (
                cleanup and cleanup["stage"] == "deleting"
            ):
                cleanup = {
                    "journal_version": CLEANUP_JOURNAL_VERSION,
                    "stage": "planned",
                    "created_at": _utc_now(),
                    "candidates": [],
                    "deleted": [],
                    "retain": policy_retain,
                    "older_than_days": policy_age,
                }
                self._write_private_json(self.cleanup_journal_path, cleanup)
            elif apply and resume_plan:
                assert cleanup is not None
                cleanup["stage"] = "complete"
                cleanup["completed_at"] = _utc_now()
                self._write_private_json(self.cleanup_journal_path, cleanup)
            print(
                f"No indexed backups are eligible; retaining {len(verified_entries)} verified backup(s), including the newest {policy_retain} and anything newer than {policy_age} day(s)."
            )
            if outside_names:
                print(
                    f"Left {len(outside_names)} out-of-directory index record(s) untouched."
                )
            print("Homebrew kegs are not modified; Homebrew remains their cleanup owner.")
            return 0

        for value in candidate_names:
            if value in stale_names or not os.path.lexists(value):
                verb = "Remove stale index entry" if apply else "Would remove stale index entry"
            else:
                verb = "Remove" if apply else "Would remove"
            print(f"{verb}: {value}")
        if not apply:
            if not resume_plan:
                cleanup = {
                    "journal_version": CLEANUP_JOURNAL_VERSION,
                    "stage": "planned",
                    "created_at": _utc_now(),
                    "candidates": candidate_names,
                    "deleted": [],
                    "retain": policy_retain,
                    "older_than_days": policy_age,
                }
                self._write_private_json(self.cleanup_journal_path, cleanup)
            print("Dry run only. Re-run with --yes to delete exactly this eligible set.")
            print("Homebrew kegs are not modified; Homebrew remains their cleanup owner.")
            return 0

        if cleanup is None or cleanup["stage"] == "complete":
            cleanup = {
                "journal_version": CLEANUP_JOURNAL_VERSION,
                "stage": "planned",
                "created_at": _utc_now(),
                "candidates": candidate_names,
                "deleted": [],
                "retain": policy_retain,
                "older_than_days": policy_age,
            }
            self._write_private_json(self.cleanup_journal_path, cleanup)
        cleanup["stage"] = "deleting"
        self._write_private_json(self.cleanup_journal_path, cleanup)
        deleted = set(cleanup.get("deleted", []))
        for value in cleanup["candidates"]:
            if value in deleted:
                continue
            entry = entries.get(value)
            path = Path(value)
            if entry is not None and os.path.lexists(path):
                if value in self._protected_backup_paths(
                    retain=policy_retain, index=verified_index
                ):
                    raise Refusal(f"Cleanup candidate became protected: {value}")
                self._verify_indexed_backup(entry, backup_dir).unlink()
            deleted.add(value)
            cleanup["deleted"] = sorted(deleted)
            self._write_private_json(self.cleanup_journal_path, cleanup)

        index["entries"] = [
            entry for entry in index["entries"] if entry["path"] not in deleted
        ]
        index["updated_at"] = _utc_now()
        self._write_private_json(self.backup_index_path, index)
        cleanup["stage"] = "complete"
        cleanup["completed_at"] = _utc_now()
        self._write_private_json(self.cleanup_journal_path, cleanup)
        retained_verified = sum(
            entry["path"] not in deleted for entry in verified_entries
        )
        print(
            f"Removed or reconciled {len(deleted)} indexed backup(s); retained {retained_verified} verified backup(s)."
        )
        if outside_names:
            print(f"Left {len(outside_names)} out-of-directory index record(s) untouched.")
        print("Homebrew kegs were not modified; Homebrew remains their cleanup owner.")
        return 0

    def _database_value(self, sql: str) -> str:
        public_url, env = self.brew.database_environment()
        psql = self.brew.prefix / "opt/postgresql@17/bin/psql"
        result = run(
            [str(psql), "-X", "-Atq", "-v", "ON_ERROR_STOP=1", public_url, "-c", sql],
            env=env,
            timeout=30,
        )
        if result.returncode:
            raise Refusal("Could not inspect the configured UNITARES database.")
        return result.stdout.strip()

    def schema_version(self) -> int:
        value = self._database_value(
            "SELECT COALESCE(MAX(version), 0) FROM core.schema_migrations"
        )
        try:
            return int(value)
        except ValueError as exc:
            raise Refusal("The database returned an invalid schema version.") from exc

    def agent_count(self) -> int:
        value = self._database_value("SELECT count(*) FROM core.agents")
        try:
            return int(value)
        except ValueError as exc:
            raise Refusal("The database returned an invalid identity count.") from exc

    def verify_quiesced(self) -> None:
        records = self.brew.info()
        if any(
            records[name].get("running")
            or records[name].get("loaded")
            or records[name].get("registered")
            for name in WRITERS
        ):
            raise Refusal(
                "A Homebrew writer is still registered or running; no migration was attempted."
            )
        for name in WRITERS:
            port = self.brew.ports[name]
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    raise Refusal(
                        f"Port {port} still has a listener; no migration was attempted."
                    )
            except Refusal:
                raise
            except OSError:
                pass
        clients = self._database_value(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid<>pg_backend_pid() "
            "AND backend_type='client backend'"
        )
        if clients != "0":
            raise Refusal(
                f"The selected database still has {clients} other connection(s); "
                "stop their writers before resuming."
            )

    def backup_database(self, target: str, *, pristine: bool = True) -> Path:
        backup_dir = Path(
            os.environ.get(
                "UNITARES_BACKUP_DIR", str(Path.home() / ".unitares/backups")
            )
        ).expanduser()
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = backup_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise Refusal(f"{backup_dir} must be a private directory owned by you.")
        safe_target = "".join(
            char if char.isalnum() or char in "._-" else "-" for char in target
        )
        path = (
            backup_dir
            / f"unitares-{datetime.now().strftime('%Y%m%d-%H%M%S')}-before-{safe_target}.dump"
        )
        public_url, env = self.brew.database_environment()
        pg_dump = self.brew.prefix / "opt/postgresql@17/bin/pg_dump"
        pg_restore = self.brew.prefix / "opt/postgresql@17/bin/pg_restore"
        failure = (
            "The database backup failed, so no package or schema change was made."
            if pristine
            else "The refreshed database backup failed. The existing update state was preserved and no retry was attempted."
        )
        verification_failure = (
            "The database backup could not be verified, so no package or schema change was made."
            if pristine
            else "The refreshed database backup could not be verified. The existing update state was preserved and no retry was attempted."
        )
        created = False
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            created = True
            with os.fdopen(fd, "wb") as output:
                result = subprocess.run(
                    [str(pg_dump), "--format=custom", public_url],
                    env=env,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=600,
                )
            if result.returncode or not path.stat().st_size:
                path.unlink()
                created = False
                raise Refusal(failure)
            check = subprocess.run(
                [str(pg_restore), "--list", str(path)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            if check.returncode:
                path.unlink()
                created = False
                raise Refusal(verification_failure)
        except (OSError, subprocess.TimeoutExpired):
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise Refusal(failure)
        except KeyboardInterrupt:
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        try:
            self.record_backup(path, target)
        except (OSError, Refusal) as exc:
            consequence = (
                "No package or schema change was made"
                if pristine
                else "The existing update state was preserved and no retry was attempted"
            )
            raise Refusal(
                f"The database backup was verified at {path}, but its retention record could not be written. "
                f"{consequence}; preserve the backup and repair the private runtime directory."
            ) from exc
        print(f"✓ Verified database backup: {path}")
        return path

    def migration_plan(self) -> tuple[bool, str]:
        public_url, env = self.brew.database_environment()
        python = self.brew.prefix / "opt/unitares/libexec/venv/bin/python"
        script = self.brew.app / "scripts/dev/apply_migrations.py"
        result = run(
            [str(python), str(script), "--db-url", public_url], env=env, timeout=120
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        if result.returncode:
            raise Refusal("The installed migration planner refused the database state.")
        return "No pending migrations" not in output, output

    def apply_migrations(self) -> None:
        self.verify_quiesced()
        public_url, env = self.brew.database_environment()
        python = self.brew.prefix / "opt/unitares/libexec/venv/bin/python"
        script = self.brew.app / "scripts/dev/apply_migrations.py"
        result = run(
            [
                str(python),
                str(script),
                "--db-url",
                public_url,
                "--apply",
                "--dialectic-writers-quiesced",
            ],
            env=env,
            timeout=900,
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode:
            if result.stderr.strip():
                print(result.stderr.strip(), file=sys.stderr)
            raise Refusal(
                "Database migrations did not complete. The new package remains installed and both writers "
                "remain stopped; repair forward, then run `unitares update --resume`."
            )

    def managed_pins(self, action: str) -> None:
        for name in FORMULAE:
            result = run([self.brew.brew, action, name], env=self.brew.env, timeout=60)
            # unpin is allowed to report that a formula was not pinned; pin is
            # idempotent on current Homebrew. Other failures are actionable.
            if result.returncode and action == "pin":
                raise Refusal(
                    f"Could not pin {name}; pin the managed UNITARES packages before continuing."
                )

    def formula_state(self) -> dict[str, dict[str, object]]:
        result = self._brew_process(
            "info", "--json=v2", "--installed", "--formula", timeout=300
        )
        try:
            formulae = json.loads(result.stdout)["formulae"]
            return {
                str(item.get("full_name") or item["name"]): {
                    "linked_keg": str(item.get("linked_keg") or ""),
                    "installed": sorted(
                        str(version["version"]) for version in item["installed"]
                    ),
                }
                for item in formulae
            }
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise Refusal(
                "Homebrew returned an unreadable installed-formula inventory."
            ) from exc

    def _upgrade_packages(self) -> None:
        self._secure_state_dir(create=True)
        log_path = self.state_dir / "native-update-homebrew.log"
        env = dict(self.brew.env, HOMEBREW_NO_INSTALL_CLEANUP="1")
        try:
            fd = os.open(
                log_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "w") as output:
                result = subprocess.run(
                    [self.brew.brew, "upgrade", "--formula", *QUALIFIED_FORMULAE],
                    env=env,
                    text=True,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=3600,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refusal(
                f"The Homebrew package transaction did not finish; inspect {log_path}, "
                "then run `unitares update --resume`. If the command is no longer on PATH, run "
                "`$(brew --prefix)/opt/unitares/bin/unitares update --resume`."
            ) from exc
        if result.returncode:
            raise Refusal(
                f"The Homebrew package transaction failed; inspect {log_path}. Both writers remain "
                "stopped; then run `unitares update --resume`. If the command is no longer on PATH, run "
                "`$(brew --prefix)/opt/unitares/bin/unitares update --resume`."
            )

    def install_packages(self, target: dict[str, dict[str, object]]) -> None:
        self.managed_pins("unpin")
        try:
            self._upgrade_packages()
            installed = self.inventory()
            mismatches = [
                name
                for name in FORMULAE
                if installed[name]["installed"] != target[name]["target"]
            ]
            if mismatches:
                raise Refusal(
                    "Homebrew did not activate the planned versions for: "
                    + ", ".join(mismatches)
                )
        except BaseException as original:
            try:
                self.managed_pins("pin")
            except Refusal as pin_error:
                if isinstance(original, Refusal):
                    raise Refusal(
                        f"{original} Additionally, restoring managed pins failed: {pin_error}"
                    ) from original
                print(
                    f"warning: restoring managed pins failed: {pin_error}",
                    file=sys.stderr,
                )
            raise
        else:
            # Managed packages stay pinned between explicit UNITARES updates so
            # a broad `brew upgrade` cannot split the server/extension set.
            self.managed_pins("pin")

    def reexec(self) -> None:
        cli = self.brew.prefix / "bin/unitares"
        if not os.access(cli, os.X_OK):
            raise Refusal(
                "The upgraded package did not install an executable unitares CLI."
            )
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(
            cli,
            [str(cli), "update", "--resume", "--yes"],
            os.environ.copy(),
        )

    @staticmethod
    def _confirm(prompt: str, assume_yes: bool) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            raise Refusal("Not a terminal; re-run with --yes to proceed.")
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}

    def report(
        self, inventory: dict[str, dict[str, object]], *, local_metadata: bool
    ) -> None:
        print("Installation: Homebrew")
        for name in FORMULAE:
            item = inventory[name]
            suffix = (
                "" if item["installed"] == item["target"] else f" → {item['target']}"
            )
            print(f"  {name}: {item['installed']}{suffix}")
        if local_metadata:
            print(
                "Tap metadata: locally cached (the apply command refreshes it before changing packages)."
            )

    def begin(self, *, check: bool, assume_yes: bool) -> int:
        self.brew.configure()
        existing = self.read_journal(required=False)
        if existing and existing["stage"] not in FINAL_STAGES:
            raise Refusal(
                f"An update stopped at stage {existing['stage']!r}; run `unitares update --resume`."
            )
        if check:
            inventory = self.inventory()
            self.report(inventory, local_metadata=True)
            pending, plan = self.migration_plan()
            if plan:
                print(plan)
            if self.changes(inventory) or pending:
                print(
                    "Run `unitares update` to refresh the tap and perform the coordinated update."
                )
            else:
                print(
                    "✓ Installed packages and database schema match the locally cached tap."
                )
            return 0

        self.refresh_tap()
        inventory = self.inventory()
        self.report(inventory, local_metadata=False)
        changes = self.changes(inventory)
        pending, plan = self.migration_plan()
        if not changes and not pending:
            self.managed_pins("pin")
            print("✓ Already current; managed UNITARES packages are pinned.")
            return 0
        if plan:
            print(plan)
        target_label = str(inventory["unitares"]["target"])
        if not self._confirm(
            f"Stop both writers, make a verified backup, install {target_label}, apply migrations and verify health?",
            assume_yes,
        ):
            print("Nothing changed.")
            return 0

        records = self.brew.info()
        writer_states = [bool(records[name].get("running")) for name in WRITERS]
        if any(writer_states) and not all(writer_states):
            raise Refusal(
                "Only one UNITARES writer is running; reconcile the native services before updating."
            )
        state = {
            "journal_version": JOURNAL_VERSION,
            "created_at": _utc_now(),
            "stage": "planned",
            "target": inventory,
            "writers_were_running": all(writer_states),
            "config_sha256": hashlib.sha256(self.brew.config.read_bytes()).hexdigest(),
        }
        self.write_journal(state)
        return self.resume(state=state)

    def abort(self) -> int:
        state = self.read_journal(required=True)
        assert state is not None
        if state["stage"] in FINAL_STAGES:
            print(f"Native update journal is already {state['stage']}.")
            return 0
        if state["stage"] not in {"planned", "stopped", "backed_up"}:
            raise Refusal(
                "This update cannot be abandoned because packages or schema may have changed; repair forward with "
                "`unitares update --resume`."
            )
        self.brew.configure()
        if hashlib.sha256(self.brew.config.read_bytes()).hexdigest() != state.get(
            "config_sha256"
        ):
            raise Refusal(
                "Restore the configuration recorded for this update before abandoning it."
            )
        self.repair_managed_links()
        self.managed_pins("pin")
        live = self.inventory()
        changed = [
            name
            for name in FORMULAE
            if live[name]["installed"] != state["target"][name]["installed"]
        ]
        if changed:
            raise Refusal(
                "This update cannot be abandoned because installed versions changed for: "
                + ", ".join(changed)
                + ". Repair forward with `unitares update --resume`."
            )
        before = state.get("formula_state_before")
        if before is not None:
            if not isinstance(before, dict):
                raise Refusal(
                    "The update journal contains an unreadable Homebrew formula inventory."
                )
            after = self.formula_state()
            if after != before:
                state["formula_state_at_abort"] = after
                state["formula_changes_at_abort"] = sorted(
                    name
                    for name in set(before) | set(after)
                    if before.get(name) != after.get(name)
                )
        if "schema_before" in state and self.schema_version() != state["schema_before"]:
            raise Refusal(
                "This update cannot be abandoned because the schema version changed; repair forward with "
                "`unitares update --resume`."
            )
        state["stage"] = "aborted"
        state["aborted_at"] = _utc_now()
        state["abort_reason"] = "operator abandoned before package or schema change"
        self.write_journal(state)
        if state["writers_were_running"]:
            self.brew.start()
        else:
            print("Writers were stopped before the update and remain stopped.")
        print("Native update safely abandoned before any package or schema change.")
        return 0

    def resume(
        self,
        *,
        state: dict | None = None,
        accept_identity_change: bool = False,
        assume_yes: bool = False,
    ) -> int:
        loaded_from_journal = state is None
        state = state or self.read_journal(required=True)
        assert state is not None
        initial_stage = state["stage"]
        if state["stage"] in FINAL_STAGES:
            print(f"Native update journal is already {state['stage']}.")
            return 0
        self.brew.configure()
        if hashlib.sha256(self.brew.config.read_bytes()).hexdigest() != state.get(
            "config_sha256"
        ):
            raise Refusal(
                "The private native configuration changed during the update. Writers remain stopped; "
                "inspect the configuration and journal before resuming."
            )
        if accept_identity_change and state["stage"] != "verification_failed":
            raise Refusal(
                "--accept-identity-change is only valid for a recorded identity verification failure."
            )
        if state["stage"] == "verification_failed":
            self.verify_quiesced()
            if not accept_identity_change:
                raise Refusal(
                    "Identity count changed during the quiesced update. Inspect the database and backup, "
                    "then explicitly continue with `unitares update --resume --accept-identity-change`."
                )
            state["agent_count_accepted"] = self.agent_count()
            state["identity_change_accepted_at"] = _utc_now()
            state["stage"] = "migrated"
            self.write_journal(state)
        if state["stage"] == "planned":
            self.brew.stop()
            self.brew.configure()
            self.verify_quiesced()
            state["stage"] = "stopped"
            state["schema_before"] = self.schema_version()
            state["agent_count_before"] = self.agent_count()
            self.write_journal(state)
        if state["stage"] == "stopped":
            self.verify_quiesced()
            try:
                backup = self.backup_database(
                    str(state["target"]["unitares"]["target"])
                )
            except Refusal:
                state["stage"] = "aborted"
                self.write_journal(state)
                if state["writers_were_running"]:
                    self.brew.start()
                raise
            state["backup"] = str(backup)
            state["stage"] = "backed_up"
            self.write_journal(state)
        if state["stage"] == "backed_up":
            self.verify_quiesced()
            package_inventory = state["target"]
            if loaded_from_journal:
                # Tap metadata can change between any interrupted journal stage
                # and this recovery. Resolve it again before package changes and
                # require renewed consent for a different target.
                self.refresh_tap()
                self.repair_managed_links()
                refreshed = self.inventory()
                targets_changed = any(
                    refreshed[name]["target"] != state["target"][name]["target"]
                    for name in FORMULAE
                )
                self.report(refreshed, local_metadata=False)
                if targets_changed and not self._confirm(
                    "The tap target changed since this update began. Continue with the refreshed package set?",
                    assume_yes,
                ):
                    raise Refusal(
                        "Target change was not accepted; writers remain stopped. Review and run "
                        "`unitares update --resume` to confirm it, or `unitares update --abort` "
                        "to restore prior state while abort remains safe."
                    )
                if targets_changed:
                    state.setdefault("superseded_targets", []).append(
                        {
                            name: state["target"][name]["target"]
                            for name in FORMULAE
                        }
                    )
                for name in FORMULAE:
                    state["target"][name]["target"] = refreshed[name]["target"]
                package_inventory = {
                    name: {
                        **state["target"][name],
                        "installed": refreshed[name]["installed"],
                    }
                    for name in FORMULAE
                }
            if loaded_from_journal and initial_stage == "backed_up":
                # A prior package attempt may have been interrupted. Refresh
                # evidence immediately before retrying so even external writes
                # between attempts are included in the new backup.
                previous = state.get("backup")
                state["schema_before"] = self.schema_version()
                state["agent_count_before"] = self.agent_count()
                backup = self.backup_database(
                    str(state["target"]["unitares"]["target"]), pristine=False
                )
                if previous and previous != str(backup):
                    state.setdefault("superseded_backups", []).append(previous)
                state["backup"] = str(backup)
                self.write_journal(state)
            if self.changes(package_inventory):
                if "formula_state_before" not in state:
                    state["formula_state_before"] = self.formula_state()
                    self.write_journal(state)
                self.install_packages(state["target"])
                state["formula_state_after"] = self.formula_state()
                state["stage"] = "packages_installed"
                self.write_journal(state)
                self.reexec()
                raise AssertionError("reexec returned")
            self.managed_pins("pin")
            if "formula_state_before" in state:
                state["formula_state_after"] = self.formula_state()
            state["stage"] = "packages_installed"
            self.write_journal(state)
        if state["stage"] in {"packages_installed", "migration_failed"}:
            self.verify_quiesced()
            try:
                self.apply_migrations()
            except Refusal:
                state["stage"] = "migration_failed"
                state["schema_after"] = self.schema_version()
                self.write_journal(state)
                raise
            state["schema_after"] = self.schema_version()
            state["agent_count_after"] = self.agent_count()
            if state["agent_count_after"] != state["agent_count_before"]:
                state["stage"] = "verification_failed"
                self.write_journal(state)
                raise Refusal(
                    "Identity count changed during the quiesced update. Writers remain stopped; inspect the database and backup, "
                    "then explicitly continue with `unitares update --resume --accept-identity-change`."
                )
            state["stage"] = "migrated"
            self.write_journal(state)
        if state["stage"] in {"migrated", "restart_failed"}:
            if state["writers_were_running"]:
                try:
                    self.brew.start(allow_update_stage=True)
                except Refusal:
                    state["stage"] = "restart_failed"
                    self.write_journal(state)
                    raise
            else:
                self.brew.check_schema()
                print("Writers were stopped before the update and remain stopped.")
            state["stage"] = "complete"
            self.write_journal(state)
            print(
                f"✓ Native update complete. Schema {state['schema_before']} → {state['schema_after']}. "
                f"Verified backup: {state['backup']}"
            )
            return 0
        raise Refusal(f"Cannot resume unknown native update stage {state['stage']!r}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="report against locally cached tap metadata",
    )
    group.add_argument(
        "--resume", action="store_true", help="resume the durable native update journal"
    )
    group.add_argument(
        "--abort",
        action="store_true",
        help="abandon only if no managed package or schema version changed",
    )
    group.add_argument(
        "--cleanup-backups",
        action="store_true",
        help="dry-run indexed backup retention; add --yes to delete eligible backups",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="confirm the coordinated update non-interactively",
    )
    parser.add_argument(
        "--accept-identity-change",
        action="store_true",
        help="after inspection, explicitly accept a recorded identity-count change",
    )
    parser.add_argument(
        "--retain-backups",
        type=int,
        default=3,
        metavar="N",
        help="with --cleanup-backups, always retain at least the newest N indexed backups (default: 3)",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=30,
        metavar="N",
        help="with --cleanup-backups, only consider indexed backups at least N days old (default: 30)",
    )
    args = parser.parse_args(argv)
    if args.accept_identity_change and not args.resume:
        parser.error("--accept-identity-change requires --resume")
    if not args.cleanup_backups and (
        args.retain_backups != 3 or args.older_than_days != 30
    ):
        parser.error("--retain-backups and --older-than-days require --cleanup-backups")
    try:
        brew = Homebrew()
        updater = NativeUpdater(brew)
        if args.check:
            return updater.begin(check=True, assume_yes=args.yes)
        with brew.lock():
            if args.resume:
                return updater.resume(
                    accept_identity_change=args.accept_identity_change,
                    assume_yes=args.yes,
                )
            if args.abort:
                return updater.abort()
            if args.cleanup_backups:
                return updater.cleanup_backups(
                    retain=args.retain_backups,
                    older_than_days=args.older_than_days,
                    apply=args.yes,
                )
            return updater.begin(check=False, assume_yes=args.yes)
    except (Refusal, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
