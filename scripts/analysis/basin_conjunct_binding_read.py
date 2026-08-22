#!/usr/bin/env python3
"""Which HIGH-basin conjunct actually binds a BOUNDARY classification, and how
close the verdict-path E ever comes to its 0.6 bound.

Read-only. Answers the question the #1777 decision-self-loop shadow declares it
cannot (``basin_boundary_flip_counterfactual``): not by simulating a
counterfactual, but by reading the inputs the deployed classifier actually saw.
The verdict path classifies the basin from the ODE GovernanceState
(``monitor_decision.make_decision`` -> ``classify_basin(state.E, ...)``), which
``eisv_telemetry.measurement.ode.values`` records per check-in; the persisted
basin is ``eisv_telemetry.policy_evaluation.inputs.basin``.

What it prints (Markdown):
  1. Agreement between the persisted basin and the basin recomputed from the
     recorded ODE inputs -- the provenance check. If this is not ~100% on
     BOUNDARY rows the rest of the read is not about the deployed classifier.
  2. Per-basin distribution of the verdict-path E (min / p05 / p50): the margin
     to the E >= 0.6 conjunct.
  3. For BOUNDARY rows, the exact set of failing HIGH conjuncts, with agent
     count and p50 E per set. An "E"-only set is the guide self-loop binding.
  4. sub_action by basin (how much guide is BOUNDARY-driven).
  5. Share of BOUNDARY rows whose agent-visible primary E sits below 0.6 while
     the deciding ODE E does not -- the penalty that is shown but never decides.

Usage:
  python3 scripts/analysis/basin_conjunct_binding_read.py --window-days 60
  GOVERNANCE_DATABASE_URL=postgresql://... python3 scripts/analysis/basin_conjunct_binding_read.py

Reference read (2026-08-21; `eisv_telemetry` has persisted since 2026-08-10, so
any --window-days above 12 reads the same 12 days): BOUNDARY agreement
12,628/12,628; ODE E in BOUNDARY min 0.618 / p05 0.630 / p50 0.657; failing
sets I+S 9,510, S 3,104, risk 10, I 3, S+risk 1; E-only 0. Contract ledger: "Decision self-loop at the basin".
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

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
  WHERE s.recorded_at >= now() - ($1::int * INTERVAL '1 day')
    AND s.synthetic = false
    AND s.state_json ? 'eisv_telemetry'
), valid AS (
  SELECT * FROM rows WHERE e IS NOT NULL AND basin IS NOT NULL AND risk IS NOT NULL
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

Q_AGREEMENT = _ROWS_CTE + f"""
SELECT basin, {_RECOMP} AS recomputed, count(*) AS n
FROM valid GROUP BY 1, 2 ORDER BY 3 DESC
"""

Q_E_MARGIN = _ROWS_CTE + """
SELECT basin, count(*) AS n,
  min(e) AS min_e,
  percentile_cont(0.05) WITHIN GROUP (ORDER BY e) AS p05_e,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY e) AS p50_e
FROM valid GROUP BY 1 ORDER BY 2 DESC
"""

Q_FAILING_SETS = _ROWS_CTE + f"""
SELECT {_FAILING_SET} AS failing_set,
  count(*) AS n, count(DISTINCT identity_id) AS agents,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY e) AS p50_e
FROM valid WHERE basin = 'boundary' GROUP BY 1 ORDER BY 2 DESC
"""

Q_SUB_BY_BASIN = _ROWS_CTE + """
SELECT sub, basin, count(*) AS n FROM valid
WHERE sub IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC
"""

Q_SHOWN_NOT_DECIDING = _ROWS_CTE + f"""
SELECT count(*) AS boundary_rows,
  count(*) FILTER (WHERE primary_e < {HIGH_E_MIN}) AS primary_e_below,
  count(*) FILTER (WHERE primary_e < {HIGH_E_MIN} AND e >= {HIGH_E_MIN}) AS shown_not_deciding
FROM valid WHERE basin = 'boundary' AND primary_e IS NOT NULL
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


async def run(db_url: str, window_days: int) -> int:
    conn = await asyncpg.connect(db_url)
    try:
        agreement = await conn.fetch(Q_AGREEMENT, window_days)
        margin = await conn.fetch(Q_E_MARGIN, window_days)
        failing = await conn.fetch(Q_FAILING_SETS, window_days)
        sub = await conn.fetch(Q_SUB_BY_BASIN, window_days)
        shown = await conn.fetchrow(Q_SHOWN_NOT_DECIDING, window_days)
    finally:
        await conn.close()

    print(f"# Basin conjunct binding read — last {window_days} days\n")
    print("Verdict-path inputs = `eisv_telemetry.measurement.ode.values` + "
          "`measurement.coherence.value` + `policy_evaluation.inputs.risk_score`; "
          "persisted basin = `policy_evaluation.inputs.basin`.\n")

    print("## 1. Provenance: persisted basin vs recomputed from recorded inputs\n")
    print(_table(["persisted", "recomputed", "n"],
                 [(r["basin"], r["recomputed"], r["n"]) for r in agreement]))
    bnd_total = sum(r["n"] for r in agreement if r["basin"] == "boundary")
    bnd_agree = sum(r["n"] for r in agreement
                    if r["basin"] == "boundary" and r["recomputed"] == "boundary")
    print(f"\nBOUNDARY agreement: {bnd_agree}/{bnd_total}"
          + ("" if bnd_agree == bnd_total else
             "  <-- NOT exact; the inputs are not the classifier's, stop here"))

    print("\n## 2. Verdict-path E by basin (margin to the E >= 0.6 conjunct)\n")
    print(_table(["basin", "n", "min E", "p05 E", "p50 E"],
                 [(r["basin"], r["n"], r["min_e"], r["p05_e"], r["p50_e"])
                  for r in margin]))

    print("\n## 3. BOUNDARY rows: exact failing HIGH-conjunct sets\n")
    print(_table(["failing set", "n", "agents", "p50 E"],
                 [(r["failing_set"] or "(none)", r["n"], r["agents"], r["p50_e"])
                  for r in failing]))
    e_only = sum(r["n"] for r in failing if r["failing_set"] == "E")
    e_any = sum(r["n"] for r in failing if "E" in (r["failing_set"] or "").split("+"))
    print(f"\nE-only (the guide self-loop binding alone): {e_only}. "
          f"E in any failing set: {e_any}.")

    print("\n## 4. sub_action by basin\n")
    print(_table(["sub_action", "basin", "n"],
                 [(r["sub"], r["basin"], r["n"]) for r in sub]))

    print("\n## 5. Shown but never deciding\n")
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
    ap.add_argument("--window-days", type=int, default=60)
    ap.add_argument("--db-url",
                    default=os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DB_URL))
    args = ap.parse_args(argv)
    return asyncio.run(run(args.db_url, args.window_days))


if __name__ == "__main__":
    sys.exit(main())
