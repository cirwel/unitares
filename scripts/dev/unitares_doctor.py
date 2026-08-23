#!/usr/bin/env python3
"""Diagnose a UNITARES install.

Usage:
    python3 scripts/dev/unitares_doctor.py
    python3 scripts/dev/unitares_doctor.py --mode local
    python3 scripts/dev/unitares_doctor.py --mode operator
    python3 scripts/dev/unitares_doctor.py --json

Modes:
    local      Checks needed for local adoption (postgres + schema + Redis
               continuity + anchor dir). Sufficient for a fresh-machine
               bring-up where the agent client spawns governance directly.
    operator   Adds HTTP/launchd checks: 8767 listening, PID file, LaunchAgent
               loaded, resident-agent plists, cloudflared sidecar.
    all        local + operator. Default.

Stdlib-only. Safe to run before `pip install -e .` finishes — used to verify
that the install can finish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

DEFAULT_DB_URL = "postgresql://postgres:postgres@localhost:5432/governance"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
REQUIRED_PG_EXTENSIONS = ("age", "pgcrypto", "pg_trgm", "uuid-ossp", "vector")
RESIDENT_LAUNCHD_SLOTS = (
    ("vigil", ("com.unitares.vigil",)),
    ("sentinel", ("com.unitares.sentinel", "com.unitares.sentinel-beam")),
    ("chronicler", ("com.unitares.chronicler",)),
)
ANCHOR_DIR = Path.home() / ".unitares"
SECRETS_FILE = Path.home() / ".config" / "cirwel" / "secrets.env"
HTTP_HEALTH_URL = "http://127.0.0.1:8767/health/live"
HTTP_RESIDENTS_URL = "http://127.0.0.1:8767/v1/residents"
PID_FILE_REL = "data/.mcp_server.pid"
GOVERNANCE_LAUNCHD_LABEL = "com.unitares.governance-mcp"
KNOWN_SCHEMA_MIGRATION_EXCEPTIONS = {
    # 2026-04-26: applied out-of-band before the source-file repair landed.
    # Keep this as accepted history, but still fail any new unexpected rows.
    18: "progress flat telemetry tables",
}


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class CheckResult:
    name: str
    mode: str
    status: Status
    message: str
    detail: str = ""


@dataclass
class Check:
    name: str
    mode: str  # "local" or "operator"
    fn: Callable[[], CheckResult]


# ---------------------------------------------------------------------------
# Local-mode checks
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    name, mode = "python_version", "local"
    v = sys.version_info
    if v >= (3, 12):
        return CheckResult(name, mode, Status.PASS, f"Python {v.major}.{v.minor}.{v.micro}")
    return CheckResult(
        name, mode, Status.FAIL,
        f"Python 3.12+ required (got {v.major}.{v.minor}.{v.micro})",
    )


def check_postgres_running(db_url: str) -> CheckResult:
    name, mode = "postgres_running", "local"
    if shutil.which("pg_isready") is None:
        return CheckResult(name, mode, Status.WARN,
                           "pg_isready not on PATH; install postgresql@17")
    rc = subprocess.run(
        ["pg_isready", "-d", db_url],
        capture_output=True, text=True, timeout=5,
    ).returncode
    if rc == 0:
        return CheckResult(name, mode, Status.PASS, f"reachable at {_redact(db_url)}")
    return CheckResult(name, mode, Status.FAIL,
                       f"pg_isready failed (rc={rc}); try `brew services start postgresql@17`")


def check_redis_continuity(redis_url: str) -> CheckResult:
    """Report whether production-grade session continuity is available.

    Redis absence is a warning rather than a hard failure because the server has
    an explicit degraded local-only mode for demos. The wording must still make
    clear that this is not the production continuity posture.
    """
    name, mode = "redis_continuity", "local"
    if shutil.which("redis-cli") is None:
        return CheckResult(
            name,
            mode,
            Status.WARN,
            "redis-cli not on PATH; install Redis for production session continuity",
            detail="The server can boot only in degraded local-only mode without Redis.",
        )

    parsed = urllib.parse.urlsplit(redis_url)
    host = parsed.hostname or "localhost"
    host_for_url = f"[{host}]" if ":" in host else host
    port = parsed.port or 6379
    safe_url = urllib.parse.urlunsplit(
        (parsed.scheme or "redis", f"{host_for_url}:{port}", parsed.path or "/0", "", "")
    )
    cmd = ["redis-cli", "-u", safe_url, "--no-auth-warning"]
    if parsed.username:
        cmd.extend(["--user", urllib.parse.unquote(parsed.username)])
    cmd.append("ping")
    env = os.environ.copy()
    if parsed.password:
        env["REDISCLI_AUTH"] = urllib.parse.unquote(parsed.password)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    if proc.returncode == 0 and proc.stdout.strip().upper() == "PONG":
        return CheckResult(name, mode, Status.PASS, f"reachable at {safe_url}")
    return CheckResult(
        name,
        mode,
        Status.WARN,
        "Redis unavailable; identity continuity is degraded local-only",
        detail=(proc.stderr or proc.stdout).strip(),
    )


def check_governance_database(db_url: str) -> CheckResult:
    name, mode = "governance_database", "local"
    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")
    proc = subprocess.run(
        ["psql", db_url, "-Atqc", "SELECT 1"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode == 0 and proc.stdout.strip() == "1":
        return CheckResult(name, mode, Status.PASS, "governance database accepts queries")
    return CheckResult(name, mode, Status.FAIL,
                       "cannot query governance database",
                       detail=proc.stderr.strip())


def check_pg_extensions(db_url: str) -> CheckResult:
    name, mode = "pg_extensions", "local"
    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")
    proc = subprocess.run(
        ["psql", db_url, "-Atqc", "SELECT extname FROM pg_extension ORDER BY extname"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        return CheckResult(name, mode, Status.FAIL,
                           "could not list extensions", detail=proc.stderr.strip())
    present = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    missing = [e for e in REQUIRED_PG_EXTENSIONS if e not in present]
    if not missing:
        return CheckResult(name, mode, Status.PASS,
                           f"all required extensions present ({len(REQUIRED_PG_EXTENSIONS)})")
    return CheckResult(name, mode, Status.FAIL,
                       f"missing extensions: {', '.join(missing)}")


def _source_schema_migrations(repo_root: Path) -> dict[int, str]:
    migrations_dir = repo_root / "db" / "postgres" / "migrations"
    source: dict[int, str] = {}
    if not migrations_dir.is_dir():
        return source

    insert_re = re.compile(
        r"INSERT\s+INTO\s+core\.schema_migrations\s*\([^)]*version[^)]*\)"
        r"\s*VALUES\s*(.*?)(?:ON\s+CONFLICT|;)",
        re.IGNORECASE | re.DOTALL,
    )
    value_re = re.compile(r"\(\s*(\d+)\s*,\s*'([^']+)'", re.DOTALL)

    for path in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
        text = path.read_text()
        for block in insert_re.findall(text):
            for version_raw, name in value_re.findall(block):
                version = int(version_raw)
                previous = source.get(version)
                if previous is not None and previous != name:
                    raise ValueError(
                        f"source files claim version {version} as both "
                        f"{previous!r} and {name!r}"
                    )
                source[version] = name
    return source


def _parse_schema_migration_rows(stdout: str) -> dict[int, str]:
    rows: dict[int, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        version_raw, sep, name = line.partition("|")
        if not sep:
            raise ValueError(f"unexpected schema_migrations row: {line!r}")
        rows[int(version_raw)] = name
    return rows


def _file_checksum(path: Path) -> str:
    """sha256 (hex) of a migration file's bytes.

    Hashes bytes, not decoded text, so an encoding or line-ending change is a
    different migration — which is the honest reading: psql would feed different
    bytes to the server.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _query_applied_checksums(db_url: str) -> dict[int, str | None]:
    """Return {version: checksum-or-None} from core.schema_migrations.

    Returns ``{}`` ONLY when the checksum column does not exist yet, which is
    the pre-migration-062 state and not an error — callers treat an empty result
    as "content anchoring not yet available on this database".

    Any other psql failure RAISES. Swallowing it would return ``{}`` too, and
    every caller reads that as "nothing to verify" — so a dropped connection or
    a timeout would silently disarm the drift guard and report clean. That is
    the same shape as the bug this whole mechanism exists to prevent: a check
    that stops checking without saying so. A guard must fail loudly or not at
    all.
    """
    proc = subprocess.run(
        ["psql", db_url, "-Atqc",
         "SELECT version || '|' || COALESCE(checksum, '') "
         "FROM core.schema_migrations ORDER BY version"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        # 42703 undefined_column — the only benign failure. Matched on both the
        # SQLSTATE and the message text because psql only emits the code when
        # VERBOSITY is raised, and the default is terse.
        if "42703" in stderr or (
            "checksum" in stderr and "does not exist" in stderr
        ):
            return {}
        raise RuntimeError(
            f"core.schema_migrations checksums not queryable at "
            f"{_redact(db_url)}: {stderr}"
        )
    out: dict[int, str | None] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        version_raw, sep, checksum = line.partition("|")
        if not sep:
            raise ValueError(f"unexpected schema_migrations row: {line!r}")
        out[int(version_raw)] = checksum or None
    return out


def _checksum_drift(
    recorded: dict[int, str | None],
    migrations_dir: Path,
) -> tuple[list[str], list[int]]:
    """Classify recorded checksums against the files on disk.

    Returns ``(mismatches, unverifiable)``.

    * ``mismatches`` — a recorded checksum that disagrees with the current file.
      The file changed after it was applied, so the deployed schema and the
      source no longer describe the same thing. This is the 034 failure mode,
      and it is a hard error.
    * ``unverifiable`` — versions with no recorded checksum. Applied before
      migration 062, so what actually ran is unknowable. Reported, never
      "repaired" by writing today's hash in (see 062's header).
    """
    mismatches: list[str] = []
    unverifiable: list[int] = []
    for version in sorted(recorded):
        checksum = recorded[version]
        if checksum is None:
            unverifiable.append(version)
            continue
        matches = sorted(migrations_dir.glob(f"{version:03d}_*.sql"))
        if not matches:
            # A DB version with no source file is already reported by
            # _schema_migration_drift as "unexpected"; not re-reported here.
            continue
        actual = _file_checksum(matches[0])
        if actual != checksum:
            mismatches.append(
                f"version {version} ({matches[0].name}): "
                f"recorded {checksum[:12]}… but file is {actual[:12]}… "
                f"— the file changed after it was applied"
            )
    return mismatches, unverifiable


def _schema_migration_drift(actual: dict[int, str], expected: dict[int, str]) -> list[str]:
    issues: list[str] = []
    for version in sorted(expected):
        if version not in actual:
            issues.append(f"missing {version}:{expected[version]}")
        elif actual[version] != expected[version]:
            issues.append(
                f"mismatch {version}: db={actual[version]!r} source={expected[version]!r}"
            )
    for version in sorted(set(actual) - set(expected)):
        issues.append(f"unexpected {version}:{actual[version]}")
    return issues


def check_schema_migrations(db_url: str, repo_root: Path | None = None) -> CheckResult:
    name, mode = "schema_migrations", "local"
    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")
    proc = subprocess.run(
        ["psql", db_url, "-Atqc",
         "SELECT version || '|' || name FROM core.schema_migrations ORDER BY version"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        return CheckResult(name, mode, Status.FAIL,
                           "core.schema_migrations not queryable",
                           detail=proc.stderr.strip())
    if not proc.stdout.strip():
        return CheckResult(name, mode, Status.FAIL,
                           "core.schema_migrations is empty (run migrations)")
    try:
        actual = _parse_schema_migration_rows(proc.stdout)
        if repo_root is not None:
            expected = _source_schema_migrations(repo_root)
            expected.update(KNOWN_SCHEMA_MIGRATION_EXCEPTIONS)
            drift = _schema_migration_drift(actual, expected)
            if drift:
                return CheckResult(
                    name, mode, Status.FAIL,
                    f"schema registry drift detected ({len(drift)} issue(s))",
                    detail="\n".join(drift),
                )
    except Exception as exc:
        return CheckResult(name, mode, Status.FAIL,
                           "could not validate schema_migrations against source",
                           detail=str(exc))

    version = max(actual)
    return CheckResult(name, mode, Status.PASS,
                       f"schema at version {version}; registry matches source manifest")


_SQL_INSERT_COLUMNS_RE = re.compile(
    r"INSERT\s+INTO\s+(\w+)\.(\w+)\s*\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)
_SQL_IDENT_RE = re.compile(r"^([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _scan_insert_column_refs(src_dirs: list[Path]) -> dict[tuple[str, str], set[str]]:
    """Scan Python source for ``INSERT INTO schema.table (...)`` SQL fragments.

    Returns ``{(schema, table): {col1, col2, ...}}`` for each table whose
    INSERT column list could be parsed. Only handles bare-identifier column
    lists (no function calls in the column position) — that covers every
    INSERT in the current codebase, where function calls live in VALUES.
    """
    refs: dict[tuple[str, str], set[str]] = {}
    for src_dir in src_dirs:
        if not src_dir.is_dir():
            continue
        for path in src_dir.rglob("*.py"):
            try:
                text = path.read_text()
            except Exception:
                continue
            for match in _SQL_INSERT_COLUMNS_RE.finditer(text):
                schema, table, col_list = match.group(1), match.group(2), match.group(3)
                cleaned = re.sub(r"--[^\n]*", "", col_list)
                cols: set[str] = set()
                for tok in cleaned.split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    m = _SQL_IDENT_RE.match(tok)
                    if m:
                        cols.add(m.group(1).lower())
                if cols:
                    refs.setdefault((schema.lower(), table.lower()), set()).update(cols)
    return refs


def _fetch_table_columns(db_url: str, schema: str, table: str) -> set[str] | None:
    """Return the set of column names for a table, or None on lookup failure."""
    proc = subprocess.run(
        ["psql", db_url, "-Atqc",
         f"SELECT column_name FROM information_schema.columns "
         f"WHERE table_schema='{schema}' AND table_name='{table}'"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        return None
    cols = {line.strip().lower() for line in proc.stdout.splitlines() if line.strip()}
    return cols or None


def check_column_drift(db_url: str, repo_root: Path) -> CheckResult:
    """Verify columns in INSERT INTO statements exist in the running DB.

    Catches code-vs-DB drift the schema_migrations check misses: code
    references a column the migration never added, INSERT fails at runtime
    with "column ... does not exist". Same blind-spot class as the
    2026-04-17 last_activity_at incident, the 2026-04-19 trigger_source
    outage, and the 2026-05-07 discoveries.provenance_chain bug.
    """
    name, mode = "column_drift", "local"
    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")
    src_dirs = [repo_root / "src", repo_root / "governance_core"]
    refs = _scan_insert_column_refs(src_dirs)
    if not refs:
        return CheckResult(name, mode, Status.SKIP, "no INSERT statements found")

    missing: list[str] = []
    total_refs = 0
    for (schema, table), cols in sorted(refs.items()):
        existing = _fetch_table_columns(db_url, schema, table)
        if existing is None:
            continue  # table absent or lookup error; other checks own that
        total_refs += len(cols)
        for col in sorted(cols):
            if col not in existing:
                missing.append(f"{schema}.{table}.{col}")

    if missing:
        return CheckResult(
            name, mode, Status.FAIL,
            f"code references {len(missing)} column(s) missing from DB",
            detail="\n".join(missing),
        )
    return CheckResult(
        name, mode, Status.PASS,
        f"all {total_refs} INSERT-referenced columns exist across {len(refs)} table(s)",
    )


_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_ALTER_TABLE = re.compile(r"\bALTER\s+TABLE\s+(?:ONLY\s+)?([A-Za-z_][\w.]*)", re.I)
_ADD_CONSTRAINT = re.compile(r"\bADD\s+CONSTRAINT\s+([A-Za-z_]\w*)", re.I)
_DROP_CONSTRAINT = re.compile(
    r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?([A-Za-z_]\w*)", re.I
)


def _declared_constraints(migrations_dir: Path) -> dict[tuple[str, str], str]:
    """Map (qualified_table, constraint_name) -> migration file that last ADDed it.

    Replays every ADD/DROP CONSTRAINT across the migration set in apply order
    (version, then position within the file) and keeps only constraints whose
    LAST action is an ADD. That is what makes drop-then-re-add
    (056_lease_release_reason_reclaimed) and deliberate retirement
    (047_knowledge_check_constraints_widen) read correctly instead of as drift.

    Comments are stripped first — 034's own header contains the words "ADD
    CONSTRAINT IF NOT EXISTS" in prose, which a naive scan reports as a
    constraint literally named "IF".

    Scope limit: only ALTER TABLE ... ADD CONSTRAINT is tracked. Constraints
    written inline in a CREATE TABLE body are owned by schema.sql, not by the
    migration replay, so they are out of scope here.
    """
    live: dict[tuple[str, str], str] = {}
    for path in sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name):
        sql = _SQL_BLOCK_COMMENT.sub(" ", path.read_text(encoding="utf-8"))
        sql = _SQL_LINE_COMMENT.sub(" ", sql)

        # Each ADD/DROP belongs to the nearest preceding ALTER TABLE, since the
        # two clauses routinely sit on different lines.
        tables = [(m.start(), m.group(1).lower()) for m in _ALTER_TABLE.finditer(sql)]

        def table_at(pos: int) -> str | None:
            prior = [t for off, t in tables if off < pos]
            return prior[-1] if prior else None

        events: list[tuple[int, str, str]] = []
        for m in _ADD_CONSTRAINT.finditer(sql):
            events.append((m.start(), "add", m.group(1).lower()))
        for m in _DROP_CONSTRAINT.finditer(sql):
            events.append((m.start(), "drop", m.group(1).lower()))

        for pos, action, cname in sorted(events):
            tbl = table_at(pos)
            if tbl is None:
                continue
            if action == "add":
                live[(tbl, cname)] = path.name
            else:
                live.pop((tbl, cname), None)
    return live


def _fetch_live_constraints(db_url: str) -> set[tuple[str, str]] | None:
    """Every constraint in the DB as (qualified_table, constraint_name), or None."""
    proc = subprocess.run(
        ["psql", db_url, "-Atqc",
         "SELECT n.nspname || '.' || cl.relname || '|' || c.conname "
         "FROM pg_constraint c "
         "JOIN pg_class cl ON cl.oid = c.conrelid "
         "JOIN pg_namespace n ON n.oid = cl.relnamespace"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        return None
    out: set[tuple[str, str]] = set()
    for line in proc.stdout.splitlines():
        if "|" in line:
            tbl, cname = line.strip().rsplit("|", 1)
            out.add((tbl.lower(), cname.lower()))
    return out


def schema_attestation(
    applied: dict[int, str],
    recorded: dict[int, str | None],
) -> dict[str, object]:
    """Reduce a database's migration registry to a comparable digest.

    ``constraint_drift`` and ``column_drift`` answer "is THIS database intact?".
    This answers a different question: "do two databases carry the same schema
    contract?" — which is the one that matters once more than one principal runs
    governance and they need to interoperate without a shared administrative
    root. Comparing digests is attestation: each side computes over its own
    state and the values either agree or they do not. Nothing here consults a
    central registry, and no principal's answer is authoritative over another's.

    The digest chains ``version:name:checksum`` in version order, so it is
    sensitive to a missing migration, a renamed one, and — because the checksum
    is in the chain — to a migration whose content differs from a peer's at the
    same version.

    ``coverage`` is the part that must not be dropped. A digest over rows whose
    checksums are NULL says far less than one over fully anchored rows: two
    databases could agree on every ``version:name`` pair and still have run
    different SQL, which is exactly how 034 stayed invisible. So ``verified`` /
    ``total`` travels WITH the digest and ``fully_anchored`` is false whenever
    any row is unverifiable. A digest whose coverage is partial is a weaker
    claim, and callers must be able to see that rather than read equality as
    proof of agreement.

    WHAT THIS DOES NOT DO
    ---------------------
    The digest is self-computed and unsigned. It detects *accidental* divergence
    between cooperating peers — the failure mode that actually happens, and the
    one that produced 034 — and it is not evidence against a peer that
    misreports its own state. Nothing here binds the digest to anything
    unforgeable, so a principal that wants to claim a schema it does not run
    can. Treat agreement as a cheap consistency check between parties already
    trusted to answer honestly, not as an adversarial proof.

    A second, quieter limit: an all-NULL registry is cheap to match, since the
    chain then reduces to ``version:name`` pairs anyone can reproduce. That is
    the concrete reason ``fully_anchored`` exists rather than a bare digest —
    a partially-anchored match is close to no evidence at all.
    """
    chain = []
    for version in sorted(applied):
        checksum = recorded.get(version)
        chain.append(f"{version}:{applied[version]}:{checksum or '-'}")
    body = "\n".join(chain)
    verified = sum(1 for v in applied if recorded.get(v))
    total = len(applied)
    return {
        "digest": hashlib.sha256(body.encode()).hexdigest(),
        "migrations": total,
        "verified": verified,
        "unverifiable": total - verified,
        "fully_anchored": total > 0 and verified == total,
    }


def check_migration_checksum_drift(db_url: str, repo_root: Path) -> CheckResult:
    """FAIL when a migration file changed after it was applied.

    ``constraint_drift`` and ``column_drift`` are symptom detectors: each knows
    one DDL family and notices when that specific kind of change went missing.
    They caught the 034 instance only because someone went looking. This is the
    cause detector — it does not care what the migration did, only whether the
    bytes that ran match the bytes on disk now.

    The gap it closes: ``core.schema_migrations`` recorded only
    ``(version, name, applied_at)``, so a registered version was a claim about
    WHICH migration ran and never about WHAT ran. ``apply_migrations.py`` plans
    by registered version, so once a version is in the table its file is never
    read again — edit it afterwards and nothing anywhere notices. Migration 062
    adds the checksum column that makes the two comparable.

    Versions applied before 062 have no recorded checksum. Those are reported as
    unverifiable rather than repaired: writing today's hash into an old row would
    assert that the applied content matched the current file, which is precisely
    the false-green being eliminated. See 062's header.
    """
    name, mode = "migration_checksum_drift", "local"
    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")

    migrations_dir = repo_root / "db" / "postgres" / "migrations"
    if not migrations_dir.is_dir():
        return CheckResult(name, mode, Status.SKIP, "no db/postgres/migrations directory")

    try:
        recorded = _query_applied_checksums(db_url)
    except RuntimeError as exc:
        # Not a SKIP: we could not determine whether the schema drifted, and
        # "could not check" must never render as "checked, fine".
        return CheckResult(
            name, mode, Status.FAIL,
            "could not read migration checksums — drift state is UNKNOWN",
            detail=str(exc),
        )
    if not recorded:
        return CheckResult(
            name, mode, Status.SKIP,
            "checksum column absent — database predates migration 062",
        )

    mismatches, unverifiable = _checksum_drift(recorded, migrations_dir)

    if mismatches:
        return CheckResult(
            name, mode, Status.FAIL,
            f"{len(mismatches)} migration file(s) changed after being applied",
            detail="\n".join(mismatches) + (
                "\n\nThe deployed schema and the source no longer describe the "
                "same thing, and re-running is a no-op because the version is "
                "already registered. Do NOT edit the checksum to match. Land a "
                "NEW forward migration that makes the deployed schema agree with "
                "the file, following the shape of "
                "061_lease_plane_sensor_status_check_repair.sql."
            ),
        )

    anchored = len(recorded) - len(unverifiable)
    if unverifiable:
        return CheckResult(
            name, mode, Status.PASS,
            f"{anchored}/{len(recorded)} migrations content-anchored; "
            f"{len(unverifiable)} pre-062 and unverifiable",
            detail=(
                "Unverifiable versions: "
                + ", ".join(str(v) for v in unverifiable[:20])
                + ("…" if len(unverifiable) > 20 else "")
                + "\n\nThese were applied before checksums existed, so what ran "
                  "is unknowable. This is the honest state, not a defect to fix "
                  "— never back-fill from source files. The count falls only "
                  "when migrations are legitimately applied to a fresh database."
            ),
        )

    return CheckResult(
        name, mode, Status.PASS,
        f"all {anchored} applied migrations match their source files",
    )


def check_constraint_drift(db_url: str, repo_root: Path) -> CheckResult:
    """FAIL when a migration declares a constraint the live DB does not have.

    The companion to ``column_drift``: that one catches code referencing a
    column the DB lacks, this one catches the DB lacking an *enforcement rule*
    the migrations claim to have installed. Nothing else can see it — a missing
    CHECK does not crash anything, it just silently stops rejecting the writes
    it exists to reject.

    Found live on 2026-08-10: ``substrate_state_has_sensor_status`` (migration
    034) was absent from the governance DB for three months. Root cause is the
    general hazard this check exists to catch — ``core.schema_migrations``
    recorded version 34 applied at 2026-05-03T21:28:22Z, but the commit that
    finished the file landed 74 minutes LATER, so prod was migrated from an
    in-progress working tree and ``apply_migrations.py``, which plans by
    registered version and never by file content, never revisited it. Any
    migration applied from an uncommitted tree can half-land the same way.

    Tests cannot cover this: ``ensure_test_database_schema`` re-executes
    migration files IN FULL against the test DB, so the test population is
    correct by construction while production stays wrong. This is a property of
    the deployed database, and only a check against that database can see it.
    """
    name, mode = "constraint_drift", "local"
    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")

    migrations_dir = repo_root / "db" / "postgres" / "migrations"
    if not migrations_dir.is_dir():
        return CheckResult(name, mode, Status.SKIP, "no db/postgres/migrations directory")

    declared = _declared_constraints(migrations_dir)
    if not declared:
        return CheckResult(name, mode, Status.SKIP, "no ADD CONSTRAINT statements found")

    live = _fetch_live_constraints(db_url)
    if live is None:
        return CheckResult(name, mode, Status.SKIP, "pg_constraint not queryable")

    live_tables = {tbl for tbl, _ in live}
    missing = [
        f"{tbl}.{cname}  (declared by {src})"
        for (tbl, cname), src in sorted(declared.items())
        # A table absent entirely is a different failure; schema_migrations and
        # column_drift own that, and reporting it here would just double-count.
        if tbl in live_tables and (tbl, cname) not in live
    ]

    if missing:
        return CheckResult(
            name, mode, Status.FAIL,
            f"{len(missing)} constraint(s) declared by migrations are missing from the DB",
            detail="\n".join(missing) + (
                "\n\nThe migration that declares it is almost certainly already "
                "registered in core.schema_migrations, so re-running it is a no-op "
                "— apply_migrations.py plans by version, not by file content. "
                "Repair with a NEW forward migration that re-adds the constraint "
                "(see 061_lease_plane_sensor_status_check_repair.sql for the shape, "
                "including the post-condition that refuses to register a repair it "
                "did not actually make). Check for violating rows first: adding the "
                "constraint re-validates the whole table."
            ),
        )
    return CheckResult(
        name, mode, Status.PASS,
        f"all {len(declared)} migration-declared constraint(s) present in the DB",
    )


def check_elixir_deprecated_scheme_lint(db_url: str, repo_root: Path) -> CheckResult:
    """Phase B prep (RFC §7.11.8): WARN if any Elixir source mentions a
    surface_kind currently in lease_plane.deprecated_schemes.

    Phase 0 deprecation marks a kind, Phase 2 sweeps surviving leases, Phase 3
    finalizes. Between Phase 0 and Phase 3 the operator (and CI) needs a way
    to verify no Elixir source still bakes the deprecated scheme into pattern
    matches or hardcoded strings — otherwise the post-Phase-3 grammar CHECK
    migration breaks the Elixir router on first acquire.

    Match heuristic: `f'"{kind}:'` matches double-quoted scheme prefix
    literals (`"file:"`, `"dialectic:/"`, etc.) — covers both pattern-match
    arms (`"dialectic:" <> rest -> ...`) and concatenated string literals.
    Comments mentioning the kind are also flagged (acceptable false positive;
    operator review is the recovery path).

    SKIP if psql missing, deprecated_schemes table absent (lease plane not
    installed), or no `elixir/` directory in the repo. PASS if no kinds are
    deprecated. PASS if kinds are deprecated but no Elixir source mentions
    them. WARN with a per-file detail listing if hits exist.
    """
    name, mode = "elixir_deprecated_scheme_lint", "local"

    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")

    proc = subprocess.run(
        ["psql", db_url, "-Atqc",
         "SELECT surface_kind FROM lease_plane.deprecated_schemes ORDER BY surface_kind"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        return CheckResult(
            name, mode, Status.SKIP,
            "deprecated_schemes not queryable (lease plane not installed?)",
        )

    deprecated_kinds = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not deprecated_kinds:
        return CheckResult(name, mode, Status.PASS, "no deprecated schemes")

    elixir_root = repo_root / "elixir"
    if not elixir_root.is_dir():
        return CheckResult(name, mode, Status.SKIP, "no elixir/ directory")

    hits: list[str] = []
    for ex_file in sorted(elixir_root.glob("**/*.ex")):
        # Skip vendored deps — they're third-party and out of scope.
        # Path.parts is OS-neutral (council CONCERN 1: string-comparison on
        # str(Path) breaks on Windows separators; not a live bug since the
        # repo is macOS-only, but every other check in this file uses Path
        # operations — keep style consistent).
        if "deps" in ex_file.parts or "_build" in ex_file.parts:
            continue
        try:
            text = ex_file.read_text(errors="replace")
        except OSError:
            continue
        for kind in deprecated_kinds:
            if f'"{kind}:' in text:
                hits.append(f"{ex_file.relative_to(repo_root)}: mentions deprecated {kind!r}")

    if hits:
        return CheckResult(
            name, mode, Status.WARN,
            f"{len(hits)} Elixir source mention(s) of deprecated scheme(s) "
            f"({', '.join(deprecated_kinds)})",
            detail="\n".join(hits),
        )
    return CheckResult(
        name, mode, Status.PASS,
        f"no Elixir source mentions deprecated schemes ({', '.join(deprecated_kinds)})",
    )


def check_elixir_scheme_grammar_lint(db_url: str, repo_root: Path) -> CheckResult:
    """Phase B prep (RFC §7.11.8 inverse): FAIL if canonicalize.ex mentions a
    surface scheme NOT in the live `surface_id_grammar` CHECK constraint.

    Catches the inverse drift from `elixir_deprecated_scheme_lint`. That lint
    catches schemes deprecated-but-still-mentioned in Elixir; this one catches
    schemes mentioned-by-Elixir-but-not-in-grammar. If Elixir ships a
    `dispatch("foo:/" <> rest)` arm but the migration-026 CHECK doesn't allow
    `foo:/`, every acquire of that scheme fails the storage-layer constraint
    and the Elixir router 422s on first traffic — silent until then.

    Sources of truth:
      - Grammar: live `pg_constraint.surface_id_grammar` regex, parsed for
        the alternation list and reduced to scheme names.
      - Elixir mentions: `elixir/lease_plane/lib/unitares_lease_plane/canonicalize.ex`.
        Extracts both the `@canonical_schemes ~w(...)` wordlist and `defp
        dispatch("<scheme>:..." <> rest)` arms.

    SKIP if psql missing, surface_id_grammar absent (lease plane not
    installed), or canonicalize.ex absent. PASS if Elixir-mentioned schemes
    are a subset of grammar schemes. FAIL with the offending scheme(s)
    otherwise.
    """
    name, mode = "elixir_scheme_grammar_lint", "local"

    if shutil.which("psql") is None:
        return CheckResult(name, mode, Status.SKIP, "psql not on PATH")

    proc = subprocess.run(
        ["psql", db_url, "-Atqc",
         "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
         "WHERE conname = 'surface_id_grammar'"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return CheckResult(
            name, mode, Status.SKIP,
            "surface_id_grammar constraint not queryable (lease plane not installed?)",
        )

    constraint_def = proc.stdout.strip()
    # Extract the alternation body, e.g. `file://|dialectic:/|resident:/|capture:/|td:/`.
    m = re.search(r"\^\(([^)]+)\)", constraint_def)
    if not m:
        return CheckResult(
            name, mode, Status.SKIP,
            "could not parse scheme list from surface_id_grammar constraint",
            detail=constraint_def,
        )
    grammar_schemes: set[str] = set()
    for alt in m.group(1).split("|"):
        scheme = alt.split(":", 1)[0].strip()
        if scheme:
            grammar_schemes.add(scheme)

    canonicalize_path = (
        repo_root / "elixir" / "lease_plane" / "lib"
        / "unitares_lease_plane" / "canonicalize.ex"
    )
    if not canonicalize_path.is_file():
        return CheckResult(name, mode, Status.SKIP, "canonicalize.ex not present")

    try:
        text = canonicalize_path.read_text(errors="replace")
    except OSError as exc:
        return CheckResult(name, mode, Status.SKIP,
                           "canonicalize.ex unreadable", detail=str(exc))

    # scheme -> short site descriptor used in FAIL detail.
    elixir_mentions: dict[str, str] = {}

    # 1. Wordlist: `@canonical_schemes ~w(file dialectic resident maintenance capture td agent)`.
    for match in re.finditer(r"@canonical_schemes\s+~w\(([^)]+)\)", text):
        for scheme in match.group(1).split():
            if scheme:
                elixir_mentions.setdefault(scheme, "@canonical_schemes wordlist")

    # 2. Dispatch arms: `defp dispatch("<scheme>:..." <> rest)`. Scheme name is
    #    everything before the first `:`. Covers `"file://"`, `"dialectic:/"`,
    #    `"resident:/"`, `"capture:/"`, `"td:/"` consistently.
    for match in re.finditer(
        r'defp\s+dispatch\(\s*"([a-z][a-z0-9_-]*):', text
    ):
        scheme = match.group(1)
        elixir_mentions.setdefault(scheme, f'defp dispatch("{scheme}:..." <> rest)')

    drift = sorted(s for s in elixir_mentions if s not in grammar_schemes)
    if drift:
        detail_lines = [f"  {s}: {elixir_mentions[s]}" for s in drift]
        detail_lines.append(
            f"\nGrammar allows: {', '.join(sorted(grammar_schemes))}"
        )
        return CheckResult(
            name, mode, Status.FAIL,
            f"{len(drift)} Elixir scheme(s) not in grammar CHECK: "
            f"{', '.join(drift)}",
            detail="\n".join(detail_lines),
        )

    return CheckResult(
        name, mode, Status.PASS,
        f"canonicalize.ex schemes match grammar "
        f"({', '.join(sorted(grammar_schemes))})",
    )


def check_anchor_dir() -> CheckResult:
    name, mode = "anchor_directory", "local"
    if ANCHOR_DIR.is_dir():
        return CheckResult(name, mode, Status.PASS, f"{ANCHOR_DIR} exists")
    return CheckResult(name, mode, Status.WARN,
                       f"{ANCHOR_DIR} missing — first onboard() will create it")


def check_secrets_file() -> CheckResult:
    name, mode = "secrets_file", "local"
    if not SECRETS_FILE.exists():
        return CheckResult(name, mode, Status.WARN,
                           f"{SECRETS_FILE} not present (only needed if calling external providers)")
    actual = stat.S_IMODE(SECRETS_FILE.stat().st_mode)
    if actual == 0o600:
        return CheckResult(name, mode, Status.PASS, f"{SECRETS_FILE} (0600)")
    return CheckResult(name, mode, Status.FAIL,
                       f"{SECRETS_FILE} mode is {oct(actual)} — must be 0600",
                       detail=f"chmod 600 {SECRETS_FILE}")


# ---------------------------------------------------------------------------
# Operator-mode checks
# ---------------------------------------------------------------------------


def check_http_listening() -> CheckResult:
    """Is something accepting TCP connections on 8767? Fast signal, separate
    from HTTP responsiveness so a slow event loop doesn't masquerade as a
    dead server."""
    name, mode = "http_listening", "operator"
    try:
        with socket.create_connection(("127.0.0.1", 8767), timeout=1):
            return CheckResult(name, mode, Status.PASS, "TCP listener on 127.0.0.1:8767")
    except (ConnectionError, socket.timeout, OSError) as e:
        return CheckResult(name, mode, Status.FAIL,
                           "no TCP listener on 8767", detail=str(e))


def check_http_health() -> CheckResult:
    """Does /health/live respond within 5s? A slow event loop (e.g., a
    process_agent_update holding a per-agent lock) can stall this even when
    the listener is up — that's a *latency* finding, not a *down* finding."""
    name, mode = "http_health", "operator"
    try:
        with urllib.request.urlopen(HTTP_HEALTH_URL, timeout=5) as resp:
            if resp.status == 200:
                return CheckResult(name, mode, Status.PASS,
                                   f"{HTTP_HEALTH_URL} returned 200")
            return CheckResult(name, mode, Status.FAIL,
                               f"{HTTP_HEALTH_URL} returned {resp.status}")
    except socket.timeout:
        return CheckResult(name, mode, Status.WARN,
                           "/health/live did not respond within 5s",
                           detail="event loop may be saturated by a slow handler — check mcp_server_error.log for `lock_timeout` lines")
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        return CheckResult(name, mode, Status.FAIL,
                           "HTTP health endpoint unreachable",
                           detail=str(e))


def _http_health_available(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(HTTP_HEALTH_URL, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pid_file_context(service_active: bool) -> str:
    if service_active:
        return (
            "live service detected; this checkout may not be the launchd "
            "working directory"
        )
    return "server not running, or stdio mode"


def check_pid_file(repo_root: Path, service_active: bool = False) -> CheckResult:
    name, mode = "pid_file", "operator"
    pid_file = repo_root / PID_FILE_REL
    service_active = service_active or _http_health_available()
    if not pid_file.exists():
        return CheckResult(name, mode, Status.WARN,
                           f"{pid_file} missing — {_pid_file_context(service_active)}")
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return CheckResult(name, mode, Status.FAIL,
                           f"{pid_file} is not a valid PID")
    try:
        os.kill(pid, 0)
        return CheckResult(name, mode, Status.PASS, f"pid {pid} alive")
    except ProcessLookupError:
        if service_active:
            return CheckResult(
                name, mode, Status.WARN,
                f"pid {pid} not running (stale file) — {_pid_file_context(True)}",
            )
        return CheckResult(name, mode, Status.FAIL, f"pid {pid} not running (stale file)")
    except PermissionError:
        return CheckResult(name, mode, Status.PASS, f"pid {pid} alive (not signalable)")


def _launchctl_loaded() -> set[str]:
    if shutil.which("launchctl") is None:
        return set()
    proc = subprocess.run(["launchctl", "list"],
                          capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        return set()
    out = set()
    for line in proc.stdout.splitlines()[1:]:  # skip header
        parts = line.split(None, 2)
        if len(parts) == 3:
            out.add(parts[2])
    return out


def check_launchagent(loaded: set[str]) -> CheckResult:
    name, mode = "launchagent_loaded", "operator"
    label = GOVERNANCE_LAUNCHD_LABEL
    if label in loaded:
        return CheckResult(name, mode, Status.PASS, f"{label} loaded")
    return CheckResult(name, mode, Status.WARN,
                       f"{label} not loaded — stdio mode is fine, "
                       f"but `unitares` CLI / remote MCP clients need this")


def check_resident_agents(loaded: set[str]) -> CheckResult:
    name, mode = "resident_agents", "operator"
    missing: list[str] = []
    resolved: list[str] = []
    for slot_name, labels in RESIDENT_LAUNCHD_SLOTS:
        present = [label for label in labels if label in loaded]
        if present:
            resolved.append(f"{slot_name}={'+'.join(present)}")
        else:
            missing.append(f"{slot_name} ({' or '.join(labels)})")
    if not missing:
        return CheckResult(name, mode, Status.PASS,
                           f"resident agents loaded: {', '.join(resolved)}")
    return CheckResult(name, mode, Status.WARN,
                       f"resident agents not loaded: {', '.join(missing)}")


def check_ipv6_sidecar(loaded: set[str]) -> CheckResult:
    name, mode = "ipv6_sidecar", "operator"
    label = "com.unitares.ipv6-loopback-proxy"
    if label in loaded:
        return CheckResult(name, mode, Status.PASS, f"{label} loaded")
    return CheckResult(name, mode, Status.SKIP,
                       f"{label} not loaded (only needed if cloudflared 2026.3+ is exposing /ws/eisv)")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _redact(db_url: str) -> str:
    if "@" in db_url:
        creds, _, rest = db_url.partition("@")
        scheme, _, _ = creds.partition("://")
        return f"{scheme}://***@{rest}"
    return db_url


def check_dockerfile_pinned_tags(repo_root: Path) -> CheckResult:
    """FAIL if any Dockerfile base image or compose `image:` uses a floating
    tag (`:latest`, or no tag at all).

    Why this is a gate: `apache/age:latest` silently floated PG17 -> PG18
    upstream and broke the documented `docker compose up && make demo`
    quickstart (pgvector compiled against the wrong PG headers, the PG18
    entrypoint rejected the old volume mount). Nothing caught it because the
    base image was unpinned and the quickstart was never built in CI. A pinned
    digest/tag turns "upstream moved under us" into an explicit, reviewable
    version bump (which Dependabot's docker ecosystem then proposes).

    A `:latest` literal or a tagless `FROM image` / `image: name` is a FAIL.
    Pinned tags (`:release_PG18_1.7.0`), digests (`@sha256:...`), and build
    references to other stages (`FROM builder`) are fine. Local build stage
    names declared earlier in the same file are not flagged.
    """
    name, mode = "dockerfile_pinned_tags", "local"

    # Dockerfiles anywhere + root compose files. Skip vendored/build trees.
    skip_parts = {"node_modules", "deps", "_build", ".git", ".venv", "venv"}
    targets: list[Path] = []
    for pat in ("**/Dockerfile", "**/Dockerfile.*"):
        targets += repo_root.glob(pat)
    for pat in ("docker-compose.yml", "docker-compose.yaml",
                "docker-compose.*.yml", "docker-compose.*.yaml"):
        targets += repo_root.glob(pat)
    targets = [p for p in sorted(set(targets))
               if not (skip_parts & set(p.parts))]

    def _is_floating(ref: str) -> bool:
        ref = ref.strip()
        if "@sha256:" in ref:           # digest-pinned
            return False
        # Strip a registry-host:port prefix so its colon isn't read as a tag.
        last = ref.rsplit("/", 1)[-1]
        if ":" not in last:             # no tag at all -> floats to :latest
            return True
        return last.rsplit(":", 1)[1] == "latest"

    offenders: list[str] = []
    for f in targets:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        stage_names: set[str] = set()
        for i, raw in enumerate(lines, 1):
            line = raw.strip()
            if line.startswith("FROM "):
                parts = line[5:].split()
                if not parts:
                    continue
                image = parts[0]
                # `FROM x AS name` registers a local stage; later `FROM name`
                # referencing it is not an external image.
                if image in stage_names:
                    pass
                elif _is_floating(image):
                    offenders.append(f"{f.relative_to(repo_root)}:{i} FROM {image}")
                if len(parts) >= 3 and parts[1].upper() == "AS":
                    stage_names.add(parts[2])
            elif line.startswith("image:"):
                ref = line.split(":", 1)[1].strip().strip('"').strip("'")
                if ref and _is_floating(ref):
                    offenders.append(f"{f.relative_to(repo_root)}:{i} image: {ref}")

    if not targets:
        return CheckResult(name, mode, Status.SKIP, "no Dockerfiles or compose files found")
    if offenders:
        return CheckResult(
            name, mode, Status.FAIL,
            f"{len(offenders)} floating base image tag(s) — pin to a version or digest",
            detail="\n".join(offenders),
        )
    return CheckResult(name, mode, Status.PASS,
                       f"all base images pinned ({len(targets)} file(s) scanned)")


def check_flags_catalog_fresh(repo_root: Path) -> CheckResult:
    """FAIL if docs/FLAGS.md is out of date vs the source flags.

    docs/FLAGS.md is generated by scripts/dev/flag_catalog.py from every
    UNITARES_*/GOVERNANCE_* env read in the tree. A flag added or removed without
    regenerating leaves the catalog silently wrong — the exact discoverability rot
    the catalog exists to prevent. Re-run the generator to refresh, then commit.
    """
    name, mode = "flags_catalog_fresh", "local"
    script = repo_root / "scripts" / "dev" / "flag_catalog.py"
    if not script.exists():
        return CheckResult(name, mode, Status.SKIP, "flag_catalog.py not present")
    proc = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return CheckResult(name, mode, Status.PASS, "docs/FLAGS.md is up to date")
    return CheckResult(
        name, mode, Status.FAIL, "docs/FLAGS.md is stale",
        detail=((proc.stderr or proc.stdout or "").strip()
                + "  -> run: python3 scripts/dev/flag_catalog.py"),
    )


def check_tool_edge_index_fresh(repo_root: Path) -> CheckResult:
    """FAIL if docs/dev/TOOL_EDGE_INDEX.md is out of date vs the live registries.

    The index resolves every tool -> handler -> action delegate -> params schema
    edge by importing the handler package, because none of those edges are
    written down anywhere statically (decorator registration, router closures, a
    string list of schema modules). A tool, action, or alias added without
    regenerating leaves the only readable map of dispatch silently wrong.

    SKIPs on exit 2 — the generator needs the handler package importable
    (requirements-core.txt). "Cannot look" is not "looked and found drift", and
    this check must stay honest on a pre-install tree like the rest of the
    doctor.
    """
    name, mode = "tool_edge_index_fresh", "local"
    script = repo_root / "scripts" / "dev" / "tool_edge_index.py"
    if not script.exists():
        return CheckResult(name, mode, Status.SKIP, "tool_edge_index.py not present")
    proc = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return CheckResult(name, mode, Status.PASS, "docs/dev/TOOL_EDGE_INDEX.md is up to date")
    if proc.returncode == 2:
        return CheckResult(
            name, mode, Status.SKIP, "handler package not importable",
            detail=(proc.stderr or proc.stdout or "").strip(),
        )
    return CheckResult(
        name, mode, Status.FAIL, "docs/dev/TOOL_EDGE_INDEX.md is stale",
        detail=((proc.stderr or proc.stdout or "").strip()
                + "  -> run: python3 scripts/dev/tool_edge_index.py"),
    )


def check_class_anchors_fresh(repo_root: Path) -> CheckResult:
    """WARN if the per-class anchors have gone stale, or if the two tables desync.

    `DELTA_NORM_MAX_BY_CLASS` and `HEALTHY_OPERATING_POINT_BY_CLASS`
    (config/governance_config.py) are ONE artifact, not two. A single
    `scripts/calibrate_class_conditional.py` run computes one `measured_on` and
    renders both tables from it, so a date read off the first is the date of the
    pair. Only the first carries `ScaleConstant` metadata — the second is a bare
    `Dict[str, Tuple[float, float, float]]` with nowhere to put a date — which is
    why this check reads dates from `DELTA_NORM_MAX_BY_CLASS` and asserts key
    parity rather than reading each table separately.

    That parity assertion is the load-bearing part. Before 2026-08-19 this check
    iterated `DELTA_NORM_MAX_BY_CLASS` alone and said nothing about the other
    table, while its docstring claimed to cover both. That mattered because the
    two feed different-liveness paths:

      DELTA_NORM_MAX_BY_CLASS      -> _compute_manifold denominator
                                      (grounding; shadow unless
                                       UNITARES_GROUNDING_APPLY is on)
      HEALTHY_OPERATING_POINT_BY_CLASS -> healthy_S -> get_s_setpoint
                                      (LIVE: UNITARES_S_SETPOINT defaults ON)

    i.e. the table that was checked is the shadow one, and the table that was not
    is the live one. If the key sets ever diverge, the co-generation invariant
    this check relies on is broken and the dates stop describing the live table —
    so divergence is reported rather than assumed away.

    WARN, never FAIL: staleness degrades a signal, it does not break the build.
    Keys on the OLDEST measured class, so a fresh refresh of one class cannot
    mask a stale neighbour (the Lumen 2026-06 case, where a stale anchor pinned
    manifold coherence at 0 on every check-in).

    Remediation is deliberately NOT a bare command. The generator is monolithic
    (no per-class selector), so refreshing one stale class recomputes every class
    in the window, and any class falling under `--n-min` is emitted as a comment
    rather than a value — which REMOVES it and routes its agents to the
    `DELTA_NORM_MAX_DEFAULT` / `HEALTHY_OPERATING_POINT_DEFAULT` placeholders.
    For `default` (N=16 in the 2026-06-27 window, against `--n-min 30`) that
    makes "refresh" a deletion. Whether a re-fit is authorised at all is a
    governance question, not a doctor question: see
    docs/ontology/eisv-proprioception-contract.md.
    """
    name, mode = "class_anchors_fresh", "local"
    stale_days = 90
    try:
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from config.governance_config import (
            DELTA_NORM_MAX_BY_CLASS,
            HEALTHY_OPERATING_POINT_BY_CLASS,
        )
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        # Co-generation invariant. If these diverge, the dates below no longer
        # describe HEALTHY_OPERATING_POINT_BY_CLASS and this check would be
        # silently reporting on only half of what it claims to cover.
        delta_keys = set(DELTA_NORM_MAX_BY_CLASS)
        healthy_keys = set(HEALTHY_OPERATING_POINT_BY_CLASS)
        if delta_keys != healthy_keys:
            only_delta = sorted(delta_keys - healthy_keys)
            only_healthy = sorted(healthy_keys - delta_keys)
            return CheckResult(
                name, mode, Status.WARN,
                "class anchor tables have diverged — dates no longer describe both",
                detail=(f"only in DELTA_NORM_MAX: {only_delta or 'none'}; "
                        f"only in HEALTHY_OPERATING_POINT: {only_healthy or 'none'}. "
                        "They are co-generated by one calibrate_class_conditional.py "
                        "run; a divergence means one was hand-edited."),
            )

        # Per-class, measured-only: an alias (provenance!='measured') is
        # deliberately not a measurement, so it never counts as stale.
        stale = []
        future = []
        undated = []
        for cls, sc in DELTA_NORM_MAX_BY_CLASS.items():
            if getattr(sc, "provenance", None) != "measured":
                continue
            mo = getattr(sc, "measured_on", None)
            if not mo:
                undated.append(cls)
                continue
            try:
                age = (now - datetime.strptime(mo, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
            except ValueError:
                # A measured constant whose date cannot be parsed is not "fine";
                # it is unverifiable. Surfaced rather than skipped.
                undated.append(cls)
                continue
            if age < 0:
                # Future-dated: a typo or clock skew. Without this branch the
                # entry passes the `age > stale_days` test forever, and reads as
                # indistinguishable from genuinely fresh.
                future.append((cls, age))
                continue
            if age > stale_days:
                stale.append((cls, age))

        problems = []
        if future:
            problems.append("future-dated: " + ", ".join(f"{c}({a}d)" for c, a in future))
        if undated:
            problems.append("measured but undated/unparseable: " + ", ".join(undated))
        if stale:
            stale.sort(key=lambda x: -x[1])
            problems.append("stale: " + ", ".join(f"{c}({a}d)" for c, a in stale))

        if not problems:
            return CheckResult(name, mode, Status.PASS,
                               f"all measured class anchors within {stale_days}d "
                               f"(both tables, {len(delta_keys)} classes)")

        if stale:
            worst = stale[0]
            summary = (f"{len(stale)} class anchor(s) stale "
                       f"(oldest {worst[0]} {worst[1]}d > {stale_days}d)")
        elif future:
            summary = f"{len(future)} class anchor(s) future-dated (clock skew or typo)"
        else:
            summary = f"{len(undated)} measured class anchor(s) carry no usable date"

        return CheckResult(
            name, mode, Status.WARN, summary,
            detail=("; ".join(problems)
                    + "  -> a refresh (scripts/calibrate_class_conditional.py) has no "
                      "per-class selector: it regenerates EVERY class in the window and "
                      "emits any class below --n-min as a comment, removing it. For "
                      "`default` (N=16 < 30) that is a deletion, not a refresh. Whether "
                      "a re-fit is authorised is a governance question: "
                      "docs/ontology/eisv-proprioception-contract.md"),
        )
    except Exception as e:
        return CheckResult(name, mode, Status.SKIP, f"anchor freshness check skipped: {e}")


# ---------------------------------------------------------------------------
# Telemetry-liveness checks (operator mode)
#
# Four incidents share one failure class: a telemetry channel that stops
# meaning anything without announcing it. tool_usage.success hardcoded true
# across 3.12M rows (2026-05); grounding enrichment silently no-op'd by
# pipeline ordering for weeks (2026-06); the 06-13 validation oneshot
# reporting 0.000 in every cohort because its join spanned disjoint identity
# namespaces; the Jul-09 broker cutover leaving Lumen governance-dark 10.5h.
# Each check below encodes one fingerprint: "this stream was alive, and now
# it is silent (or flat)". WARN, not FAIL — a dead sensor degrades evidence,
# it doesn't break the install.
# ---------------------------------------------------------------------------


def _psql_row(db_url: str, sql: str, timeout: int = 20) -> list[str] | None:
    """Run a single-row query via psql; return |-split fields or None on any error."""
    if shutil.which("psql") is None:
        return None
    proc = subprocess.run(
        ["psql", db_url, "-Atqc", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip().splitlines()[0].split("|")


def check_failure_label_live(db_url: str) -> CheckResult:
    """WARN if tool telemetry flows at volume but records zero failures.

    audit.tool_usage.success is the one EISV-blind external outcome label the
    schema holds. It sat hardcoded-true across 3.12M rows until 2026-05-30
    (PR #543) — making every discrimination test structurally impossible. A
    week of real traffic with 0 failures means the classifier regressed to
    that state, not that nothing failed.
    """
    name, mode = "failure_label_live", "operator"
    row = _psql_row(db_url, (
        "SELECT count(*), count(*) FILTER (WHERE success = false) "
        "FROM audit.tool_usage WHERE ts > now() - interval '7 days'"
    ))
    if row is None:
        return CheckResult(name, mode, Status.SKIP, "audit.tool_usage not queryable")
    calls, failures = int(row[0]), int(row[1])
    if calls == 0:
        return CheckResult(name, mode, Status.SKIP, "no tool telemetry in 7d (fresh install?)")
    if calls >= 10_000 and failures == 0:
        return CheckResult(
            name, mode, Status.WARN,
            f"0 failures across {calls} tool calls in 7d — failure classifier "
            "looks dead (hardcoded-true regression)",
            detail="see src/services/tool_usage_recorder.py classify_tool_result()",
        )
    return CheckResult(name, mode, Status.PASS,
                       f"{failures} failures / {calls} calls in 7d")


def check_checkin_stream_live(db_url: str) -> CheckResult:
    """WARN if the fleet-wide governance check-in stream has gone silent.

    Residents check in on minutes-to-30min cadences, so hours of zero
    process_agent_update/sync_state across the whole fleet is a transport or
    broker outage (Jul-09 Elixir-cutover class: Lumen governance-dark 10.5h
    while everything else looked healthy), not a quiet fleet.
    """
    name, mode = "checkin_stream_live", "operator"
    row = _psql_row(db_url, (
        "SELECT count(*) FILTER (WHERE ts > now() - interval '6 hours'), count(*) "
        "FROM audit.tool_usage "
        "WHERE tool_name IN ('process_agent_update', 'sync_state') "
        "AND ts > now() - interval '7 days'"
    ))
    if row is None:
        return CheckResult(name, mode, Status.SKIP, "audit.tool_usage not queryable")
    recent, week = int(row[0]), int(row[1])
    if week == 0:
        return CheckResult(name, mode, Status.SKIP, "no check-in history in 7d")
    if recent == 0:
        return CheckResult(
            name, mode, Status.WARN,
            f"0 check-ins in 6h (vs {week} over 7d) — fleet governance-dark; "
            "check broker/transport before anything else",
        )
    return CheckResult(name, mode, Status.PASS, f"{recent} check-ins in last 6h")


RESIDENT_MIN_CHECKINS = 20    # below this there is no cadence to be silent against
RESIDENT_MIN_ACTIVE_DAYS = 3  # a burst over one or two days is a task, not a resident
# ⛔Distinct calendar dates are NOT a duration, and counting them in UTC
# inflates evening work by one. Measured 2026-08-23: `opus_1a0de9ed`
# (purpose='deployment', 36 check-ins) worked the afternoon of Aug 20 and the
# evening of Aug 21 — TWO days — but Denver evening is next-day UTC, so it
# registered THREE UTC dates (08-20/21/22), cleared RESIDENT_MIN_ACTIVE_DAYS by
# exactly one, and was scored as a resident. It then warned forever, since a
# finished agent's last_seen only recedes: precisely the permanent-false-warning
# class the day threshold exists to prevent.
#
# Span is the timezone-independent form of the same intent. Measured the same
# day, the separation is far wider than the day count's: that task agent spanned
# 1.32 days while every real resident spanned 6.98-7.00. ⛔Both conditions are
# kept — days alone admits a long-running burst, span alone admits an agent that
# checked in twice a week apart.
RESIDENT_MIN_ACTIVE_SPAN_DAYS = 3


def _host_awake_s() -> float | None:
    """Seconds since this host last woke from sleep, None when unknowable.

    Central Postgres runs on this host, so while it sleeps NO agent can check
    in — every resident's ``last_seen`` recedes together, and the doctor jobs
    themselves coalesce and fire minutes after wake, sampling exactly the
    post-wake worst case. Silence that accrued during host sleep is therefore
    not attributable to any agent. macOS only (``kern.waketime``); any parse
    failure returns None so the caller fails open toward judging real silence.
    """
    out = _run_sysctl_waketime()
    m = re.search(r"sec\s*=\s*(\d+)", out)
    if not m:
        return None
    wake = float(m.group(1))
    if wake <= 0:
        return None
    return max(0.0, time.time() - wake)


def _run_sysctl_waketime() -> str:
    """Isolated for tests; returns raw ``sysctl -n kern.waketime`` output.

    Caveat measured 2026-08-05: DarkWakes bump ``kern.waketime`` too, so on a
    lid-closed TCPKeepAlive-churn night the derived awake time UNDER-counts.
    The error direction is safe for the clamp below (extra suppression while
    the host is not meaningfully serving), but it means the clamp re-arms
    from the most recent wake of any kind, not the last full wake.
    """
    if shutil.which("sysctl") is None:
        return ""
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "kern.waketime"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _db_is_local(db_url: str) -> bool:
    """True only when the DB host is demonstrably this machine.

    The awake-time clamp's whole premise is "the DB slept when this host
    slept". A doctor run from a just-woken laptop against a remote always-on
    Postgres would cap every resident's silence at the laptop's awake seconds
    — masking real outages on infrastructure that never slept — so the clamp
    must apply ONLY to a co-located DB and fail toward wall-clock silence
    everywhere else.
    """
    try:
        host = urllib.parse.urlsplit(db_url).hostname
    except ValueError:
        return False
    return host in (None, "", "localhost", "127.0.0.1", "::1")


def _event_driven_labels(timeout: float = 3.0) -> set[str]:
    """Resident labels the SERVER reports as event-driven.

    Asked of the server rather than decided here, because the disagreement is
    the defect: on 2026-08-15 `/v1/residents` reported Watcher healthy at 110s
    silence against a 48h threshold while this check warned it had been silent
    98min. Two surfaces answering "is this agent inactive?" about the same
    agent at the same instant, differently. Reading the answer from the one
    that owns the concept makes them structurally unable to diverge.

    Two rejected alternatives, both of which look right and are not:

    * A hardcoded event-driven roster — this check's own docstring argues at
      length against rosters, because they go stale silently. That argument
      does not stop applying just because the roster would be short.
    * Importing ``src.resident_progress.registry.is_event_driven_label`` —
      it resolves against a manifest named by
      ``UNITARES_RESIDENT_PROGRESS_MANIFEST``, and the doctor plists set only
      ``PATH``. The import would return False for every label and the
      exemption would be permanently, invisibly inert: a fix that reads as
      correct in review and does nothing in production.

    Fails OPEN — an empty set means every resident is judged, exactly as
    before. A doctor that goes quiet because the server it monitors is
    unreachable would be the worst possible failure mode for this check.
    """
    try:
        with urllib.request.urlopen(HTTP_RESIDENTS_URL, timeout=timeout) as resp:
            if resp.status != 200:
                return set()
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — availability of the exemption is optional
        return set()
    residents = payload.get("residents")
    if not isinstance(residents, list):
        return set()
    return {
        str(r.get("label"))
        for r in residents
        if isinstance(r, dict) and r.get("event_driven") is True and r.get("label")
    }


def _sql_string_list(values: set[str]) -> str:
    """Render labels as a SQL literal list. Labels arrive over HTTP, so quote."""
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))


def check_resident_checkin_stale(db_url: str) -> CheckResult:
    """WARN if an INDIVIDUAL agent has gone silent, against its OWN cadence.

    ``checkin_stream_live`` above is fleet-aggregate and only fires at exactly
    zero, so a single dead resident hides behind its healthy peers — which is
    precisely what happened on 2026-07-29: Sentinel stopped checking in for
    24h (lease-blocked upstream of GovernanceCheckin) while four other
    residents checked in every few minutes and every aggregate read PASS.

    Each agent is scored against a baseline derived from its own history
    rather than a fleet-wide cadence constant: hardcoded per-agent intervals
    go stale silently, and judging an agent against its own normal is the
    same posture the behavioral path takes. 6x an agent's own median gap is
    deliberately loose — a check that cries wolf gets ignored, and the failure
    mode being caught here is "stopped", not "a bit late". The 30-minute floor
    keeps fast residents from tripping on a single slow cycle.

    Ephemeral harness sessions are excluded structurally rather than by a
    hardcoded roster (rosters go stale silently — see the class-anchor
    calibration gap): a session identity carries a ``#`` fragment marker or a
    harness prefix, while residents are bare names. A finished session is
    *supposed* to stop, so scoring it here would generate exactly the
    permanent-warning noise that trains an operator to ignore the check.

    That marker test is necessary but not sufficient, and the gap it left is
    the reason for ``RESIDENT_MIN_ACTIVE_DAYS``: a **bare-named** task agent
    passes all three markers, and ``RESIDENT_MIN_CHECKINS`` alone does not
    exclude it because a short burst easily clears 20. Once scored it can never
    recover — a finished agent's ``last_seen`` only recedes, so ``silent_s``
    grows without bound and the test can never return to PASS. It ages out only
    when the 7-day window drops it, and the next named task agent recreates it.
    Measured 2026-08-02: ``SchmidtPacketAudit`` (32 check-ins, 1 active day) and
    ``fable-lease-triage`` (33 check-ins, 2 active days) were both permanently
    WARN while all five real residents read healthy — every warning the check
    emitted was false.

    Requiring activity on several distinct days is the same fix
    ``finding_producer_live`` already applies via ``PRODUCER_MIN_ACTIVE_DAYS``,
    for the same reason: a burst yields a tiny median gap, so the burst
    masquerades as high cadence and its perfectly healthy silence reads as
    death. Measured margin on the same day: task agents 1-2 active days, real
    residents 5-8 — the threshold sits in an empty band with no overlap.

    The cost is a stated warm-up: a genuinely new resident is unjudged for its
    first ``RESIDENT_MIN_ACTIVE_DAYS`` days. That is a bounded blind spot at the
    start of an agent's life, and it is preferable to an unbounded stream of
    false positives on the one check that exists because a real resident outage
    hid behind healthy peers.

    The threshold is the agent's own IDLE ENVELOPE, not just a multiple of its
    median: ``greatest(6 x median, 2 x p95, 30min)``. A hook-fired resident's
    cadence tracks operator activity, so its gap distribution is bimodal —
    measured on Watcher 2026-08-05 (635 check-ins/7d): p50=3.4min from editing
    bursts but p90=40min, p95=80min, max=256min from routine idle evenings. A
    median-only threshold (30min here) sits at ~p87 of that agent's NORMAL
    distribution, so every quiet evening warned: five times over 08-03..08-05,
    silent 50-115min, while Watcher's log showed clean check-ins and zero
    errors — it was not failing, it was not being invoked. 2 x p95 (~160min)
    suppresses all five from the agent's own history, no roster; for timer
    residents p95 ~ median so nothing changes, and the motivating incident
    (Sentinel silent 24h on 2026-07-29) still fires with a 9x margin even
    against Watcher's wide envelope — against Sentinel's OWN envelope
    (~36min) the margin is ~40x.

    Silence is also clamped to time since the HOST last woke — applied only
    when the DB is co-located (``_db_is_local``): central Postgres lives on
    this machine, so during host sleep no resident can check in and the
    interval-coalesced doctor fires minutes after wake — sampling the
    post-wake worst case. Two of the five Watcher warnings were mostly
    host-sleep (107 of 115 and 38 of 50 silent minutes asleep), and the same
    runs flagged Lumen and Steward as 94-163min silent for the sole reason
    that the laptop lid was closed. On hosts without ``kern.waketime`` (or
    with a remote DB) the clamp fails open to judging wall-clock silence.

    Stated mechanism, so nobody re-derives it optimistically: the clamp
    RE-ARMS FROM ZERO at each wake (including DarkWakes) — it does not
    accumulate awake time across sleep cycles. A dead resident is therefore
    caught on the first awake stretch longer than its envelope (30-160min
    across today's fleet — any normal working session), and until such a
    stretch occurs detection is deferred, not lost. The p95 term and the
    clamp are COMPLEMENTARY, both needed: the five 50-115min warnings fall
    inside 2 x p95 alone, but Watcher's raw 7d envelope also contains
    168-256min gaps which are all sleep-attributable — on a fully-awake day
    a genuine >160min Watcher idle would still warn until its own p95
    adapts, and the clamp is what absorbs the sleep-era tail meanwhile.
    """
    name, mode = "resident_checkin_stale", "operator"
    awake_s = _host_awake_s() if _db_is_local(db_url) else None
    # An event-driven resident has no cadence to be stale against: it fires on
    # external triggers, so silence measures operator activity, not health.
    # Worse, its envelope is self-referential -- a busy session compresses its
    # own median/p95, TIGHTENING the threshold until an ordinary quiet stretch
    # trips it. Measured on Watcher: p95 80min (2026-08-05, per the envelope
    # rationale above) -> 28min (2026-08-15), so 2 x p95 fell 160min -> 56min
    # and routine 98min gaps began warning. The envelope fix narrowed this
    # class; it cannot close it, because the premise it rests on does not hold
    # for a producer with no cadence of its own.
    exempt = _event_driven_labels()
    exempt_clause = (
        f" AND label NOT IN ({_sql_string_list(exempt)})" if exempt else ""
    )
    attributable_silence = (
        f"least(EXTRACT(epoch FROM (now() - last_seen)), {awake_s:.0f})"
        if awake_s is not None
        else "EXTRACT(epoch FROM (now() - last_seen))"
    )
    row = _psql_row(db_url, (
        "WITH gaps AS ("
        "  SELECT a.label, s.recorded_at,"
        "         s.recorded_at - lag(s.recorded_at) OVER "
        "           (PARTITION BY a.label ORDER BY s.recorded_at) AS gap"
        "  FROM core.identities i"
        "  JOIN core.agent_state s ON s.identity_id = i.identity_id"
        "  JOIN core.agents a ON a.id = i.agent_id"
        "  WHERE s.synthetic IS NOT TRUE AND a.label IS NOT NULL"
        "    AND a.label NOT LIKE '%#%'"
        "    AND a.label NOT ILIKE 'claude%' AND a.label NOT ILIKE 'codex%'"
        "    AND s.recorded_at > now() - interval '7 days'), "
        "stats AS ("
        "  SELECT label, count(*) AS n, max(recorded_at) AS last_seen,"
        "         percentile_cont(0.5) WITHIN GROUP "
        "           (ORDER BY EXTRACT(epoch FROM gap)) AS med_gap,"
        "         percentile_cont(0.95) WITHIN GROUP "
        "           (ORDER BY EXTRACT(epoch FROM gap)) AS p95_gap"
        "  FROM gaps WHERE gap IS NOT NULL"
        "  GROUP BY label"
        f"  HAVING count(*) >= {RESIDENT_MIN_CHECKINS}"
        "     AND count(DISTINCT (recorded_at AT TIME ZONE 'UTC')::date) "
        f"         >= {RESIDENT_MIN_ACTIVE_DAYS}"
        "     AND max(recorded_at) - min(recorded_at) "
        f"         >= interval '{RESIDENT_MIN_ACTIVE_SPAN_DAYS} days'), "
        "stale AS ("
        "  SELECT label, med_gap, p95_gap,"
        "         EXTRACT(epoch FROM (now() - last_seen)) AS silent_s,"
        f"         {attributable_silence} AS attr_s"
        "  FROM stats"
        f"  WHERE {attributable_silence} "
        "        > greatest(med_gap * 6, p95_gap * 2, 1800)"
        f"{exempt_clause}) "
        "SELECT (SELECT count(*) FROM stats), (SELECT count(*) FROM stale), "
        "       coalesce((SELECT string_agg("
        "         label || ' silent ' || round(silent_s/60.0) || 'min "
        "(attributable ' || round(attr_s/60.0) || 'min, own median ' "
        "|| round(med_gap/60.0) || 'min, p95 ' "
        "|| round(p95_gap/60.0) || 'min)', '; ' "
        "         ORDER BY silent_s DESC) FROM stale), '')"
    ))
    if row is None:
        return CheckResult(name, mode, Status.SKIP, "core.agent_state not queryable")
    tracked, stale, detail = int(row[0]), int(row[1]), row[2]
    exempt_note = (
        f" ({len(exempt)} event-driven, exempt: {', '.join(sorted(exempt))})"
        if exempt else ""
    )
    if tracked == 0:
        return CheckResult(
            name, mode, Status.SKIP,
            f"no agent has {RESIDENT_MIN_CHECKINS}+ check-ins across "
            f"{RESIDENT_MIN_ACTIVE_DAYS}+ distinct days spanning "
            f"{RESIDENT_MIN_ACTIVE_SPAN_DAYS}+ days in 7d (fresh install?)")
    if stale:
        return CheckResult(
            name, mode, Status.WARN,
            f"{stale} of {tracked} residents silent past their own idle "
            f"envelope: {detail}{exempt_note}",
            detail="a live PID proves nothing — check the agent's log for an "
                   "upstream gate (see immortal_lease) before assuming a crash",
        )
    return CheckResult(
        name, mode, Status.PASS,
        f"{tracked} residents all within their own idle envelope{exempt_note}")


def check_immortal_lease(db_url: str) -> CheckResult:
    """WARN on live leases renewed far past any sane TTL — the orphan signature.

    An acquire that succeeds server-side but misses the client's
    ``lease_plane_timeout_ms`` leaves the client without a ``lease_id``, so it
    can never release. The lease-plane-side holder then auto-renews every
    TTL/3 forever with ``holder_pid`` NULL, and ``Reaper.perform`` only sweeps
    leases whose ``expires_at`` is already past — which never happens. The TTL
    becomes decorative and the lease is immortal, silently blocking every
    later acquire on that surface (2026-07-29: Sentinel's check-in emitter,
    291 consecutive skipped ticks, nothing escalated).

    Detection is span-based rather than holder-based because the orphan's
    tell is that ``expires_at - acquired_at`` keeps growing past the TTL it
    was granted with, while a healthy lease's span stays at its TTL. The
    35-minute threshold sits above the longest legitimate TTL in use (the
    30-minute dispatch surface) so a normal long-lived lease does not trip it.

    Span alone is NOT sufficient: the router routes every ``resident:/``
    acquire onto the local_beam auto-renew path regardless of the client's
    requested holder_kind (``http_router.ex acquire_for_surface`` — residents
    rely on server-side auto-renew for continuity), so a HEALTHY resident
    presence lease also grows span >> TTL for the gov-mcp process's lifetime.
    Across 2026-07-31/08-01 ``resident:/steward`` — actively client-renewed
    every sync cycle — was flagged by this check and force-released SEVEN
    times; at every kill the client had renewed 0.1-3.9 minutes earlier.
    Each release was followed by a next-cycle re-acquire and a re-flag ~35
    minutes later (the false-positive kill loop this exclusion closes).

    The discriminator is client contact: resident client renews carry a
    substrate observation, so they refresh ``substrate_state_observed_at``;
    the plane-side auto-renew never touches it. A lease whose observation
    timestamp is fresh has a live renewer and is excluded. True orphans
    (never any substrate payload, or the client died) have it NULL or stale
    and still warn — verified against the 2026-08-01 incident set: the
    genuinely stranded ``resident:/ship_sh_claude/adjudication-evidence`` and
    ``resident:/sentinel_cycle`` leases carry NULL, live steward carried a
    seconds-old timestamp.
    """
    name, mode = "immortal_lease", "operator"
    row = _psql_row(db_url, (
        "SELECT count(*), coalesce(string_agg(DISTINCT surface_id, ', '), '') "
        "FROM lease_plane.surface_leases "
        "WHERE released_at IS NULL AND expires_at > now() "
        "AND (expires_at - acquired_at) > interval '35 minutes' "
        "AND (substrate_state_observed_at IS NULL "
        "     OR substrate_state_observed_at < now() - interval '35 minutes')"
    ))
    if row is None:
        return CheckResult(name, mode, Status.SKIP,
                           "lease_plane.surface_leases not queryable "
                           "(lease plane not installed?)")
    count, surfaces = int(row[0]), row[1]
    if count:
        return CheckResult(
            name, mode, Status.WARN,
            f"{count} lease(s) renewed past any sane TTL with no recent "
            f"client contact: {surfaces}",
            detail="confirm the holder is really gone before acting — a fresh "
                   "substrate_state_observed_at or renew events off the TTL/3 "
                   "grid mean a LIVE client (do NOT release); only then "
                   "force-release via POST /v1/lease/force-release on the "
                   "lease plane (LEASE_FORCE_RELEASE_TOKEN, a separate "
                   "per-path token) — releasing kills the renew timer and the "
                   "next tick acquires cleanly",
        )
    return CheckResult(name, mode, Status.PASS, "no immortal leases")


def check_grounding_stage_live(db_url: str) -> CheckResult:
    """WARN if grounding shadow events were flowing and have stopped.

    The grounding enrichment ran as a silent no-op for weeks in 2026-06
    because it executed after persist/response-build — no error, no signal
    (PR #1095). With UNITARES_GROUNDING_SHADOW on, every check-in emits a
    grounding_shadow audit event; a stream that was alive over the week but
    empty for a day means the stage detached again.
    """
    name, mode = "grounding_stage_live", "operator"
    row = _psql_row(db_url, (
        "SELECT count(*) FILTER (WHERE ts > now() - interval '24 hours'), count(*) "
        "FROM audit.events WHERE event_type = 'grounding_shadow' "
        "AND ts > now() - interval '7 days'"
    ))
    if row is None:
        return CheckResult(name, mode, Status.SKIP, "audit.events not queryable")
    day, week = int(row[0]), int(row[1])
    if week == 0:
        return CheckResult(name, mode, Status.SKIP,
                           "no grounding_shadow events in 7d (shadow flag off?)")
    if day == 0:
        return CheckResult(
            name, mode, Status.WARN,
            f"grounding_shadow went silent (0 in 24h vs {week} over 7d) — "
            "enrichment stage likely detached from the check-in pipeline again",
        )
    return CheckResult(name, mode, Status.PASS,
                       f"{day} grounding_shadow events in 24h")


def check_cold_start_pause_canary(db_url: str) -> CheckResult:
    """WARN if a non-authored Phi cold-start pause fires again after #1819.

    A brand-new identity has no behavioral evidence, so the verdict is owned by
    the Phi cold-start prior, which the result envelope itself labels
    non-discriminative. Before #1819 that prior could still deliver a
    circuit-breaker pause on an agent's first or second turn -- 15 of them
    across 14 identities in the 12 days to 2026-08-22, 7 of which were that
    session's last recorded act. #1819 downgrades a *proven* risk-only
    cold-start hard stop to guidance, so the expected steady state is zero.

    Zero is also what this check sees when nothing is looking, which is the
    whole reason it exists. The denominator is cold-start *decisions* of any
    action: if no identity has been through cold start at all in the window,
    the population is empty and the check SKIPs rather than reporting a clean
    zero. A pass therefore means "cold starts happened and none of them
    paused", never "no evidence either way".

    Detection only -- it reports a finding and stops. It must never re-check
    itself and post an outcome: `build_resolution_outcome_args` hardcodes
    `verification_source='external_signal'`, which tiers TRUSTED_EXTERNAL, and
    a signal derived from the loop cannot anchor the loop (Invariant 4).
    """
    name, mode = "cold_start_pause_canary", "operator"
    row = _psql_row(db_url, (
        "WITH d AS ("
        "  SELECT state_json->'eisv_telemetry'#>>'{policy_evaluation,action}' AS act,"
        "         state_json->'eisv_telemetry'#>>'{policy_evaluation,inputs,verdict_source}' AS vsrc"
        "  FROM core.agent_state"
        "  WHERE recorded_at > now() - interval '7 days'"
        "    AND state_json ? 'eisv_telemetry')"
        "SELECT count(*) FILTER (WHERE vsrc = 'phi_cold_start'),"
        "       count(*) FILTER (WHERE vsrc = 'phi_cold_start' AND act = 'pause')"
        " FROM d"
    ))
    if row is None or len(row) < 2:
        return CheckResult(name, mode, Status.SKIP, "core.agent_state not queryable")
    cold_starts, pauses = int(row[0]), int(row[1])
    if cold_starts == 0:
        return CheckResult(
            name, mode, Status.SKIP,
            "no phi_cold_start decisions in 7d — nothing to observe, so a zero "
            "here would not mean the guard is working",
        )
    if pauses:
        return CheckResult(
            name, mode, Status.WARN,
            f"{pauses} phi_cold_start pause(s) in 7d across {cold_starts} "
            "cold-start decisions — #1819 downgrades a proven risk-only cold "
            "start to guidance, so check in order: is #1819 actually DEPLOYED "
            "(compare the running build_sha, not master), is "
            "GOVERNANCE_NON_AUTHORED_COLD_START_GUARD on, and did an "
            "independent hard stop legitimately fire",
        )
    return CheckResult(name, mode, Status.PASS,
                       f"0 pauses across {cold_starts} cold-start decisions in 7d")


def check_label_join_overlap(db_url: str) -> CheckResult:
    """WARN if the failure-labeled and check-in populations are fully disjoint.

    Validating EISV against the exogenous failure label requires agents that
    BOTH check in AND have recorded failures. The 06-13 scheduled validation
    returned 0.000 in every cohort because the two populations lived in
    disjoint identity namespaces — a broken join that read as a clean result.
    Overlap is expected to be small (participation is low); zero, with both
    sides populated, is the broken-join fingerprint.
    """
    name, mode = "label_join_overlap", "operator"
    row = _psql_row(db_url, (
        "WITH failers AS (SELECT DISTINCT agent_id FROM audit.tool_usage "
        "  WHERE success = false AND ts > now() - interval '30 days'), "
        "checkers AS (SELECT DISTINCT agent_id FROM audit.tool_usage "
        "  WHERE tool_name IN ('process_agent_update', 'sync_state') "
        "  AND ts > now() - interval '30 days') "
        "SELECT (SELECT count(*) FROM failers), (SELECT count(*) FROM checkers), "
        "(SELECT count(*) FROM failers f JOIN checkers c USING (agent_id))"
    ), timeout=30)
    if row is None:
        return CheckResult(name, mode, Status.SKIP, "audit.tool_usage not queryable")
    failers, checkers, overlap = int(row[0]), int(row[1]), int(row[2])
    if failers == 0 or checkers == 0:
        return CheckResult(name, mode, Status.SKIP,
                           f"one side empty (failers={failers}, checkers={checkers})")
    if overlap == 0:
        return CheckResult(
            name, mode, Status.WARN,
            f"failure-labeled and check-in populations disjoint "
            f"(failers={failers}, checkers={checkers}, overlap=0) — "
            "EISV-vs-outcome validation join is structurally impossible",
        )
    return CheckResult(
        name, mode, Status.PASS,
        f"{overlap} of {failers} failure-bearing agents also check in "
        f"({checkers} checkers, 30d)",
    )


# Metric columns on core.agent_state whose fleet-level dynamic range is useful
# to screen. All are bounded in [0, 1] (or [-1, 1]), so one absolute floor is a
# practical operator heuristic. It is not an information-theoretic test: low
# population SD can hide useful within-agent or outcome-linked structure.
DEGENERACY_METRICS = ("coherence", "entropy", "integrity", "risk_score")
DEGENERACY_MIN_N = 500      # below this, flatness is small-sample, not a defect
DEGENERACY_SD = 0.01        # measured margin: nearest healthy metric is 4x above
DEGENERACY_FIELDS_PER_METRIC = 5  # sd, distinct, n, min, max


def check_signal_degeneracy(db_url: str) -> CheckResult:
    """WARN when a live metric needs a producer/consumer dynamic-range review.

    The sibling checks above ask "is this stream still flowing?". This one asks
    the other half of the same question — "does this signal have enough fleet
    dynamic range for its consumers?" — which the section header has always
    claimed ("silent *or flat*") but nothing implemented. A metric pinned to
    one value, or wobbling only in its fourth decimal, keeps flowing forever and
    can leave a fixed-threshold consumer effectively inert.

    Two degeneracy modes, both seen in production:
      * constant  — exactly one distinct value
      * collapsed — many distinct values but dispersion near zero (coherence,
        which moves only in the 4th decimal)

    Calibration, measured on live data over 7d (n=6232) rather than chosen:
        stability_index  sd 0.000000, 1 distinct   <- constant
        coherence        sd 0.005262               <- collapsed
        integrity        sd 0.043202               <- healthy, 4x the floor
        risk_score       sd 0.061173               <- healthy
        entropy          sd 0.080398               <- healthy

    `stability_index` was the original `constant` exemplar and is no longer
    checked: it was a dead field (retired 20684dd1) that the INSERT path kept
    writing as a hardcoded 0.0. Migration 058 finished that retirement — the
    column is nullable, the sentinels are NULL, and the writer omits it — so
    there is no longer a signal there to be degenerate. Dropping it from this
    tuple is the point of the fix, not a weakening of the check: a permanent
    expected-WARN trains operators to ignore the check that carries it.

    KNOWN GAP, stated so nobody over-trusts this: population dispersion does not
    catch every degenerate metric. `lineage_similarity` is saturated — pinned
    near 0.633 for any long-lived agent — yet its stored population sd is 0.096,
    because reseeds write 1.0 and short-lived agents spread out. Its degeneracy
    is only visible by recomputing the metric against a covariate that should
    drive it (observation_count spanning 1520x moves it 0.0095). Catching that
    class needs a per-metric hypothesis about what *should* vary the signal, and
    is deliberately out of scope here.

    Important limit: population SD is a screening statistic, not mutual
    information and not predictive validity. A low-SD metric may still encode
    within-agent, cohort, temporal, or outcome-linked structure. This check
    therefore asks for contract review; it does not declare the producer dead.

    WARN, not FAIL — insufficient dynamic range can degrade a consumer without
    breaking the install. It is still expected to fire on arrival: coherence
    has low fleet dispersion as of 2026-07-30 and remains so.
    """
    name, mode = "signal_degeneracy", "operator"
    cols = ", ".join(
        f"stddev({m}::numeric), count(DISTINCT {m}), count({m}), "
        f"min({m}::numeric), max({m}::numeric)" for m in DEGENERACY_METRICS
    )
    # Exclude server-authored bootstrap rows. They are labelled `synthetic`
    # precisely because they are not measurements — the onboarding contract
    # already excludes them from calibration, outcome correlation, trust-tier
    # observation counts and real-check-in counts, and `resident_checkin_stale`
    # above filters them for the same reason. This check did not, and one row
    # is enough to wreck the statistic it exists to compute: on 2026-08-21 a
    # single synthetic row carrying coherence=1.0000 moved the reported range
    # from [0.4659, 0.5039] to [0.4659, 1.0000] and sd from 0.006818 to
    # 0.008601 over n=9930. A dynamic-range check that counts non-measurements
    # reports 14x the range the consumers actually see, and it errs toward
    # "healthy" — the direction that retires an alarm that should stand.
    row = _psql_row(db_url, (
        f"SELECT {cols} FROM core.agent_state "
        "WHERE recorded_at > now() - interval '7 days' "
        "AND synthetic IS NOT TRUE"
    ), timeout=30)
    if row is None or len(row) < DEGENERACY_FIELDS_PER_METRIC * len(DEGENERACY_METRICS):
        return CheckResult(name, mode, Status.SKIP, "core.agent_state not queryable")

    review, healthy, thin = [], [], []
    for i, metric in enumerate(DEGENERACY_METRICS):
        offset = DEGENERACY_FIELDS_PER_METRIC * i
        sd_raw, distinct_raw, n_raw, min_raw, max_raw = row[offset:offset + 5]
        try:
            n, distinct = int(n_raw), int(distinct_raw)
        except ValueError:
            continue
        if n < DEGENERACY_MIN_N:
            thin.append(f"{metric} (n={n})")
            continue
        if distinct <= 1:
            review.append(f"{metric}: constant in 7d (n={n})")
            continue
        try:
            sd, low, high = float(sd_raw), float(min_raw), float(max_raw)
        except ValueError:
            continue
        if sd < DEGENERACY_SD:
            review.append(
                f"{metric}: low fleet dispersion sd={sd:.6f}, "
                f"range=[{low:.4f}, {high:.4f}] over {distinct} values (n={n})"
            )
        else:
            healthy.append(f"{metric} sd={sd:.4f}")

    if not review and not healthy:
        return CheckResult(name, mode, Status.SKIP,
                           f"insufficient state rows in 7d ({', '.join(thin) or 'none'})")
    if review:
        return CheckResult(
            name, mode, Status.WARN,
            f"{len(review)} metric(s) need dynamic-range review: " + "; ".join(review),
            detail=(
                "Low population SD is a screening heuristic, not proof of zero "
                "information. Compare the observed range with every configured "
                "consumer threshold; then check producer provenance, within-agent "
                "and cohort variation, autocorrelation/effective sample size, and "
                "outcome association. Retire or repair a field only from that full "
                "contract review, and do not recalibrate merely to force crossings. "
                + (f"Other metrics: {', '.join(healthy)}." if healthy else "")
            ),
        )
    return CheckResult(name, mode, Status.PASS,
                       f"all {len(healthy)} metrics vary: {', '.join(healthy)}")


PRODUCER_MIN_N = 10          # below this there is no cadence to be silent against
PRODUCER_MIN_ACTIVE_DAYS = 3  # a single-day burst is an incident, not a cadence
PRODUCER_REGULAR_GAP_H = 72  # a producer must have reported at least this often
PRODUCER_FLOOR_H = 168       # never warn before a week of silence
PRODUCER_GAP_MULTIPLE = 10   # silence this many times its own median gap


def _psql_rows(db_url: str, sql: str, timeout: int = 20) -> list[list[str]] | None:
    """Run a multi-row query via psql; return |-split rows or None on any error."""
    if shutil.which("psql") is None:
        return None
    proc = subprocess.run(
        ["psql", db_url, "-Atqc", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        return None
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line]


def check_finding_producer_live(db_url: str) -> CheckResult:
    """WARN when a detector that used to report regularly has gone silent.

    The checks above ask whether a *stream* is flowing and whether a *metric*
    can move. This one asks the question that covers both from the outside: is
    the thing that produces findings still producing any? A detector that dies
    emits nothing, and nothing is exactly what a healthy detector emits on a
    quiet day — so its death is invisible by construction. Every other signal
    stays green: the process runs, its state files update, its check-ins report
    stale counts.

    The incident this encodes (2026-06-29 → 2026-07-30): PR #1276 pointed
    Watcher's default detector at an ollama tag that was never pulled. Every
    hook-fired scan died with `model call failed: HTTP Error 404` into a log
    nobody reads — 420 of them — while the hook kept firing, the pattern floor
    kept updating, and zero findings reached governance for 31 days. #1403 fixed
    the tag; nothing had reported the outage.

    Self-relative by design, so no cadence table has to be maintained and
    event-driven producers do not get punished for being quiet:
      * only producers with >= PRODUCER_MIN_N findings are judged at all
      * only those active on >= PRODUCER_MIN_ACTIVE_DAYS distinct days. A
        fire-on-failure producer emits a burst during one incident and nothing
        for months; the burst gives it a tiny median gap, so without this it
        masquerades as a high-cadence producer and its healthy silence reads as
        death. Measured: lease_plane_health_finding is 14 findings across ONE
        day (a 66x consecutive-failure incident plus its RECOVERED notice), and
        warned on arrival until this gate was added. Judged producers span
        13-48 distinct days; every excluded one spans <= 3.
      * only those whose own median inter-finding gap is <= PRODUCER_REGULAR_GAP_H
        (i.e. they demonstrably reported on a regular cadence)
      * only those whose LAST word was not an all-clear. A fire-on-failure
        producer that recovers posts an info-severity RECOVERED notice and then
        goes correctly silent — silence after an all-clear is its designed
        healthy state. The active-days gate cannot catch the multi-incident
        version: bridge_liveness_finding spans 13 active days (the July 11-24
        wedge storms), so it passed every gate, and on 2026-08-05 this check
        called it "silent 8.4d (usually every 1.7h)" while the watchdog was
        running green every 120s and the bridge heartbeat was seconds fresh.
        Its cadence baseline was wedge-burst re-alerts, not a heartbeat; its
        last finding was "RECOVERED: Discord bridge is alive again". Judged by
        last severity, not by name, so no producer roster is introduced.
      * warn when silence exceeds both PRODUCER_FLOOR_H and
        PRODUCER_GAP_MULTIPLE x that producer's own median gap

    WARN, not FAIL — a dead detector degrades evidence, it doesn't break the
    install. A retired producer will also warn; the fix there is to stop
    counting it, which is a decision, not a defect.

    Stated blind spot, wider than the fire-on-failure case: ANY producer whose
    most recent row is an info all-clear becomes unjudged, including a
    continuous-cadence producer that dies immediately after posting one — and
    info all-clears are incident-correlated, so that coincidence is less rare
    than it sounds (sentinel_finding posted 'lease starvation CLEARED' on
    2026-08-01; had its emitter wedged right then, this check would not have
    said so). Accepted deliberately: for the live fleet the flagship producer
    has a twin on the SAME emit path with zero info rows ever
    (sentinel_alarm_finding, 0.09h median) — a dead findings path silences the
    twin too and the twin still trips this check within the week floor. The
    residual (a producer with no non-info twin dying exactly inside its
    info-last window) cannot be separated from designed health by finding
    cadence at all; its liveness signal is host-level (the watchdog's own log
    mtime / launchd state), not the finding stream — wiring that is the
    follow-up, not another cadence knob here.
    """
    name, mode = "finding_producer_live", "operator"
    rows = _psql_rows(db_url, (
        "SELECT event_type, count(*), "
        "  round(extract(epoch FROM (now() - max(ts))) / 3600.0, 1), "
        "  round(extract(epoch FROM percentile_cont(0.5) WITHIN GROUP (ORDER BY gap))"
        "        / 3600.0, 2), "
        "  count(DISTINCT ts::date), "
        "  coalesce((array_agg(severity ORDER BY ts DESC))[1], '') "
        "FROM (SELECT event_type, ts, payload->>'severity' AS severity, "
        "             ts - lag(ts) OVER (PARTITION BY event_type ORDER BY ts) AS gap "
        "      FROM audit.events "
        "      WHERE ts > now() - interval '90 days' AND event_type LIKE '%\\_finding') s "
        "GROUP BY 1"
    ), timeout=30)
    if rows is None:
        return CheckResult(name, mode, Status.SKIP, "audit.events not queryable")
    if not rows:
        return CheckResult(name, mode, Status.SKIP, "no finding producers in 90d")

    silent, live, unjudged = [], [], 0
    for row in rows:
        if len(row) < 5:
            continue
        producer, n_raw, silent_raw, median_raw, days_raw = row[:5]
        last_severity = row[5] if len(row) > 5 else ""
        try:
            n, silent_h = int(n_raw), float(silent_raw)
            median_h, active_days = float(median_raw), int(days_raw)
        except ValueError:
            unjudged += 1
            continue
        if (n < PRODUCER_MIN_N or active_days < PRODUCER_MIN_ACTIVE_DAYS
                or median_h > PRODUCER_REGULAR_GAP_H
                or last_severity == "info"):
            unjudged += 1
            continue
        threshold = max(PRODUCER_FLOOR_H, PRODUCER_GAP_MULTIPLE * median_h)
        if silent_h > threshold:
            silent.append(
                f"{producer}: silent {silent_h / 24:.1f}d "
                f"(n={n}, usually every {median_h:.1f}h)"
            )
        else:
            live.append(f"{producer} ({silent_h:.0f}h)")

    if not silent and not live:
        return CheckResult(name, mode, Status.SKIP,
                           f"no producer has a regular cadence to judge ({unjudged} skipped)")
    if silent:
        return CheckResult(
            name, mode, Status.WARN,
            f"{len(silent)} finding producer(s) went quiet: " + "; ".join(silent),
            detail=("Silence from a detector is not evidence of health — it is the same "
                    "output a dead one produces. Run the producer's own self-test before "
                    "assuming the codebase simply got clean. "
                    + (f"Reporting: {', '.join(live)}." if live else "")),
        )
    return CheckResult(name, mode, Status.PASS,
                       f"all {len(live)} regular producer(s) reporting: {', '.join(live)}")


#: Files whose finding declarations are fixtures, not producers.
_PRODUCER_SCAN_EXCLUDE = ("/tests/", "test_", "conftest")
#: Where a real producer declares the event_type it will post.
_PRODUCER_DECL = re.compile(
    r'(?:FINDING_KIND\s*=\s*|event_type\s*=\s*)["\']([a-z0-9_]+_finding)["\']'
)


def declared_finding_producers(repo_root: Path) -> set[str]:
    """Event types the source says something will post.

    Deliberately source-derived rather than registry-derived: a registry is
    another thing that can go stale, and the declaration site cannot lie about
    its own intent to post.
    """
    declared: set[str] = set()
    for sub in ("agents", "scripts"):
        base = repo_root / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            # Match the path RELATIVE to repo_root. Matching the absolute path
            # would let any ancestor directory containing "test_" (a pytest
            # tmp_path, a checkout under ~/test_repos) silently exclude the
            # entire tree and make this check pass by seeing nothing.
            rel = path.relative_to(repo_root).as_posix()
            if any(x in rel for x in _PRODUCER_SCAN_EXCLUDE):
                continue
            try:
                declared.update(_PRODUCER_DECL.findall(path.read_text(errors="ignore")))
            except OSError:
                continue
    return declared


def check_producer_never_reported(db_url: str, repo_root: Path) -> CheckResult:
    """WARN when source declares a finding producer that has NEVER posted once.

    ``finding_producer_live`` is self-relative: it judges a producer against its
    own past cadence, so it detects *died* and is structurally blind to
    *never-born*. A producer with zero rows is absent from its result set
    entirely — not judged and found healthy, simply invisible.

    That blind spot is not theoretical. ``deploy_drift_doctor`` ran hourly for
    its entire life posting nothing: its interpreter could not import the
    escalation module, the exception was swallowed by a bare ``except``, and
    ``finding_producer_live`` never had a cadence to measure it against. It was
    found by a human asking "has this ever actually fired?" — which is the
    question this check asks on a timer.

    WARN, not FAIL: a genuinely new producer legitimately sits here until its
    first real condition fires. The fix for that is time or a self-test, not a
    code change — so this must not block CI or page anyone.
    """
    name, mode = "producer_never_reported", "operator"
    declared = declared_finding_producers(repo_root)
    if not declared:
        return CheckResult(name, mode, Status.SKIP,
                           "no finding producers declared in source")

    rows = _psql_rows(db_url, (
        "SELECT DISTINCT event_type FROM audit.events "
        "WHERE event_type LIKE '%\\_finding'"
    ))
    if rows is None:
        return CheckResult(name, mode, Status.SKIP, "audit.events not queryable")

    seen = {r[0] for r in rows if r and r[0]}
    never = sorted(declared - seen)
    if not never:
        return CheckResult(
            name, mode, Status.PASS,
            f"all {len(declared)} declared producer(s) have reported at least once",
        )
    return CheckResult(
        name, mode, Status.WARN,
        f"{len(never)} declared producer(s) have NEVER posted a finding: "
        + ", ".join(never),
        detail=("Zero findings is not the same as nothing to report — it is also "
                "what a broken escalation path looks like, and the two are "
                "indistinguishable from the outside. Check the producer can "
                "import agents.common.findings under the interpreter its plist "
                "actually uses, then confirm end-to-end with a real condition. "
                "A newly added producer will appear here until it first fires."),
    )


# The adjudication queue's own definition, mirrored from
# src/http_routes/sentinel.py (_SENTINEL_FINDING_EVENT_TYPES, _SENTINEL_BACKLOG_DEFAULT_
# SEVERITIES). Duplicated deliberately rather than imported: the doctor runs
# against a DEPLOYED database from a checkout that may not be the deployed
# tree, so importing server code would silently measure the wrong definition.
# tests/test_adjudication_feedstock.py asserts the mirror still matches source,
# so a change to the queue's definition cannot silently desync this check.
ADJUDICABLE_EVENT_TYPES = ("sentinel_finding", "sentinel_alarm_finding")
ADJUDICABLE_SEVERITIES = ("high", "critical")
FEEDSTOCK_DRY_DAYS = 7       # queue-eligible findings absent this long
FEEDSTOCK_ALIVE_MIN = 20     # ...while producers emitted at least this many


def check_adjudication_feedstock(db_url: str) -> CheckResult:
    """WARN when findings still flow but NONE of them can be adjudicated.

    ``finding_producer_live`` asks whether producers are alive.
    ``producer_never_reported`` asks whether they were ever born. Both are
    answered by the finding stream itself, and both read green while the thing
    the findings exist to feed receives nothing — because the adjudication
    queue does not consume the stream, it consumes a narrow SLICE of it:
    ``ADJUDICABLE_EVENT_TYPES`` at ``ADJUDICABLE_SEVERITIES``. A producer can
    be loudly, healthily alive and contribute zero adjudicable rows, and every
    liveness signal stays green while the falsifiability anchor starves.

    That is the live state as of 2026-08-10 and the reason this check exists.
    Sentinel emitted 201 findings in 7 days — 136 on one day — and **not one**
    was queue-eligible: all `medium`. An empty queue and a healthy queue are
    the same observation, and the queue had been dry for nine days with nothing
    saying so.

    ⛔CORRECTION 2026-08-19: this docstring used to conclude the lease fixes
    (#1443/#1444/#1459) had REMOVED the producing condition, making the zero
    permanently fair and the lever retirable. That is FALSE. A real forced
    release fired 2026-08-10 23:46:25 on `resident:/steward_eisv_sync`
    (held_x_ttl 87.6, holder_pid_null true, non-test surface), alarmed 28.5s
    later, and was adjudicated 2026-08-13 via the dashboard. The fixes made the
    condition RARE, not absent. "Removed" would justify retiring the lever;
    "rare" justifies keeping it.

    So a dry window here has three causes, not two, and this check alone cannot
    tell them apart: (a) condition genuinely gone, (b) queue DRAINED — the last
    eligible finding was adjudicated and none has arrived since, (c) alarm path
    BROKEN. ⛔Do NOT "fix" this by passing when the newest eligible finding has
    a newer adjudication: that shortcut lets case (c) go green forever against
    a stale matched pair. The separation is `forced_release_transform`, which
    asserts the upstream invariant; this check stays WARN by design.

    Federation note, and the reason this reports PER PRODUCER rather than a
    single boolean: 8 of 10 finding producers are structurally unadjudicatable
    — wrong event_type, wrong severity, or both — so the entire falsifiability
    anchor rests on one producer's output. When that producer legitimately goes
    quiet there is no second source, and the coverage table below is the
    measurement any fix to that has to be designed against.

    ⛔The attribution objection this text used to raise is STALE and was
    blocking correct work. It said adjudicating a doctor finding would book the
    outcome against SENTINEL's EISV, so attribution had to come first.
    Attribution now comes first by construction:
    ``http_sentinel_adjudicate`` resolves the producer via
    ``_finding_producer_uuid`` (``src/http_routes/sentinel.py``), falls back to
    the Sentinel substrate uuid ONLY for Sentinel's own families, and otherwise
    returns 422 rather than mis-booking against the wrong resident.

    ⛔Read that narrowly. ``event_type_is_sentinel_family`` is named for the
    family but implemented as ``producer_ref == "sentinel"`` — a check on the
    raw ``audit.events.agent_id`` slug, not the event type. So the fallback is
    slug-scoped: any producer that writes the literal slug ``sentinel`` still
    books against Sentinel. Narrower than the name promises, and worth fixing
    before widening admits a producer that could collide with it.

    ⛔That removes ONE blocker, not the gate. Widening still faces TWO
    independent gates, both necessary, and this check measures only the first:

      * ELIGIBILITY — ``ADJUDICABLE_EVENT_TYPES`` at ``ADJUDICABLE_SEVERITIES``.
        This is what the coverage table below counts. Attribution work does not
        move it: a fully conformant producer emitting medium-severity findings
        still cannot enter the queue.
      * ATTRIBUTION CONFORMANCE — a producer writing a bare slug into
        ``audit.events.agent_id`` has no identity to attribute to, so it 422s.
        Fail-closed and correct, but it yields no anchor. ⛔Do NOT trust any
        list of which producers conform, including one written here: this is
        actively changing. ``agents/watcher/findings.py`` now resolves
        Watcher's UUID and ``agents/common/findings.py`` gives the doctor layer
        ``doctor_layer_agent_id``, so a census written a week ago is already
        wrong — as an earlier draft of this very paragraph was. Derive it when
        you need it, with a LEFT JOIN from ``audit.events`` to ``core.agents``
        over the window you care about. (And no "N of M" ratio: at roughly 24
        findings/day that decays within a day.)

    ⛔Before widening anything, resolve the harder question this check cannot
    answer. The MECHANISM for a false positive exists — the dashboard offers a
    "False positive" dismissal and the endpoint records it as a bad outcome
    (``reason="fp"``), so this is not a channel that structurally cannot
    dismiss. What is missing is MEASUREMENT: no report in ``scripts/ops`` or
    ``scripts/dev`` computes dismissal rate per family, and the in-queue record
    to date is 17 adjudications, all confirmed. ⛔Dismissal evidence from other
    channels (``watcher_finding_dismissed``) is a different population on a
    different path and does not transfer. Decide how a false positive would be
    OBSERVED for a family before admitting it — an unmeasured channel becomes
    the all-positive generator Invariant 4 exists to exclude, whether or not
    the button exists.

    WARN, not FAIL. A dry queue is a real condition to surface, not a broken
    install, and the correct response is sometimes "nothing is wrong, the
    lever retired" — which is a decision, not a defect.
    """
    name, mode = "adjudication_feedstock", "operator"

    types_sql = ", ".join(f"'{t}'" for t in ADJUDICABLE_EVENT_TYPES)
    sevs_sql = ", ".join(f"'{s}'" for s in ADJUDICABLE_SEVERITIES)

    rows = _psql_rows(db_url, (
        "SELECT event_type, "
        "  count(*), "
        f"  count(*) FILTER (WHERE payload->>'severity' IN ({sevs_sql}) "
        f"                     AND event_type IN ({types_sql})), "
        "  coalesce(round(extract(epoch FROM (now() - max(ts))) / 86400.0, 1), -1) "
        "FROM audit.events "
        "WHERE event_type LIKE '%\\_finding' "
        f"  AND ts > now() - interval '{FEEDSTOCK_DRY_DAYS} days' "
        "GROUP BY event_type ORDER BY 2 DESC"
    ))
    if rows is None:
        return CheckResult(name, mode, Status.SKIP, "audit.events not queryable")
    if not rows:
        # No findings at all is finding_producer_live's question, not this one.
        return CheckResult(name, mode, Status.SKIP,
                           f"no findings in {FEEDSTOCK_DRY_DAYS}d — liveness "
                           "is finding_producer_live's call, not this check's")

    total = sum(int(r[1]) for r in rows if len(r) > 1)
    eligible = sum(int(r[2]) for r in rows if len(r) > 2)

    coverage = ", ".join(
        f"{r[0]}={r[2]}/{r[1]}" for r in rows if len(r) > 2
    )

    if eligible > 0:
        return CheckResult(
            name, mode, Status.PASS,
            f"{eligible}/{total} finding(s) in {FEEDSTOCK_DRY_DAYS}d are "
            f"queue-eligible across {len(rows)} producer(s)",
            detail=f"per-producer eligible/total: {coverage}",
        )

    if total < FEEDSTOCK_ALIVE_MIN:
        # Too quiet overall to distinguish a dry queue from a quiet fleet.
        return CheckResult(
            name, mode, Status.PASS,
            f"only {total} finding(s) in {FEEDSTOCK_DRY_DAYS}d — too few to "
            "call the queue dry",
            detail=f"per-producer eligible/total: {coverage}",
        )

    return CheckResult(
        name, mode, Status.WARN,
        f"adjudication queue is DRY: {total} finding(s) in "
        f"{FEEDSTOCK_DRY_DAYS}d from {len(rows)} producer(s), 0 eligible",
        detail=(
            f"per-producer eligible/total: {coverage}. "
            f"Eligible = event_type in {ADJUDICABLE_EVENT_TYPES} at severity "
            f"in {ADJUDICABLE_SEVERITIES}. Producers are alive; nothing they "
            "emit can be adjudicated while every liveness check stays green. "
            "⚠️Scope: a dry queue does NOT mean the falsifiability anchor is "
            "starved. The queue is fed only by forced-release findings, which "
            "assert a database fact rather than an inference — the sole bad "
            "label is a false-positive dismissal, and a recorded fact cannot "
            "be one. Measured: 17 adjudications, 100% confirmed, zero bad, "
            "ever. So a FULL queue supplies the anchor only non-falsifiable "
            "positives; what this warning tracks is adjudication coverage, not "
            "evidence for the anchor. This is not "
            "automatically a defect — the producing condition may have been "
            "genuinely fixed, in which case the honest response is to retire "
            "the lever rather than restore the alarm. Read "
            "forced_release_transform before deciding which: it asserts the "
            "upstream invariant and is what separates a DRAINED queue from a "
            "DEAD one. On widening: the old ATTRIBUTION objection here is "
            "resolved — adjudication now resolves the finding's own producer "
            "and returns 422 rather than booking against another resident. "
            "But that removes one blocker, not the gate. TWO independent gates "
            f"remain, both necessary: ELIGIBILITY ({ADJUDICABLE_EVENT_TYPES} at "
            f"{ADJUDICABLE_SEVERITIES} — what the coverage table above actually "
            "measures) and ATTRIBUTION CONFORMANCE (a producer writing a bare "
            "slug has no identity, so it 422s). Closing conformance does NOT "
            "open the queue; a conformant producer emitting medium-severity "
            "findings still cannot enter. ⛔And note what the in-queue record "
            "says: 17 adjudications, 100% confirmed, ZERO dismissals ever. No "
            "tooling in scripts/ops or scripts/dev measures dismissal rate. So "
            "before widening anything, decide how a false positive would be "
            "detected at all — a family that can only confirm is the "
            "all-positive generator Invariant 4 exists to exclude."
        ),
    )


# The transform this check asserts: every real (non-test) forced lease release
# MUST become a queue-admissible sentinel finding. Sentinel builds the finding's
# fingerprint as "forced_release:ad_hoc:{lease_plane_events.event_id}", so the
# two substrates join deterministically on that UUID.
#
# ⛔ The finding PAYLOAD also carries a key named `event_id`, and it is NOT the
# join key. Both producers (agents/sentinel/forced_release_alarm.py:220 and
# elixir/.../forced_release_poller/logic.ex:98) write the lease UUID into
# `extra.event_id`, yet the rows that actually landed in audit.events carry a
# small integer there instead (47, 62, 58, 24 ...). Something between the
# producer and the audit row overwrites it; that mechanism was NOT traced, and
# it does not need to be — the lesson is only that the payload field is not
# trustworthy as a key. Join on the FINGERPRINT, whose UUID suffix is written
# identically by both producers and is verified to match 21/21 over 90 days.
# Joining on the payload integer matches nothing, silently, forever — the same
# failure class as the emitter-keyed dedup fingerprint fixed in #1708.
FORCED_TRANSFORM_FINGERPRINT_PREFIX = "forced_release:ad_hoc:"

# Mirrored from agents/sentinel/forced_release_alarm.py
# (_SUPPRESSED_TEST_SURFACE_PREFIXES). Duplicated for the same reason as the
# queue definition above: the doctor runs against a DEPLOYED database from a
# checkout that may not be the deployed tree.
#
# ⛔ BOTH prefixes are load-bearing. The producer suppresses the legacy
# pre-#1102 naming as well as the reserved namespace, and the governance DB
# forbids DELETE so those rows are permanent: 257 of them, 2026-06-03 to
# 06-27. Excluding only "td:/test/" counts every one of them as a real forced
# release that never alarmed, i.e. 257 false FAILs — this check reporting a
# broken transform while the transform is fine, which is the exact failure it
# exists to prevent.
FORCED_TRANSFORM_TEST_SURFACE_PREFIXES = (
    "td:/test/",
    "td:/force-release-contract-test-",
)
FORCED_TRANSFORM_DAYS = 30        # lookback for the ABSENCE arm
FORCED_TRANSFORM_LATENCY_DAYS = 7  # lookback for the LATENCY arm (see docstring)
FORCED_TRANSFORM_SETTLE_S = 300   # too fresh to have alarmed yet; do not judge
FORCED_TRANSFORM_LATENCY_WARN_S = 900  # normal is 25-36s; see docstring


def _forced_transform_surface_filter() -> str:
    """SQL excluding every suppressed test-surface prefix, from the one tuple."""
    return " AND ".join(
        f"surface_id NOT LIKE '{prefix}%'"
        for prefix in FORCED_TRANSFORM_TEST_SURFACE_PREFIXES
    )


def check_forced_release_transform(db_url: str) -> CheckResult:
    """FAIL when a real forced lease release produced no queue-admissible finding.

    ``adjudication_feedstock`` reasons only from downstream ``audit.events``, so
    it cannot tell a DRAINED queue (last eligible finding was adjudicated,
    healthy) from a DEAD one (the alarm path broke and no finding will ever
    arrive again). Both look like zero. This check supplies the orthogonal
    signal it lacks: it reads the UPSTREAM substrate and asserts the transform.

    Deliberately a CONDITIONAL INVARIANT, not a heartbeat. When no real forced
    release happened it is vacuously satisfied and SKIPs — that is correct, not
    a blind spot, and it is why this cannot be folded into a fixed-window
    "has anything arrived lately" test. Real forced releases are bursty and
    rare: measured over 60 days there is a 33-day gap (2026-06-27 -> 07-30),
    then 9.1 days (08-01 -> 08-10). Any 7d window over that process reads dry
    most of the time in perfect health.

    Backtest at introduction (90d, genuinely-real surfaces only): n=21, 21
    alarmed, 0 unmatched, 0 false positives. ⚠️An earlier version of this
    number said 128/128 over 60d; that filtered only "td:/test/" and so counted
    100 legacy fixtures as real forced releases. Corrected 2026-08-19 — the
    real n is 21, and it is small because genuine forced releases are rare.

    Two arms, because presence alone is not enough:

    1. ABSENCE — an unmatched non-test forced release means the transform is
       broken. FAIL.
    2. LATENCY — matched, but slow, scoped to the last
       ``FORCED_TRANSFORM_LATENCY_DAYS`` days. The two arms need different
       windows: absence is a permanently lost adjudication and stays worth
       surfacing across the sparse event process, but a latency excursion is a
       statement about health NOW. Scoped to 30d this arm re-reported the
       resolved 2026-07-30 degradation for ten consecutive days — "an open
       finding nobody closes is how a detector decays into noise."
       Measured transform latency is tightly
       n=21 over 90d with p50 26.7s and min 0.6s. Two of those 21 sit at
       26,581s (7.4h) — both the same 2026-07-30 23:53:47 incident, on
       `resident:/steward` and `resident:/steward_eisv_sync`. They DID
       eventually match, so an absence-only check reads green over a real
       outage. WARN above ``FORCED_TRANSFORM_LATENCY_WARN_S`` (900s: ~34x p50,
       ~30x below the known excursion). ⚠️n is small — treat 900s as a
       separating value between two well-clustered groups, not as a fitted
       percentile.
       ⚠️Known confound: if the host slept between the lease event and the
       alarm, the delay is real but is not Sentinel's fault (see
       ``_host_awake_s`` and the 2026-08-03 mobile/sleep class). That is why
       this arm is WARN and names sleep in its detail rather than FAILing.

    Events newer than ``FORCED_TRANSFORM_SETTLE_S`` are excluded: at ~28s
    typical latency, judging a 10-second-old event would flap.

    Reads two tables and writes nothing. It mints no identity and emits no
    finding, so unlike a synthetic canary it books nothing against Sentinel's
    EISV — which is why it, and not a canary, is the primary control here
    (dialectic ce6f53ad3e0f404e, 2026-08-19).
    """
    name, mode = "forced_release_transform", "operator"
    prefix = FORCED_TRANSFORM_FINGERPRINT_PREFIX

    rows = _psql_rows(db_url, (
        "WITH real_forced AS ("
        "  SELECT event_id, ts, surface_id"
        "  FROM lease_plane.lease_plane_events"
        "  WHERE event_type = 'forced'"
        f"   AND {_forced_transform_surface_filter()}"
        f"   AND ts > now() - interval '{FORCED_TRANSFORM_DAYS} days'"
        f"   AND ts < now() - interval '{FORCED_TRANSFORM_SETTLE_S} seconds'"
        "), alarms AS ("
        "  SELECT payload->>'fingerprint' AS fp, ts AS alarm_ts"
        "  FROM audit.events"
        "  WHERE event_type IN ('sentinel_finding', 'sentinel_alarm_finding')"
        f"   AND ts > now() - interval '{FORCED_TRANSFORM_DAYS + 1} days'"
        ") "
        "SELECT r.surface_id,"
        "       to_char(r.ts, 'YYYY-MM-DD HH24:MI:SS'),"
        "       (a.fp IS NOT NULL),"
        "       coalesce(round(extract(epoch FROM (a.alarm_ts - r.ts))::numeric, 1), -1),"
        f"      (r.ts > now() - interval '{FORCED_TRANSFORM_LATENCY_DAYS} days') "
        "FROM real_forced r "
        f"LEFT JOIN alarms a ON a.fp = '{prefix}' || r.event_id::text "
        "ORDER BY r.ts DESC"
    ))
    if rows is None:
        return CheckResult(name, mode, Status.SKIP,
                           "lease_plane.lease_plane_events not queryable")
    if not rows:
        # Vacuously satisfied. The invariant has nothing to say, and saying
        # nothing is the honest result — see the docstring on why this is not
        # a heartbeat.
        return CheckResult(
            name, mode, Status.SKIP,
            f"no real forced releases in {FORCED_TRANSFORM_DAYS}d — invariant "
            "vacuously satisfied, nothing to assert",
            detail=("Test-surface fixtures are excluded by design (both the "
                    "reserved and the legacy pre-#1102 prefix): they are "
                    "suppressed before the alarm, so they exercise the lease "
                    "plane and prove nothing about alarm->queue."),
        )

    unmatched = [r for r in rows if len(r) > 2 and r[2] not in ("t", "true")]
    matched = [r for r in rows if len(r) > 2 and r[2] in ("t", "true")]

    if unmatched:
        worst = ", ".join(f"{r[0]} @ {r[1]}" for r in unmatched[:4])
        return CheckResult(
            name, mode, Status.FAIL,
            f"{len(unmatched)} of {len(rows)} real forced release(s) in "
            f"{FORCED_TRANSFORM_DAYS}d produced NO sentinel finding",
            detail=(
                f"unmatched: {worst}"
                f"{' ...' if len(unmatched) > 4 else ''}. The forced-release "
                "alarm path is not transforming lease events into "
                "queue-admissible findings, so the adjudication queue is DEAD, "
                "not drained — and adjudication_feedstock cannot tell the "
                "difference on its own. Check the Sentinel forced-release "
                "poller and agents/sentinel/forced_release_alarm.py. Join key "
                f"is '{prefix}' || lease_plane_events.event_id (UUID) — NOT the "
                "finding payload's integer event_id field."
            ),
        )

    # Recency is decided by Postgres (column 5), not by parsing the rendered
    # timestamp here — the doctor and the DB have disagreed about timezone
    # before, and a retention/window compare is exactly where that bites.
    recent = [r for r in matched
              if len(r) > 4 and r[4] in ("t", "true") and float(r[3]) >= 0]
    latencies = [float(r[3]) for r in recent]
    slow = [r for r in recent
            if float(r[3]) > FORCED_TRANSFORM_LATENCY_WARN_S]
    worst_s = max(latencies) if latencies else 0.0

    if slow:
        return CheckResult(
            name, mode, Status.WARN,
            f"transform is intact but SLOW: {len(slow)} of {len(rows)} forced "
            f"release(s) alarmed later than {FORCED_TRANSFORM_LATENCY_WARN_S}s "
            f"(worst {worst_s:.0f}s)",
            detail=(
                f"slowest: {slow[0][0]} @ {slow[0][1]} took {float(slow[0][3]):.0f}s. "
                "Every event matched, so an absence-only check reads green here; "
                "normal transform latency is 25-36s. ⚠️Confound: if the host "
                "slept between the lease event and the alarm the delay is real "
                "but not Sentinel's fault — check pmset -g log before treating "
                "this as a Sentinel defect."
            ),
        )

    if recent:
        latency_note = (f"worst latency {worst_s:.0f}s over {len(recent)} "
                        f"event(s) in {FORCED_TRANSFORM_LATENCY_DAYS}d")
    else:
        latency_note = (f"no forced releases in the last "
                        f"{FORCED_TRANSFORM_LATENCY_DAYS}d, so latency is "
                        "unjudged this window")
    return CheckResult(
        name, mode, Status.PASS,
        f"{len(matched)}/{len(rows)} real forced release(s) in "
        f"{FORCED_TRANSFORM_DAYS}d each produced a sentinel finding "
        f"({latency_note})",
        detail=("Transform invariant holds, so a dry adjudication queue is "
                "DRAINED, not dead."),
    )


def build_checks(
    repo_root: Path,
    db_url: str,
    redis_url: str = DEFAULT_REDIS_URL,
) -> list[Check]:
    loaded_cache: dict[str, set[str]] = {}

    def loaded() -> set[str]:
        if "v" not in loaded_cache:
            loaded_cache["v"] = _launchctl_loaded()
        return loaded_cache["v"]

    return [
        Check("python_version", "local", check_python_version),
        Check("postgres_running", "local", lambda: check_postgres_running(db_url)),
        Check("redis_continuity", "local", lambda: check_redis_continuity(redis_url)),
        Check("governance_database", "local", lambda: check_governance_database(db_url)),
        Check("pg_extensions", "local", lambda: check_pg_extensions(db_url)),
        Check("schema_migrations", "local", lambda: check_schema_migrations(db_url, repo_root)),
        Check("column_drift", "local", lambda: check_column_drift(db_url, repo_root)),
        # Companion to the above: column_drift catches code naming a column the
        # DB lacks, this one catches the DB lacking an enforcement rule the
        # migrations claim to have installed. Neither sees the other's case.
        Check("constraint_drift", "local",
              lambda: check_constraint_drift(db_url, repo_root)),
        # Cause detector under both of the above: they each know one DDL family
        # and notice when that kind of change went missing, this one notices
        # when the applied bytes and the file stopped agreeing at all.
        Check("migration_checksum_drift", "local",
              lambda: check_migration_checksum_drift(db_url, repo_root)),
        Check("elixir_deprecated_scheme_lint", "local",
              lambda: check_elixir_deprecated_scheme_lint(db_url, repo_root)),
        Check("elixir_scheme_grammar_lint", "local",
              lambda: check_elixir_scheme_grammar_lint(db_url, repo_root)),
        Check("dockerfile_pinned_tags", "local",
              lambda: check_dockerfile_pinned_tags(repo_root)),
        Check("flags_catalog_fresh", "local",
              lambda: check_flags_catalog_fresh(repo_root)),
        Check("tool_edge_index_fresh", "local",
              lambda: check_tool_edge_index_fresh(repo_root)),
        Check("class_anchors_fresh", "local",
              lambda: check_class_anchors_fresh(repo_root)),
        Check("anchor_directory", "local", check_anchor_dir),
        Check("secrets_file", "local", check_secrets_file),
        Check("http_listening", "operator", check_http_listening),
        Check("http_health", "operator", check_http_health),
        Check("pid_file", "operator",
              lambda: check_pid_file(repo_root, GOVERNANCE_LAUNCHD_LABEL in loaded())),
        Check("launchagent_loaded", "operator", lambda: check_launchagent(loaded())),
        Check("resident_agents", "operator", lambda: check_resident_agents(loaded())),
        Check("ipv6_sidecar", "operator", lambda: check_ipv6_sidecar(loaded())),
        Check("failure_label_live", "operator", lambda: check_failure_label_live(db_url)),
        Check("checkin_stream_live", "operator", lambda: check_checkin_stream_live(db_url)),
        Check("resident_checkin_stale", "operator", lambda: check_resident_checkin_stale(db_url)),
        Check("immortal_lease", "operator", lambda: check_immortal_lease(db_url)),
        Check("grounding_stage_live", "operator", lambda: check_grounding_stage_live(db_url)),
        Check("cold_start_pause_canary", "operator",
              lambda: check_cold_start_pause_canary(db_url)),
        Check("label_join_overlap", "operator", lambda: check_label_join_overlap(db_url)),
        Check("signal_degeneracy", "operator", lambda: check_signal_degeneracy(db_url)),
        Check("finding_producer_live", "operator",
              lambda: check_finding_producer_live(db_url)),
        # Companion to the above: that one catches DIED, this one catches
        # NEVER-BORN. Neither sees the other's case.
        Check("producer_never_reported", "operator",
              lambda: check_producer_never_reported(db_url, repo_root)),
        # Third of the family. Those two ask whether findings are BEING MADE;
        # this one asks whether any of them can be CONSUMED. A producer that is
        # alive and loud satisfies both of the above while contributing nothing
        # the adjudication queue will accept — which is the live 2026-08-10
        # state and is invisible to every liveness signal.
        Check("adjudication_feedstock", "operator",
              lambda: check_adjudication_feedstock(db_url)),
        # The orthogonal signal the one above lacks. adjudication_feedstock
        # reasons only from downstream audit.events, so a DRAINED queue and a
        # DEAD one are the same observation to it. This reads the UPSTREAM
        # substrate and asserts the transform, which is the only thing that
        # separates them.
        Check("forced_release_transform", "operator",
              lambda: check_forced_release_transform(db_url)),
    ]


def run_checks(checks: list[Check], mode: str) -> list[CheckResult]:
    selected = [c for c in checks if mode == "all" or c.mode == mode]
    results = []
    for c in selected:
        try:
            results.append(c.fn())
        except Exception as e:  # never crash the whole run
            results.append(CheckResult(c.name, c.mode, Status.FAIL,
                                       "check raised exception", detail=repr(e)))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_GLYPH = {
    Status.PASS: ("✓", "\033[32m"),
    Status.FAIL: ("✗", "\033[31m"),
    Status.WARN: ("⚠", "\033[33m"),
    Status.SKIP: ("·", "\033[90m"),
}
_RESET = "\033[0m"


def render_text(results: list[CheckResult], use_color: bool) -> str:
    lines = []
    by_mode: dict[str, list[CheckResult]] = {}
    for r in results:
        by_mode.setdefault(r.mode, []).append(r)
    for mode in ("local", "operator"):
        if mode not in by_mode:
            continue
        lines.append(f"\n=== {mode} ===")
        for r in by_mode[mode]:
            glyph, color = _GLYPH[r.status]
            prefix = f"{color}{glyph}{_RESET}" if use_color else glyph
            lines.append(f"  {prefix} {r.name}: {r.message}")
            if r.detail:
                lines.append(f"      {r.detail}")
    fails = sum(1 for r in results if r.status == Status.FAIL)
    warns = sum(1 for r in results if r.status == Status.WARN)
    passes = sum(1 for r in results if r.status == Status.PASS)
    lines.append(f"\n{passes} pass · {fails} fail · {warns} warn")
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == Status.FAIL for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("local", "operator", "all"), default="all")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--db-url", default=os.environ.get("DB_POSTGRES_URL", DEFAULT_DB_URL))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", DEFAULT_REDIS_URL))
    parser.add_argument(
        "--attest", action="store_true",
        help="emit this database's schema attestation (digest + coverage) as "
             "JSON and exit. Two deployments compare digests to establish that "
             "they carry the same schema contract; neither consults the other, "
             "and a digest whose coverage is partial is a weaker claim — read "
             "fully_anchored before treating equality as agreement.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent.parent

    if args.attest:
        applied = _parse_schema_migration_rows(
            subprocess.run(
                ["psql", args.db_url, "-Atqc",
                 "SELECT version || '|' || name FROM core.schema_migrations "
                 "ORDER BY version"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        )
        print(json.dumps(
            schema_attestation(applied, _query_applied_checksums(args.db_url)),
            indent=2,
        ))
        return 0

    checks = build_checks(repo_root, args.db_url, args.redis_url)
    results = run_checks(checks, args.mode)

    if args.json:
        payload = {
            "mode": args.mode,
            "results": [
                {**asdict(r), "status": r.status.value} for r in results
            ],
            "exit_code": exit_code(results),
        }
        print(json.dumps(payload, indent=2))
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print(render_text(results, use_color))

    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
