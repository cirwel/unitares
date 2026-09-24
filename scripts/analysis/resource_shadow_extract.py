#!/usr/bin/env python3
"""Frozen extract for the resource-grounded E shadow study (#2410).

Protocol: docs/proposals/registered/resource-grounded-e-shadow-v0.md.

Reads the local collector rows (``resource_shadow.v1`` JSONL written by the
governance plugin's ``hooks/resource-observe``), joins the persisted EISV
states of the same agent UUIDs in one read-only snapshot, and writes a frozen
extract: two JSONL files plus a manifest carrying the SHA-256 of each file and
of the pre-registration. The study's single read runs on an extract, never on a
live query, because ``core.agent_state`` keeps only 90 days.

This script computes no metric and no predictor-outcome association. It
refuses, before any database access, when the pre-registration is missing or
when the output directory would sit inside a git work tree.

Column semantics (src/db/mixins/state.py): E = state_json->>'E',
I = integrity, S = entropy, V = volatility. The ``entropy`` column is S.

Environment:
    GOVERNANCE_DATABASE_URL  (default: postgresql://postgres:postgres@localhost:5432/governance)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = "resource_shadow.v1"
EXTRACT_SCHEMA = "resource_shadow_extract.v1"
EVENTS = ("stop", "tool-failure", "pre-compact")
REPO_ROOT = Path(__file__).resolve().parents[2]
PREREG = REPO_ROOT / "docs" / "proposals" / "registered" / "resource-grounded-e-shadow-v0.md"
DEFAULT_SHADOW_DIR = Path.home() / ".unitares" / "resource-shadow"
DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)
# Protocol section 5: a state older than this at a prediction point is
# missing. The extract pulls this much history before the first row so the
# analysis can apply the rule without another query.
STATE_LOOKBACK = dt.timedelta(minutes=30)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
# Protocol section 3: the fields each event may carry.
COMMON_FIELDS = ("schema", "ts", "host", "event", "session_id", "agent_uuid")
EVENT_FIELDS = {
    "stop": ("ctx_tokens_band", "model"),
    "tool-failure": ("tool_name", "is_interrupt"),
    "pre-compact": ("compact_trigger", "ctx_tokens_band", "model"),
}

STATE_SQL = """
SELECT i.agent_id,
       s.recorded_at,
       NULLIF(s.state_json->>'E', '')::float AS e,
       s.integrity AS i,
       s.entropy AS s,
       s.volatility AS v
FROM core.agent_state s
JOIN core.identities i ON i.identity_id = s.identity_id
WHERE i.agent_id = ANY($1::text[])
  AND s.recorded_at >= $2
  AND s.recorded_at <= $3
  AND s.synthetic IS NOT TRUE
ORDER BY i.agent_id, s.recorded_at
"""


class ExtractRefused(RuntimeError):
    """A precondition failed before any database access."""


@dataclass
class LoadedRows:
    rows: list[dict[str, Any]] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_ts(value: Any) -> dt.datetime | None:
    """Parse the collector's exact UTC form; anything else is rejected."""
    if not isinstance(value, str) or not _TS_RE.match(value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc)


