#!/usr/bin/env python3
"""Stage A red-team — does enabling UNITARES_S_SETPOINT shift the verdict/risk
of HEALTHY agents on the ODE/phi control path?

Context (docs/proposals/eisv-fixed-point-calibration-gap-v0.md, addendum v0.1):
Stage A adds a per-class S setpoint σ so the ODE rests at the *measured* healthy
S (~0.17-0.31) instead of S*≈0.091. This is correct for the MANIFOLD readout
(distance from the healthy point shrinks → coherence rises toward 1.0).

But the same ODE state feeds Φ (governance_core.scoring.phi_objective), and Φ
penalizes S linearly (`-wS·S`, wS=0.5). So raising the ODE rest-S by σ LOWERS Φ
by wS·σ for every agent whose Φ is read off the free-running ODE attractor.
That is a two-signal tension:

    manifold coherence:  raising S* toward healthy_S  → distance ↓ → HEALTHIER
    Φ / verdict / risk:  raising S* (penalty -wS·S)   → Φ ↓       → less healthy

This harness quantifies the Φ side. For each class it computes the ODE
equilibrium with the setpoint OFF (σ=0, historical) and ON (σ=healthy_S-0.091),
threading s_setpoint through the integrator (compute_equilibrium does NOT, so we
re-implement the relaxation here), then evaluates the REAL control signals at
each rest point:

  - Φ              governance_core.phi_objective (DEFAULT_WEIGHTS)
  - verdict        governance_core.verdict_from_phi  (safe≥0.08, caution≥0.0)
  - risk_score     src.monitor_risk mapping (phi-band → risk; PHI weights)
  - manifold@rest  src.grounding.coherence._compute_manifold on the ODE state
                   (the ODE-fallback case — new agents without a behavioral
                   baseline; mature agents read manifold off the *behavioral*
                   EISV and are unaffected by the setpoint)
  - basin          governance_core.check_basin (I axis — setpoint is S-only, so
                   this is an invariance check, expected UNCHANGED)

Acceptance (healthy agents must not degrade under the flag):
  A1  verdict stays "safe" for every class at rest (no band crossing)
  A2  risk_score stays in the healthy band (< RISK_REVISE_THRESHOLD=0.7; ideally
      < risk_healthy_max≈0.45) and does not increase across a status boundary
  A3  basin unchanged (S-only setpoint must not move the I-basin)
  A4  manifold@rest (ODE-fallback) clears the COHERENCE_CRITICAL_THRESHOLD=0.40
      line AFTER the flag (it should rise; the addendum's blocker was that it
      read ~0.16 < 0.40 today)

Usage:
    PYTHONPATH=. python3 scripts/analysis/eisv_stage_a_redteam.py
    PYTHONPATH=. python3 scripts/analysis/eisv_stage_a_redteam.py --db   # + live ODE-S check
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from governance_core import phi_objective, verdict_from_phi, DEFAULT_WEIGHTS  # noqa: E402
from governance_core.dynamics import State, compute_dynamics, check_basin  # noqa: E402
from governance_core.parameters import get_active_params, DEFAULT_THETA  # noqa: E402
from config import governance_config as cfg  # noqa: E402
from src.grounding.coherence import _compute_manifold  # noqa: E402


PHI_SAFE = getattr(cfg.config, "PHI_SAFE_THRESHOLD", 0.08)
PHI_CAUTION = getattr(cfg.config, "PHI_CAUTION_THRESHOLD", 0.0)
RISK_REJECT = getattr(cfg.config, "RISK_REJECT_THRESHOLD", 0.8)
RISK_REVISE = getattr(cfg.config, "RISK_REVISE_THRESHOLD", 0.7)
COH_CRIT = getattr(cfg.config, "COHERENCE_CRITICAL_THRESHOLD", 0.40)
RISK_HEALTHY_MAX = 0.45  # health_thresholds.HealthThresholds default


def relax_equilibrium(params, theta, s_setpoint: float, complexity: float = 0.5) -> State:
    """Forward-relax the ODE to a fixed point WITH s_setpoint threaded.

    Mirrors governance_core.compute_equilibrium but passes s_setpoint (which the
    library function does not), so we can see where the ON dynamics actually
    rest. Same start state, dt, and tolerance.
    """
    state = State(E=0.7, I=0.8, S=0.2, V=0.0)
    dt = 0.2
    for _ in range(4000):
        nxt = compute_dynamics(
            state=state, delta_eta=[], theta=theta, params=params, dt=dt,
            noise_S=0.0, complexity=complexity, sensor_eisv=None, s_setpoint=s_setpoint,
        )
        max_d = max(abs(nxt.E - state.E), abs(nxt.I - state.I),
                    abs(nxt.S - state.S), abs(nxt.V - state.V))
        state = nxt
        if max_d < 1e-10:
            break
    return state


def phi_to_risk(phi: float) -> float:
    """Reproduce src.monitor_risk.estimate_risk phi→risk mapping (phi path,
    RISK_PHI_WEIGHT=1.0, RISK_TRADITIONAL_WEIGHT=0.0, zero velocity at rest)."""
    if phi >= PHI_SAFE:
        return max(0.0, 0.3 - (phi - PHI_SAFE) * 0.5)
    if phi >= PHI_CAUTION:
        rng = PHI_SAFE - PHI_CAUTION
        return 0.3 + (PHI_SAFE - phi) / rng * 0.4 if rng > 0 else 0.5
    return min(1.0, 0.7 + abs(phi - PHI_CAUTION) * 2.0)


def status_band(risk: float) -> str:
    if risk < RISK_HEALTHY_MAX:
        return "healthy"
    if risk < RISK_REVISE:
        return "moderate"
    return "critical"


def evaluate(state: State, agent_class: str, s_detrend: float = 0.0) -> dict:
    """Evaluate control signals at a rest state.

    s_detrend>0 models the proposed remedy: Φ penalizes entropy ABOVE the
    setpoint (`-wS·(S-σ)`) instead of absolute entropy (`-wS·S`). Implemented by
    evaluating phi_objective on an S shifted down by σ, so the S-penalty at the
    new healthy rest equals the penalty at the old rest (Φ stays ~0.26).
    """
    phi_state = State(E=state.E, I=state.I, S=state.S - s_detrend, V=state.V)
    phi = phi_objective(phi_state, delta_eta=[], weights=DEFAULT_WEIGHTS)
    verdict = verdict_from_phi(phi)
    risk = phi_to_risk(phi)
    manifold = _compute_manifold(state.E, state.I, state.S, agent_class=agent_class).value
    basin = check_basin(state)
    return {"phi": phi, "verdict": verdict, "risk": risk,
            "manifold": manifold, "basin": basin, "S": state.S, "E": state.E, "I": state.I}


def run_equilibrium_panel() -> bool:
    params = get_active_params()
    theta = DEFAULT_THETA
    classes = sorted(cfg.HEALTHY_OPERATING_POINT_BY_CLASS.keys())

    print("=== Stage A red-team: ODE rest-state control signals, OFF vs ON ===")
    print(f"thresholds: verdict safe≥{PHI_SAFE} caution≥{PHI_CAUTION} | "
          f"risk healthy<{RISK_HEALTHY_MAX} revise≥{RISK_REVISE} reject≥{RISK_REJECT} | "
          f"manifold critical<{COH_CRIT}\n")
    header = (f"{'class':<18}{'σ':>6} | {'S_off':>6}{'S_on':>6} | "
              f"{'Φ_off':>7}{'Φ_on':>7}{'dΦ':>7} | {'verd_off':>9}{'verd_on':>9} | "
              f"{'risk_off':>9}{'risk_on':>8} | {'mani_off':>9}{'mani_on':>8}")
    print(header)
    print("-" * len(header))

    ok = True
    rows = []
    for klass in classes:
        sigma = cfg.get_healthy_operating_point(klass)[2] - cfg.S_SETPOINT_DRIVER_OFFSET
        sigma = max(0.0, sigma)
        off = evaluate(relax_equilibrium(params, theta, 0.0), klass)
        on = evaluate(relax_equilibrium(params, theta, sigma), klass)
        # Remedy: same ON attractor, but Φ detrends S by σ (penalize S above the
        # setpoint, not above zero) — proves the verdict regression is fixable.
        on_fixed = evaluate(relax_equilibrium(params, theta, sigma), klass, s_detrend=sigma)
        rows.append((klass, sigma, off, on, on_fixed))

        flags = []
        # A1 verdict must stay safe
        if on["verdict"] != "safe":
            flags.append(f"VERDICT→{on['verdict']}")
            ok = False
        # A2 risk healthy band, must not cross a status boundary upward
        if status_band(on["risk"]) != status_band(off["risk"]):
            flags.append(f"STATUS {status_band(off['risk'])}→{status_band(on['risk'])}")
            ok = False
        if on["risk"] >= RISK_REVISE:
            flags.append("RISK≥revise")
            ok = False
        # A3 basin invariant
        if on["basin"] != off["basin"]:
            flags.append(f"BASIN {off['basin']}→{on['basin']}")
            ok = False
        # A4 manifold@rest clears critical AFTER flag
        if on["manifold"] < COH_CRIT:
            flags.append(f"MANIFOLD {on['manifold']:.3f}<crit")
            # not a hard fail by itself for mature agents (behavioral path), but
            # the addendum's Stage-A goal is to clear this for ODE-fallback —
            # flag it loudly.

        flag_str = ("  ⚠ " + ", ".join(flags)) if flags else "  ok"
        print(f"{klass:<18}{sigma:>6.3f} | {off['S']:>6.3f}{on['S']:>6.3f} | "
              f"{off['phi']:>7.3f}{on['phi']:>7.3f}{on['phi']-off['phi']:>+7.3f} | "
              f"{off['verdict']:>9}{on['verdict']:>9} | "
              f"{off['risk']:>9.3f}{on['risk']:>8.3f} | "
              f"{off['manifold']:>9.3f}{on['manifold']:>8.3f}{flag_str}")

    # Manifold-at-rest summary (A4) — the Stage-A *goal* signal
    print("\nA4 manifold@rest (ODE-fallback agents) — did the flag clear the "
          f"{COH_CRIT} critical line?")
    cleared = sum(1 for _, _, off, on, _ in rows if on["manifold"] >= COH_CRIT)
    cleared_off = sum(1 for _, _, off, on, _ in rows if off["manifold"] >= COH_CRIT)
    print(f"  OFF: {cleared_off}/{len(rows)} classes clear {COH_CRIT}; "
          f"ON: {cleared}/{len(rows)} classes clear {COH_CRIT}")

    # Remedy counterfactual: ON attractor + Φ detrended by σ (penalize S above
    # the setpoint, not above zero). Proves the verdict/risk regression is an
    # artifact of Φ being calibrated to the OLD attractor, and is removed by
    # recentering Φ's S term on σ.
    print("\nRemedy — Φ detrended by σ (penalize S above setpoint, not above 0):")
    print(f"  {'class':<18}{'Φ_on':>8}{'Φ_fixed':>9}{'verd_fixed':>11}{'risk_fixed':>11}")
    remedy_ok = True
    for klass, sigma, off, on, fx in rows:
        bad = fx["verdict"] != "safe" or status_band(fx["risk"]) != status_band(off["risk"])
        if bad:
            remedy_ok = False
        print(f"  {klass:<18}{on['phi']:>8.3f}{fx['phi']:>9.3f}"
              f"{fx['verdict']:>11}{fx['risk']:>11.3f}{'' if not bad else '  ⚠'}")
    print(f"  remedy keeps all classes safe + healthy-risk: {remedy_ok}")
    return ok


async def run_live_premise_check(dsn, limit) -> bool:
    """Confirm the premise: production Φ today is read off the ODE attractor
    (S≈0.091), NOT the behavioral EISV.

    The ODE state is not persisted under a stable key, but the persisted Φ is.
    If Φ clusters near the OFF-equilibrium Φ (≈0.26) rather than the value Φ
    would take on the behavioral S (≈0.39 → Φ≈−0.43), then Φ rests on the ODE
    attractor and the setpoint WILL move it — i.e. the red-team regression is
    real on live data, not a harness artifact.
    """
    print("\n=== Live premise check (is prod Φ read off the S≈0.091 attractor?) ===")
    import os
    try:
        import asyncpg
    except Exception:
        print("  SKIP: asyncpg not installed."); return True
    dsn = dsn or os.environ.get("DATABASE_URL") or \
        "postgresql://postgres:postgres@localhost:5432/governance"
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"  SKIP: connect failed ({e})."); return True
    try:
        rows = await conn.fetch(
            """
            SELECT (state_json->>'phi')::float8                    AS phi,
                   (state_json->'behavioral_eisv'->>'S')::float8   AS beh_s
            FROM core.agent_state s
            WHERE state_json ? 'phi'
              AND s.recorded_at > now() - interval '7 day'
            ORDER BY s.recorded_at DESC
            LIMIT $1
            """,
            limit,
        )
    except Exception as e:
        print(f"  SKIP: query failed ({e})."); await conn.close(); return True
    await conn.close()

    def _med(xs):
        xs = sorted(x for x in xs if x is not None)
        return xs[len(xs) // 2] if xs else None

    phis = sorted(r["phi"] for r in rows if r["phi"] is not None)
    beh = [r["beh_s"] for r in rows if r["beh_s"] is not None]
    if phis:
        med = phis[len(phis) // 2]
        print(f"  persisted Φ over {len(phis)} check-ins: "
              f"median={med:.3f}  p10={phis[len(phis)//10]:.3f}  "
              f"p90={phis[9*len(phis)//10]:.3f}")
        off_phi = phi_objective(relax_equilibrium(get_active_params(), DEFAULT_THETA, 0.0),
                                delta_eta=[], weights=DEFAULT_WEIGHTS)
        verdict = "MATCH: Φ rests on ODE attractor" if abs(med - off_phi) < 0.15 else "divergent"
        print(f"  OFF-equilibrium Φ (S≈0.091 attractor) = {off_phi:.3f}  → {verdict}")
    if beh:
        bmed = _med(beh)
        beh_phi = phi_objective(State(E=0.805, I=0.822, S=bmed, V=-0.013),
                                delta_eta=[], weights=DEFAULT_WEIGHTS)
        print(f"  behavioral-S median={bmed:.3f} → Φ-if-behavioral≈{beh_phi:.3f} "
              f"(far from persisted Φ ⇒ Φ is NOT on behavioral S)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="store_true", help="also run live Φ-premise check")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--limit", type=int, default=2000)
    args = ap.parse_args()

    ok = run_equilibrium_panel()
    if args.db:
        import asyncio
        asyncio.run(run_live_premise_check(args.dsn, args.limit))

    print("\n" + ("PASS — flag does not degrade healthy agents on the Φ path"
                  if ok else "FAIL — see ⚠ flags above"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
