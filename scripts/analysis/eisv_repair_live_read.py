#!/usr/bin/env python3
"""Live read of the 2026-10-15 basin and I-rest repairs.

Implements the rule declared in
``docs/evaluations/eisv-i-rest-live-read/rule-v0.md`` before the read. It
compares behavioral ``safe`` → ``approve`` in the post-repair era (RK1) with
the pre-repair baseline era (ID), each read alone, against contract row 50's
equilibrium-shift estimate. It is a telemetry read: it joins no outcome labels
and changes nothing.

The rule fixes every threshold below. Change them only by amending the rule,
before a read, and saying so there.

Refuses (exit 2) before 2026-10-23T00:00Z, when ``UNITARES_RESIDENTS`` is
unset (residents would fold into generic classes and pollute their strata),
or when a window crosses an instrument change.

Usage:
  UNITARES_RESIDENTS=... python3 scripts/analysis/eisv_repair_live_read.py
  UNITARES_RESIDENTS=... python3 scripts/analysis/eisv_repair_live_read.py --json
  ... --endpoints per_stratum_endpoints.json   # else the read is range-only
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import eisv_eras  # noqa: E402 — standalone module, no server imports
from src.grounding import class_indicator  # noqa: E402
from scripts.analysis import eisv_era_sql  # noqa: E402

RULE_PATH = "docs/evaluations/eisv-i-rest-live-read/rule-v0.md"
SCHEMA = "eisv.repair_live_read.v1"
DEFAULT_DB_URL = "postgresql://postgres:postgres@localhost:5432/governance"

NOT_BEFORE = datetime(2026, 10, 23, tzinfo=timezone.utc)
POST_ERA = "RK1"
POST_INSTRUMENT_ID = "RK1:a2579740d3bea62a"
BASELINE_ERA = "ID"
BASELINE_END_ERA = "A1"  # the baseline ends at A1's ambiguous_from

# Contract row 50, equilibrium-shift estimate (n = 9,088 rows, 209 identities).
PREDICTED_RANGE = (0.441, 0.845)
DEPLOYED_ENDPOINTS = (0.267, 0.289)

MIN_IDENTITIES = 5
MIN_SAFE_ROWS = 100
MIN_ROWS_PER_IDENTITY = 10
MAX_IDENTITY_SHARE = 0.5
SETTLE_ROWS = eisv_eras.DEFAULT_SETTLED_UPDATES

BEHAVIORAL = "behavioral"
DRIVER_BEHAVIORAL = "behavioral_assessment"
DRIVER_OTHER = "other"


class ReadRefused(Exception):
    """The read cannot run under the rule; the message says why."""


@dataclass(frozen=True)
class Row:
    identity_id: str
    recorded_at: datetime
    sensor: Optional[str]
    verdict: Optional[str]
    action: Optional[str]
    verdict_source: Optional[str]
    stamp: Optional[Mapping[str, Any]]
    e: Optional[float]
    label: Optional[str]
    tags: tuple[str, ...]
    first_seen: datetime


@dataclass
class Window:
    era: str
    start: datetime
    end: datetime
    stamped: bool


@dataclass
class StratumStats:
    safe_rows: int = 0
    approve_rows: int = 0
    identities: int = 0
    counted_identities: int = 0
    p50_rows_per_identity: Optional[float] = None
    max_identity_share: Optional[float] = None
    pooled_share: Optional[float] = None
    identity_p50_share: Optional[float] = None
    e_trend_p50: Optional[float] = None


@dataclass
class EraResult:
    window: Window
    strata: dict[tuple[str, str], StratumStats] = field(default_factory=dict)
    excluded_unsettled: int = 0
    other_sensor_rows: dict[str, int] = field(default_factory=dict)
    stamped_rows: int = 0


# ── Windows ──────────────────────────────────────────────────────────


def _boundary(era_id: str, path: Path) -> eisv_eras.Boundary:
    for boundary in eisv_eras.load_boundaries(path):
        if boundary.id == era_id:
            return boundary
    raise ReadRefused(f"era {era_id!r} is not in the instrument-era registry")


def _interval(boundary: eisv_eras.Boundary) -> tuple[datetime, datetime]:
    interval = boundary.interval
    if interval is None:
        raise ReadRefused(f"era {boundary.id!r} has no recorded effective_at")
    return interval


def resolve_windows(as_of: datetime, path: Path = eisv_eras.REGISTRY_PATH) -> tuple[Window, Window]:
    """The post-repair and baseline windows the rule names.

    Each era starts where its change had certainly taken effect (the end of its
    ambiguous interval). The post-repair window ends at the read time or at
    the earliest instant of any later splitting change, whichever is first.
    """
    post_start = _interval(_boundary(POST_ERA, path))[1]
    later = [
        b.interval[0]
        for b in eisv_eras.load_boundaries(path)
        if b.splits and b.interval is not None and b.interval[0] > post_start
    ]
    post_end = min([as_of, *later])
    baseline_start = _interval(_boundary(BASELINE_ERA, path))[1]
    baseline_end = _interval(_boundary(BASELINE_END_ERA, path))[0]
    return (
        Window(POST_ERA, post_start, post_end, stamped=True),
        Window(BASELINE_ERA, baseline_start, baseline_end, stamped=False),
    )


# ── Settling ─────────────────────────────────────────────────────────


def settled_rows(rows: Sequence[Row], window: Window) -> tuple[list[Row], int, int]:
    """Rows past the restore transient, the excluded count, and stamped rows seen.

    Stamped eras use ``is_settled``. The baseline predates stamping, so it
    uses the stamp-free equivalent: an identity first seen inside the era is
    settled from its first row, one carried from before after SETTLE_ROWS rows.
    """
    stamped = sum(1 for r in rows if isinstance(r.stamp, Mapping))
    if window.stamped:
        kept = [r for r in rows if eisv_eras.is_settled(r.stamp, SETTLE_ROWS)]
        return kept, len(rows) - len(kept), stamped
    by_identity: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_identity[row.identity_id].append(row)
    kept: list[Row] = []
    for identity_rows in by_identity.values():
        identity_rows.sort(key=lambda r: r.recorded_at)
        carried = identity_rows[0].first_seen < window.start
        kept.extend(identity_rows[SETTLE_ROWS:] if carried else identity_rows)
    return kept, len(rows) - len(kept), stamped


# ── Strata and statistics ────────────────────────────────────────────


def stratum_key(row: Row, roster: frozenset[str]) -> tuple[str, str]:
    driver = DRIVER_BEHAVIORAL if row.verdict_source == DRIVER_BEHAVIORAL else DRIVER_OTHER
    cls = class_indicator.classify_by_label_and_tags(row.label, row.tags, known=roster)
    return driver, cls


def _e_trend(rows: Sequence[Row]) -> Optional[float]:
    """Median E over an identity's last quarter of rows minus its first quarter."""
    values = [r.e for r in sorted(rows, key=lambda r: r.recorded_at) if r.e is not None]
    quarter = len(values) // 4
    if quarter == 0:
        return None
    return statistics.median(values[-quarter:]) - statistics.median(values[:quarter])


