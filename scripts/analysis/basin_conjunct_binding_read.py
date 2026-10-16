#!/usr/bin/env python3
"""Which HIGH-basin conjunct actually binds a BOUNDARY classification, and how
close the verdict-path E ever comes to its 0.6 bound.

Read-only and descriptive. It does **not** answer the question the #1777
decision-self-loop shadow declares it cannot
(``basin_boundary_flip_counterfactual``). It reads the inputs the deployed
classifier actually saw, which can exclude a direct same-check-in data path and
measure observed margins, but cannot estimate a recursive counterfactual. The
verdict path classifies the basin from the ODE GovernanceState
(``monitor_decision.make_decision`` -> ``classify_basin(state.E, ...)``), which
``eisv_telemetry.measurement.ode.values`` records per check-in; the persisted
basin is ``eisv_telemetry.policy_evaluation.inputs.basin``.

What it prints (Markdown):
  1. Missingness in the recorded classifier inputs. Incomplete rows are excluded
     from every recomputation rather than falling through SQL CASE to BOUNDARY.
  2. Agreement between the persisted basin and the basin recomputed from the
     recorded ODE inputs -- the provenance check. If this is not ~100% on
     BOUNDARY rows the rest of the read is not about the deployed classifier.
  3. Per-basin distribution of the verdict-path E (min / p05 / p50): the margin
     to the E >= 0.6 conjunct.
  4. For BOUNDARY rows, the exact set of failing HIGH conjuncts, with agent
     count and p50 E per set. An "E" entry describes an observed binding
     conjunct; it does not attribute that row to the guide self-loop.
  5. sub_action by basin (how much guide is BOUNDARY-driven).
  6. Share of BOUNDARY rows whose agent-visible primary E sits below 0.6 while
     the deciding ODE E does not -- the penalty that is shown but never decides.

The window defaults to the current EISV instrument era (src/eisv_eras.py). A
window that crosses an instrument change is refused unless
``--allow-cross-era REASON`` is given, and the crossed changes are printed.

Usage:
  python3 scripts/analysis/basin_conjunct_binding_read.py
  python3 scripts/analysis/basin_conjunct_binding_read.py --window-days 60 --allow-cross-era "why"
  GOVERNANCE_DATABASE_URL=postgresql://... python3 scripts/analysis/basin_conjunct_binding_read.py

Historical reference read (2026-08-21): the old completeness predicate admitted
rows with missing I/S/V/coherence. Its printed BOUNDARY agreement and "adequate
power" interpretation are withdrawn. See the contract ledger, "Decision
self-loop at the basin", and the 2026-08-22 containment note.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src import eisv_eras  # noqa: E402 — standalone module, no server imports

DEFAULT_DB_URL = "postgresql://localhost:5432/governance"

# Mirror of config.governance_config.BASIN_HIGH / BASIN_LOW_* so this read has
# no import-time dependency on the server package. Drift-guarded by eye: if the
# config constants move, the agreement table in section 1 collapses.
HIGH_E_MIN, HIGH_I_MIN, HIGH_S_MAX, HIGH_V_ABS_MAX = 0.6, 0.7, 0.25, 0.15
HIGH_COH_MIN, HIGH_RISK_MAX = 0.45, 0.45
LOW_I_CEIL, LOW_COH_CEIL, LOW_V_ABS_FLOOR, LOW_RISK_FLOOR = 0.5, 0.40, 0.30, 0.70

_ROWS_CTE = """
WITH rows AS (
  SELECT s.identity_id,
    (s.state_json->'eisv_telemetry'->'measurement'->'ode'->'values'->>'E')::float AS e,
    (s.state_json->'eisv_telemetry'->'measurement'->'ode'->'values'->>'I')::float AS i,
    (s.state_json->'eisv_telemetry'->'measurement'->'ode'->'values'->>'S')::float AS st,
    (s.state_json->'eisv_telemetry'->'measurement'->'ode'->'values'->>'V')::float AS v,
    (s.state_json->'eisv_telemetry'->'measurement'->'coherence'->>'value')::float AS coh,
    (s.state_json->'eisv_telemetry'->'policy_evaluation'->'inputs'->>'risk_score')::float AS risk,
    (s.state_json->>'E')::float AS primary_e,
    s.state_json->'eisv_telemetry'->'policy_evaluation'->'inputs'->>'basin' AS basin,
    s.state_json->'eisv_telemetry'->'policy_evaluation'->>'sub_action' AS sub
  FROM core.agent_state s
  WHERE s.recorded_at >= $1::timestamptz AND s.recorded_at < $2::timestamptz
    AND s.synthetic = false
    AND s.state_json ? 'eisv_telemetry'
), eligible AS (
  SELECT * FROM rows WHERE e IS NOT NULL AND basin IS NOT NULL AND risk IS NOT NULL
), complete AS (
  SELECT * FROM eligible
  WHERE i IS NOT NULL AND st IS NOT NULL AND v IS NOT NULL AND coh IS NOT NULL
)
"""

_RECOMP = f"""
  CASE WHEN i < {LOW_I_CEIL} OR coh < {LOW_COH_CEIL} OR abs(v) > {LOW_V_ABS_FLOOR}
            OR risk >= {LOW_RISK_FLOOR} THEN 'low'
       WHEN e >= {HIGH_E_MIN} AND i >= {HIGH_I_MIN} AND st <= {HIGH_S_MAX}
            AND abs(v) <= {HIGH_V_ABS_MAX} AND coh >= {HIGH_COH_MIN}
            AND risk < {HIGH_RISK_MAX} THEN 'high'
       ELSE 'boundary' END
