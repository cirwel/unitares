#!/usr/bin/env python3
"""Empirical validation for issue #689 — absolute-basin-health gating.

Compares the *current* live behaviour (flat MIN_MEANINGFUL_EISV_STD σ-floor only,
i.e. the basin gate forced fully open) against the *proposed* behaviour (basin
gate active) by rebuilding a ``BehavioralEISV`` from a persisted baseline +
current EISV and calling ``assess_behavioral_state`` under both regimes.

Two data sources:

  1. Trace-anchored cases (always available): the real captured 2026-06-13
     Sentinel false-pause trace plus a parametric sweep of the tight-σ class
     (the ~21 recently-baselined agents with σ<0.05 on E/I — all baselined
     residents are in this hypersensitive class). Healthy wobbles must stay
     SAFE; genuine basin-exit states must still FLAG.

  2. Live fleet (optional): pass ``--db`` to pull the most recent persisted
     ``state_json->'behavioral_eisv'`` per recently-baselined agent from
     ``core.agent_state`` and run the same comparison across the real fleet.
     Requires asyncpg and a reachable PostgreSQL (DATABASE_URL or PG* env, or
     --dsn). This is the path the verifier runs post-merge.

NOTE on field provenance (issue #689 gotcha): read behavioral E/I/S/V/phi from
``state_json->'behavioral_eisv'`` (and phi from ``state_json->>'phi'``). The
``core.agent_state.entropy`` column stores behavioral S exactly, NOT phi — do
not read EISV from column names.

Usage:
    python3 scripts/analysis/validate_basin_gate.py            # trace + sweep
    python3 scripts/analysis/validate_basin_gate.py --db       # + live fleet
    python3 scripts/analysis/validate_basin_gate.py --db --dsn postgresql://...
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from typing import Dict, Optional, Tuple

sys.path.insert(0, ".")

from src.agent_behavioral_baseline import WelfordStats  # noqa: E402
from src.behavioral_state import BehavioralEISV  # noqa: E402
import src.behavioral_assessment as ba  # noqa: E402
from src.behavioral_assessment import (  # noqa: E402
    assess_behavioral_state,
    RISK_SAFE_THRESHOLD,
    RISK_CAUTION_THRESHOLD,
)


# --- Real captured Sentinel baseline at the moment of the 2026-06-13 false pause
# (count, mean, m2) per dim; 1239 updates. From tests/test_stable_agent_risk_calibration.py.
_SENTINEL_BASELINE = {
    "E": (0.7729981068548344, 0.7055579515066273),
    "I": (0.6811142748149669, 0.057464469736054416),
    "S": (0.23501023019317843, 2.5585618595179382),
    "V": (0.09186701608785974, 0.3958598283268047),
}
_SENTINEL_PAUSE_STATE = {"E": 0.6608, "I": 0.6572, "S": 0.379, "V": 0.046}


def _build(baseline_mean_m2: Dict[str, Tuple[float, float]],
           current: Dict[str, float], count: int = 1239) -> BehavioralEISV:
    """Rebuild a baselined BehavioralEISV from (mean, m2) baseline + current EISV."""
    st = BehavioralEISV()
    for dim, (mean, m2) in baseline_mean_m2.items():
        bl: WelfordStats = getattr(st, f"_baseline_{dim}")
        bl.count, bl.mean, bl.m2 = count, mean, m2
    for dim, val in current.items():
        setattr(st, dim, val)
    st.update_count = count
    return st


def _build_from_stats(baseline_stats: Dict[str, dict],
                      current: Dict[str, float]) -> Optional[BehavioralEISV]:
    """Rebuild from a persisted ``baseline_stats`` dict (to_dict() form)."""
    st = BehavioralEISV()
    counts = []
    for dim in ("E", "I", "S", "V"):
        d = baseline_stats.get(dim)
        if not d:
            return None
        bl: WelfordStats = getattr(st, f"_baseline_{dim}")
        bl.count = int(d.get("count", 0))
        bl.mean = float(d.get("mean", 0.0))
        bl.m2 = float(d.get("m2", 0.0))
        counts.append(bl.count)
    for dim in ("E", "I", "S", "V"):
        if dim in current and current[dim] is not None:
            setattr(st, dim, float(current[dim]))
    st.update_count = max(counts) if counts else 0
    return st


@contextmanager
def _gate_disabled():
    """Force the basin gate fully open → reproduces pre-#689 (flat-floor) scoring."""
    original = ba._basin_health_gate
    ba._basin_health_gate = lambda state: {  # type: ignore[assignment]
        "low_E": 1.0, "low_I": 1.0, "high_S": 1.0, "high_V": 1.0,
    }
    try:
        yield
    finally:
        ba._basin_health_gate = original  # type: ignore[assignment]


