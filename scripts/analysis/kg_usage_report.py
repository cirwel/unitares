#!/usr/bin/env python3
"""Knowledge-graph usage report — is the shared memory actually being consulted?

Writes to the KG have always been audited; reads were not until the
``knowledge_read`` broadcaster event shipped (src/mcp_handlers/knowledge/
handlers.py::_broadcast_knowledge_read). That event records, per read, the
READER ``agent_id`` and — when knowable — the WRITER ``agent_id``. So the data
to answer "is anyone pulling from this, and are agents reading EACH OTHER's
discoveries?" already lives in ``audit.events``. This script surfaces it.

It is a read-only analysis tool in the same self-grading spirit as the EISV
ablation harness: it reports what the audit log can support and labels its own
confidence rather than asserting. The headline metric is **cross-agent reads**
— a read where reader != writer — because that, not raw write volume, is the
signal that the KG is functioning as shared memory rather than a write-only log.

Honesty caveats baked into the output:
  - Read-auditing only exists since the read-event shipped; windows that
    predate it undercount reads. Writes are audited further back.
  - A reader's identity is null for server-inferred / pre-onboard callers; those
    reads are counted but cannot be attributed (reader/writer unknown).
  - ``search`` carries only a SAMPLE of writer ids, so cross-agent reads via
    search are a LOWER BOUND. ``details``/``get`` carry the exact writer.

Usage:
    python3 scripts/analysis/kg_usage_report.py --window-days 90
    GOVERNANCE_DATABASE_URL=postgresql://... python3 scripts/analysis/kg_usage_report.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

WRITE_EVENT = "knowledge_write"
READ_EVENT = "knowledge_read"

# Read actions that carry writer attribution (so a read can be classed
# self vs cross-agent). ``list`` is stats-only and carries no writer.
_ATTRIBUTABLE_ACTIONS = {"search", "get", "details"}


def _writer_ids_from_read(payload: Dict[str, Any]) -> List[str]:
    """Extract the writer agent id(s) a read touched, if recorded.

    ``details``/``get`` record a single ``writer_agent_id``; ``search`` records
    a sample list ``writer_agent_ids``. Returns [] when none are attributable.
    """
    if not isinstance(payload, dict):
        return []
    single = payload.get("writer_agent_id")
    if single:
        return [str(single)]
    sample = payload.get("writer_agent_ids")
    if isinstance(sample, list):
        return [str(w) for w in sample if w]
    return []


def summarize_usage(
    writes: Sequence[Dict[str, Any]],
    reads: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Pure aggregation over write/read event rows.

    Each row is ``{"agent_id": <str|None>, "payload": <dict>}``. For reads the
    payload carries ``action`` and (when knowable) writer attribution. Kept
    free of DB I/O so the classification logic is unit-testable.
    """
    unique_authors = {w.get("agent_id") for w in writes if w.get("agent_id")}

    reads_by_action: Counter = Counter()
    unique_readers = set()
    unattributed_reader_reads = 0  # reader identity unknown (pre-onboard/inferred)
    attributable_reads = 0         # reads we could class self vs cross
    self_reads = 0
    cross_agent_reads = 0
    pair_counts: Counter = Counter()  # (reader, writer) for cross-agent reads
    cross_reader_counts: Counter = Counter()  # reader -> #cross-agent reads

    for r in reads:
        payload = r.get("payload") or {}
        action = payload.get("action") if isinstance(payload, dict) else None
        reads_by_action[action or "unknown"] += 1

        reader = r.get("agent_id")
        if reader:
            unique_readers.add(reader)
        else:
            unattributed_reader_reads += 1

        if action not in _ATTRIBUTABLE_ACTIONS or not reader:
            continue
        writer_ids = _writer_ids_from_read(payload)
        if not writer_ids:
            continue

        # A read is cross-agent if it touched any writer that isn't the reader.
        others = [w for w in writer_ids if w != reader]
        attributable_reads += 1
        if others:
            cross_agent_reads += 1
            cross_reader_counts[reader] += 1
            for w in others:
                pair_counts[(reader, w)] += 1
        else:
            self_reads += 1

    total_writes = len(writes)
    total_reads = len(reads)

    # Concentration: is cross-agent usage broad, or dominated by one reader
    # (typically a resident sweeper bulk-searching the corpus)? Without this,
    # "CROSS-AGENT ACTIVE" reads as peer-to-peer when it may be one consumer.
    cross_agent_unique_readers = len(cross_reader_counts)
    top_cross_reader_share = (
        round(cross_reader_counts.most_common(1)[0][1] / cross_agent_reads, 3)
        if cross_agent_reads
        else None
    )

    return {
        "total_writes": total_writes,
        "unique_authors": len(unique_authors),
        "total_reads": total_reads,
        "reads_by_action": dict(reads_by_action),
        "unique_readers": len(unique_readers),
        "unattributed_reader_reads": unattributed_reader_reads,
        "attributable_reads": attributable_reads,
        "self_reads": self_reads,
        "cross_agent_reads": cross_agent_reads,
        "cross_agent_unique_readers": cross_agent_unique_readers,
        "top_cross_reader_share": top_cross_reader_share,
        "read_write_ratio": round(total_reads / total_writes, 3) if total_writes else None,
        "top_reader_writer_pairs": [
            {"reader": reader, "writer": writer, "reads": n}
            for (reader, writer), n in pair_counts.most_common(10)
        ],
        "verdict": _verdict(
            total_reads,
            attributable_reads,
            cross_agent_reads,
            cross_agent_unique_readers,
            top_cross_reader_share,
        ),
    }


