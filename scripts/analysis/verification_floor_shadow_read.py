"""Soak read for the verification-floor shadow — a rate, with its denominator.

The verification floor (`docs/proposals/verification-weighted-verdict-v0.md`)
ships default-off with its shadow default-ON, so every deployment that has not
opted out is already computing the signal on every check-in. Its acceptance gate
asks for "a real false-positive-regression pass on a larger benign corpus". Live
traffic *is* that corpus; this script reads what the sink
(`src/verification_floor_shadow.py`, event type `verification_floor_shadow`)
recorded and reports the rate.

Measurement only. This script changes no flag, threshold, verdict or weight, and
it does not recommend enabling anything. Enabling the floor stays a conjunction
of the owners' sign-off and a safety-envelope review; a number here discharges
one clause of one bullet of that gate, and nothing else.

What it refuses to do
---------------------
* **Report a rate from a numerator.** Under
  `GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD=firings` the sink writes only
  would-fire rows. There is no denominator in that data, so the report names the
  mode and declines rather than dividing by the rows that happen to be present.
* **Pool simulation rows with live traffic.** `simulate_update` drives the same
  monitor path; its rows are synthetic and are counted separately.
* **Read an empty window as a clean record.** Zero rows is reported as one of
  four states (`CLAUDE.md`, *Measurement authority*) — never surfaced, not
  reachable, not recorded, or genuinely quiet — and this data alone cannot
  distinguish the first three from each other. Silence here is a coverage
  finding, not a false-positive rate of zero.
* **Call an unscored caller a clean one.** The detector reads English
  first-person narration. A templated status line or a state digest cannot fire
  it, so those rows are excluded from the prose denominator and reported as
  their own bucket. A rate over the wider set understates what enabling would do
  to the agents that actually write prose.

Usage
-----
    python3 scripts/analysis/verification_floor_shadow_read.py --window-days 14
    python3 scripts/analysis/verification_floor_shadow_read.py --input data/audit_log.jsonl
    python3 scripts/analysis/verification_floor_shadow_read.py --json

`--input` accepts either a JSONL export of payloads or the raw
`data/audit_log.jsonl` (whose lines are audit entries carrying `details`); both
shapes are detected. Covered by `tests/test_verification_floor_shadow_read.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.verification_floor_shadow import (  # noqa: E402
    EVENT_TYPE,
    RECORD_ALL,
    SCHEMA,
)

DEFAULT_DB_URL = "postgresql://localhost:5432/governance"
DEFAULT_WINDOW_DAYS = 14

FETCH_QUERY = f"""
    SELECT agent_id, ts, payload
    FROM audit.events
    WHERE event_type = '{EVENT_TYPE}'
      AND ts >= now() - ($1::int * interval '1 day')
    ORDER BY ts
