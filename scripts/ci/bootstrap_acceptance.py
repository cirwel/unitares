#!/usr/bin/env python3
"""Exercise a packaged native install against an explicitly provisioned empty DB.

Never creates/drops databases or starts shared services. CI owns its disposable
Postgres/Redis services; local callers supply dedicated acceptance databases.
All application files, runtime state, processes and evidence belong to work-dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def query(psql: str, db_url: str, sql: str) -> str:
    result = subprocess.run(
        [psql, "-X", "-v", "ON_ERROR_STOP=1", db_url, "-At"],
        input=sql, text=True, capture_output=True, timeout=30,
    )
    require(result.returncode == 0, "PostgreSQL query failed; check the acceptance connection and permissions")
    return result.stdout.strip()


def empty_database(psql: str, db_url: str, expected: str) -> None:
    require(expected.startswith("unitares_acceptance_"),
            "Use a dedicated database named unitares_acceptance_<purpose>")
    require(query(psql, db_url, "SELECT current_database();") == expected,
            "Connected database does not match --expected-database")
    objects = query(psql, db_url, """
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%'
          AND nspname NOT IN ('public', 'information_schema', 'ag_catalog')
        UNION ALL
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid
              AND d.classid='pg_class'::regclass AND d.deptype='e');
    """)
    require(not objects, "Acceptance requires an empty database; refusing existing objects")
    clients = query(psql, db_url, """
        SELECT count(*) FROM pg_stat_activity
        WHERE datname=current_database() AND pid != pg_backend_pid();
    """)
    require(clients == "0", "Acceptance database has other clients; stop its writers first")


def ledger(psql: str, db_url: str) -> list[dict]:
    return json.loads(query(psql, db_url, """
        SELECT coalesce(json_agg(row_to_json(m) ORDER BY version), '[]'::json)
        FROM core.schema_migrations m;
    """))


def verify_checksums(app: Path, rows: list[dict], interrupted: bool) -> None:
    by_version = {row["version"]: row for row in rows}
    require(62 in by_version, "Migration 62 was not applied")
    for path in sorted((app / "db/postgres/migrations").glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version < 62:
            continue
        require(version in by_version, f"Missing migration {version}")
        expected = None if interrupted and version == 62 else hashlib.sha256(path.read_bytes()).hexdigest()
        require(by_version[version]["checksum"] == expected,
                f"Incorrect checksum/provenance for migration {version}")


def run_logged(command: list[str], app: Path, env: dict, log: Path) -> int:
    with log.open("w") as output:
        result = subprocess.run(command, cwd=app, env=env, stdout=output,
                                stderr=subprocess.STDOUT, timeout=300)
    print(f"{log.name}: exit {result.returncode}", flush=True)
    return result.returncode


def interruption_wrapper(directory: Path, psql: str) -> Path:
    """Delegate every SQL call to real psql; fail after 062's committed -f call."""
    directory.mkdir()
    wrapper = directory / "psql"
    marker = directory / "interrupted"
    wrapper.write_text(f"#!{sys.executable}\n" + f"""
import pathlib
import subprocess
import sys
args = sys.argv[1:]
result = subprocess.run([{psql!r}, *args])
if result.returncode == 0 and '-f' in args:
    migration = pathlib.Path(args[args.index('-f') + 1]).name
    if migration.startswith('062_'):
        pathlib.Path({str(marker)!r}).touch()
        print('Injected interruption after migration 062 committed', file=sys.stderr)
        sys.exit(97)
sys.exit(result.returncode)
""")
    wrapper.chmod(0o755)
    return marker


