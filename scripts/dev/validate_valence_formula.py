#!/usr/bin/env python3
"""Trace-replay validation gate for the candidate behavioral-V (Valence) formula.

WHY THIS EXISTS
---------------
The deployed behavioral V is double-smoothed: ``raw_v = self.E - self.I`` is the
gap of the already-EMA'd E and I, then V is EMA'd again. A candidate fix takes
the gap of the *raw* observations instead (``raw_v = E_obs - I_obs``), removing
one smoothing stage so V tracks the imbalance with less lag.

A 3-seat review council (2026-06-19) split on whether that change is safe: the
behavioral risk score is the PRIMARY verdict on the live pause path, and the
candidate increases V's variance, which interacts with the #686/#689
false-pause band-aids. The adversarial seat required this be replayed against
real check-in traces (with a baseline-stats reset) before it ships.

This harness is that gate. It feeds an observation trace through the REAL
``BehavioralEISV.update`` and ``assess_behavioral_state`` under BOTH formulas
(via the ``_raw_valence`` override seam — no logic is reimplemented here) and
reports verdict flips, false-pause candidates on healthy trajectories, V
variance deltas, and the post-deploy migration discontinuity.

USAGE
-----
    # Synthetic regimes (runs anywhere; no DB needed):
    python3 scripts/dev/validate_valence_formula.py --synthetic

    # Real traces exported from the governance DB (preferred for the gate):
    python3 scripts/dev/validate_valence_formula.py --trace traces.jsonl --out report.json

TRACE FORMAT (JSONL, one object per agent)
    {"agent_id": "...", "label": "healthy|hot|careful|transient|sentinel|unknown",
     "observations": [[E_obs, I_obs, S_obs], ...]}
``label`` is advisory; only traces whose label marks them healthy gate the
false-pause check (a flip toward risk on a genuinely-healthy agent is the
failure we care about). To export real traces, dump each agent's
``obs_history`` (raw pre-EMA observations) from persisted behavioral state.

EXIT STATUS
    0  candidate looks safe under the supplied traces (gateable)
    1  candidate introduces verdict regressions on healthy traces, OR the
       migration discontinuity exceeds the sigma budget without a baseline reset
Stdlib + repo-local imports only.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Dict, List

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.behavioral_state import (  # noqa: E402
    BehavioralEISV,
    BASELINE_WARMUP_UPDATES,
    MIN_MEANINGFUL_EISV_STD,
)
from src.behavioral_assessment import assess_behavioral_state  # noqa: E402

_VERDICT_RANK = {"safe": 0, "caution": 1, "high-risk": 2}
_MIN_STD_FLOOR = MIN_MEANINGFUL_EISV_STD


class LegacyV(BehavioralEISV):
    """v1 deployed formula: gap of the already-EMA'd E,I (double-smoothing).

    As of V_FORMULA_VERSION 2 the base class default IS the candidate
    (single-EMA of raw imbalance, ``E_obs - I_obs``). This subclass restores the
    v1 behavior so the gate remains a live A/B: ``LegacyV`` = old, base = new.
    """

    def _raw_valence(self, E_obs: float, I_obs: float) -> float:
        return self.E - self.I


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def _step_record(state: BehavioralEISV, idx: int) -> Dict:
    """Assess a state (rho/CE held at 0 to isolate the V-driven delta)."""
    result = assess_behavioral_state(state, rho=0.0, continuity_energy=0.0)
    return {
        "idx": idx,
        "V": round(state.V, 5),
        "abs_V": round(abs(state.V), 5),
        "z_V": round(state.deviation("V"), 4),
        "baselined": state.is_baselined,
        "risk": result.risk,
        "verdict": result.verdict,
        "high_V": result.components.get("high_V", 0.0),
    }


def replay(observations: List[List[float]], candidate: bool) -> List[Dict]:
    # new (v2) = base default; old (v1) = LegacyV override.
    state: BehavioralEISV = BehavioralEISV() if candidate else LegacyV()
    out: List[Dict] = []
    for idx, obs in enumerate(observations):
        e, i, s = (float(obs[0]), float(obs[1]), float(obs[2]))
        state.update(e, i, s)
        out.append(_step_record(state, idx))
    return out


def compare_trace(trace: Dict) -> Dict:
    obs = trace["observations"]
    label = trace.get("label", "unknown")
    healthy = label in ("healthy", "sentinel")  # traces that SHOULD stay safe

    old = replay(obs, candidate=False)
    new = replay(obs, candidate=True)

    flips, healthy_regressions = [], []
    risk_deltas = []
    for o, n in zip(old, new):
        risk_deltas.append(n["risk"] - o["risk"])
        if o["verdict"] != n["verdict"]:
            worse = _VERDICT_RANK[n["verdict"]] > _VERDICT_RANK[o["verdict"]]
            flip = {
                "idx": o["idx"], "from": o["verdict"], "to": n["verdict"],
                "worse": worse, "baselined": n["baselined"],
                "risk_old": o["risk"], "risk_new": n["risk"],
            }
            flips.append(flip)
            if worse and healthy:
                healthy_regressions.append(flip)

    # V dynamic range (Seat 3's "candidate increases variance" claim)
    post_warmup = slice(BASELINE_WARMUP_UPDATES, None)
    v_old = [r["V"] for r in old][post_warmup]
    v_new = [r["V"] for r in new][post_warmup]
    std_old = statistics.pstdev(v_old) if len(v_old) > 1 else 0.0
    std_new = statistics.pstdev(v_new) if len(v_new) > 1 else 0.0

    return {
        "agent_id": trace.get("agent_id", "?"),
        "label": label,
        "n_obs": len(obs),
        "verdict_flips": flips,
        "n_flips": len(flips),
        "healthy_regressions": healthy_regressions,
        "n_healthy_regressions": len(healthy_regressions),
        "risk_delta_mean": round(statistics.fmean(risk_deltas), 4) if risk_deltas else 0.0,
        "risk_delta_max_abs": round(max((abs(d) for d in risk_deltas), default=0.0), 4),
        "V_std_old": round(std_old, 5),
        "V_std_new": round(std_new, 5),
        "V_std_ratio": round(std_new / std_old, 2) if std_old > 1e-9 else None,
    }


def migration_probe(observations: List[List[float]], sigma_budget: float) -> Dict:
    """Quantify Seat 3 RISK 2: new-formula V judged against the OLD baseline.

    Converge an agent under the v1 (deployed) formula, snapshot its V baseline,
    then take one more step computing V the v2 (new) way. Without a reset the new
    value is z-scored against the stale (v1, tighter-σ) baseline; with the reset
    the baseline is re-seeded from the v2 trajectory.

    ``z_spike_with_reset`` is no longer assumed — it exercises the REAL
    ``BehavioralEISV._reseed_v_baseline`` so this gate proves the shipped reset
    actually absorbs the spike, not just that one is planned.
    """
    # Converge under the v1 (deployed) formula.
    old = LegacyV()
    for obs in observations:
        old.update(float(obs[0]), float(obs[1]), float(obs[2]))
    base_mean, base_std = old._baseline_V.mean, old._baseline_V.std

    # One more observation, V computed the v2 way (from the v1-converged V).
    nxt = observations[-1]
    e, i = float(nxt[0]), float(nxt[1])
    alpha_v = old.alphas["V"]
    raw_v_new = e - i                       # v2: raw imbalance
    v_new = (1.0 - alpha_v) * old.V + alpha_v * raw_v_new

    denom = base_std if base_std > 1e-9 else 1e-9
    z_no_reset = abs(v_new - base_mean) / denom

    # WITH reset: re-seed _baseline_V from the v2 trajectory exactly as the live
    # migration does, then z-score the same v2 step against the re-seeded stats.
    reseeded = BehavioralEISV()
    reseeded.alphas = dict(old.alphas)
    reseeded.obs_history = [[float(o[0]), float(o[1]), float(o[2])] for o in observations]
    reseeded._reseed_v_baseline()
    rs_mean, rs_std = reseeded._baseline_V.mean, reseeded._baseline_V.std
    rs_denom = rs_std if rs_std > 1e-9 else _MIN_STD_FLOOR
    z_with_reset = abs(v_new - rs_mean) / rs_denom

    return {
        "old_baseline_V_mean": round(base_mean, 5),
        "old_baseline_V_std": round(base_std, 6),
        "candidate_V_first_step": round(v_new, 5),
        "reseeded_baseline_V_mean": round(rs_mean, 5),
        "reseeded_baseline_V_std": round(rs_std, 6),
        "z_spike_no_reset": round(z_no_reset, 3),
        "z_spike_with_reset": round(z_with_reset, 3),
        "sigma_budget": sigma_budget,
        "exceeds_budget_without_reset": z_no_reset > sigma_budget,
        "reset_clears_spike": z_with_reset <= sigma_budget,
        # The reset is the mitigation; the residual "required" flag now means
        # "would exceed budget if the reset were NOT applied".
        "reset_required": z_no_reset > sigma_budget,
    }


# --------------------------------------------------------------------------- #
# Synthetic regimes (deterministic)
# --------------------------------------------------------------------------- #
def _series(rng, n, e, i, s, jit=0.03):
    def c(x, j):
        return max(0.0, min(1.0, x + rng.uniform(-j, j)))
    return [[c(e, jit), c(i, jit), c(s, jit)] for _ in range(n)]


def synthetic_traces(seed: int = 7) -> List[Dict]:
    rng = random.Random(seed)
    healthy = _series(rng, 80, 0.70, 0.80, 0.15)                 # I>E: V small negative
    hot = _series(rng, 80, 0.86, 0.55, 0.20)                     # E>>I: V positive, large
    careful = _series(rng, 80, 0.52, 0.86, 0.18)                 # I>>E: V negative, large
    # transient: healthy, then a 6-step divergence burst, then recover
    transient = _series(rng, 50, 0.70, 0.80, 0.15)
    transient += _series(rng, 6, 0.88, 0.50, 0.30)
    transient += _series(rng, 30, 0.70, 0.80, 0.15)
    # sentinel: documented false-pause shape — steady (0.77,0.68) then dip to (0.66,0.66)
    sentinel = _series(rng, 50, 0.77, 0.68, 0.12, jit=0.01)
    sentinel += _series(rng, 20, 0.66, 0.66, 0.12, jit=0.01)
    return [
        {"agent_id": "syn-healthy", "label": "healthy", "observations": healthy},
        {"agent_id": "syn-hot", "label": "hot", "observations": hot},
        {"agent_id": "syn-careful", "label": "careful", "observations": careful},
        {"agent_id": "syn-transient", "label": "transient", "observations": transient},
        {"agent_id": "syn-sentinel", "label": "sentinel", "observations": sentinel},
    ]


def load_traces(path: Path) -> List[Dict]:
    traces = []
    with path.open() as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                sys.exit(f"trace line {lineno}: invalid JSON ({exc})")
            if not obj.get("observations"):
                sys.exit(f"trace line {lineno}: missing/empty 'observations'")
            traces.append(obj)
    if not traces:
        sys.exit("no traces loaded")
    return traces


# --------------------------------------------------------------------------- #
def build_report(traces: List[Dict], sigma_budget: float) -> Dict:
    per_trace = [compare_trace(t) for t in traces]
    migrations = [
        {"agent_id": t.get("agent_id", "?"),
         **migration_probe(t["observations"], sigma_budget)}
        for t in traces if t.get("label") in ("healthy", "sentinel")
    ]
    total_healthy_regressions = sum(t["n_healthy_regressions"] for t in per_trace)
    migration_needs_reset = any(m["reset_required"] for m in migrations)
    # As of V_FORMULA_VERSION 2 the reset ships WITH the flip, so the gate no
    # longer fails merely because a stale-baseline spike WOULD occur without it;
    # it fails only if a healthy verdict regresses, or the shipped reset fails
    # to bring every flagged spike back inside the sigma budget.
    reset_clears_all = all(m.get("reset_clears_spike", False) for m in migrations)
    gate_pass = total_healthy_regressions == 0 and reset_clears_all
    return {
        "summary": {
            "n_traces": len(traces),
            "total_verdict_flips": sum(t["n_flips"] for t in per_trace),
            "total_healthy_regressions": total_healthy_regressions,
            "migration_reset_would_be_needed": migration_needs_reset,
            "reset_clears_all_spikes": reset_clears_all,
            "gate_pass": gate_pass,
        },
        "per_trace": per_trace,
        "migration": migrations,
    }


def _print_human(report: Dict) -> None:
    s = report["summary"]
    print("=== Valence formula validation ===", file=sys.stderr)
    print(f"traces: {s['n_traces']}  verdict_flips: {s['total_verdict_flips']}  "
          f"healthy_regressions: {s['total_healthy_regressions']}  "
          f"reset_clears_all_spikes: {s['reset_clears_all_spikes']}", file=sys.stderr)
    for t in report["per_trace"]:
        ratio = t["V_std_ratio"]
        print(f"  [{t['label']:9}] {t['agent_id']:14} flips={t['n_flips']} "
              f"healthy_regr={t['n_healthy_regressions']} "
              f"riskΔmean={t['risk_delta_mean']:+.3f} "
              f"V_std old→new {t['V_std_old']:.4f}→{t['V_std_new']:.4f} "
              f"(x{ratio})", file=sys.stderr)
    for m in report["migration"]:
        print(f"  [migration] {m['agent_id']:14} z_spike no_reset={m['z_spike_no_reset']} "
              f"→ with_reset={m['z_spike_with_reset']} budget={m['sigma_budget']} "
              f"reset_clears={m['reset_clears_spike']}", file=sys.stderr)
    print(f"GATE: {'PASS' if s['gate_pass'] else 'FAIL'}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--trace", type=Path, help="JSONL trace file (real or exported)")
    src.add_argument("--synthetic", action="store_true", help="Use built-in synthetic regimes")
    p.add_argument("--out", type=Path, help="Write JSON report here (default: stdout)")
    p.add_argument("--sigma-budget", type=float, default=3.0,
                   help="Max tolerated first-step z-spike on a formula switch without "
                        "a baseline reset (default 3.0)")
    p.add_argument("--seed", type=int, default=7, help="Synthetic RNG seed")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    traces = synthetic_traces(args.seed) if args.synthetic else load_traces(args.trace)
    report = build_report(traces, args.sigma_budget)

    payload = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(payload)
    else:
        print(payload)
    _print_human(report)
    return 0 if report["summary"]["gate_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
