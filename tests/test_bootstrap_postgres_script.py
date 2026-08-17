"""Contract tests for the advanced bare-metal PostgreSQL bootstrap."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