"""

_FAILING_SET = f"""
  concat_ws('+',
    CASE WHEN e < {HIGH_E_MIN} THEN 'E' END,
    CASE WHEN i < {HIGH_I_MIN} THEN 'I' END,
    CASE WHEN st > {HIGH_S_MAX} THEN 'S' END,
    CASE WHEN abs(v) > {HIGH_V_ABS_MAX} THEN 'V' END,
    CASE WHEN coh < {HIGH_COH_MIN} THEN 'coh' END,
    CASE WHEN risk >= {HIGH_RISK_MAX} THEN 'risk' END)
"""

Q_COMPLETENESS = _ROWS_CTE + """
SELECT basin,
  count(*) AS eligible_rows,
  count(*) FILTER (
    WHERE i IS NULL OR st IS NULL OR v IS NULL OR coh IS NULL
  ) AS incomplete_rows,
  count(*) FILTER (WHERE i IS NULL) AS missing_i,
  count(*) FILTER (WHERE st IS NULL) AS missing_s,
  count(*) FILTER (WHERE v IS NULL) AS missing_v,
  count(*) FILTER (WHERE coh IS NULL) AS missing_coherence
FROM eligible GROUP BY 1 ORDER BY 2 DESC
"""

Q_AGREEMENT = _ROWS_CTE + f"""
SELECT basin, {_RECOMP} AS recomputed, count(*) AS n
FROM complete GROUP BY 1, 2 ORDER BY 3 DESC
"""

Q_E_MARGIN = _ROWS_CTE + """
SELECT basin, count(*) AS n,
  count(DISTINCT identity_id) AS agents,
  min(e) AS min_e,
  percentile_cont(0.05) WITHIN GROUP (ORDER BY e) AS p05_e,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY e) AS p50_e
FROM complete GROUP BY 1 ORDER BY 2 DESC
"""

Q_FAILING_SETS = _ROWS_CTE + f"""
SELECT {_FAILING_SET} AS failing_set,
  count(*) AS n, count(DISTINCT identity_id) AS agents,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY e) AS p50_e
FROM complete WHERE basin = 'boundary' GROUP BY 1 ORDER BY 2 DESC
"""

Q_SUB_BY_BASIN = _ROWS_CTE + """
SELECT sub, basin, count(*) AS n FROM complete
WHERE sub IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC
"""

Q_SHOWN_NOT_DECIDING = _ROWS_CTE + f"""
SELECT count(*) AS boundary_rows,
  count(*) FILTER (WHERE primary_e < {HIGH_E_MIN}) AS primary_e_below,
  count(*) FILTER (WHERE primary_e < {HIGH_E_MIN} AND e >= {HIGH_E_MIN}) AS shown_not_deciding
FROM complete WHERE basin = 'boundary' AND primary_e IS NOT NULL
"""


def _fmt(x) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)


def _table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(_fmt(v) for v in r) + " |")
    return "\n".join(out)


Q_INSTRUMENT_IDS = """
SELECT s.state_json->'eisv_instrument'->>'id' AS instrument_id, count(*) AS n
FROM core.agent_state s
WHERE s.recorded_at >= $1::timestamptz AND s.recorded_at < $2::timestamptz
  AND s.synthetic = false
