#!/usr/bin/env python3
"""Archive, and later reconcile, the rows the registered stop-rule read can touch.

Procedure owner: ``docs/proposals/registered/eisv-outcome-grounding-stop-rule-v0.md``,
*Amendment 2026-10-15: data boundary at the first instrument change*. When the
first change to the live EISV update path deploys, the registered read's data
boundary moves to that instant T. ``audit.outcome_events`` has no insert-time
column and ``core.agent_state`` is subject to retention, so neither a backfill
carrying a pre-T timestamp nor a retention deletion can be detected from the
live tables alone. This tool closes that gap with two subcommands:

``export``
    Run once, at or after T (at the deployment that T records). Writes, under
    ``--out``, one gzip JSONL file per table holding every row the read can
    reach that predates T, plus ``manifest.json`` with row counts and sha256
    digests. The manifest's ``snapshot_sha256`` is recorded in the stop-rule
    document's amendment record by pull request. The archive itself never
    enters the repository.

``reconcile``
    Run after the registered read, as its pre-declared reconciliation. Applies
    the same predicates to the live database and reports, per table, the rows
    present live but absent from the snapshot (backfills), the rows present in
    the snapshot but absent live (retention or deletion), and the rows whose
    content changed. Counts only. Discrepancies are disclosed in the read's
    report; they do not change its verdict rule.

What is archived, all strictly before T:

- ``audit.outcome_events`` rows with ``ts`` in [T - window - margin, T);
- every ``core.agent_state`` row with ``recorded_at`` < T (the read's
  prior-state join has no lower bound);
- ``(identity_id, agent_id)`` from ``core.identities`` for every identity
  those state rows reference.

Neither subcommand joins a state to an outcome or computes any statistic; this
is an archival read of the kind ``outcome_inventory.py`` performs, not a
discrimination read. Both run in one read-only REPEATABLE READ transaction.

Usage:
    python3 scripts/analysis/eisv_read_input_snapshot.py export \\
        --boundary 2026-10-20T09:30:00Z --out ~/.unitares/analysis/stop-rule-snapshot-T
    python3 scripts/analysis/eisv_read_input_snapshot.py reconcile \\
        --snapshot ~/.unitares/analysis/stop-rule-snapshot-T

Env:
    GOVERNANCE_DATABASE_URL  (default inherited from eisv_skeptic_report)
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_skeptic_report import DEFAULT_DB_URL, parse_as_of

SCHEMA = "unitares.eisv_read_input_snapshot.v1"
DEFAULT_WINDOW_DAYS = 365
# Outcomes slightly older than the window start are archived too, so a later
# disagreement about the exact window edge cannot leave a row unarchived.
WINDOW_MARGIN = timedelta(days=1)


@dataclass(frozen=True)
class TableSpec:
    name: str
    file: str
    query: str  # $1 = boundary T, $2 = earliest outcome ts archived
    key_fields: tuple[str, ...]


TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        name="audit.outcome_events",
        file="outcome_events.jsonl.gz",
        query=(
            "SELECT to_jsonb(o)::text FROM audit.outcome_events o "
            "WHERE o.ts >= $2::timestamptz AND o.ts < $1::timestamptz "
            "ORDER BY o.ts, o.outcome_id"
        ),
        key_fields=("ts", "outcome_id"),
    ),
    TableSpec(
        name="core.agent_state",
        file="agent_state.jsonl.gz",
        query=(
            "SELECT to_jsonb(s)::text FROM core.agent_state s "
            "WHERE s.recorded_at < $1::timestamptz AND $2::timestamptz IS NOT NULL "
            "ORDER BY s.state_id"
        ),
        key_fields=("state_id",),
    ),
    TableSpec(
        name="core.identities",
        file="identities.jsonl.gz",
        query=(
            "SELECT jsonb_build_object('identity_id', i.identity_id, 'agent_id', i.agent_id)::text "
            "FROM core.identities i "
            "WHERE i.identity_id IN (SELECT DISTINCT s.identity_id FROM core.agent_state s "
            "WHERE s.recorded_at < $1::timestamptz) AND $2::timestamptz IS NOT NULL "
            "ORDER BY i.identity_id"
        ),
        key_fields=("identity_id",),
    ),
)


def window_start(boundary: datetime, window_days: int) -> datetime:
    return boundary - timedelta(days=window_days) - WINDOW_MARGIN


def row_key(spec: TableSpec, row_text: str) -> str:
    """A stable key for one archived row, from its own JSON text."""
    row = json.loads(row_text)
    return "|".join(str(row[field]) for field in spec.key_fields)


def row_digest(row_text: str) -> str:
    return hashlib.sha256(row_text.encode("utf-8")).hexdigest()


class _ContentHasher:
    """sha256 over the rows as newline-terminated UTF-8 lines, in export order."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self.rows = 0

    def update(self, row_text: str) -> None:
        self._hash.update(row_text.encode("utf-8") + b"\n")
        self.rows += 1

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


class _TableWriter:
    """Gzip JSONL writer (mtime pinned) that digests rows as they are written."""

    def __init__(self, path: Path) -> None:
        fd = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._raw = os.fdopen(fd, "wb")
        self._out = gzip.GzipFile(fileobj=self._raw, mode="wb", mtime=0)
        self._hasher = _ContentHasher()

    def write(self, row_text: str) -> None:
        if "\n" in row_text:
            raise ValueError("row text must be a single line")
        self._hasher.update(row_text)
        self._out.write(row_text.encode("utf-8") + b"\n")

    def close(self) -> dict[str, Any]:
        self._out.close()
        self._raw.close()
        return {"rows": self._hasher.rows, "content_sha256": self._hasher.hexdigest()}