def stratum_stats(rows: Sequence[Row]) -> StratumStats:
    stats = StratumStats(safe_rows=len(rows))
    if not rows:
        return stats
    by_identity: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_identity[row.identity_id].append(row)
    stats.approve_rows = sum(1 for r in rows if r.action == "approve")
    stats.pooled_share = stats.approve_rows / stats.safe_rows
    stats.identities = len(by_identity)
    sizes = [len(v) for v in by_identity.values()]
    stats.p50_rows_per_identity = statistics.median(sizes)
    stats.max_identity_share = max(sizes) / stats.safe_rows
    counted = {k: v for k, v in by_identity.items() if len(v) >= MIN_ROWS_PER_IDENTITY}
    stats.counted_identities = len(counted)
    if counted:
        stats.identity_p50_share = statistics.median(
            sum(1 for r in v if r.action == "approve") / len(v) for v in counted.values()
        )
        trends = [t for t in (_e_trend(v) for v in counted.values()) if t is not None]
        stats.e_trend_p50 = statistics.median(trends) if trends else None
    return stats


def read_era(rows: Sequence[Row], window: Window, roster: frozenset[str]) -> EraResult:
    result = EraResult(window=window)
    # Any stamped row counts, whatever its sensor: in the unstamped baseline
    # era one is an integrity failure for the whole read.
    stamped_any = sum(1 for r in rows if isinstance(r.stamp, Mapping))
    behavioral: list[Row] = []
    for row in rows:
        if row.sensor == BEHAVIORAL:
            behavioral.append(row)
        else:
            key = row.sensor or "unrecorded"
            result.other_sensor_rows[key] = result.other_sensor_rows.get(key, 0) + 1
    kept, result.excluded_unsettled, _ = settled_rows(behavioral, window)
    result.stamped_rows = stamped_any
    grouped: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in kept:
        if row.verdict == "safe":
            grouped[stratum_key(row, roster)].append(row)
    result.strata = {key: stratum_stats(v) for key, v in grouped.items()}
    return result


