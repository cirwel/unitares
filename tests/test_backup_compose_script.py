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
import os, shutil, sys
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
    sys.exit(2)

if args[0] == "cp":
    src, dst = args[1], Path(args[2])
    if src.endswith("/dump.rdb"):
        dst.write_bytes(b"REDIS0011fake-rdb-payload")
        sys.exit(0)
    if src.endswith(":/app/data"):
        (dst / "secrets").mkdir(parents=True, exist_ok=True)
        (dst / "secrets" / "continuity_token_secret").write_text("secret-seed")
        # Simulate docker-compose.full.yml's model cache, which must be excluded.
        (dst / "hf" / "models--BAAI--bge-m3").mkdir(parents=True, exist_ok=True)
        (dst / "hf" / "models--BAAI--bge-m3" / "weights.bin").write_bytes(b"x" * 1024)
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


def test_backup_compose_captures_all_three_stores_and_satisfies_health_check(tmp_path: Path):
    res = _run_backup(tmp_path)
    assert res.returncode == 0, res.stderr

    backup_dir = tmp_path / "backups"
    pg_files = list(backup_dir.glob("governance_*.sql.gz"))
    redis_files = list(backup_dir.glob("redis_*.rdb"))
    data_files = list(backup_dir.glob("governance_data_*.tar.gz"))

    assert len(pg_files) == 1
    assert len(redis_files) == 1
    assert len(data_files) == 1

    for artifact in (*pg_files, *redis_files, *data_files):
        mode = stat.S_IMODE(artifact.stat().st_mode)
        assert mode == 0o600, f"{artifact.name} expected 0600, got {oct(mode)}"

    with tarfile.open(data_files[0], "r:gz") as tar:
        names = tar.getnames()
    assert "data/secrets/continuity_token_secret" in names
    assert not any(n.startswith("data/hf") for n in names), names

    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["status"] == "ok"
    assert status["file"] == str(pg_files[0])

    health = subprocess.run(
        ["bash", str(HEALTH_SCRIPT)],
        env={**os.environ, "BACKUP_DIR": str(backup_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert health.returncode == 0, health.stderr


def test_backup_compose_rejects_truncated_pg_dump(tmp_path: Path):
    res = _run_backup(tmp_path, {"FAKE_PG_MODE": "truncated"})
    assert res.returncode != 0

    backup_dir = tmp_path / "backups"
    assert not (backup_dir / "last_backup_success.txt").exists()
    assert list(backup_dir.glob("governance_*.sql.gz")) == []
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["status"] == "error"


def test_backup_compose_rejects_failed_redis_bgsave(tmp_path: Path):
    res = _run_backup(tmp_path, {"FAKE_REDIS_MODE": "err"})
    assert res.returncode != 0

    backup_dir = tmp_path / "backups"
    assert not (backup_dir / "last_backup_success.txt").exists()
    status = json.loads((backup_dir / "last_backup_status.json").read_text())
    assert status["status"] == "error"


def test_backup_compose_recipient_encryption_and_plaintext_cleanup(tmp_path: Path):
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

    res = _run_backup(
        tmp_path,
        {
            "AGE_BIN": str(fake_age),
            "AGE_RECIPIENT_FILE": str(pub),
            "BACKUP_KEEP_PLAINTEXT": "0",
        },
    )
    assert res.returncode == 0, res.stderr

    backup_dir = tmp_path / "backups"
    assert list(backup_dir.glob("*.sql.gz")) == []
    assert list(backup_dir.glob("*.rdb")) == []
    assert list(backup_dir.glob("*.tar.gz")) == []
    assert len(list(backup_dir.glob("governance_*.sql.gz.age"))) == 1
    assert len(list(backup_dir.glob("redis_*.rdb.age"))) == 1
    assert len(list(backup_dir.glob("governance_data_*.tar.gz.age"))) == 1
