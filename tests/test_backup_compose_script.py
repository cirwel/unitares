"""Hermetic tests for scripts/ops/backup_compose.sh."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "scripts" / "ops" / "backup_compose.sh"
HEALTH_SCRIPT = ROOT / "scripts" / "ops" / "check_governance_backup_health.sh"


def _write_fake_docker(bin_dir: Path, state_dir: Path) -> Path:
    fake_docker = bin_dir / "fake-docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env python3
import io, os, sys, tarfile
from pathlib import Path

state = Path({str(state_dir)!r})
args = sys.argv[1:]

if args[0] == "exec":
    idx = 1
    while idx < len(args) and args[idx].startswith("-"):
        if args[idx] == "-e":
            idx += 2
        else:
            idx += 1
    container = args[idx]
    cmd = args[idx + 1 :]
    if cmd[0] == "pg_isready":
        sys.exit(0)
    if cmd[0] == "pg_dump":
        mode = os.environ.get("FAKE_PG_MODE", "ok")
        if mode == "truncated":
            sys.stdout.write("-- partial SQL dump without completion trailer\\n")
            sys.exit(0)
        if mode == "fail":
            sys.exit(1)
        sys.stdout.write(
            "-- PostgreSQL database dump\\n"
            "SELECT 1;\\n"
            "-- PostgreSQL database dump complete\\n"
        )
        sys.exit(0)
    if cmd[0] == "redis-cli":
        rcmd = cmd[1:]
        lastsave_file = state / "lastsave"
        if not lastsave_file.exists():
            lastsave_file.write_text("1700000000")
        if rcmd == ["LASTSAVE"]:
            print(lastsave_file.read_text().strip())
            sys.exit(0)
        if rcmd == ["BGSAVE"]:
            if os.environ.get("FAKE_REDIS_MODE") == "err":
                print("Background saving started")
                sys.exit(0)
            lastsave_file.write_text(str(int(lastsave_file.read_text().strip()) + 5))
            print("Background saving started")
            sys.exit(0)
        if rcmd == ["INFO", "persistence"]:
            status = "err" if os.environ.get("FAKE_REDIS_MODE") == "err" else "ok"
            print(f"rdb_bgsave_in_progress:0\\r\\naof_rewrite_in_progress:0\\r\\nrdb_last_bgsave_status:{{status}}\\r\\n")
            sys.exit(0)
        if rcmd[:2] == ["CONFIG", "GET"]:
            key = rcmd[2]
            val = "/data" if key == "dir" else "dump.rdb"
            print(f"{{key}}\\n{{val}}")
            sys.exit(0)
    if cmd[0] == "redis-check-rdb":
        if os.environ.get("FAKE_REDIS_RDB_MODE") == "corrupt_crc":
            sys.exit(1)
        sys.exit(0)
    if cmd[0] == "python":
        data_mode = os.environ.get("FAKE_DATA_MODE", "ok")
        if data_mode == "fail":
            sys.exit(2)
        if data_mode == "empty":
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w:gz"):
                pass
            sys.exit(0)
        app_data = state / "fake_app_data"
        (app_data / "secrets").mkdir(parents=True, exist_ok=True)
        (app_data / "secrets" / "continuity_token_secret").write_text("secret-seed")
        (app_data / "hf" / "models--BAAI--bge-m3").mkdir(parents=True, exist_ok=True)
        (app_data / "hf" / "models--BAAI--bge-m3" / "weights.bin").write_bytes(b"x" * 1024)
        # Execute the streamed script against fake_app_data instead of /app/data.
        snippet = cmd[2].replace('"/app/data"', repr(str(app_data)))
        exec(compile(snippet, "<container-python>", "exec"), {{}})
        sys.exit(0)
    sys.exit(2)

if args[0] == "cp":
    src, dst = args[1], Path(args[2])
    if src.endswith("/dump.rdb"):
        rdb_mode = os.environ.get("FAKE_REDIS_RDB_MODE", "ok")
        if rdb_mode == "cp_fail":
            sys.exit(1)
        if rdb_mode == "truncated":
            dst.write_bytes(b"REDIS0011partial-without-eof")
            sys.exit(0)
        # Valid RDB: 9-byte header + payload + 0xFF EOF opcode + 8-byte CRC64 footer.
        dst.write_bytes(b"REDIS0011\\xfe\\x00\\xff" + b"\\x00" * 8)
        sys.exit(0)
    sys.exit(3)

sys.exit(4)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return fake_docker


def _run_backup(tmp_path: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    backup_dir = tmp_path / "backups"
    bin_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    fake_docker = _write_fake_docker(bin_dir, state_dir)

    env = {
        **os.environ,
        "DOCKER_BIN": str(fake_docker),
        "BACKUP_DIR": str(backup_dir),
        "DUMP_RETRIES": "1",
        "DUMP_RETRY_SLEEP_SEC": "0",
        "REDIS_BGSAVE_TIMEOUT_SEC": "3",
    }
    for key in (
        "AGE_RECIPIENT",
        "AGE_RECIPIENT_FILE",
        "BACKUP_KEEP_PLAINTEXT",
        "PURGE_PRIOR_PLAINTEXT",
        "BACKUP_SYNC_CMD",
    ):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd="/",  # Prove cron invocation outside repo cwd works.
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_health(backup_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HEALTH_SCRIPT)],
        env={**os.environ, "BACKUP_DIR": str(backup_dir)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_backup_compose_captures_all_three_stores_and_satisfies_health_check(tmp_path: Path):
    res = _run_backup(tmp_path)
    assert res.returncode == 0, res.stderr

    backup_dir = tmp_path / "backups"
    pg_files = list(backup_dir.glob("governance_*.sql.gz"))
    redis_files = list(backup_dir.glob("redis_*.rdb"))
    data_files = list(backup_dir.glob("governance_data_*.tar.gz"))
    manifests = list(backup_dir.glob("manifest_*.sha256"))

    assert len(pg_files) == 1
    assert len(redis_files) == 1
    assert len(data_files) == 1
    assert len(manifests) == 1

    for artifact in (*pg_files, *redis_files, *data_files, *manifests):
        mode = stat.S_IMODE(artifact.stat().st_mode)
        assert mode == 0o600, f"{artifact.name} expected 0600, got {oct(mode)}"

    with tarfile.open(data_files[0], "r:gz") as tar:
        names = tar.getnames()
    assert "data/secrets/continuity_token_secret" in names
    assert not any(n.startswith("data/hf") for n in names), names

    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["status"] == "ok"
    assert status["file"] == str(pg_files[0])

    health = _run_health(backup_dir)
    assert health.returncode == 0, health.stderr


def test_backup_compose_rejects_truncated_pg_dump(tmp_path: Path):
    res = _run_backup(tmp_path, {"FAKE_PG_MODE": "truncated"})
    assert res.returncode != 0

    backup_dir = tmp_path / "backups"
    assert not (backup_dir / "last_backup_success.txt").exists()
    assert list(backup_dir.glob("governance_*.sql.gz")) == []
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["status"] == "error"
    assert _run_health(backup_dir).returncode != 0


def test_backup_compose_rejects_failed_redis_bgsave_and_health_check_fails(tmp_path: Path):
    """First-ever run failing after Postgres must not leave a staged .sql.gz
    behind in $BACKUP_DIR that fools check_governance_backup_health.sh."""
    res = _run_backup(tmp_path, {"FAKE_REDIS_MODE": "err"})
    assert res.returncode != 0

    backup_dir = tmp_path / "backups"
    assert not (backup_dir / "last_backup_success.txt").exists()
    assert list(backup_dir.glob("governance_*.sql.gz")) == []
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["status"] == "error"
    assert _run_health(backup_dir).returncode != 0


def test_backup_compose_rejects_truncated_redis_rdb(tmp_path: Path):
    for bad_rdb in ("truncated", "corrupt_crc"):
        sub = tmp_path / bad_rdb
        sub.mkdir()
        res = _run_backup(sub, {"FAKE_REDIS_RDB_MODE": bad_rdb})
        assert res.returncode != 0

        backup_dir = sub / "backups"
        assert list(backup_dir.glob("redis_*.rdb")) == []
        assert list(backup_dir.glob("governance_*.sql.gz")) == []
        assert _run_health(backup_dir).returncode != 0


def test_backup_compose_rejects_governance_data_failure(tmp_path: Path):
    for bad_mode in ("fail", "empty"):
        sub = tmp_path / bad_mode
        sub.mkdir()
        res = _run_backup(sub, {"FAKE_DATA_MODE": bad_mode})
        assert res.returncode != 0
        backup_dir = sub / "backups"
        assert list(backup_dir.glob("governance_data_*.tar.gz")) == []
        assert list(backup_dir.glob("governance_*.sql.gz")) == []
        assert _run_health(backup_dir).returncode != 0


def test_backup_compose_recipient_encryption_defaults_to_removing_plaintext(tmp_path: Path):
    """Setting AGE_RECIPIENT_FILE without BACKUP_KEEP_PLAINTEXT must omit
    plaintext dumps from $BACKUP_DIR by default while still passing health checks."""
    fake_age = tmp_path / "fake-age"
    fake_age.write_text(
        """#!/usr/bin/env python3
import pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index("-o") + 1])
src = pathlib.Path(args[-1])
out.write_bytes(b"AGE-ENCRYPTED:" + src.read_bytes())
""",
        encoding="utf-8",
    )
    fake_age.chmod(0o755)
    pub = tmp_path / "backup.agekey.pub"
    pub.write_text("age1examplepublicrecipientkey\n", encoding="utf-8")

    # Pre-seed a plaintext dump from an earlier unencrypted run: by default,
    # turning on age emits no NEW plaintext while preserving prior restore points
    # until the operator verifies decryption and opts into PURGE_PRIOR_PLAINTEXT=1.
    preexisting = tmp_path / "backups"
    preexisting.mkdir(parents=True, exist_ok=True)
    (preexisting / "governance_20250101_000000.sql.gz").write_bytes(b"old-plain")
    (preexisting / "redis_20250101_000000.rdb").write_bytes(b"old-rdb")
    (preexisting / "governance_data_20250101_000000.tar.gz").write_bytes(b"old-tar")

    res = _run_backup(
        tmp_path,
        {
            "AGE_BIN": str(fake_age),
            "AGE_RECIPIENT_FILE": str(pub),
        },
    )
    assert res.returncode == 0, res.stderr

    backup_dir = tmp_path / "backups"
    assert [p.name for p in backup_dir.glob("*.sql.gz")] == ["governance_20250101_000000.sql.gz"]
    assert len(list(backup_dir.glob("governance_*.sql.gz.age"))) == 1
    assert len(list(backup_dir.glob("redis_*.rdb.age"))) == 1
    assert len(list(backup_dir.glob("governance_data_*.tar.gz.age"))) == 1
    assert _run_health(backup_dir).returncode == 0

    purge = _run_backup(
        tmp_path,
        {
            "AGE_BIN": str(fake_age),
            "AGE_RECIPIENT_FILE": str(pub),
            "PURGE_PRIOR_PLAINTEXT": "1",
        },
    )
    assert purge.returncode == 0, purge.stderr
    assert list(backup_dir.glob("*.sql.gz")) == []
    assert list(backup_dir.glob("*.rdb")) == []
    assert list(backup_dir.glob("*.tar.gz")) == []


def test_backup_compose_rejects_partial_age_failure(tmp_path: Path):
    """If age writes partial output and exits non-zero, the run fails without
    publishing partial ciphertext or marking status ok."""
    bad_age = tmp_path / "bad-age"
    bad_age.write_text(
        """#!/usr/bin/env python3
import pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index("-o") + 1])
out.write_bytes(b"PARTIAL-TRUNCATED-AGE")
sys.exit(1)
""",
        encoding="utf-8",
    )
    bad_age.chmod(0o755)
    pub = tmp_path / "backup.agekey.pub"
    pub.write_text("age1examplepublicrecipientkey\n", encoding="utf-8")

    res = _run_backup(
        tmp_path,
        {
            "AGE_BIN": str(bad_age),
            "AGE_RECIPIENT_FILE": str(pub),
        },
    )
    assert res.returncode != 0
    backup_dir = tmp_path / "backups"
    assert list(backup_dir.glob("*.age")) == []
    assert list(backup_dir.glob("*.sql.gz")) == []
    assert _run_health(backup_dir).returncode != 0


def test_backup_compose_recovers_orphaned_empty_lock_and_rejects_keep_zero_without_age(tmp_path: Path):
    no_age = tmp_path / "no_age"
    no_age.mkdir()
    bad = _run_backup(no_age, {"BACKUP_KEEP_PLAINTEXT": "0"})
    assert bad.returncode != 0
    assert "requires AGE_RECIPIENT" in bad.stderr

    orphan = tmp_path / "orphan"
    (orphan / "backups" / ".backup.lock").mkdir(parents=True)
    # Simulate a leftover crashed .stage_* directory containing plaintext.
    stale_stage = orphan / "backups" / ".stage_20260101_000000_aaaaaa"
    stale_stage.mkdir(parents=True)
    (stale_stage / "governance_20260101_000000.sql.gz").write_bytes(b"stale")
    ok = _run_backup(orphan, {"LOCK_ORPHAN_SEC": "0"})
    assert ok.returncode == 0, ok.stderr
    assert not stale_stage.exists()

    # Simulate PID reuse after reboot: live PID (this pytest process) whose command
    # line is not backup_compose.sh must be reclaimed rather than blocking forever.
    reused = tmp_path / "reused_pid"
    reused_lock = reused / "backups" / ".backup.lock"
    reused_lock.mkdir(parents=True)
    (reused_lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    ok_reused = _run_backup(reused)
    assert ok_reused.returncode == 0, ok_reused.stderr

    # Conversely, a live shell holder running backup_compose.sh must NOT be
    # reclaimed, and its in-flight .stage_* directory must remain untouched.
    held = tmp_path / "held_live"
    held_script = held / "backup_compose.sh"
    held.mkdir(parents=True)
    held_script.write_text("#!/usr/bin/env bash\nsleep 30\n", encoding="utf-8")
    held_script.chmod(0o755)
    holder = subprocess.Popen(["bash", str(held_script)])
    try:
        held_lock = held / "backups" / ".backup.lock"
        held_lock.mkdir(parents=True)
        (held_lock / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
        (held_lock / "cmd").write_text("backup_compose.sh\n", encoding="utf-8")
        (held_lock / "token").write_text(f"{holder.pid}_1\n", encoding="utf-8")
        inflight_stage = held / "backups" / ".stage_inflight"
        inflight_stage.mkdir(parents=True)

        blocked = _run_backup(held)
        assert blocked.returncode != 0
        assert held_lock.exists(), "contending run must not delete live holder's lock"
        assert inflight_stage.exists(), "contending run must not sweep live holder's stage dir"
        status = json.loads((held / "backups" / "last_backup_status.json").read_text())
        assert status["status"] == "error"
        assert "backup lock held by PID" in status["detail"]
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_backup_compose_rejects_purge_without_age_and_empty_manifest_failure(tmp_path: Path):
    purge_no_age = tmp_path / "purge_no_age"
    purge_no_age.mkdir()
    bad_purge = _run_backup(purge_no_age, {"PURGE_PRIOR_PLAINTEXT": "1"})
    assert bad_purge.returncode != 0
    assert "PURGE_PRIOR_PLAINTEXT=1 requires AGE_RECIPIENT" in bad_purge.stderr

    # Simulate disk full inside write_manifest after creating an empty .sha256 file:
    # if write_manifest failed to check python3 exit status / non-empty output,
    # trailing `chmod 600` would return 0 and publish an empty manifest.
    site_dir = tmp_path / "site_hook"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(
        """import pathlib