# ── Outcomes ─────────────────────────────────────────────────────────


def floor_failures(stats: StratumStats) -> list[str]:
    failures = []
    if stats.counted_identities < MIN_IDENTITIES:
        failures.append(
            f"{stats.counted_identities} identities with >= {MIN_ROWS_PER_IDENTITY} safe rows "
            f"(< {MIN_IDENTITIES})"
        )
    if stats.safe_rows < MIN_SAFE_ROWS:
        failures.append(f"{stats.safe_rows} safe rows (< {MIN_SAFE_ROWS})")
    if stats.max_identity_share is not None and stats.max_identity_share > MAX_IDENTITY_SHARE:
        failures.append(
            f"one identity holds {stats.max_identity_share:.1%} of rows (> {MAX_IDENTITY_SHARE:.0%})"
        )
    return failures


def stratum_outcome(
    post: Optional[StratumStats],
    baseline: Optional[StratumStats],
    *,
    endpoints: Optional[tuple[float, float]] = None,
    global_reasons: Sequence[str] = (),
) -> tuple[str, list[str]]:
    """Return ``(outcome, reasons)`` for one stratum under the rule."""
    reasons = list(global_reasons)
    if not post or not post.safe_rows:
        reasons.append("stratum empty in the post-repair era")
    if not baseline or not baseline.safe_rows:
        reasons.append("stratum empty in the baseline era")
    # The floors bind in both eras: the baseline decides the composition
    # qualifier and the reported shift.
    if post and post.safe_rows:
        reasons.extend(f"post-repair era: {why}" for why in floor_failures(post))
    if baseline and baseline.safe_rows:
        reasons.extend(f"baseline era: {why}" for why in floor_failures(baseline))
    if reasons:
        return "INCONCLUSIVE", reasons
    lo, hi = endpoints or PREDICTED_RANGE
    share = post.pooled_share
    if share < lo:
        return "BELOW", []
    if share <= hi:
        return "CONSISTENT", []
    if baseline.pooled_share > max(DEPLOYED_ENDPOINTS):
        return "ABOVE, composition", []
    if post.e_trend_p50 is not None and post.e_trend_p50 > 0:
        return "ABOVE, loop-consistent", []
    return "ABOVE, unattributed", []


# ── Report ───────────────────────────────────────────────────────────