"""

#: The four states a zero can be in. Named here so a report of silence has to
#: pick one rather than implying the last.
ZERO_STATES = (
    "never_surfaced: nothing put the instrument in a caller's context",
    "not_reachable: it errored, or was built and never wired",
    "not_recorded: the sink was off, narrowed, or the writes failed",
    "genuinely_quiet: it ran, recorded, and no check-in would have fired",
)


def _normalize(row: Dict[str, Any]) -> Dict[str, Any]:
    """Accept a bare payload or a raw audit-log line; return the payload."""
    if not isinstance(row, dict):
        return {}
    if row.get("schema") == SCHEMA:
        return row
    if row.get("event_type") == EVENT_TYPE and isinstance(row.get("details"), dict):
        payload = dict(row["details"])
        payload.setdefault("agent_id", row.get("agent_id"))
        payload.setdefault("timestamp", row.get("timestamp"))
        return payload
    return row


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A rate, or None when there is no denominator to divide by."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate shadow rows into the soak report. Pure.

    Rows may arrive as payloads (DB or JSONL export) or as raw audit-log lines;
    provenance does not change the result.
    """
    normalized = [_normalize(row) for row in rows]
    normalized = [row for row in normalized if row]

    schemas: Counter = Counter(row.get("schema") or "unknown" for row in normalized)
    # Never pool schema versions: a later version may change what a firing means.
    current = [row for row in normalized if row.get("schema") == SCHEMA]

    modes: Counter = Counter(row.get("record_mode") or "unknown" for row in current)
    scopes: Counter = Counter(
        row.get("measurement_scope") or "unknown" for row in current
    )

    live = [row for row in current if row.get("measurement_scope") == "live"]
    simulation = [row for row in current if row.get("measurement_scope") == "simulation"]

    # A rate needs rows that were written whether or not they fired. Any window
    # containing a narrowed mode has a denominator this data cannot supply.
    non_all_modes = {mode for mode in modes if mode != RECORD_ALL}
    rate_computable = bool(live) and not non_all_modes

    scoreable = [row for row in live if row.get("scoreable")]
    unscoreable_reasons: Counter = Counter(
        row.get("unscoreable_reason") or "unknown"
        for row in live
        if not row.get("scoreable")
    )
    # The prose denominator: rows the detector could structurally fire on. A
    # first-person pronoun is necessary for its verb-object patterns, not
    # sufficient — this bucket excludes what cannot fire, it does not certify
    # what remains.
    prose = [row for row in scoreable if row.get("first_person")]
    non_prose = len(scoreable) - len(prose)

    fired = [row for row in live if row.get("would_fire")]
    escalated = [row for row in fired if row.get("would_escalate_verdict")]
    # The candidate false positives: the deployment proceeded, and enabling the
    # floor would have raised the verdict on that same check-in. Candidate, not
    # confirmed — whether the escalation was wrong is a human read of `matches`.
    candidate_false_positives = [
        row
        for row in escalated
        if str(row.get("live_action") or "").strip().lower() in {"proceed", "approve", "continue"}
    ]
    agreed_escalations = [
        row
        for row in escalated
        if str(row.get("live_action") or "").strip().lower() not in {"proceed", "approve", "continue"}
    ]

    bands: Counter = Counter(row.get("verdict") or "unknown" for row in fired)
    categories: Counter = Counter()
    for row in fired:
        for key in (row.get("categories") or {}):
            categories[key] += 1

    agents = {row.get("agent_id") for row in live if row.get("agent_id")}
    firing_agents = {row.get("agent_id") for row in fired if row.get("agent_id")}

    report: Dict[str, Any] = {
        "event_type": EVENT_TYPE,
        "schema": SCHEMA,
        "rows_total": len(normalized),
        "rows_by_schema": dict(schemas),
        "rows_by_record_mode": dict(modes),
        "rows_by_measurement_scope": dict(scopes),
        "live_rows": len(live),
        "simulation_rows": len(simulation),
        "distinct_agents": len(agents),
        "denominator": {
            "computable": rate_computable,
            "all_evaluations": len(live),
            "scoreable": len(scoreable),
            "prose": len(prose),
            "scoreable_non_prose": non_prose,
            "unscoreable_reasons": dict(unscoreable_reasons),
        },
        "firings": {
            "would_fire": len(fired),
            "would_escalate_verdict": len(escalated),
            "candidate_false_positives": len(candidate_false_positives),
            "escalations_the_fleet_also_acted_on": len(agreed_escalations),
            "distinct_firing_agents": len(firing_agents),
            "bands": dict(bands),
            "categories": dict(categories.most_common()),
        },
        "rates": {},
        "caveats": [],
    }

    if rate_computable:
        report["rates"] = {
            "would_fire_over_scoreable": _rate(len(fired), len(scoreable)),
            "would_fire_over_prose": _rate(len(fired), len(prose)),
            "would_escalate_over_prose": _rate(len(escalated), len(prose)),
            "candidate_false_positive_over_prose": _rate(
                len(candidate_false_positives), len(prose)
            ),
        }
    else:
        if non_all_modes:
            report["caveats"].append(
                "No rate reported: rows in this window were written under "
                f"record_mode {sorted(non_all_modes)}, which omits non-firing "
                "evaluations. The denominator is absent from the data, not zero. "
                f"Set {RECORD_ALL!r} and re-soak."
            )
        if not live:
            report["caveats"].append(
                "No live rows in this window. This is one of four states and "
                "this data cannot say which: " + "; ".join(ZERO_STATES)
            )

    if simulation:
        report["caveats"].append(
            f"{len(simulation)} simulation row(s) excluded from every rate; "
            "synthetic traffic is not a false-positive corpus."
        )
    if non_prose:
        report["caveats"].append(
            f"{non_prose} scoreable row(s) carry no first-person pronoun. The "
            "detector reads English first-person narration, so those callers are "
            "UNSCORED by this channel, not cleared by it. `*_over_prose` excludes "
            "them; `would_fire_over_scoreable` does not and reads lower for it."
        )
    if fired and not report["firings"]["categories"]:
        report["caveats"].append(
            "Firings recorded with no category detail — rows predate the "
            "category capture or were truncated."
        )
    if rate_computable and not fired:
        report["caveats"].append(
            "Zero firings over a recorded denominator. This is the one zero here "
            "that IS informative: the instrument ran and wrote "
            f"{len(live)} evaluation(s). It bounds the false-positive rate over "
            "this window's traffic; it establishes nothing about recall."
        )

    return report