def _verdict(
    total_reads: int,
    attributable_reads: int,
    cross_agent_reads: int,
    cross_agent_unique_readers: int = 0,
    top_cross_reader_share: float | None = None,
) -> str:
    """Self-grading label — describes what the log supports, does not assert."""
    if total_reads == 0:
        return (
            "WRITE-ONLY — no reads recorded in this window. The KG is being "
            "written to but not consulted (or read-auditing postdates the "
            "window). Not evidence the store is unused; check a window after "
            "read-events shipped."
        )
    if attributable_reads == 0:
        return (
            "READ-BUT-UNATTRIBUTED — reads happened but none carried both a "
            "reader and writer id, so self vs cross-agent cannot be told apart "
            "(mostly anonymous/list reads)."
        )
    if cross_agent_reads == 0:
        return (
            "SINGLE-AGENT — every attributable read is a self-read. No agent "
            "was observed consulting another agent's discovery; the KG is "
            "functioning as private notes, not shared memory."
        )
    base = (
        f"CROSS-AGENT ACTIVE — {cross_agent_reads} attributable cross-agent "
        f"read(s) across {cross_agent_unique_readers} reader(s): agents are "
        f"consulting each other's discoveries. (Lower bound: search records "
        f"only a sample of writers.)"
    )
    # Guard against over-reading: if one reader dominates, the cross-agent
    # signal is likely a single sweeper, not broad peer-to-peer exchange.
    if (
        cross_agent_unique_readers <= 2
        or (top_cross_reader_share is not None and top_cross_reader_share >= 0.7)
    ):
        share = (
            f"{round(top_cross_reader_share * 100)}%"
            if top_cross_reader_share is not None
            else "most"
        )
        base += (
            f" CONCENTRATED, though: {share} of cross-agent reads come from a "
            f"single reader (typically a resident sweeper bulk-searching the "
            f"corpus) — broad peer-to-peer use is NOT demonstrated."
        )
    return base


def format_report(summary: Dict[str, Any], *, window_days: int) -> str:
    lines = [
        "Knowledge-Graph Usage Report",
        f"  window: last {window_days} days",
        "",
        f"  writes (discoveries):     {summary['total_writes']}  "
        f"(unique authors: {summary['unique_authors']})",
        f"  reads (all actions):      {summary['total_reads']}  "
        f"(unique readers: {summary['unique_readers']})",
        f"  read/write ratio:         {summary['read_write_ratio']}",
        "",
        "  reads by action:",
    ]
    for action, n in sorted(summary["reads_by_action"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {action:<10} {n}")
    lines += [
        "",
        f"  attributable reads:       {summary['attributable_reads']}",
        f"    self-reads:             {summary['self_reads']}",
        f"    cross-agent reads:      {summary['cross_agent_reads']}  "
        f"(distinct readers: {summary['cross_agent_unique_readers']}, "
        f"top reader share: {summary['top_cross_reader_share']})",
        f"  unattributed reads:       {summary['unattributed_reader_reads']} "
        f"(reader id unknown — pre-onboard/server-inferred)",
    ]
    if summary["top_reader_writer_pairs"]:
        lines += ["", "  top cross-agent reader -> writer pairs:"]
        for p in summary["top_reader_writer_pairs"]:
            lines.append(f"    {p['reader']} -> {p['writer']}  ({p['reads']})")
    lines += ["", f"  VERDICT: {summary['verdict']}"]
    return "\n".join(lines)


_FETCH_QUERY = """
    SELECT event_type, agent_id, payload
    FROM audit.events
    WHERE event_type = ANY($1::text[])
      AND ts >= now() - ($2::int * interval '1 day')
"""


async def fetch_rows(db_url: str, *, window_days: int):
    """Fetch knowledge_write/knowledge_read rows from audit.events."""
    try:
        import asyncpg
    except ImportError:
        print(
            "error: asyncpg not installed. Install with `pip install asyncpg`.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(_FETCH_QUERY, [WRITE_EVENT, READ_EVENT], window_days)
    finally:
        await conn.close()

    writes: List[Dict[str, Any]] = []
    reads: List[Dict[str, Any]] = []
    for rec in records:
        payload = rec["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        row = {"agent_id": rec["agent_id"], "payload": payload or {}}
        if rec["event_type"] == WRITE_EVENT:
            writes.append(row)
        else:
            reads.append(row)
    return writes, reads


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--output", help="Optional report output path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    writes, reads = await fetch_rows(args.db_url, window_days=args.window_days)
    summary = summarize_usage(writes, reads)
    if args.json:
        report = json.dumps(
            {"window_days": args.window_days, **summary}, indent=2, sort_keys=True
        )
    else:
        report = format_report(summary, window_days=args.window_days)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