def build_report(
    post: EraResult,
    baseline: EraResult,
    *,
    endpoints: Optional[Mapping[str, Sequence[float]]] = None,
    era_lines: Sequence[str] = (),
    instrument_ids: Sequence[str] = (),
) -> dict[str, Any]:
    global_reasons = []
    if baseline.stamped_rows:
        global_reasons.append(
            f"{baseline.stamped_rows} stamped rows in the baseline era, which predates stamping"
        )
    if [i for i in instrument_ids if i != POST_INSTRUMENT_ID]:
        global_reasons.append(f"post-era instrument ids {list(instrument_ids)} != {POST_INSTRUMENT_ID}")
    strata = []
    for key in sorted(set(post.strata) | set(baseline.strata)):
        name = f"{key[0]}/{key[1]}"
        pair = (endpoints or {}).get(name)
        outcome, reasons = stratum_outcome(
            post.strata.get(key),
            baseline.strata.get(key),
            endpoints=tuple(pair) if pair else None,
            global_reasons=global_reasons,
        )
        p, b = post.strata.get(key), baseline.strata.get(key)
        strata.append(
            {
                "stratum": name,
                "outcome": outcome,
                "reasons": reasons,
                "endpoints": list(pair) if pair else list(PREDICTED_RANGE),
                "endpoint_source": "per_stratum" if pair else "range_only",
                "post": vars(p) if p else None,
                "baseline": vars(b) if b else None,
                "shift": (
                    p.pooled_share - b.pooled_share
                    if p and b and p.pooled_share is not None and b.pooled_share is not None
                    else None
                ),
            }
        )
    return {
        "schema": SCHEMA,
        "rule": RULE_PATH,
        # True when any stratum fell back to the pooled range.
        "range_only": any(s["endpoint_source"] == "range_only" for s in strata),
        "eras": {
            era.window.era: {
                "start": era.window.start.isoformat(),
                "end": era.window.end.isoformat(),
                "excluded_unsettled": era.excluded_unsettled,
                "other_sensor_rows": era.other_sensor_rows,
            }
            for era in (post, baseline)
        },
        "era_lines": list(era_lines),
        "strata": strata,
    }


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1%}"


def render(report: Mapping[str, Any]) -> str:
    lines = [
        "# EISV repair live read",
        "",
        f"Rule: `{report['rule']}`. Endpoints: per stratum where supplied, otherwise contract "
        "row 50's pooled range (range-only); the Endpoints column says which.",
        "",
    ]
    for era, info in report["eras"].items():
        lines.append(
            f"- {era}: {info['start']} .. {info['end']}; unsettled excluded {info['excluded_unsettled']}; "
            f"non-behavioral rows {info['other_sensor_rows']} (reported, never compared)."
        )
    lines.extend(f"- {line}" for line in report["era_lines"])
    lines += [
        "",
        "| Stratum | Outcome | Endpoints | Shift |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{s['stratum']}` | {s['outcome']} | "
        f"{s['endpoint_source']} [{_pct(s['endpoints'][0])}, {_pct(s['endpoints'][1])}] | {_pct(s['shift'])} |"
        for s in report["strata"]
    ]
    lines += [
        "",
        "Per era (P = post-repair, B = baseline). Pooled share decides; the identity p50 is reported beside it.",
        "",
        "| Stratum | Era | Pooled share | Safe rows | Identities | Counted ids | p50 rows/id | Max id share | Id p50 share | E trend p50 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in report["strata"]:
        for era, stats in (("P", s["post"]), ("B", s["baseline"])):
            st = stats or {}
            p50_rows = st.get("p50_rows_per_identity")
            trend = st.get("e_trend_p50")
            lines.append(
                f"| `{s['stratum']}` | {era} | {_pct(st.get('pooled_share'))} | {st.get('safe_rows', 0)} | "
                f"{st.get('identities', 0)} | {st.get('counted_identities', 0)} | "
                f"{'-' if p50_rows is None else f'{p50_rows:g}'} | {_pct(st.get('max_identity_share'))} | "
                f"{_pct(st.get('identity_p50_share'))} | {'-' if trend is None else f'{trend:+.4f}'} |"
            )
    reasons = [(s["stratum"], s["reasons"]) for s in report["strata"] if s["reasons"]]
    if reasons:
        lines += ["", "Inconclusive because:"]
        lines += [f"- `{name}`: " + "; ".join(why) for name, why in reasons]
    fallback = [s["stratum"] for s in report["strata"] if s["endpoint_source"] == "range_only"]
    if fallback:
        lines += [
            "",
            "Range-only for " + ", ".join(f"`{name}`" for name in fallback)
            + ": BELOW or ABOVE there means outside the fleet-mix estimate, not off that stratum's estimate.",
        ]
    return "\n".join(lines) + "\n"


# ── Database ─────────────────────────────────────────────────────────

FETCH_SQL = """
WITH window_rows AS (
    SELECT s.identity_id, s.recorded_at, s.state_json
    FROM core.agent_state s
    WHERE s.recorded_at >= %(start)s AND s.recorded_at < %(end)s
      AND s.synthetic IS NOT TRUE
),
first_seen AS (
    SELECT f.identity_id, min(f.recorded_at) AS first_seen
    FROM core.agent_state f
    WHERE f.identity_id IN (SELECT DISTINCT identity_id FROM window_rows)
    GROUP BY f.identity_id
)
SELECT
    w.identity_id::text,
    w.recorded_at,
    w.state_json->>'sensor_eisv_source',
    w.state_json->>'verdict',
    w.state_json->>'action',
    w.state_json #>> '{eisv_telemetry,policy_evaluation,inputs,verdict_source}',
    w.state_json->'eisv_instrument',
    w.state_json->>'E',
    i.metadata->>'label',
    COALESCE(i.metadata->'tags', '[]'::jsonb),
    fs.first_seen
FROM window_rows w
JOIN core.identities i USING (identity_id)
JOIN first_seen fs USING (identity_id)
"""


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def row_from_db(record: Sequence[Any]) -> Row:
    stamp = _json(record[6])
    tags = _json(record[9])
    return Row(
        identity_id=record[0],
        recorded_at=record[1],
        sensor=record[2],
        verdict=record[3],
        action=record[4],
        verdict_source=record[5],
        stamp=stamp if isinstance(stamp, Mapping) else None,
        e=_float(record[7]),
        label=record[8],
        tags=tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
        first_seen=record[10],
    )


def fetch(conn: Any, window: Window) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(FETCH_SQL, {"start": window.start, "end": window.end})
        return [row_from_db(r) for r in cur.fetchall()]


# ── Entry point ──────────────────────────────────────────────────────


def _parse_endpoints(path: Optional[str]) -> Optional[dict[str, list[float]]]:
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(k): [float(v[0]), float(v[1])] for k, v in data.items()}