def check_server(app: Path, work: Path, env: dict, launcher: str | None) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = ([launcher] if launcher else [sys.executable, str(app / "src/mcp_server.py"),
                                           "--host", "127.0.0.1"])
    command += ["--port", str(port)]
    native_env = work / "native.env"
    native_env.write_text("# Acceptance configuration is supplied through the process environment.\n")
    env = {**env, "UNITARES_NATIVE_APP_ROOT": str(app), "UNITARES_NATIVE_ENV": str(native_env),
           "UNITARES_SERVER_PID_FILE": str(work / "server.pid"),
           "UNITARES_SERVER_LOCK_FILE": str(work / "server.lock")}
    with (work / "server.log").open("w") as output:
        process = subprocess.Popen(command, cwd=app, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                require(process.poll() is None, "Server exited before readiness; see server.log")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=3) as response:
                        ready = json.load(response)
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                        health = json.load(response)
                    if ready.get("status") == "ready" and health.get("database", {}).get("status") == "connected":
                        (work / "health.json").write_text(json.dumps(health, indent=2) + "\n")
                        return
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                time.sleep(1)
            raise RuntimeError("Server never became ready with a connected database; see server.log")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--scenario", required=True, choices=("fresh", "interrupted"))
    parser.add_argument("--server-launcher", help="Installed native server wrapper (Homebrew acceptance)")
    args = parser.parse_args()
    source, work = args.app_root.resolve(), args.work_dir.resolve()
    require(source.is_dir(), "Application artifact does not exist")
    require(source != work and source not in work.parents, "work-dir must be outside the application artifact")
    require(not work.exists(), "Use a new work-dir so old evidence cannot count as this run")
    psql = shutil.which("psql")
    require(psql is not None, "Real psql is required on PATH")
    empty_database(psql, args.db_url, args.expected_database)
    work.mkdir(parents=True)
    app = work / "app"
    shutil.copytree(source, app, ignore=shutil.ignore_patterns(
        ".git", ".env", ".env.*", "data", ".venv", "venv", "__pycache__", ".test-cache", ".review-cache", ".scratch"))
    (app / "data").mkdir()
    # No inherited deployment configuration, model credentials or residents.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}}
    env.update({"DB_POSTGRES_URL": args.db_url, "DB_BACKEND": "postgres",
                "REDIS_URL": args.redis_url, "UNITARES_RESIDENTS": "",
                "UNITARES_SECRETS_ENV": "/dev/null", "UNITARES_KNOWLEDGE_BACKEND": "postgres",
                "UNITARES_DIALECTIC_BACKEND": "postgres", "UNITARES_MODEL_BASE_URL": "http://127.0.0.1:1",
                "LEASE_PLANE_BASE_URL": "http://127.0.0.1:1", "UNITARES_LEASE_PLANE_URL": "http://127.0.0.1:1"})
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', os.defpath)}"
    bootstrap = ["bash", str(app / "scripts/install/bootstrap_postgres.sh"), "--apply", "--db-url", args.db_url]
    runner = [sys.executable, str(app / "scripts/dev/apply_migrations.py"), "--db-url", args.db_url]
    interrupted = args.scenario == "interrupted"
    if interrupted:
        wrapper_dir = work / "fault-bin"
        marker = interruption_wrapper(wrapper_dir, psql)
        fault_env = {**env, "PATH": f"{wrapper_dir}{os.pathsep}{env['PATH']}"}
        rc = run_logged(bootstrap, app, fault_env, work / "bootstrap.log")
        require(rc == 97 and marker.exists(), "Bootstrap did not stop at the injected migration-62 interruption")
        before = ledger(psql, args.db_url)
        require(max(row["version"] for row in before) == 62, "Bootstrap advanced beyond the interrupted migration")
        require(next(row for row in before if row["version"] == 62)["checksum"] is None,
                "Interrupted checksum must remain unverifiable")
        require(run_logged(runner + ["--apply", "--dialectic-writers-quiesced"], app, env,
                           work / "recovery.log") == 0, "Migration recovery failed")
        require([row for row in ledger(psql, args.db_url) if row["version"] <= 62] == before,
                "Recovery rewrote an already committed migration record")
    else:
        require(run_logged(bootstrap, app, env, work / "bootstrap.log") == 0, "Fresh bootstrap failed")
    require(run_logged(runner + ["--check"], app, env, work / "migration-check.log") == 0,
            "Migration registry does not match the installed artifact")
    rows = ledger(psql, args.db_url)
    verify_checksums(app, rows, interrupted)
    require(run_logged(runner + ["--apply", "--dialectic-writers-quiesced"], app, env,
                       work / "repeat-migrations.log") == 0, "Repeated migration runner failed")
    require(ledger(psql, args.db_url) == rows, "Repeated migration execution changed the ledger")
    check_server(app, work, env, args.server_launcher)
    evidence = {"scenario": args.scenario, "version": (app / "VERSION").read_text().strip(),
                "database": args.expected_database, "max_migration": max(row["version"] for row in rows),
                "migration_count": len(rows), "server": "ready, database connected",
                "bootstrap_sha256": hashlib.sha256((app / "scripts/install/bootstrap_postgres.sh").read_bytes()).hexdigest()}
    (work / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"Acceptance FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    except subprocess.SubprocessError as exc:
        # Subprocess exceptions include argv, which can contain the test DSN.
        print(f"Acceptance FAILED: {type(exc).__name__}; inspect the phase logs", file=sys.stderr)
        sys.exit(1)