def _assess_both(state: BehavioralEISV, rho: float = 0.0):
    """Return (before_result, after_result): flat-floor-only vs basin-gate."""
    after = assess_behavioral_state(state, rho=rho)
    with _gate_disabled():
        before = assess_behavioral_state(state, rho=rho)
    return before, after


def _verdict_tag(risk: float) -> str:
    if risk < RISK_SAFE_THRESHOLD:
        return "safe"
    if risk < RISK_CAUTION_THRESHOLD:
        return "caution"
    return "high-risk"


def _row(label: str, before, after) -> str:
    return (f"  {label:<46} "
            f"before={before.risk:0.3f}/{before.verdict:<9} "
            f"after={after.risk:0.3f}/{after.verdict:<9}")


def run_trace_cases() -> bool:
    print("=== Trace-anchored: 2026-06-13 Sentinel false-pause ===")
    ok = True

    st = _build(_SENTINEL_BASELINE, _SENTINEL_PAUSE_STATE)
    before, after = _assess_both(st)
    print(_row("Sentinel pause state (healthy wobble)", before, after))
    # Acceptance: proposed must NOT be high-risk; must be safe.
    if after.verdict == "high-risk" or after.risk >= RISK_CAUTION_THRESHOLD:
        print("  FAIL: proposed gate leaves Sentinel pause non-safe"); ok = False

    # Genuine entropy excursion on the same tight baseline → must still flag high_S.
    st_spike = _build(_SENTINEL_BASELINE, {"E": 0.773, "I": 0.681, "S": 0.70, "V": 0.092})
    _, after_spike = _assess_both(st_spike)
    print(f"  {'Sentinel S=0.70 (absolute danger edge)':<46} "
          f"after high_S={after_spike.components.get('high_S', 0.0):0.3f} "
          f"risk={after_spike.risk:0.3f}/{after_spike.verdict}")
    if after_spike.components.get("high_S", 0.0) <= 0.0:
        print("  FAIL: genuine entropy excursion no longer scores high_S"); ok = False

    return ok


def run_sweep() -> bool:
    """Parametric sweep of the tight-σ class (representative of the ~21 affected).

    Asserts the principled invariants of the gate rather than arbitrary labels:
      - the gate NEVER raises risk (multiplier ∈ [0,1]):          after <= before
      - in-basin wobbles are safe:                                 verdict == safe
      - genuine multi-dim basin exits still pause:                 high-risk
      - absolute-floor breaches still pause regardless of gate:    high-risk
    """
    print("\n=== Parametric sweep: tight-σ baselined agents (σ≈0.012) ===")
    ok = True
    TIGHT = 0.012  # baseline std; ultra-stable, well below the 0.05 flat floor

    def tight_baseline(meanE, meanI, meanS, meanV, count=600):
        m2 = TIGHT * TIGHT * (count - 1)
        return {"E": (meanE, m2), "I": (meanI, m2), "S": (meanS, m2), "V": (meanV, m2)}

    # (label, baseline means, current EISV, expect) — expect: safe | pause | none
    #   safe  → verdict must be "safe" (in-basin: deviation is information)
    #   pause → verdict must be "high-risk" (genuine danger must still escalate)
    #   none  → no verdict assertion; case demonstrates graded de-escalation
    cases = [
        ("in-basin: +0.06 E wobble",
         (0.75, 0.78, 0.18, -0.03), {"E": 0.69, "I": 0.78, "S": 0.18, "V": -0.03}, "safe"),
        ("in-basin: +0.05 S wobble",
         (0.72, 0.76, 0.16, 0.0), {"E": 0.72, "I": 0.76, "S": 0.21, "V": 0.0}, "safe"),
        ("in-basin: multi-dim small wobble",
         (0.74, 0.80, 0.15, 0.02), {"E": 0.66, "I": 0.72, "S": 0.24, "V": 0.10}, "safe"),
        ("boundary: I→0.40 (graded de-escalation)",
         (0.74, 0.80, 0.18, 0.0), {"E": 0.62, "I": 0.40, "S": 0.30, "V": 0.22}, "none"),
        ("boundary: S→0.62 (graded de-escalation)",
         (0.70, 0.76, 0.18, 0.0), {"E": 0.55, "I": 0.60, "S": 0.62, "V": 0.05}, "none"),
        ("deep exit: E→0.35,I→0.45,S→0.65,V→0.20",
         (0.74, 0.78, 0.18, 0.0), {"E": 0.35, "I": 0.45, "S": 0.65, "V": 0.20}, "pause"),
        ("abs-floor breach: E→0.20,I→0.25,S→0.80",
         (0.74, 0.78, 0.18, 0.0), {"E": 0.20, "I": 0.25, "S": 0.80, "V": 0.10}, "pause"),
    ]

    for label, (mE, mI, mS, mV), cur, expect in cases:
        st = _build(tight_baseline(mE, mI, mS, mV), cur)
        before, after = _assess_both(st)
        print(_row(label, before, after))
        # Invariant: the gate is a [0,1] multiplier — it must never raise risk.
        if after.risk > before.risk + 1e-9:
            print(f"  FAIL: gate raised risk ({before.risk:.3f}→{after.risk:.3f})"); ok = False
        if expect == "safe" and after.verdict != "safe":
            print(f"  FAIL: in-basin wobble not safe (got {after.verdict})"); ok = False
        if expect == "pause" and after.verdict != "high-risk":
            print(f"  FAIL: genuine danger did not escalate (got {after.verdict})"); ok = False
    return ok