def write_table(path: Path, rows: Iterable[str]) -> dict[str, Any]:
    """Write rows as gzip JSONL and return their count and content digest."""
    writer = _TableWriter(path)
    try:
        for row_text in rows:
            writer.write(row_text)
    finally:
        summary = writer.close()
    return summary


def read_table(path: Path) -> Iterator[str]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            yield line.rstrip("\n")


def snapshot_sha256(tables: dict[str, dict[str, Any]]) -> str:
    """One digest over every table's row count and content digest."""
    canonical = json.dumps(
        {name: {"rows": t["rows"], "content_sha256": t["content_sha256"]} for name, t in tables.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest(
    *,
    boundary: datetime,
    window_days: int,
    exported_at: datetime,
    tables: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "boundary": boundary.astimezone(timezone.utc).isoformat(),
        "window_days": window_days,
        "outcome_ts_from": window_start(boundary, window_days).astimezone(timezone.utc).isoformat(),
        "exported_at": exported_at.astimezone(timezone.utc).isoformat(),
        "tables": tables,
        "snapshot_sha256": snapshot_sha256(tables),
    }


def verify_snapshot(directory: Path) -> dict[str, Any]:
    """Load the manifest and check every file's content against it."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError(f"unexpected snapshot schema {manifest.get('schema')!r}")
    for spec in TABLES:
        recorded = manifest["tables"][spec.name]
        hasher = _ContentHasher()
        for row_text in read_table(directory / recorded["file"]):
            hasher.update(row_text)
        if hasher.rows != recorded["rows"] or hasher.hexdigest() != recorded["content_sha256"]:
            raise ValueError(f"{spec.name}: archived file does not match its manifest entry")
    if snapshot_sha256(manifest["tables"]) != manifest["snapshot_sha256"]:
        raise ValueError("manifest snapshot_sha256 does not match its table entries")
    return manifest


def digest_map(spec: TableSpec, rows: Iterable[str]) -> dict[str, str]:
    return {row_key(spec, row_text): row_digest(row_text) for row_text in rows}


def reconcile_digests(snapshot: dict[str, str], live: dict[str, str]) -> dict[str, int]:
    """Counts of agreement between archived and live rows, keyed by row identity."""
    snapshot_keys = snapshot.keys()
    live_keys = live.keys()
    shared = snapshot_keys & live_keys
    return {
        "snapshot_rows": len(snapshot),
        "live_rows": len(live),
        "matched": sum(1 for key in shared if snapshot[key] == live[key]),
        "changed": sum(1 for key in shared if snapshot[key] != live[key]),
        "live_only_backfill": len(live_keys - snapshot_keys),
        "snapshot_only_removed": len(snapshot_keys - live_keys),
    }


async def _stream_rows(conn: Any, spec: TableSpec, boundary: datetime, since: datetime):
    async for record in conn.cursor(spec.query, boundary, since, prefetch=2000):
        yield record[0]


async def _connect(db_url: str) -> Any:
    import asyncpg

    conn = await asyncpg.connect(db_url)
    # to_jsonb renders timestamptz in the session time zone; pin it so an
    # export and a later reconciliation produce byte-identical row text.
    await conn.execute("SET TIME ZONE 'UTC'")
    return conn


async def export_snapshot(
    db_url: str, *, boundary: datetime, window_days: int, out: Path, now: datetime | None = None
) -> dict[str, Any]:
    exported_at = now or datetime.now(timezone.utc)
    if boundary > exported_at:
        raise ValueError(
            "the boundary is in the future: export at or after T, so every pre-T row exists"
        )
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError(f"{out} is not empty; a snapshot is written once")
    since = window_start(boundary, window_days)
    tables: dict[str, dict[str, Any]] = {}
    conn = await _connect(db_url)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            for spec in TABLES:
                writer = _TableWriter(out / spec.file)
                try:
                    async for row_text in _stream_rows(conn, spec, boundary, since):
                        writer.write(row_text)
                finally:
                    summary = writer.close()
                tables[spec.name] = {"file": spec.file, **summary}
    finally:
        await conn.close()
    manifest = build_manifest(
        boundary=boundary, window_days=window_days, exported_at=exported_at, tables=tables
    )
    fd = os.open(os.fspath(out / "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


async def reconcile_snapshot(db_url: str, *, snapshot: Path) -> dict[str, Any]:
    manifest = verify_snapshot(snapshot)
    boundary = parse_as_of(manifest["boundary"])
    since = window_start(boundary, int(manifest["window_days"]))
    result: dict[str, Any] = {
        "schema": f"{SCHEMA}.reconciliation",
        "boundary": manifest["boundary"],
        "snapshot_sha256": manifest["snapshot_sha256"],
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
    }
    conn = await _connect(db_url)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            for spec in TABLES:
                archived = digest_map(spec, read_table(snapshot / manifest["tables"][spec.name]["file"]))
                live = {
                    row_key(spec, row_text): row_digest(row_text)
                    async for row_text in _stream_rows(conn, spec, boundary, since)
                }
                result["tables"][spec.name] = reconcile_digests(archived, live)
    finally:
        await conn.close()
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Archive the pre-T rows the registered read can touch.")
    export.add_argument("--boundary", type=parse_as_of, required=True, help="T, as recorded in the manifest entry.")
    export.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    export.add_argument("--out", type=Path, required=True, help="An empty directory outside the repository.")
    reconcile = sub.add_parser("reconcile", help="Compare a snapshot with the live tables (counts only).")
    reconcile.add_argument("--snapshot", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "export":
            result = asyncio.run(
                export_snapshot(
                    args.db_url, boundary=args.boundary, window_days=args.window_days, out=args.out.expanduser()
                )
            )
        else:
            result = asyncio.run(reconcile_snapshot(args.db_url, snapshot=args.snapshot.expanduser()))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
