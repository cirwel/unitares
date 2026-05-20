#!/usr/bin/env python3
"""§129 Wave 1 condition 1 re-evaluation script.

Runs the three falsifier conditions from
`docs/proposals/wave-1-window-evaluation-2026-05-18.md`:

  1. `incident_id` is populated in the decorator emit payload
     (`src/mcp_handlers/decorators.py`). Shipped 2026-05-19 (#463); verified
     here against recent rows.
  2. Window is under representative load: `core.agent_state` writes
     averaging >= 500/day across the window.
  3. `count(DISTINCT payload->>'incident_id')` over the window is 0.

Default window: T+0 = 2026-05-19 (decorator fix merge), T+14 = 2026-06-02.
Override with --start / --days.

Exit codes:
  0 — all three conditions met (Wave 1 condition 1 substantively passes)
  1 — at least one condition not met
  2 — window has not yet completed; informational run

Usage:
    python3 scripts/dev/section_129_reeval.py
    python3 scripts/dev/section_129_reeval.py --json
    python3 scripts/dev/section_129_reeval.py --start 2026-05-19 --days 14
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import psycopg2  # type: ignore
import psycopg2.extras  # type: ignore


DEFAULT_START = date(2026, 5, 19)
DEFAULT_DAYS = 14
LOAD_FLOOR_WRITES_PER_DAY = 500


@dataclass
class ConditionResult:
    name: str
    met: bool
    detail: dict


@dataclass
class EvalResult:
    window_start: str
    window_end: str
    window_complete: bool
    conditions: list[ConditionResult]
    overall_pass: bool


def connect():
    dsn = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    return psycopg2.connect(dsn)


def check_incident_id_wired(cur, start: datetime, end: datetime) -> ConditionResult:
    cur.execute(
        """
        SELECT
            count(*) FILTER (WHERE payload ? 'incident_id') AS with_id,
            count(*)                                        AS total,
            min(ts), max(ts)
        FROM audit.events
        WHERE event_type = 'coordination_failure.mcp_handler_timeout.tool_decorator'
          AND ts >= %s AND ts < %s
        """,
        (start, end),
    )
    row = cur.fetchone()
    with_id, total, first_ts, last_ts = row
    detail = {
        "decorator_emits_in_window": total,
        "decorator_emits_with_incident_id": with_id,
        "first_emit_ts": first_ts.isoformat() if first_ts else None,
        "last_emit_ts": last_ts.isoformat() if last_ts else None,
    }
    if total == 0:
        detail["note"] = (
            "no decorator emits in window; cannot verify field is populated. "
            "Re-check after a coordination event fires."
        )
        return ConditionResult("incident_id_wired", met=False, detail=detail)
    met = with_id == total
    if not met:
        detail["missing_count"] = total - with_id
    return ConditionResult("incident_id_wired", met=met, detail=detail)


def check_representative_load(cur, start: datetime, end: datetime) -> ConditionResult:
    cur.execute(
        """
        SELECT date_trunc('day', recorded_at AT TIME ZONE 'UTC') AS day, count(*) AS writes
        FROM core.agent_state
        WHERE recorded_at >= %s AND recorded_at < %s
        GROUP BY 1
        ORDER BY 1
        """,
        (start, end),
    )
    rows = cur.fetchall()
    by_day = {r[0].date().isoformat(): int(r[1]) for r in rows}
    days_in_window = (end - start).days
    total = sum(by_day.values())
    avg = (total / days_in_window) if days_in_window > 0 else 0.0
    met = avg >= LOAD_FLOOR_WRITES_PER_DAY
    return ConditionResult(
        "representative_load",
        met=met,
        detail={
            "writes_per_day_avg": round(avg, 1),
            "floor_writes_per_day": LOAD_FLOOR_WRITES_PER_DAY,
            "days_in_window": days_in_window,
            "by_day": by_day,
        },
    )


def check_zero_incidents(cur, start: datetime, end: datetime) -> ConditionResult:
    cur.execute(
        """
        SELECT
            count(*)                                  AS raw_rows,
            count(DISTINCT payload->>'incident_id')   AS distinct_incidents,
            count(*) FILTER (WHERE payload ? 'incident_id') AS rows_with_id
        FROM audit.events
        WHERE event_type LIKE 'coordination_failure.%%'
          AND ts >= %s AND ts < %s
        """,
        (start, end),
    )
    raw_rows, distinct_incidents, rows_with_id = cur.fetchone()
    detail = {
        "raw_rows": int(raw_rows),
        "distinct_incidents": int(distinct_incidents),
        "rows_with_incident_id": int(rows_with_id),
    }
    if raw_rows > 0 and rows_with_id < raw_rows:
        detail["caveat"] = (
            "some rows lack incident_id; distinct count is suppressed and not a "
            "trustworthy measurement until Condition 1 is fully met."
        )
    met = distinct_incidents == 0
    return ConditionResult("zero_incidents", met=met, detail=detail)


def run(start_date: date, days: int) -> EvalResult:
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=days)
    now = datetime.now(timezone.utc)
    window_complete = end <= now
    effective_end = end if window_complete else now
    with connect() as conn:
        with conn.cursor() as cur:
            c1 = check_incident_id_wired(cur, start, effective_end)
            c2 = check_representative_load(cur, start, effective_end)
            c3 = check_zero_incidents(cur, start, effective_end)
    conditions = [c1, c2, c3]
    overall = window_complete and all(c.met for c in conditions)
    return EvalResult(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        window_complete=window_complete,
        conditions=conditions,
        overall_pass=overall,
    )


def render_text(result: EvalResult) -> str:
    lines = [
        f"§129 Wave 1 condition 1 re-evaluation",
        f"Window: {result.window_start} -> {result.window_end}",
        f"Window complete: {result.window_complete}",
        "",
    ]
    for c in result.conditions:
        status = "PASS" if c.met else "FAIL"
        lines.append(f"[{status}] {c.name}")
        for k, v in c.detail.items():
            if k == "by_day":
                continue
            lines.append(f"    {k}: {v}")
        if "by_day" in c.detail:
            lines.append("    by_day:")
            for day, writes in c.detail["by_day"].items():
                lines.append(f"      {day}: {writes}")
        lines.append("")
    verdict = "PASS" if result.overall_pass else (
        "PENDING (window incomplete)" if not result.window_complete else "FAIL"
    )
    lines.append(f"Overall: {verdict}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        type=lambda s: date.fromisoformat(s),
        default=DEFAULT_START,
        help=f"window start date (UTC). Default {DEFAULT_START.isoformat()} (decorator fix merge)",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help=f"window length in days. Default {DEFAULT_DAYS}"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    result = run(args.start, args.days)
    if args.json:
        payload = {
            "window_start": result.window_start,
            "window_end": result.window_end,
            "window_complete": result.window_complete,
            "conditions": [asdict(c) for c in result.conditions],
            "overall_pass": result.overall_pass,
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_text(result))

    if not result.window_complete:
        return 2
    return 0 if result.overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