GROUP BY 1 ORDER BY 1 NULLS FIRST
"""


async def run(db_url: str, start: datetime, end: datetime, crossed=(),
              allow_cross: str | None = None) -> int:
    conn = await asyncpg.connect(db_url)
    try:
        id_rows = await conn.fetch(Q_INSTRUMENT_IDS, start, end)
        id_counts = {r["instrument_id"]: r["n"] for r in id_rows}
        try:
            stamped_ids = eisv_eras.require_single_instrument(id_counts, allow_cross=allow_cross)
        except eisv_eras.EraCrossingError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        completeness = await conn.fetch(Q_COMPLETENESS, start, end)
        agreement = await conn.fetch(Q_AGREEMENT, start, end)
        margin = await conn.fetch(Q_E_MARGIN, start, end)
        failing = await conn.fetch(Q_FAILING_SETS, start, end)
        sub = await conn.fetch(Q_SUB_BY_BASIN, start, end)
        shown = await conn.fetchrow(Q_SHOWN_NOT_DECIDING, start, end)
    finally:
        await conn.close()

    print(f"# Basin conjunct binding read — {start.isoformat()} to {end.isoformat()}\n")
    if crossed:
        print("**Pooled across EISV instrument changes** (allowed by --allow-cross-era): "
              + ", ".join(b.id for b in crossed) + ". Rows on either side of a change "
              "come from different instruments.\n")
    unstamped = id_counts.get(None, 0)
    print("Instrument ids in window: "
          + (", ".join(f"`{k}` ({id_counts[k]} rows)" for k in stamped_ids) or "none stamped")
          + f"; unstamped rows: {unstamped}.\n")
    if len(stamped_ids) > 1:
        print("**Pooled across stamped instrument ids** (allowed by --allow-cross-era).\n")
    print("Verdict-path inputs = `eisv_telemetry.measurement.ode.values` + "
          "`measurement.coherence.value` + `policy_evaluation.inputs.risk_score`; "
          "persisted basin = `policy_evaluation.inputs.basin`.\n")
    print("This is an observed-input audit, not a decision-channel-neutralized "
          "recursive replay. Rows are repeated telemetry, not independent "
          "counterfactual units.\n")

    print("## 1. Recorded-input completeness\n")
    print(_table(
        ["basin", "eligible", "incomplete", "missing I", "missing S",
         "missing V", "missing coherence"],
        [(r["basin"], r["eligible_rows"], r["incomplete_rows"],
          r["missing_i"], r["missing_s"], r["missing_v"],
          r["missing_coherence"]) for r in completeness],
    ))

    print("\n## 2. Provenance: persisted basin vs recomputed from complete inputs\n")
    print(_table(["persisted", "recomputed", "n"],
                 [(r["basin"], r["recomputed"], r["n"]) for r in agreement]))
    bnd_total = sum(r["n"] for r in agreement if r["basin"] == "boundary")
    bnd_agree = sum(r["n"] for r in agreement
                    if r["basin"] == "boundary" and r["recomputed"] == "boundary")
    print(f"\nBOUNDARY agreement: {bnd_agree}/{bnd_total}"
          + ("" if bnd_agree == bnd_total else
             "  <-- NOT exact; the inputs are not the classifier's, stop here"))

    print("\n## 3. Verdict-path E by basin (margin to the E >= 0.6 conjunct)\n")
    print(_table(["basin", "n", "agents", "min E", "p05 E", "p50 E"],
                 [(r["basin"], r["n"], r["agents"], r["min_e"], r["p05_e"], r["p50_e"])
                  for r in margin]))

    print("\n## 4. BOUNDARY rows: exact failing HIGH-conjunct sets\n")
    print(_table(["failing set", "n", "agents", "p50 E"],
                 [(r["failing_set"] or "(none)", r["n"], r["agents"], r["p50_e"])
                  for r in failing]))
    e_only = sum(r["n"] for r in failing if r["failing_set"] == "E")
    e_any = sum(r["n"] for r in failing if "E" in (r["failing_set"] or "").split("+"))
    print(f"\nE-only observed binding set: {e_only}. "
          f"E in any failing set: {e_any}. This does not attribute cause.")

    print("\n## 5. sub_action by basin\n")
    print(_table(["sub_action", "basin", "n"],
                 [(r["sub"], r["basin"], r["n"]) for r in sub]))

    print("\n## 6. Shown but never deciding on the same check-in\n")
    if shown and shown["boundary_rows"]:
        print(f"BOUNDARY rows with agent-visible primary E < 0.6: "
              f"{shown['primary_e_below']}/{shown['boundary_rows']} "
              f"({100.0 * shown['primary_e_below'] / shown['boundary_rows']:.1f}%); "
              f"of those, the deciding ODE E was >= 0.6 in {shown['shown_not_deciding']}.")
    else:
        print("no BOUNDARY rows with a primary E in window")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--window-days", type=int, default=None,
                    help="default: since the current instrument era began")
    ap.add_argument("--allow-cross-era", metavar="REASON", default=None,
                    help="run even if the window crosses an EISV instrument change")
    ap.add_argument("--db-url",
                    default=os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DB_URL))
    args = ap.parse_args(argv)
    end = datetime.now(timezone.utc)
    if args.window_days is None:
        start = current_era_start()
    else:
        start = end - timedelta(days=args.window_days)
    try:
        crossed = eisv_eras.require_single_era(start, end, allow_cross=args.allow_cross_era)
    except eisv_eras.EraCrossingError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.db_url, start, end, crossed, args.allow_cross_era))


def current_era_start() -> datetime:
    """When the current era's change had certainly taken effect."""
    current = eisv_eras.current_era_id()
    boundary = next(b for b in eisv_eras.load_boundaries() if b.id == current)
    if boundary.interval is None:
        raise SystemExit(
            f"era {current} has no recorded time; filter rows on "
            "state_json.eisv_instrument.era instead of a time window"
        )
    return boundary.interval[1]


if __name__ == "__main__":
    sys.exit(main())
