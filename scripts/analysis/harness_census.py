#!/usr/bin/env python3
"""Descriptive harness census — inventory every harness seen in S22 provenance.

Read-only. Reads durable S22 write context from
``core.agent_state.state_json.provenance_context`` and
``knowledge.discoveries.provenance.s22_context`` (reusing the s22_h5 collector),
then rolls it up by canonical harness: entry counts, distinct agents, first/last
seen, transports/models, and per-harness situating-metadata ratio.

This is a *census*, not a registry: harness labels are self-declared and confer no
authority (``docs/ontology/harness-substrate-plurality.md``). It is the evidence
``docs/ontology/plan.md`` Track D wants before promoting ``harness_id``.

Usage:
    python3 scripts/analysis/harness_census.py
    python3 scripts/analysis/harness_census.py --json
    python3 scripts/analysis/harness_census.py --limit-per-source 1000 --output data/analysis/harness_census.md

Env:
    GOVERNANCE_DATABASE_URL  (whatever src.db.get_db resolves)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import close_db  # noqa: E402
from src.identity.s22_h5_comparison import collect_s22_h5_entries  # noqa: E402
from src.identity.harness_census import build_harness_census  # noqa: E402


def _render_markdown(census: dict) -> str:
    lines = [
        "# Harness census (descriptive)",
        "",
        f"- entries: **{census['total_entries']}** "
        f"(attributed {census['attributed_entries']}, "
        f"unattributed {census['unattributed_entries']})",
        f"- distinct harnesses: **{census['distinct_harnesses']}**",
        "",
        "| harness | entries | agents | situated | first seen | last seen | transports | models |",
        "|---|--:|--:|--:|---|---|---|---|",
    ]
    for h in census["harnesses"]:
        lines.append(
            f"| `{h['canonical_harness']}` | {h['entry_count']} | {h['distinct_agents']} | "
            f"{h['situating_metadata_ratio']:.2f} | {h['first_seen'] or '—'} | "
            f"{h['last_seen'] or '—'} | {', '.join(h['transports']) or '—'} | "
            f"{', '.join(h['models']) or '—'} |"
        )
    if census["unattributed_entries"]:
        lines += ["", f"_{census['unattributed_entries']} entries carried no harness "
                  "label (the labelling gap — these are invisible to any future registry)._"]
    return "\n".join(lines) + "\n"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit-per-source", type=int, default=500,
                    help="Max S22 rows to read from agent_state and KG independently.")
    ap.add_argument("--json", action="store_true", help="Emit the full census as JSON.")
    ap.add_argument("--output", default=None, help="Write the markdown report here.")
    args = ap.parse_args()

    try:
        entries = await collect_s22_h5_entries(limit_per_source=args.limit_per_source)
        census = build_harness_census(entries)
    finally:
        await close_db()

    if args.json:
        print(json.dumps(census, indent=2, sort_keys=True))
        return 0
    report = _render_markdown(census)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(report)
        print(f"Wrote {args.output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
