#!/usr/bin/env python3
"""Per-agent EISV local frames: each agent against its own settled reference.

Read-only and advisory. For one sensor instrument, each agent's latest EISV is
expressed as a deviation from a frozen reference window taken from the same
agent, within one stamped instrument id (era and configuration). It never
compares agents with each other, never pools instruments or instrument ids,
and sets nothing: no verdict, action, pause, risk or stored value. See
``docs/ontology/eisv-proprioception-contract.md`` §Reference frame, chart and
metric.

How a frame is built, per agent:

1. Keep rows whose ``sensor_eisv_source`` equals ``--instrument``; drop rows
   with no source tag.
2. Keep stamped rows only (``state_json.eisv_instrument``), group them by the
   stamp's ``id``, and use the agent's latest id only.
3. Drop the restore transient (``eisv_eras.is_settled``).
4. Reference: the first ``--reference`` settled rows, frozen. Current: the
   median of the last ``--recent`` rows after the reference. If there are not
   enough rows for both, the agent is reported as ``insufficient``, with no
   numbers.
5. Deviation per axis: (current - reference median) / max(1.4826 * MAD,
   ``--floor``). The raw difference is reported beside it.

Check-ins are autocorrelated, so a reference of N rows holds fewer than N
independent observations. Deviations are descriptive, not outcome evidence.

Storage: E is ``state_json.E``; I, S and V are the ``integrity``, ``entropy``
and ``volatility`` columns (``entropy`` holds S, not E).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import eisv_eras  # noqa: E402 — standalone module, no server imports

SCHEMA = "eisv.local_frame.v1"
AXES = ("E", "I", "S", "V")
INSTRUMENTS = ("behavioral", "physical")
DEFAULT_REFERENCE = 30
DEFAULT_RECENT = 5
DEFAULT_FLOOR = 0.01
MAD_TO_SD = 1.4826


def _stamp(state_json: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    s = state_json.get("eisv_instrument")
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except ValueError:
            return None
    return s if isinstance(s, Mapping) and s.get("id") else None


def _median_mad(xs: list[float]) -> tuple[float, float]:
    m = statistics.median(xs)
    return m, statistics.median(abs(x - m) for x in xs)


def frame_for_agent(
    rows: list[Mapping[str, Any]],
    *,
    instrument: str,
    reference: int = DEFAULT_REFERENCE,
    recent: int = DEFAULT_RECENT,
    floor: float = DEFAULT_FLOOR,
    min_settled_updates: int = eisv_eras.DEFAULT_SETTLED_UPDATES,
) -> dict[str, Any]:
    """One agent's frame. ``rows`` are that agent's rows, oldest first.

    Each row: ``{"recorded_at", "state_json", "E", "I", "S", "V"}``.
    """
    usable, dropped = [], defaultdict(int)
    for r in rows:
        sj = r.get("state_json") or {}
        if sj.get("sensor_eisv_source") is None:
            dropped["no_source_tag"] += 1
            continue
        if sj.get("sensor_eisv_source") != instrument:
            dropped["other_instrument"] += 1
            continue
        stamp = _stamp(sj)
        if stamp is None:
            dropped["unstamped"] += 1
            continue
        if any(r.get(a) is None for a in AXES):
            dropped["missing_axis"] += 1
            continue
        usable.append((stamp, r))

    out: dict[str, Any] = {"schema": SCHEMA, "instrument": instrument, "advisory": True,
                           "dropped": dict(dropped)}
    if not usable:
        return {**out, "status": "insufficient", "n_settled": 0}

    latest_id = usable[-1][0]["id"]
    same_id = [(s, r) for s, r in usable if s["id"] == latest_id]
    settled = [r for s, r in same_id if eisv_eras.is_settled(s, min_settled_updates)]
    out.update(instrument_id=latest_id, era=same_id[-1][0].get("era"),
               n_settled=len(settled), earlier_ids_ignored=len(usable) - len(same_id))
    if len(settled) < reference + recent:
        return {**out, "status": "insufficient", "needed": reference + recent}

    ref, cur = settled[:reference], settled[-recent:]
    axes = {}
    for a in AXES:
        ref_vals = [float(r[a]) for r in ref]
        m, mad = _median_mad(ref_vals)
        now = statistics.median(float(r[a]) for r in cur)
        scale = max(MAD_TO_SD * mad, floor)
        axes[a] = {"reference_median": round(m, 4), "current_median": round(now, 4),
                   "raw_difference": round(now - m, 4), "deviation": round((now - m) / scale, 2),
                   "scale_floored": MAD_TO_SD * mad < floor}
    return {**out, "status": "ok", "reference_rows": reference, "recent_rows": recent,
            "reference_window": [str(ref[0]["recorded_at"]), str(ref[-1]["recorded_at"])],
            "axes": axes}


def frames(rows: Iterable[Mapping[str, Any]], *, instrument: str, **kw: Any) -> dict[str, dict[str, Any]]:
    """Frames for every agent in ``rows`` (any order). Keyed by identity id."""
    if instrument not in INSTRUMENTS:
        raise ValueError(f"instrument must be one of {INSTRUMENTS}")
    by_agent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by_agent[str(r["identity_id"])].append(r)
    return {aid: frame_for_agent(sorted(rs, key=lambda r: r["recorded_at"]), instrument=instrument, **kw)
            for aid, rs in by_agent.items()}


ROWS_SQL = """
    SELECT identity_id, recorded_at, state_json,
           (state_json->>'E')::float AS e, integrity, entropy, volatility
    FROM core.agent_state
    WHERE recorded_at >= %s AND recorded_at < %s
      AND synthetic = false
      AND state_json ? 'eisv_instrument'
      AND state_json->>'sensor_eisv_source' = %s
    ORDER BY identity_id, recorded_at