def main(argv: Optional[Sequence[str]] = None, *, now: Optional[datetime] = None, connect: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--endpoints", help="JSON mapping stratum -> [low, high]")
    args = parser.parse_args(argv)

    as_of = now or datetime.now(timezone.utc)
    if as_of < NOT_BEFORE:
        print(f"refused: the rule's read is due on or after {NOT_BEFORE.isoformat()}", file=sys.stderr)
        return 2
    roster = class_indicator.load_resident_labels()
    if not roster:
        print(
            f"refused: {class_indicator.RESIDENT_ROSTER_ENV} is unset; residents would fold "
            "into generic classes and pollute their strata",
            file=sys.stderr,
        )
        return 2
    try:
        post_window, baseline_window = resolve_windows(as_of)
    except ReadRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for window in (post_window, baseline_window):
        if eisv_eras.check_window(window.start, window.end) is None:
            return 2

    if connect is None:
        import psycopg2

        connect = psycopg2.connect
    conn = connect(args.db_url)
    # One read-only REPEATABLE READ snapshot for the id check and both reads.
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                eisv_era_sql.instrument_id_counts_sql("%s", "%s"),
                (post_window.start, post_window.end),
            )
            id_counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        era_lines = eisv_eras.check_window(post_window.start, post_window.end, id_counts=id_counts)
        if era_lines is None:
            return 2
        post = read_era(fetch(conn, post_window), post_window, roster)
        baseline = read_era(fetch(conn, baseline_window), baseline_window, roster)
    finally:
        conn.close()

    report = build_report(
        post,
        baseline,
        endpoints=_parse_endpoints(args.endpoints),
        era_lines=era_lines,
        instrument_ids=sorted(k for k, n in id_counts.items() if k and n),
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