_orig = pathlib.Path.write_text
def _boom(self, data, *args, **kwargs):
    if self.name.startswith("manifest_") and self.name.endswith(".sha256"):
        self.open("wb").close()
        raise OSError("simulated disk full during manifest write")
    return _orig(self, data, *args, **kwargs)
pathlib.Path.write_text = _boom
""",
        encoding="utf-8",
    )
    manifest_fail = tmp_path / "manifest_fail"
    manifest_fail.mkdir()
    res = _run_backup(manifest_fail, {"PYTHONPATH": str(site_dir)})
    assert res.returncode != 0
    backup_dir = manifest_fail / "backups"
    assert list(backup_dir.glob("manifest_*.sha256")) == []
    assert list(backup_dir.glob("governance_*.sql.gz")) == []
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["status"] == "error"


def test_backup_compose_lock_token_preservation_and_empty_ps_age_fallback(tmp_path: Path):
    # 1. If a displaced run's lock token no longer matches .backup.lock/token,
    # its EXIT cleanup must NOT delete the successor's lock directory.
    token_case = tmp_path / "token_case"
    token_case.mkdir()
    displaced = _run_backup(
        token_case,
        {
            "BACKUP_SYNC_CMD": 'printf "successor_token\\n" > "$BACKUP_DIR/.backup.lock/token"; exit 1',
        },
    )
    assert displaced.returncode != 0
    assert (token_case / "backups" / ".backup.lock").exists()

    # 2. When `ps` returns empty args (e.g. hidepid=2), LOCK_MAX_AGE_SEC decides
    # whether a live PID holds the lock (age < max) or is reclaimed (age >= max).
    empty_ps_bin = tmp_path / "empty_ps_bin"
    empty_ps_bin.mkdir()
    fake_ps = empty_ps_bin / "ps"
    fake_ps.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_ps.chmod(0o755)
    patched_path = f"{empty_ps_bin}:{os.environ.get('PATH', '')}"

    young = tmp_path / "empty_ps_young"
    young_lock = young / "backups" / ".backup.lock"
    young_lock.mkdir(parents=True)
    (young_lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    blocked = _run_backup(young, {"PATH": patched_path, "LOCK_MAX_AGE_SEC": "3600"})
    assert blocked.returncode != 0
    assert young_lock.exists()

    expired = tmp_path / "empty_ps_expired"
    expired_lock = expired / "backups" / ".backup.lock"
    expired_lock.mkdir(parents=True)
    (expired_lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    reclaimed = _run_backup(expired, {"PATH": patched_path, "LOCK_MAX_AGE_SEC": "0"})
    assert reclaimed.returncode == 0, reclaimed.stderr


def test_backup_compose_rejects_failing_sync_cmd_in_plaintext_mode(tmp_path: Path):
    """A failing BACKUP_SYNC_CMD in plaintext mode must not publish a .sql.gz
    into $BACKUP_DIR that satisfies check_governance_backup_health.sh."""
    res = _run_backup(tmp_path, {"BACKUP_SYNC_CMD": "exit 1"})
    assert res.returncode != 0
    backup_dir = tmp_path / "backups"
    assert list(backup_dir.glob("governance_*.sql.gz")) == []
    assert _run_health(backup_dir).returncode != 0