"""


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    ap.add_argument("--instrument", required=True, choices=INSTRUMENTS)
    ap.add_argument("--days", type=int, default=14, help="window, clipped to the current era")
    ap.add_argument("--reference", type=int, default=DEFAULT_REFERENCE)
    ap.add_argument("--recent", type=int, default=DEFAULT_RECENT)
    ap.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)

    import psycopg2  # local import: the pure functions above need no driver

    from scripts.analysis import eisv_era_sql

    end = datetime.now(timezone.utc)
    era_start, stamp_era = eisv_eras.current_era_window_start()
    era_id = eisv_eras.current_era_id()
    start = max(end - timedelta(days=args.days), era_start)
    if eisv_eras.check_window(start, end) is None:
        return 2
    conn = psycopg2.connect(args.db_url)
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    with conn.cursor() as cur:
        cur.execute(eisv_era_sql.instrument_id_counts_sql("%s", "%s"), (start, end))
        id_counts = {iid: n for iid, n in cur.fetchall()}
        # The frame keeps each agent's latest instrument id only, so a window
        # holding several ids is reported, not refused.
        provenance = eisv_eras.check_window(
            start, end, id_counts=id_counts, allow_cross="frames use each agent's latest instrument id only")
        cur.execute(ROWS_SQL, (start, end, args.instrument))
        rows = [{"identity_id": iid, "recorded_at": ts, "state_json": sj if isinstance(sj, dict) else json.loads(sj),
                 "E": e, "I": i, "S": s, "V": v} for iid, ts, sj, e, i, s, v in cur.fetchall()]
    if stamp_era is not None:  # untimed current boundary: keep rows stamped with it
        rows = [r for r in rows if (_stamp(r["state_json"]) or {}).get("era") == stamp_era]
    conn.close()

    result = frames(rows, instrument=args.instrument, reference=args.reference,
                    recent=args.recent, floor=args.floor)
    if args.json:
        print(json.dumps({"schema": SCHEMA, "era": era_id, "window": [start.isoformat(), end.isoformat()],
                          "provenance": provenance, "agents": result}, indent=1, default=str))
        return 0
    for line in provenance or []:
        print(line)
    print(f"Local frames, instrument={args.instrument}, era={era_id}. Advisory; each agent against its own "
          f"frozen reference ({args.reference} settled rows); not comparable across agents.")
    ok = {k: v for k, v in result.items() if v["status"] == "ok"}
    print(f"agents: {len(result)}  with a frame: {len(ok)}  insufficient: {len(result) - len(ok)}")
    for aid, f in sorted(ok.items()):
        devs = "  ".join(f"{a} {f['axes'][a]['deviation']:+.2f} ({f['axes'][a]['raw_difference']:+.3f})" for a in AXES)
        print(f"  {aid[:8]}  n={f['n_settled']:<5} {devs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
