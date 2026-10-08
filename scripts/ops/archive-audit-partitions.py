#!/usr/bin/env python3
"""Archive old audit.events / audit.tool_usage months, then drop them.

Retention used to live inside audit.partition_maintenance(), which dropped
these months outright. Migration 073 took it out; this script is the
replacement: a partition is dropped only after its rows are in a compressed
CSV that has been read back and counted.

    archive-audit-partitions.py              # dry run: list what is eligible
    archive-audit-partitions.py --export     # write archives, drop nothing
    archive-audit-partitions.py --apply      # write archives, then drop

Eligibility is the same rule the drop functions use (a month whose upper
bound is older than now() minus the retention, compared as an instant; see
migration 055), evaluated in SQL so the two cannot disagree. Check-ins
(core.agent_state) and outcome_events are never touched: they are kept
(operator decision, 2026-10-07).

Per partition the archive is <dir>/<partition>.csv.gz plus one line in
<dir>/manifest.jsonl (rows, bytes, sha256, bounds). A drop rechecks the live
row count inside the dropping transaction and refuses if it changed.
Uses psql, like the other ops scripts; the DSN comes from
GOVERNANCE_DATABASE_URL.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/governance"
DEFAULT_DIR = Path.home() / "backups" / "archive" / "audit-partitions"
# The retentions partition_maintenance() applied before 073.
PARENTS = {"events": 180, "tool_usage": 90}
NAME = re.compile(r"^[a-z0-9_]+$")

ELIGIBLE_SQL = """
SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_inherits i ON i.inhrelid = c.oid
JOIN pg_class parent ON parent.oid = i.inhparent
WHERE n.nspname = 'audit' AND parent.relname = '{parent}' AND c.relkind = 'r'
  AND pg_get_expr(c.relpartbound, c.oid) ~ 'TO \\(''([^'']+)'''
  AND ((regexp_match(pg_get_expr(c.relpartbound, c.oid), 'TO \\(''([^'']+)'''))[1])::timestamptz
      < now() - make_interval(hours => {days} * 24)
ORDER BY 1
"""


def psql(dsn: str, sql: str) -> str:
    out = subprocess.run(
        ["psql", dsn, "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )
    return out.stdout


def eligible(dsn: str, parent: str, days: int) -> list[tuple[str, str]]:
    rows = psql(dsn, ELIGIBLE_SQL.format(parent=parent, days=int(days))).splitlines()
    return [tuple(r.split("\t", 1)) for r in rows if r]


def count_csv_records(path: Path) -> int:
    """Data records in a gzipped CSV with a header; quoted newlines count once."""
    with gzip.open(path, "rt", newline="") as fh:
        return sum(1 for _ in csv.reader(fh)) - 1


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(dsn: str, name: str, out_dir: Path) -> dict:
    """Write <name>.csv.gz, read it back, and return its manifest entry."""
    rows = int(psql(dsn, f"SELECT count(*) FROM audit.{name}").strip())
    final = out_dir / f"{name}.csv.gz"
    partial = final.with_suffix(".gz.partial")
    with gzip.open(partial, "wb") as gz:
        proc = subprocess.Popen(
            ["psql", dsn, "-X", "-v", "ON_ERROR_STOP=1", "-c",
             f"\\copy (SELECT * FROM audit.{name}) TO STDOUT WITH (FORMAT csv, HEADER)"],
            stdout=subprocess.PIPE,
        )
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            gz.write(chunk)
        if proc.wait() != 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"export of audit.{name} failed (psql exit {proc.returncode})")
    archived = count_csv_records(partial)
    if archived != rows:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"audit.{name}: archive holds {archived} rows, table {rows}")
    partial.rename(final)
    return {"partition": name, "rows": rows, "bytes": final.stat().st_size,
            "sha256": sha256(final), "file": final.name}


def drop(dsn: str, name: str, rows: int) -> None:
    psql(dsn, f"""
BEGIN;
LOCK TABLE audit.{name} IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
  IF (SELECT count(*) FROM audit.{name}) <> {int(rows)} THEN
    RAISE EXCEPTION 'audit.{name} changed since it was archived';
  END IF;
END $$;
DROP TABLE audit.{name};
COMMIT;""")


def archived_entry(out_dir: Path, name: str) -> dict | None:
    """The manifest entry for an archive that still matches its file, if any."""
    manifest = out_dir / "manifest.jsonl"
    final = out_dir / f"{name}.csv.gz"
    if not (manifest.exists() and final.exists()):
        return None
    entries = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    for entry in reversed(entries):
        if entry["partition"] == name:
            return entry if entry["sha256"] == sha256(final) else None
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--export", action="store_true", help="write archives, drop nothing")
    mode.add_argument("--apply", action="store_true", help="write archives, then drop")
    ap.add_argument("--archive-dir", type=Path, default=DEFAULT_DIR)
    for parent, days in PARENTS.items():
        ap.add_argument(f"--{parent.replace('_', '-')}-days", type=int, default=days)
    args = ap.parse_args(argv)
    dsn = os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DSN)

    plan = []
    for parent in PARENTS:
        days = getattr(args, f"{parent}_days")
        if days < 0:
            ap.error("retention days must be non-negative")
        for name, bound in eligible(dsn, parent, days):
            if not NAME.match(name):
                raise SystemExit(f"refusing unexpected partition name {name!r}")
            plan.append((name, bound))
    if not plan:
        print("nothing eligible")
        return 0
    for name, bound in plan:
        print(f"eligible: audit.{name}  {bound}")
    if not (args.export or args.apply):
        print("dry run: nothing written or dropped (--export or --apply to act)")
        return 0

    args.archive_dir.mkdir(parents=True, exist_ok=True)
    for name, bound in plan:
        entry = archived_entry(args.archive_dir, name)
        if entry is None:
            entry = export(dsn, name, args.archive_dir)
            entry.update(bound=bound, archived_at=datetime.now(timezone.utc).isoformat())
            with (args.archive_dir / "manifest.jsonl").open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
            print(f"archived: audit.{name}  {entry['rows']} rows  {entry['file']}")
        else:
            print(f"already archived: audit.{name}  {entry['rows']} rows")
        if args.apply:
            drop(dsn, name, entry["rows"])
            print(f"dropped: audit.{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