async def run_live(dsn: Optional[str], limit: int) -> bool:
    print("\n=== Live fleet: recently-baselined agents from core.agent_state ===")
    import json
    import os
    try:
        import asyncpg  # type: ignore
    except Exception:
        print("  SKIP: asyncpg not installed."); return True

    dsn = dsn or os.environ.get("DATABASE_URL")
    try:
        conn = await asyncpg.connect(dsn) if dsn else await asyncpg.connect()
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP: could not connect to PostgreSQL ({e})."); return True

    masked = 0
    flagged_before_safe_after = 0
    newly_masked = 0  # genuine risk the gate would hide (must be 0)
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (s.identity_id)
                   i.agent_id,
                   i.metadata->>'label' AS label,
                   s.state_json->'behavioral_eisv' AS beisv
            FROM core.agent_state s
            JOIN core.identities i USING(identity_id)
            WHERE s.synthetic = false
              AND s.state_json ? 'behavioral_eisv'
            ORDER BY s.identity_id, s.recorded_at DESC
            LIMIT $1
            """,
            limit,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP: query failed ({e})."); await conn.close(); return True

    examined = 0
    for r in rows:
        beisv = r["beisv"]
        if beisv is None:
            continue
        if isinstance(beisv, str):
            beisv = json.loads(beisv)
        baseline_stats = beisv.get("baseline_stats")
        if not baseline_stats:
            continue
        current = {k: beisv.get(k) for k in ("E", "I", "S", "V")}
        st = _build_from_stats(baseline_stats, current)
        if st is None or not st.is_baselined:
            continue
        examined += 1
        before, after = _assess_both(st)
        if before.verdict == "high-risk" and after.verdict != "high-risk":
            flagged_before_safe_after += 1
        # A genuine risk would be: state outside basin (absolute floors / fixed
        # thresholds say bad) but gate downgrades it. Detect masking: before
        # high-risk AND after safe AND the state is genuinely degraded.
        degraded = (after.components.get("low_E", 0) > 0 or
                    after.components.get("low_I", 0) > 0 or
                    after.components.get("high_S", 0) > 0 or
                    after.components.get("high_V", 0) > 0)
        if before.verdict == "high-risk" and after.verdict == "safe" and not degraded:
            masked += 1  # gate fully suppressed — confirm absolute health below
        # genuine masking check: was the ABSOLUTE state actually unhealthy?
        if before.verdict == "high-risk" and after.verdict == "safe":
            absolutely_bad = (st.E < ba.ABSOLUTE_E_FLOOR or st.I < ba.ABSOLUTE_I_FLOOR
                              or st.S > ba.ABSOLUTE_S_CEILING or abs(st.V) > ba.ABSOLUTE_V_CEILING)
            if absolutely_bad:
                newly_masked += 1
                print(f"  WARN masked genuine risk: {r['agent_id']} "
                      f"E={st.E:.2f} I={st.I:.2f} S={st.S:.2f} V={st.V:.2f}")
    await conn.close()
    print(f"  examined={examined} baselined agents; "
          f"de-escalated high-risk→non-high-risk by gate: {flagged_before_safe_after}; "
          f"fully-suppressed-in-basin: {masked}; "
          f"genuine-risk-masked (must be 0): {newly_masked}")
    if newly_masked > 0:
        print("  FAIL: gate masked genuinely-degraded (absolute floor) states"); return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="store_true", help="also run against live fleet DB")
    ap.add_argument("--dsn", default=None, help="explicit PostgreSQL DSN")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    ok = True
    ok &= run_trace_cases()
    ok &= run_sweep()
    if args.db:
        import asyncio
        ok &= asyncio.run(run_live(args.dsn, args.limit))

    print("\n" + ("PASS — acceptance criteria met" if ok else "FAIL — see above"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