def render(report: Dict[str, Any]) -> str:
    lines = [
        f"verification-floor shadow soak — {report['schema']}",
        "",
        f"rows                    {report['rows_total']}"
        f"  (live {report['live_rows']}, simulation {report['simulation_rows']})",
        f"record modes seen       {report['rows_by_record_mode']}",
        f"distinct agents         {report['distinct_agents']}",
        "",
        "denominator",
        f"  evaluations           {report['denominator']['all_evaluations']}",
        f"  scoreable             {report['denominator']['scoreable']}",
        f"  prose (can fire)      {report['denominator']['prose']}",
        f"  scoreable non-prose   {report['denominator']['scoreable_non_prose']}",
        f"  unscoreable           {report['denominator']['unscoreable_reasons']}",
        "",
        "firings",
        f"  would fire            {report['firings']['would_fire']}",
        f"  would escalate        {report['firings']['would_escalate_verdict']}",
        f"  candidate FPs         {report['firings']['candidate_false_positives']}"
        "   (fleet proceeded, floor would have raised the verdict)",
        f"  fleet also acted      {report['firings']['escalations_the_fleet_also_acted_on']}",
        f"  bands                 {report['firings']['bands']}",
        f"  categories            {report['firings']['categories']}",
        "",
    ]
    if report["rates"]:
        lines.append("rates")
        for key, value in report["rates"].items():
            lines.append(f"  {key:<38} {value}")
    else:
        lines.append("rates                   NOT REPORTED (see caveats)")
    if report["caveats"]:
        lines.append("")
        lines.append("caveats")
        for caveat in report["caveats"]:
            lines.append(f"  - {caveat}")
    lines.append("")
    lines.append(
        "A candidate false positive is a candidate. Read its `matches` before "
        "calling it wrong. This report discharges one clause of the enable "
        "gate; the owners' sign-off and the safety-envelope review are not it."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    """Load payloads or raw audit lines; non-shadow lines are skipped."""
    rows: List[Dict[str, Any]] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if parsed.get("event_type") not in (None, EVENT_TYPE):
                continue
            rows.append(parsed)
    return rows


async def fetch_rows(db_url: str, window_days: int) -> List[Dict[str, Any]]:
    try:
        import asyncpg
    except ImportError:
        print(
            "error: asyncpg not installed. Install with `pip install asyncpg`, "
            "or use --input with a JSONL export.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(FETCH_QUERY, window_days)
    finally:
        await conn.close()

    rows = []
    for record in records:
        payload = record["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        row = dict(payload or {})
        row.setdefault("agent_id", record["agent_id"])
        rows.append(row)
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-url",
        default=os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DB_URL),
    )
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument(
        "--input",
        help=(
            "JSONL export of verification_floor_shadow payloads, or the raw "
            "data/audit_log.jsonl (bypasses the DB)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.input:
        rows = load_jsonl(args.input)
    else:
        rows = asyncio.run(fetch_rows(args.db_url, args.window_days))
    report = summarize(rows)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