def project_row(row: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the protocol lists for the row's event.

    The extract is the privacy boundary for everything downstream, so a
    source row carrying any other key (tool input, error text, a token) is
    reduced to the documented fields rather than copied whole.
    """
    keep = COMMON_FIELDS + EVENT_FIELDS[row["event"]]
    return {key: row.get(key) for key in keep}


def validate_row(row: Any) -> str | None:
    """Return a drop reason, or None when the row is usable."""
    if not isinstance(row, dict):
        return "not_object"
    if row.get("schema") != SCHEMA:
        return "wrong_schema"
    if row.get("event") not in EVENTS:
        return "unknown_event"
    if parse_ts(row.get("ts")) is None:
        return "bad_ts"
    uuid = row.get("agent_uuid")
    if uuid is not None and not (isinstance(uuid, str) and _UUID_RE.match(uuid)):
        return "bad_agent_uuid"
    return None


def load_shadow_rows(files: Iterable[Path]) -> LoadedRows:
    loaded = LoadedRows()
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                loaded.drop("not_json")
                continue
            reason = validate_row(row)
            if reason:
                loaded.drop(reason)
                continue
            loaded.rows.append(project_row(row))
    loaded.rows.sort(key=lambda r: (parse_ts(r["ts"]), r.get("session_id") or "", r["event"]))
    return loaded


def shadow_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.jsonl") if p.is_file())


def agent_uuids(rows: Sequence[dict[str, Any]]) -> list[str]:
    return sorted({r["agent_uuid"] for r in rows if r.get("agent_uuid")})


def state_window(rows: Sequence[dict[str, Any]]) -> tuple[dt.datetime, dt.datetime]:
    stamps = [parse_ts(r["ts"]) for r in rows]
    return min(stamps) - STATE_LOOKBACK, max(stamps)  # type: ignore[type-var]


def inside_git_worktree(path: Path) -> bool:
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return False
        probe = probe.parent
    result = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def preflight(prereg: Path, out_dir: Path) -> str:
    """Check every no-DB precondition; return the pre-registration SHA-256."""
    if not prereg.is_file():
        raise ExtractRefused(f"pre-registration missing: {prereg}")
    if out_dir.exists():
        raise ExtractRefused(f"output directory already exists, extracts are immutable: {out_dir}")
    if inside_git_worktree(out_dir):
        raise ExtractRefused(f"output directory is inside a git work tree: {out_dir}")
    return sha256_bytes(prereg.read_bytes())


def _jsonl(rows: Iterable[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows).encode()


def state_rows(db_rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    out = []
    for agent_id, recorded_at, e, i, s, v in db_rows:
        out.append({
            "agent_uuid": agent_id,
            "recorded_at": recorded_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "E": e, "I": i, "S": s, "V": v,
        })
    return out


def write_extract(
    out_dir: Path,
    *,
    observations: list[dict[str, Any]],
    states: list[dict[str, Any]],
    dropped: dict[str, int],
    prereg_sha256: str,
    snapshot_now: dt.datetime | None,
    window: tuple[dt.datetime, dt.datetime] | None,
    source_files: list[Path],
) -> dict[str, Any]:
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    files = {"observations.jsonl": _jsonl(observations), "eisv_states.jsonl": _jsonl(states)}
    for name, data in files.items():
        path = out_dir / name
        path.write_bytes(data)
        os.chmod(path, 0o600)
    manifest = {
        "schema": EXTRACT_SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "db_snapshot_now": snapshot_now.isoformat() if snapshot_now else None,
        "prereg_path": str(PREREG.relative_to(REPO_ROOT)),
        "prereg_sha256": prereg_sha256,
        "script_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "state_window": [w.isoformat() for w in window] if window else None,
        "state_lookback_minutes": int(STATE_LOOKBACK.total_seconds() // 60),
        "source_files": [p.name for p in source_files],
        "dropped_rows": dropped,
        "agent_uuid_count": len(agent_uuids(observations)),
        "files": {
            name: {"sha256": sha256_bytes(data), "rows": data.count(b"\n")}
            for name, data in files.items()
        },
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.chmod(manifest_path, 0o600)
    return manifest


def fetch_states(db_url: str, uuids: list[str], window: tuple[dt.datetime, dt.datetime]):
    """One REPEATABLE READ read-only transaction via asyncpg, the repo's driver."""
    import asyncio

    import asyncpg  # local import: keeps the pure functions importable without a DB driver

    async def _run():
        conn = await asyncpg.connect(db_url)
        try:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                snapshot_now = await conn.fetchval("SELECT now()")
                rows = await conn.fetch(STATE_SQL, uuids, window[0], window[1])
        finally:
            await conn.close()
        return snapshot_now, [tuple(r) for r in rows]

    return asyncio.run(_run())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shadow-dir", type=Path, default=DEFAULT_SHADOW_DIR)
    parser.add_argument("--out", type=Path, required=True, help="new directory, outside any git work tree")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = parser.parse_args(argv)

    out_dir = args.out.expanduser().resolve()
    try:
        prereg_sha = preflight(PREREG, out_dir)
    except ExtractRefused as exc:
        print(f"[resource-shadow-extract] REFUSED: {exc}", file=sys.stderr)
        return 2

    sources = shadow_files(args.shadow_dir.expanduser())
    loaded = load_shadow_rows(sources)
    uuids = agent_uuids(loaded.rows)
    snapshot_now = None
    window = None
    states: list[dict[str, Any]] = []
    if loaded.rows and uuids:
        window = state_window(loaded.rows)
        snapshot_now, db_rows = fetch_states(args.db_url, uuids, window)
        states = state_rows(db_rows)

    manifest = write_extract(
        out_dir,
        observations=loaded.rows,
        states=states,
        dropped=loaded.dropped,
        prereg_sha256=prereg_sha,
        snapshot_now=snapshot_now,
        window=window,
        source_files=sources,
    )
    counts = {name: meta["rows"] for name, meta in manifest["files"].items()}
    print(f"[resource-shadow-extract] wrote {out_dir} {counts} dropped={loaded.dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
