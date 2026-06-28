#!/usr/bin/env python3
"""Diagnose a UNITARES install.

Usage:
    python3 scripts/dev/unitares_doctor.py
    python3 scripts/dev/unitares_doctor.py --mode local
    python3 scripts/dev/unitares_doctor.py --mode operator
    python3 scripts/dev/unitares_doctor.py --json

Modes:
    local      Checks needed for stdio-only adoption (postgres + schema +
               anchor dir). Sufficient for a fresh-machine bring-up where the
               agent client spawns governance directly via stdio.
    operator   Adds HTTP/launchd checks: 8767 listening, PID file, LaunchAgent
               loaded, resident-agent plists, cloudflared sidecar.
    all        local + operator. Default.

Stdlib-only. Safe to run before `pip install -e .` finishes — used to verify
that the install can finish.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

DEFAULT_DB_URL = "postgresql://postgres:postgres@localhost:5432/governance"
REQUIRED_PG_EXTENSIONS = ("age", "pgcrypto", "pg_trgm", "uuid-ossp", "vector")
RESIDENT_LAUNCHD_SLOTS = (
    ("vigil", ("com.unitares.vigil",)),
    ("sentinel", ("com.unitares.sentinel", "com.unitares.sentinel-beam")),
    ("chronicler", ("com.unitares.chronicler",)),
)
ANCHOR_DIR = Path.home() / ".unitares"
SECRETS_FILE = Path.home() / ".config" / "cirwel" / "secrets.env"
HTTP_HEALTH_URL = "http://127.0.0.1:8767/health/live"
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


def check_class_anchors_fresh(repo_root: Path) -> CheckResult:
    """WARN if the per-class manifold anchors have gone stale.

    HEALTHY_OPERATING_POINT_BY_CLASS / DELTA_NORM_MAX_BY_CLASS
    (config/governance_config.py) are hand-snapshotted from a healthy-regime
    slice via scripts/calibrate_class_conditional.py. They feed manifold
    coherence and (via healthy_S) the live S-setpoint, but nothing forces a
    refresh — so they silently drift from reality. In 2026-06 Lumen's anchor was
    ~2 months stale, pinning its manifold coherence at 0 on every check-in. WARN
    (not FAIL — staleness degrades a signal, it doesn't break the build) when the
    newest 'measured_on' is older than the threshold.

    Regenerate WITH the live roster so per-label residents map to the keys:
      UNITARES_RESIDENTS=<csv> python3 scripts/calibrate_class_conditional.py
    """
    name, mode = "class_anchors_fresh", "local"
    stale_days = 90
    try:
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from config.governance_config import DELTA_NORM_MAX_BY_CLASS
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # Per-class, measured-only: an alias (provenance!='measured') is
        # deliberately not a measurement, so it never counts as stale. Keying on
        # the OLDEST measured class (not the newest) is the point — a fresh
        # refresh of one class must not mask a stale neighbour (Lumen 2026-06).
        stale = []
        for cls, sc in DELTA_NORM_MAX_BY_CLASS.items():
            if getattr(sc, "provenance", None) != "measured":
                continue
            mo = getattr(sc, "measured_on", None)
            if not mo:
                continue
            try:
                age = (now - datetime.strptime(mo, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
            except ValueError:
                continue
            if age > stale_days:
                stale.append((cls, age))
        if not stale:
            return CheckResult(name, mode, Status.PASS,
                               "all measured class anchors within "
                               f"{stale_days}d")
        stale.sort(key=lambda x: -x[1])
        worst = stale[0]
        return CheckResult(
            name, mode, Status.WARN,
            f"{len(stale)} class anchor(s) stale (oldest {worst[0]} {worst[1]}d > {stale_days}d)",
            detail=("stale: " + ", ".join(f"{c}({a}d)" for c, a in stale)
                    + "  -> UNITARES_RESIDENTS=<csv> python3 scripts/calibrate_class_conditional.py"),
        )
    except Exception as e:
        return CheckResult(name, mode, Status.SKIP, f"anchor freshness check skipped: {e}")


def build_checks(repo_root: Path, db_url: str) -> list[Check]:
    loaded_cache: dict[str, set[str]] = {}

    def loaded() -> set[str]:
        if "v" not in loaded_cache:
            loaded_cache["v"] = _launchctl_loaded()
        return loaded_cache["v"]

    return [
        Check("python_version", "local", check_python_version),
        Check("postgres_running", "local", lambda: check_postgres_running(db_url)),
        Check("governance_database", "local", lambda: check_governance_database(db_url)),
        Check("pg_extensions", "local", lambda: check_pg_extensions(db_url)),
        Check("schema_migrations", "local", lambda: check_schema_migrations(db_url, repo_root)),
        Check("column_drift", "local", lambda: check_column_drift(db_url, repo_root)),
        Check("elixir_deprecated_scheme_lint", "local",
              lambda: check_elixir_deprecated_scheme_lint(db_url, repo_root)),
        Check("elixir_scheme_grammar_lint", "local",
              lambda: check_elixir_scheme_grammar_lint(db_url, repo_root)),
        Check("dockerfile_pinned_tags", "local",
              lambda: check_dockerfile_pinned_tags(repo_root)),
        Check("flags_catalog_fresh", "local",
              lambda: check_flags_catalog_fresh(repo_root)),
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
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent.parent
    checks = build_checks(repo_root, args.db_url)
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
