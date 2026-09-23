"""Soak read for the verification-floor shadow — a rate, with its denominator.

The verification floor (`docs/proposals/active/verification-weighted-verdict-v0.md`)
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
* **Divide one population by another.** Every rate below draws its numerator and
  denominator from the same set of rows. An earlier draft took firings from all
  live rows and divided by first-person rows only, on the false premise that the
  detector needs a first-person pronoun to fire; on traffic whose firings were
  mostly pronoun-free that reported a false-positive rate above 1.0 — on the
  exact number the enable gate consumes. Input shape is now a **stratum**,
  reported on both sides of each rate, never a filter applied to one side.

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


def _stratum(rows: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """Counts and rates for one stratum, numerator and denominator from IT.

    This function is the whole guard against the defect described in the module
    docstring: a rate can only be built from rows that are in ``rows``, so it is
    bounded in [0, 1] by construction and no caller can pair a wider numerator
    with a narrower denominator.
    """
    fired = [row for row in rows if row.get("fired")]
    escalated = [row for row in fired if row.get("escalated_verdict")]
    proceeded = [
        row
        for row in escalated
        if str(row.get("live_action") or "").strip().lower()
        in {"proceed", "approve", "continue"}
    ]
    return {
        "stratum": label,
        "rows": len(rows),
        "fired": len(fired),
        "escalated_verdict": len(escalated),
        "escalated_while_fleet_proceeded": len(proceeded),
        "rates": {
            "fired": _rate(len(fired), len(rows)),
            "escalated_verdict": _rate(len(escalated), len(rows)),
            "escalated_while_fleet_proceeded": _rate(len(proceeded), len(rows)),
        },
    }


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate rows into the soak report. Pure.

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

    # Shadow rows answer "what WOULD the floor have done". Applied rows are the
    # enabled floor actually escalating. Pooling them would let enforcement
    # inflate a false-positive rate about a decision not yet taken.
    shadow = [row for row in live if not row.get("applied")]
    applied = [row for row in live if row.get("applied")]

    # A rate needs rows written whether or not they fired. Any window containing
    # a narrowed mode has a denominator this data cannot supply.
    non_all_modes = {mode for mode in modes if mode != RECORD_ALL}
    rate_computable = bool(shadow) and not non_all_modes

    scoreable = [row for row in shadow if row.get("scoreable")]
    unscoreable_reasons: Counter = Counter(
        row.get("unscoreable_reason") or "unknown"
        for row in shadow
        if not row.get("scoreable")
    )

    # Input shape is a STRATUM, not a filter. Each sub-rate is computed inside
    # its own stratum, so none can exceed 1.0 and none borrows a numerator from
    # the other. See the module docstring for the defect this replaced.
    first_person = [row for row in scoreable if row.get("first_person")]
    pronoun_free = [row for row in scoreable if not row.get("first_person")]

    overall = _stratum(scoreable, "scoreable")
    categories: Counter = Counter()
    bands: Counter = Counter()
    for row in shadow:
        if not row.get("fired"):
            continue
        bands[row.get("verdict") or "unknown"] += 1
        for key in (row.get("categories") or {}):
            categories[key] += 1

    agents = {row.get("agent_id") for row in live if row.get("agent_id")}
    firing_agents = {
        row.get("agent_id") for row in shadow if row.get("fired") and row.get("agent_id")
    }

    report: Dict[str, Any] = {
        "event_type": EVENT_TYPE,
        "schema": SCHEMA,
        "rows_total": len(normalized),
        "rows_by_schema": dict(schemas),
        "rows_by_record_mode": dict(modes),
        "rows_by_measurement_scope": dict(scopes),
        "live_rows": len(live),
        "simulation_rows": len(simulation),
        "applied_rows": len(applied),
        "shadow_rows": len(shadow),
        "distinct_agents": len(agents),
        "denominator": {
            "computable": rate_computable,
            "shadow_evaluations": len(shadow),
            "scoreable": len(scoreable),
            "unscoreable_reasons": dict(unscoreable_reasons),
        },
        "firings": {
            "fired": overall["fired"],
            "escalated_verdict": overall["escalated_verdict"],
            "candidate_false_positives": overall["escalated_while_fleet_proceeded"],
            "distinct_firing_agents": len(firing_agents),
            "bands": dict(bands),
            "categories": dict(categories.most_common()),
        },
        "rates": {},
        "strata": {},
        "caveats": [],
    }

    if rate_computable:
        report["rates"] = dict(overall["rates"])
        report["strata"] = {
            "first_person": _stratum(first_person, "first_person"),
            "pronoun_free": _stratum(pronoun_free, "pronoun_free"),
        }
    else:
        if non_all_modes:
            report["caveats"].append(
                "No rate reported: rows in this window were written under "
                f"record_mode {sorted(non_all_modes)}, which omits non-firing "
                "evaluations. The denominator is absent from the data, not zero. "
                f"Set {RECORD_ALL!r} and re-soak."
            )
        if not shadow:
            report["caveats"].append(
                "No live shadow rows in this window. This is one of four states "
                "and this data cannot say which: " + "; ".join(ZERO_STATES)
            )

    if simulation:
        report["caveats"].append(
            f"{len(simulation)} simulation row(s) excluded from every rate; "
            "synthetic traffic is not a false-positive corpus."
        )
    if applied:
        report["caveats"].append(
            f"{len(applied)} row(s) carry applied=true — the floor was ENABLED "
            "for that traffic and those escalations were enforced, not "
            "hypothetical. They are excluded from every rate here, which is "
            "about a decision already taken on that traffic, not a pending one."
        )
    if rate_computable and report["strata"]["pronoun_free"]["fired"]:
        report["caveats"].append(
            f"{report['strata']['pronoun_free']['fired']} firing(s) in rows with "
            "no first-person pronoun. The detector does not require one, so this "
            "is expected and is reported as its own stratum rather than dropped. "
            "Do not exclude these rows from a headline rate: they are real "
            "firings on real traffic."
        )
    if rate_computable and not overall["fired"]:
        report["caveats"].append(
            "Zero firings over a recorded denominator. This is the one zero here "
            "that IS informative: the instrument ran and wrote "
            f"{len(shadow)} shadow evaluation(s). It bounds the false-positive "
            "rate over this window's traffic; it establishes nothing about recall."
        )

    return report


def render(report: Dict[str, Any]) -> str:
    lines = [
        f"verification-floor shadow soak — {report['schema']}",
        "",
        f"rows                    {report['rows_total']}"
        f"  (live {report['live_rows']}: shadow {report['shadow_rows']}, "
        f"applied {report['applied_rows']}; simulation {report['simulation_rows']})",
        f"record modes seen       {report['rows_by_record_mode']}",
        f"distinct agents         {report['distinct_agents']}",
        "",
        "denominator (shadow rows only)",
        f"  evaluations           {report['denominator']['shadow_evaluations']}",
        f"  scoreable             {report['denominator']['scoreable']}",
        f"  unscoreable           {report['denominator']['unscoreable_reasons']}",
        "",
        "firings",
        f"  fired                 {report['firings']['fired']}",
        f"  escalated verdict     {report['firings']['escalated_verdict']}",
        f"  candidate FPs         {report['firings']['candidate_false_positives']}"
        "   (fleet proceeded, floor would have raised the verdict)",
        f"  bands                 {report['firings']['bands']}",
        f"  categories            {report['firings']['categories']}",
        "",
    ]
    if report["rates"]:
        lines.append("rates over scoreable shadow rows")
        for key, value in report["rates"].items():
            lines.append(f"  {key:<38} {value}")
        lines.append("")
        lines.append("by input stratum (each rate inside its own stratum)")
        for name, stratum in report["strata"].items():
            lines.append(
                f"  {name:<16} rows={stratum['rows']:<6} fired={stratum['fired']:<5} "
                f"fired_rate={stratum['rates']['fired']}"
            )
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
