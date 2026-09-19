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
- **``no_trigger_recomputed`` names a state, not a cause.** A row can hold
  ``tool_observed`` with no recomputed trigger because a *higher* grade was
  clamped down to the provenance ceiling, OR because the trigger it had at
  write time is not one the current vocabulary recognises. Only the final
  grade is persisted, never the pre-clamp one, so this script cannot tell the
  two apart and does not claim to.
- **Caller-authored does not mean false.** Most agents describing a tool call
  really did run it. The split measures what the server can *check*, not who
  lied.
- **``calibration_eligible`` counts the three OBSERVABLE conditions, not the
  gate.** The recorder's condition has four parts, and ``persistence_status ==
  "created"`` is not a field on the row: an idempotent re-submission that
  returned an existing row did not train again, and nothing stored
  distinguishes it. So the count is an upper bound on training events, and
  exact only as what it literally is — rows meeting the other three.

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
from src.mcp_handlers.observability.outcome_events import HARD_EXOGENOUS_TYPES
from src.outcome_corroboration import (
    GRADE_ORDER,
    GRADE_WEIGHTS,
    SERVER_SET_TOOL_TRIGGERS,
    TOOL_OBSERVED,
    tool_observation_triggers,
)


#: Bucket names for the 0.65 population, by which KIND of trigger fired.
BUCKET_SERVER_SET = "server_set_only"
BUCKET_CALLER_AUTHORED = "caller_authored_only"
BUCKET_BOTH = "both"
#: Deliberately NOT named for a cause. A tool_observed row with no recomputed
#: trigger has at least two possible histories and this script can tell them
#: apart in neither direction: the grade may have been CLAMPED down from a
#: higher one to the provenance ceiling, or the row may have had a trigger at
#: write time that the current vocabulary no longer recognises. An earlier
#: name, ``no_trigger_clamped``, asserted the first; an external review
#: (gpt-5.6-terra, 2026-09-19) pointed out that nothing persisted establishes
#: it, because only the FINAL grade is stored, never the pre-clamp one.
BUCKET_NO_TRIGGER = "no_trigger_recomputed"

#: The write-time weight floor calibration trains above
#: (``_MIN_TACTICAL_EVIDENCE_WEIGHT`` in the recorder, defined as the
#: TOOL_OBSERVED weight).
_TACTICAL_WEIGHT_FLOOR = GRADE_WEIGHTS[TOOL_OBSERVED]

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


def _as_weight(raw: Any) -> float | None:
    """A stored evidence_weight as a float, or None if it is not a number.

    Deliberately not ``float(raw or 0.0)``. A read-only diagnostic must not
    abort a whole collection on one malformed row, and ``float("banana")``
    raises; ``bool`` is excluded because ``float(True)`` is 1.0, which would
    clear the 0.65 floor on a value that is not a weight at all. Both were
    raised by the external review's last pass (gpt-5.6-terra, 2026-09-19).
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _bucket_for(triggers: set[str]) -> str:
    if not triggers:
        return BUCKET_NO_TRIGGER
    server = triggers & SERVER_SET_TOOL_TRIGGERS
    caller = triggers - SERVER_SET_TOOL_TRIGGERS
    if server and caller:
        return BUCKET_BOTH
    return BUCKET_SERVER_SET if server else BUCKET_CALLER_AUTHORED


def _new_bucket_stat() -> dict:
    return {
        "rows": 0,
        "calibration_eligible": 0,
        "tactical_channel": 0,
        "distinct_agents": set(),
    }


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
            SELECT ts, agent_id, outcome_type, detail
            FROM audit.outcome_events
            {where}
            ORDER BY ts
            """,
            *params,
        )

    total = len(rows)
    ungraded = 0
    weight_missing = 0
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

        # THE ACTUATING CUT, read off the real gate rather than a proxy.
        # `_record_outcome_event_inline` trains calibration when the row was
        # created, carries a reported confidence, clears the evidence-weight
        # threshold (true for every row in this loop, which is already
        # filtered to 0.65) and is not excluded. `hard_exogenous_signal` is
        # NOT part of that gate: an earlier version of this script used it and
        # undercounted the general channel, which an external review caught
        # (gpt-5.6-terra, 2026-09-19).
        # Every condition is tested against the row, none assumed. The weight
        # test is redundant given the grade filter above -- tool_observed IS
        # 0.65, the threshold -- but the confirmation pass of the external
        # review was right that leaning on that coupling is an unstated
        # assumption in a script whose whole job is not to make them.
        # A graded row always carries evidence_weight -- both come from the
        # same as_metadata() -- so one without it is a shape this script has
        # not seen, NOT a row that failed the gate. Count it as its own state
        # rather than letting it sink silently into "not eligible", which is
        # the four-state rule applied to the instrument itself.
        weight = _as_weight(detail.get("evidence_weight"))
        if weight is None:
            weight_missing += 1
        eligible = (
            weight is not None
            and weight >= _TACTICAL_WEIGHT_FLOOR
            and detail.get("reported_confidence") is not None
            and not detail.get("calibration_excluded")
        )
        if eligible:
            stat["calibration_eligible"] += 1
            # The narrower TACTICAL channel keys on the outcome TYPE, not on
            # the nulled-out hard_exogenous_signal field.
            if row["outcome_type"] in HARD_EXOGENOUS_TYPES:
                stat["tactical_channel"] += 1
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
        "tool_observed_rows_without_a_usable_weight": weight_missing,
        "grade_histogram": {g: grade_histogram.get(g, 0) for g in GRADE_ORDER},
        "tool_observed_buckets": {
            name: {
                "rows": stat["rows"],
                "calibration_eligible": stat["calibration_eligible"],
                "tactical_channel": stat["tactical_channel"],
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
    if snapshot["tool_observed_rows_without_a_usable_weight"]:
        lines.append(
            f"rows graded {TOOL_OBSERVED} with no usable evidence_weight: "
            f"{snapshot['tool_observed_rows_without_a_usable_weight']}"
            "   <- unexpected shape, not a failed gate"
        )
    lines.append("")
    lines.append("grade histogram (graded rows only)")
    for grade in GRADE_ORDER:
        lines.append(f"  {grade:<24} {snapshot['grade_histogram'].get(grade, 0)}")
    lines.append("")
    lines.append(f"how the {TOOL_OBSERVED} (0.65) rows got there")
    lines.append("  bucket                   rows  eligible  tactical   agents")
    for name in BUCKET_ORDER:
        stat = snapshot["tool_observed_buckets"][name]
        lines.append(
            f"  {name:<22} {stat['rows']:>6} {stat['calibration_eligible']:>9}"
            f" {stat['tactical_channel']:>9} {stat['distinct_agents']:>8}"
        )
    lines.append("  eligible = met the THREE OBSERVABLE conditions of the")
    lines.append("             calibration gate: weight >= the floor, a reported")
    lines.append("             confidence, not excluded. The gate's fourth,")
    lines.append("             persistence_status == 'created', is not a field on")
    lines.append("             the row, so this is an upper bound on training.")
    lines.append("  tactical = of those, the ones whose outcome_type is in")
    lines.append("             HARD_EXOGENOUS_TYPES, the narrower tactical lane")
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
