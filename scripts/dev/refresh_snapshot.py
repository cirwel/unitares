#!/usr/bin/env python3
"""Regenerate the README "Production snapshot" numbers from the live governance DB.

The snapshot in README.md is a dated, single-operator figure block. Hand-maintaining
it means it silently goes stale (it drifted ~10x once). This turns the refresh into one
command so an evaluator never quotes a number that is six weeks behind.

Usage:
    python3 scripts/dev/refresh_snapshot.py              # print refreshed block to stdout
    python3 scripts/dev/refresh_snapshot.py --write README.md   # update in place (row-keyed)
    python3 scripts/dev/refresh_snapshot.py --check README.md   # report drift (exit 1 if stale)

--check is a manual "is the snapshot stale?" probe. It is deliberately NOT wired into CI:
the live numbers move every day, so a hard gate would always be red. Run it before a
release or when an external evaluation is imminent.

DB-derived rows come from audit.events / core.agents / knowledge.discoveries. The two
non-DB rows ("V operating range", "Tests") are left untouched by --write and emitted from
constants by the print path.

Connection: GOVERNANCE_DATABASE_URL (same env the analysis scripts use), default local.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

# Non-DB rows: preserved as-is by --write, emitted verbatim by the print path.
STATIC_ROWS = [
    ("V operating range", "Active agents often within [-0.1, 0.1]"),
    (
        "Tests",
        "8,500+ collected · smoke/pre-push subset plus 25% min coverage gate",
    ),
]


@dataclass(frozen=True)
class Snapshot:
    events_total: int
    events_7d: int
    agents_total: int
    distinct_21d: int
    distinct_7d: int
    kg_discoveries: int


def humanize(n: int) -> str:
    """Compact human form: 3,748,915 -> '3.7M', 713,540 -> '714K'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


def floor_thousands(n: int) -> str:
    """Conservative '+' floor: 3,748,915 -> '3,748,000'."""
    return f"{(n // 1000) * 1000:,}"


def headline(snap: Snapshot, date_str: str) -> str:
    events = humanize(snap.events_total)
    last7 = humanize(snap.events_7d)
    return (
        f"Frozen public snapshot from {date_str} (single-operator deployment — "
        f"the author's own traffic, not external adoption). Headline: "
        f"**{events}+ governance events processed · ≈{last7} in the last 7 days**."
    )


def db_rows(snap: Snapshot) -> list[tuple[str, str]]:
    """The DB-derived (metric, value) rows, in README order."""
    last7 = humanize(snap.events_7d)
    return [
        (
            "Agents onboarded",
            f"{snap.agents_total:,} total process-instances — overwhelmingly ephemeral "
            "CLI sessions from one operator's workstation plus a handful of long-running "
            "resident agents (launchd crons)",
        ),
        (
            "Distinct event-emitting identities (last 21 days)",
            f"{snap.distinct_21d:,}; mostly ephemeral local CLI sessions, not external adoption",
        ),
        (
            "Unique agents active (last 7 days)",
            f"{snap.distinct_7d:,} distinct event emitters",
        ),
        (
            "Governance events processed",
            f"{floor_thousands(snap.events_total)}+ (≈{last7} in the last 7 days)",
        ),
        ("Knowledge graph discoveries", f"{snap.kg_discoveries:,}"),
    ]


def render_block(snap: Snapshot, date_str: str) -> str:
    """Full, paste-ready snapshot block: headline + complete metrics table."""
    lines = [headline(snap, date_str), "", "| Metric | Value |", "|--------|-------|"]
    for metric, value in db_rows(snap) + STATIC_ROWS:
        lines.append(f"| {metric} | {value} |")
    return "\n".join(lines)


def _row_re(metric: str) -> re.Pattern[str]:
    # Match a markdown table row keyed on the metric cell (leading pipe + metric).
    return re.compile(r"^\| " + re.escape(metric) + r" \| .*\|\s*$", re.MULTILINE)


