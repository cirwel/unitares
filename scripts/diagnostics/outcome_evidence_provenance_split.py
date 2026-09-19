#!/usr/bin/env python3
"""Split the ``tool_observed`` (0.65) outcome rows by HOW they reached that grade.

Read-only. Written to answer one open question left by PR #2316, and nothing
else: 0.65 is both the ``TOOL_OBSERVED`` weight and
``_MIN_TACTICAL_EVIDENCE_WEIGHT``, the calibration admission threshold. A row
that lands there is *admitted* — it becomes a ``hard_exogenous_signal``, trains
the tactical channel, and reaches ``calibration_deviation`` in
``src/monitors/ethical_drift.py``. So the interesting question is not how many
rows sit at 0.65 but how they got there.

``src.outcome_corroboration.tool_observation_triggers`` names five triggers.
Exactly one — ``phase5_emitter`` — is set by the server and stripped from
caller-supplied detail (``_PROVENANCE_CLAIM_KEYS``), so a caller cannot spell
it. The other four are caller-authored vocabulary: an agent that *describes* a
tool call in the right shape (``kind`` + ``exit_code``, a ``tool`` plus a return
code, a trusted source string, a non-empty results payload) reaches the same
grade as one the server watched.

This script reports that distribution. It is telemetry and carries no removal
authority: per CLAUDE.md, *a usage count may retire an instrument, never a
capability*, and no number printed here decides whether a trigger stays.

WHAT A NUMBER HERE DOES NOT ESTABLISH
-------------------------------------
- **Ungraded rows are counted separately and are not a zero.** Rows written
  before the grader existed carry no ``corroboration_grade``. "Not recorded" and
  "graded and found weak" are different findings and are printed apart.
- **Triggers are RECOMPUTED from the stored detail, not read back from it.** The
  grader never persisted which trigger fired, so this re-runs the predicate over
  the row as stored. If the detail was reshaped after grading, or the trigger
  vocabulary changed since the row was written, the recomputed trigger can
  differ from the one that actually fired. It is a reconstruction.
- **``no_trigger`` is a real bucket, not an error.** A row can hold
  ``tool_observed`` because a *higher* computed grade was clamped down to the
  provenance ceiling. Those rows are at 0.65 by capping, not by tool evidence.
- **Caller-authored does not mean false.** Most agents describing a tool call
  really did run it. The split measures what the server can *check*, not who
  lied.

Usage:
    python3 scripts/diagnostics/outcome_evidence_provenance_split.py
    python3 scripts/diagnostics/outcome_evidence_provenance_split.py --since 2026-08-01T00:00:00Z
    python3 scripts/diagnostics/outcome_evidence_provenance_split.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Optional


sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from src.db import close_db, get_db
from src.grounding.outcome_anchors import EXCLUSION_REASONS_KEY
from src.outcome_corroboration import (
    GRADE_ORDER,
    SERVER_SET_TOOL_TRIGGERS,
    TOOL_OBSERVED,
    tool_observation_triggers,
)


#: Bucket names for the 0.65 population, by which KIND of trigger fired.
BUCKET_SERVER_SET = "server_set_only"
BUCKET_CALLER_AUTHORED = "caller_authored_only"
BUCKET_BOTH = "both"
BUCKET_NO_TRIGGER = "no_trigger_clamped"

BUCKET_ORDER = (
    BUCKET_SERVER_SET,
    BUCKET_CALLER_AUTHORED,
    BUCKET_BOTH,
    BUCKET_NO_TRIGGER,
)


def _as_detail(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _bucket_for(triggers: set[str]) -> str:
    if not triggers:
        return BUCKET_NO_TRIGGER
    server = triggers & SERVER_SET_TOOL_TRIGGERS
    caller = triggers - SERVER_SET_TOOL_TRIGGERS
    if server and caller:
        return BUCKET_BOTH
    return BUCKET_SERVER_SET if server else BUCKET_CALLER_AUTHORED


def _new_bucket_stat() -> dict:
    return {"rows": 0, "calibration_admitted": 0, "distinct_agents": set()}


async def collect(pool, since: Optional[datetime]) -> dict:
    """One pass over audit.outcome_events. Returns a JSON-shaped snapshot."""
    where = ""
    params: list[Any] = []
    if since is not None:
        where = "WHERE ts >= $1"
        params.append(since)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT ts, agent_id, detail
            FROM audit.outcome_events
            {where}
            ORDER BY ts
            """,
            *params,
        )

    total = len(rows)
    ungraded = 0
    grade_histogram: Counter[str] = Counter()
    buckets: dict[str, dict] = {name: _new_bucket_stat() for name in BUCKET_ORDER}
    trigger_census: Counter[str] = Counter()
    exclusion_census: Counter[str] = Counter()
    first_ts = rows[0]["ts"] if rows else None
    last_ts = rows[-1]["ts"] if rows else None

    for row in rows:
        detail = _as_detail(row["detail"])
        grade = detail.get("corroboration_grade")
        if not grade:
            ungraded += 1
            continue
        grade_histogram[str(grade)] += 1
        if grade != TOOL_OBSERVED:
            continue

        triggers = tool_observation_triggers(detail)
        trigger_census.update(triggers or {"<none>"})
        stat = buckets[_bucket_for(triggers)]
        stat["rows"] += 1
        stat["distinct_agents"].add(row["agent_id"])

        # The actuating cut: a row only reaches tactical calibration if it was
        # not excluded AND classified as a hard exogenous signal. Reporting the
        # 0.65 population without this would overstate what actually trains.
        if detail.get("hard_exogenous_signal") and not detail.get("calibration_excluded"):
            stat["calibration_admitted"] += 1
        for reason in detail.get(EXCLUSION_REASONS_KEY) or []:
            exclusion_census[str(reason)] += 1

    return {
        "window": {
            "since": since.isoformat() if since else None,
            "first_ts": first_ts.isoformat() if first_ts else None,
            "last_ts": last_ts.isoformat() if last_ts else None,
        },
        "rows_total": total,
        "rows_ungraded": ungraded,
        "grade_histogram": {g: grade_histogram.get(g, 0) for g in GRADE_ORDER},
        "tool_observed_buckets": {
            name: {
                "rows": stat["rows"],
                "calibration_admitted": stat["calibration_admitted"],
                "distinct_agents": len(stat["distinct_agents"]),
            }
            for name, stat in buckets.items()
        },
        "trigger_census": dict(sorted(trigger_census.items())),
        "calibration_exclusion_census": dict(sorted(exclusion_census.items())),
        "server_set_triggers": sorted(SERVER_SET_TOOL_TRIGGERS),
    }


