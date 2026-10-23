"""Contract tests for the advanced bare-metal PostgreSQL bootstrap."""

from __future__ import annotations

import os
import hashlib
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install" / "bootstrap_postgres.sh"
MIGRATIONS = REPO_ROOT / "db" / "postgres" / "migrations"


def _dry_run(*extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), "--dry-run", *extra],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_bootstrap_is_executable_and_dry_run_is_safe() -> None:
    assert SCRIPT.stat().st_mode & 0o111
    proc = _dry_run()
    assert proc.returncode == 0, proc.stderr
    assert "mode: dry-run" in proc.stdout
    assert "bootstrap complete" not in proc.stdout


def test_bootstrap_matches_docker_schema_order() -> None:
    proc = _dry_run()
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()

    def position(suffix: str) -> int:
        return next(i for i, line in enumerate(lines) if line.endswith(suffix))

    assert position("schema.sql") < position("003_dialectic_messages.sql")
    assert position("030_lease_plane_aborted_event.sql") < position("partitions.sql")
    assert position("partitions.sql") < position("031_r1_provisional_lineage.sql")
    assert position("graph_schema.sql") < position("031_r1_provisional_lineage.sql")

    expected = {
        path.name
        for path in MIGRATIONS.glob("*.sql")
        if int(path.name.split("_", 1)[0]) >= 3
    }
    planned = {
        line.rsplit("/", 1)[-1]
        for line in lines
        if line.startswith("would apply migrations/")
    }
    assert planned == expected


def test_dry_run_never_prints_database_credentials() -> None:
    env = os.environ.copy()
    env["DB_POSTGRES_URL"] = "postgresql://operator:do-not-print@db.example/governance"
    proc = _dry_run(env=env)
    assert proc.returncode == 0, proc.stderr
    assert "do-not-print" not in proc.stdout
    assert "do-not-print" not in proc.stderr


def test_unknown_argument_fails_without_touching_database() -> None:
    proc = subprocess.run(
        [str(SCRIPT), "--unknown"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "unknown argument" in proc.stderr


@pytest.mark.parametrize("fail_checksum", [False, True])
def test_apply_crosses_checksum_boundary_and_resumes_safely(
    tmp_path: Path, fail_checksum: bool
) -> None:
    """Exercise --apply, including psql's distinct -c and stdin behavior."""
    script = tmp_path / "scripts/install/bootstrap_postgres.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    migrations = tmp_path / "db/postgres/migrations"
    migrations.mkdir(parents=True)
    for version in (61, 62, 63):
        (migrations / f"{version:03d}_fixture.sql").write_text(f"-- {version}\n")

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"applied": [], "checksums": {}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql = bin_dir / "psql"
    psql.write_text(f"#!{sys.executable}\n" + textwrap.dedent(r"""
        import json
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        path = Path(os.environ["BOOTSTRAP_TEST_STATE"])
        state = json.loads(path.read_text())
        sql = ""
        if "-f" in args:
            file = Path(args[args.index("-f") + 1])
            if file.parent.name == "migrations":
                state["applied"].append(int(file.name.split("_", 1)[0]))
        else:
            command = next((i for i, arg in enumerate(args)
                            if arg in ("-c", "-Atqc")), None)
            sql = args[command + 1] if command is not None else sys.stdin.read()
            if command is not None and ":migration" in sql.replace(":'", ":"):
                sys.exit('ERROR: syntax error at or near ":"')
        if sql.startswith("SELECT 1"):
            if int(sql.rsplit(" ", 1)[-1]) in state["applied"]:
                print("1")
        elif sql.startswith("SELECT EXISTS"):
            print("t" if 62 in state["applied"] else "f")
        elif sql.startswith("UPDATE"):
            if os.environ["BOOTSTRAP_TEST_FAIL_CHECKSUM"] == "1":
                sys.exit("checksum write failed")
            variables = dict(args[i + 1].split("=", 1)
                             for i, arg in enumerate(args) if arg == "-v")
            assert "checksum = :'migration_checksum'" in sql
            assert "version = :migration_version" in sql
            assert "checksum IS NULL" in sql
            state["checksums"][variables["migration_version"]] = variables["migration_checksum"]
        path.write_text(json.dumps(state))
    """))
    psql.chmod(0o755)
    # The final validator is outside this shell regression's scope.
    validator = bin_dir / "python3"
    validator.write_text("#!/bin/sh\nprintf 'validator called\\n'\n")
    validator.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BOOTSTRAP_TEST_STATE": str(state),
        "BOOTSTRAP_TEST_FAIL_CHECKSUM": "1" if fail_checksum else "0",
    }

    def apply() -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(script), "--apply", "--db-url", "postgresql://fixture/unused"],
            env=env, capture_output=True, text=True, check=False,
        )

    proc = apply()
    recorded = json.loads(state.read_text())
    if fail_checksum:
        assert proc.returncode != 0
        assert "checksum write failed" in proc.stderr
        assert recorded == {"applied": [61, 62], "checksums": {}}
        assert "validator called" not in proc.stdout
        assert "bootstrap complete" not in proc.stdout
    else:
        assert proc.returncode == 0, proc.stderr
        assert recorded == {
            "applied": [61, 62, 63],
            "checksums": {
                str(version): hashlib.sha256(
                    (migrations / f"{version:03d}_fixture.sql").read_bytes()
                ).hexdigest()
                for version in (62, 63)
            },
        }
        assert "validator called" in proc.stdout
        assert "bootstrap complete" in proc.stdout

    # A previously applied migration is never re-run or retroactively hashed,
    # including migration 62 when its post-commit checksum write failed.
    env["BOOTSTRAP_TEST_FAIL_CHECKSUM"] = "0"
    resumed = apply()
    assert resumed.returncode == 0, resumed.stderr
    after_resume = json.loads(state.read_text())
    assert after_resume["applied"] == [61, 62, 63]
    assert "61" not in after_resume["checksums"]
    assert ("62" in after_resume["checksums"]) is not fail_checksum
    assert "63" in after_resume["checksums"]