_HEADLINE_RE = re.compile(
    r"^Frozen public snapshot from .*? in the last 7 days\*\*\.\s*$",
    re.MULTILINE,
)


def apply_to_readme(text: str, snap: Snapshot, date_str: str) -> str:
    """Row-keyed in-place update of headline + DB rows. Loud failure if anchors absent."""
    if not _HEADLINE_RE.search(text):
        raise SystemExit("error: snapshot headline line not found in target; aborting --write.")
    text = _HEADLINE_RE.sub(lambda _m: headline(snap, date_str), text, count=1)
    for metric, value in db_rows(snap):
        pat = _row_re(metric)
        if not pat.search(text):
            raise SystemExit(f"error: table row '{metric}' not found in target; aborting --write.")
        text = pat.sub(lambda _m, v=value, k=metric: f"| {k} | {v} |", text, count=1)
    return text


def check_readme(text: str, snap: Snapshot) -> list[str]:
    """Return a list of human-readable drift descriptions (empty = current)."""
    drift: list[str] = []
    for metric, value in db_rows(snap):
        m = _row_re(metric).search(text)
        if m is None:
            drift.append(f"{metric}: row missing from README")
            continue
        expected = f"| {metric} | {value} |"
        if m.group(0).rstrip() != expected:
            drift.append(f"{metric}: README has `{m.group(0).strip()}` — live is `{value}`")
    return drift


async def fetch_snapshot(db_url: str) -> Snapshot:
    try:
        import asyncpg
    except ModuleNotFoundError:
        raise SystemExit("error: asyncpg not installed. Install with `pip install asyncpg`.")

    conn = await asyncpg.connect(db_url)
    try:
        scalar = conn.fetchval
        return Snapshot(
            events_total=await scalar("SELECT count(*) FROM audit.events"),
            events_7d=await scalar(
                "SELECT count(*) FROM audit.events WHERE ts > now() - interval '7 days'"
            ),
            agents_total=await scalar("SELECT count(*) FROM core.agents"),
            distinct_21d=await scalar(
                "SELECT count(DISTINCT agent_id) FROM audit.events "
                "WHERE ts > now() - interval '21 days'"
            ),
            distinct_7d=await scalar(
                "SELECT count(DISTINCT agent_id) FROM audit.events "
                "WHERE ts > now() - interval '7 days'"
            ),
            kg_discoveries=await scalar("SELECT count(*) FROM knowledge.discoveries"),
        )
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", metavar="PATH", help="update the snapshot in PATH in place")
    parser.add_argument("--check", metavar="PATH", help="report drift in PATH (exit 1 if stale)")
    parser.add_argument(
        "--date",
        help="snapshot date label (default: today, UTC). Pass explicitly for reproducible runs.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL, help="governance DB URL")
    args = parser.parse_args(argv)

    if args.date:
        date_str = args.date
    else:
        # Imported lazily so the pure formatters above stay clock-free and unit-testable.
        from datetime import datetime, timezone

        date_str = datetime.now(timezone.utc).strftime("%B %-d, %Y")

    snap = asyncio.run(fetch_snapshot(args.db_url))

    if args.check:
        with open(args.check, encoding="utf-8") as fh:
            drift = check_readme(fh.read(), snap)
        if drift:
            print(f"snapshot STALE in {args.check}:", file=sys.stderr)
            for d in drift:
                print(f"  - {d}", file=sys.stderr)
            return 1
        print(f"snapshot current in {args.check}")
        return 0

    if args.write:
        with open(args.write, encoding="utf-8") as fh:
            original = fh.read()
        updated = apply_to_readme(original, snap, date_str)
        if updated == original:
            print(f"{args.write}: already current, no change")
            return 0
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(updated)
        print(f"{args.write}: snapshot updated ({date_str})")
        return 0

    print(render_block(snap, date_str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