def render(snapshot: dict) -> str:
    lines: list[str] = []
    window = snapshot["window"]
    lines.append("outcome_events corroboration provenance split")
    lines.append("=" * 46)
    lines.append(
        f"window: {window['since'] or 'all time'} "
        f"(rows {window['first_ts'] or '-'} .. {window['last_ts'] or '-'})"
    )
    lines.append(f"rows scanned:  {snapshot['rows_total']}")
    lines.append(
        f"rows ungraded: {snapshot['rows_ungraded']}"
        "   <- written before the grader; NOT a weak grade"
    )
    lines.append("")
    lines.append("grade histogram (graded rows only)")
    for grade in GRADE_ORDER:
        lines.append(f"  {grade:<24} {snapshot['grade_histogram'].get(grade, 0)}")
    lines.append("")
    lines.append(f"how the {TOOL_OBSERVED} (0.65) rows got there")
    lines.append("  bucket                   rows   admitted   agents")
    for name in BUCKET_ORDER:
        stat = snapshot["tool_observed_buckets"][name]
        lines.append(
            f"  {name:<22} {stat['rows']:>6} {stat['calibration_admitted']:>10}"
            f" {stat['distinct_agents']:>8}"
        )
    lines.append("  (admitted = reached tactical calibration: hard_exogenous_signal")
    lines.append("   set and not calibration_excluded)")
    lines.append("")
    lines.append("trigger census (a row may fire several)")
    for trigger, count in snapshot["trigger_census"].items():
        origin = (
            "server-set"
            if trigger in snapshot["server_set_triggers"]
            else "caller-authored"
        )
        lines.append(f"  {trigger:<26} {count:>6}   {origin}")
    if snapshot["calibration_exclusion_census"]:
        lines.append("")
        lines.append("calibration exclusion reasons among 0.65 rows")
        for reason, count in snapshot["calibration_exclusion_census"].items():
            lines.append(f"  {reason:<26} {count:>6}")
    lines.append("")
    lines.append("This is telemetry. It authorizes no removal: see the module")
    lines.append("docstring for the four things these numbers do not establish.")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Split 0.65 outcome rows by evidence origin.")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO-8601 lower bound on ts (e.g. 2026-08-01T00:00:00Z). Default: all rows.",
    )
    parser.add_argument("--json", action="store_true", help="Print the snapshot as JSON.")
    args = parser.parse_args()

    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            print(f"error: could not parse --since {args.since!r}", file=sys.stderr)
            return 2

    try:
        pool = await get_db()
        snapshot = await collect(pool, since)
    except Exception as exc:  # pragma: no cover - operator-facing path
        print(f"error: could not read audit.outcome_events: {exc}", file=sys.stderr)
        return 1
    finally:
        await close_db()

    print(json.dumps(snapshot, indent=2) if args.json else render(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
