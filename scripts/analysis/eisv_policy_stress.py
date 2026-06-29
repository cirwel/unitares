#!/usr/bin/env python3
"""Label-free POLICY stress-tests for the behavioral governance verdict.

The skeptic report needs exogenous outcome labels (and is starved of them). The
self-predictability eval validates the *estimator*. This validates the *policy*
(state -> verdict) WITHOUT any outcome labels — by perturbation and consistency,
not by correlation with outcomes. None of these probes derive their truth signal
from the governance loop's own outputs, so they do not trip the self-referential-
anchor circularity (roadmap Invariant 4); the "label" is constructed by us.

It drives the real policy (`assess_behavioral_state` over `BehavioralEISV`) on
synthetic check-in replays — no DB, no live state.

Probes:
  1. Monotonicity / consistency — as the absolute state strictly worsens (S up,
     or I down), risk must never go DOWN. A governor whose risk is non-monotone
     in obvious badness is incoherent, independent of any outcome.
  2. Regime-injection recall (graded) — warm an agent healthy, then inject a
     sustained deterioration of increasing severity. Peak risk should rise with
     severity and eventually escalate the verdict. Flat response = the policy is
     blind to a constructed bad event. (Also exercises the #689 basin gate:
     deviation only converts to risk once the absolute state leaves the healthy
     basin.)
  3. Time-shuffle sensitivity — a policy that claims to track *trajectories*
     should not be invariant to the ORDER of the same observations. Replay the
     same multiset concentrated-late vs spread-early; if peak risk is identical
     the policy is effectively memoryless.

Usage:
    PYTHONPATH=. python3 scripts/analysis/eisv_policy_stress.py
    PYTHONPATH=. python3 scripts/analysis/eisv_policy_stress.py --output data/analysis/eisv_policy_stress.md
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.behavioral_state import BASELINE_WARMUP_UPDATES, BehavioralEISV  # noqa: E402
from src.behavioral_assessment import assess_behavioral_state  # noqa: E402

HEALTHY = (0.80, 0.72, 0.18)  # (E, I, S) — a healthy operating point
WARMUP_N = BASELINE_WARMUP_UPDATES + 5  # ensure is_baselined


def _baselined_healthy() -> BehavioralEISV:
    s = BehavioralEISV()
    for _ in range(WARMUP_N):
        s.update(*HEALTHY)
    return s


def _peak_risk(state: BehavioralEISV, obs_seq) -> tuple[float, str]:
    """Replay obs through a COPY of state; return (peak risk, final verdict)."""
    st = copy.deepcopy(state)
    peak = 0.0
    verdict = "safe"
    for (e, i, s) in obs_seq:
        st.update(e, i, s)
        r = assess_behavioral_state(st)
        peak = max(peak, r.risk)
        verdict = r.verdict
    return peak, verdict


def probe_monotonicity() -> dict:
    """Absolute (pre-baseline / fixed-threshold) policy must be monotone in
    obvious badness. Construct states directly and assess."""
    rows_S = []
    for s_val in [0.10, 0.25, 0.40, 0.55, 0.70, 0.85, 0.95]:
        st = BehavioralEISV()  # update_count 0 -> fixed-threshold path
        st.E, st.I, st.S = 0.80, 0.72, s_val
        st.V = st.E - st.I
        rows_S.append((s_val, assess_behavioral_state(st).risk))
    rows_I = []
    for i_val in [0.90, 0.75, 0.60, 0.45, 0.30, 0.15, 0.05]:
        st = BehavioralEISV()
        st.E, st.I, st.S = 0.80, i_val, 0.18
        st.V = st.E - st.I
        rows_I.append((i_val, assess_behavioral_state(st).risk))

    eps = 1e-9
    mono_S = all(rows_S[k][1] >= rows_S[k - 1][1] - eps for k in range(1, len(rows_S)))
    # I worsens as it DECREASES, and the list is already ordered worst-last
    mono_I = all(rows_I[k][1] >= rows_I[k - 1][1] - eps for k in range(1, len(rows_I)))
    return {"rows_S": rows_S, "rows_I": rows_I, "mono_S": mono_S, "mono_I": mono_I}


def probe_injection_recall() -> dict:
    """Graded recall: inject a sustained deterioration of rising severity."""
    base = _baselined_healthy()
    e0, i0, s0 = HEALTHY
    rows = []
    for delta in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]:
        # push E,I down and S up by the severity; clamp handled in update()
        bad = (e0 - delta, i0 - delta, s0 + 3 * delta)
        peak, verdict = _peak_risk(base, [bad] * 8)
        rows.append((delta, peak, verdict))
    graded = all(rows[k][1] >= rows[k - 1][1] - 1e-9 for k in range(1, len(rows)))
    escalates = any(v != "safe" for _, _, v in rows)
    return {"rows": rows, "graded": graded, "escalates": escalates}


def probe_time_shuffle() -> dict:
    """Same obs multiset, different order: a trajectory-aware policy differs."""
    base = _baselined_healthy()
    e0, i0, s0 = HEALTHY
    healthy = [(e0, i0, s0)] * 10
    burst = [(0.25, 0.30, 0.85)] * 5
    concentrated_late = healthy + burst          # calm, then a sharp burst
    spread_early = burst + healthy               # burst first, then recovery
    # interleaved: same multiset, dispersed
    interleaved = []
    bi = 0
    for k in range(15):
        if k % 3 == 0 and bi < len(burst):
            interleaved.append(burst[bi]); bi += 1
        else:
            interleaved.append(healthy[k % len(healthy)])
    # pad interleaved to include all burst points
    while bi < len(burst):
        interleaved.append(burst[bi]); bi += 1

    late = _peak_risk(base, concentrated_late)
    early = _peak_risk(base, spread_early)
    inter = _peak_risk(base, interleaved)
    spread = max(late[0], early[0], inter[0]) - min(late[0], early[0], inter[0])
    return {
        "concentrated_late": late,
        "spread_early": early,
        "interleaved": inter,
        "risk_spread": spread,
        "order_sensitive": spread > 0.02,
    }


def build_report(mono: dict, inj: dict, shuf: dict) -> str:
    a: list[str] = []
    a.append("# EISV Policy Stress-Test (label-free)\n")
    a.append("Drives the real `assess_behavioral_state` policy on synthetic "
             "check-in replays. No outcome labels; truth signals are constructed "
             "perturbations, so no Invariant-4 circularity.\n")

    a.append("## Probe 1 — Monotonicity / consistency (fixed-threshold path)")
    a.append("Risk must not DECREASE as the state strictly worsens.")
    a.append("| S (drift, worse→) | risk |   | I (integrity, worse→) | risk |")
    a.append("|---:|---:|---|---:|---:|")
    for (sv, sr), (iv, ir) in zip(mono["rows_S"], mono["rows_I"]):
        a.append(f"| {sv:.2f} | {sr:.3f} |   | {iv:.2f} | {ir:.3f} |")
    a.append(f"\n- monotone in S (drift up): **{'PASS' if mono['mono_S'] else 'FAIL'}**")
    a.append(f"- monotone in I (integrity down): **{'PASS' if mono['mono_I'] else 'FAIL'}**")

    a.append("\n## Probe 2 — Regime-injection recall (graded)")
    a.append("Warm healthy, then inject a sustained deterioration of rising severity.")
    a.append("| severity δ | peak risk | verdict |")
    a.append("|---:|---:|---|")
    for d, pk, v in inj["rows"]:
        a.append(f"| {d:.1f} | {pk:.3f} | {v} |")
    a.append(f"\n- peak risk rises monotonically with severity: "
             f"**{'PASS' if inj['graded'] else 'FAIL'}**")
    a.append(f"- a constructed deterioration escalates the verdict off `safe`: "
             f"**{'PASS' if inj['escalates'] else 'FAIL'}**")

    a.append("\n## Probe 3 — Time-shuffle sensitivity (trajectory awareness)")
    a.append("Same observation multiset, different order. A trajectory-aware "
             "policy should NOT be order-invariant.")
    a.append("| ordering | peak risk | final verdict |")
    a.append("|---|---:|---|")
    a.append(f"| concentrated-late burst | {shuf['concentrated_late'][0]:.3f} | "
             f"{shuf['concentrated_late'][1]} |")
    a.append(f"| spread-early burst | {shuf['spread_early'][0]:.3f} | "
             f"{shuf['spread_early'][1]} |")
    a.append(f"| interleaved | {shuf['interleaved'][0]:.3f} | "
             f"{shuf['interleaved'][1]} |")
    a.append(f"\n- peak-risk spread across orderings: {shuf['risk_spread']:.3f}")
    a.append(f"- policy is order-sensitive (uses trajectory, not just the EMA "
             f"endpoint): **{'PASS' if shuf['order_sensitive'] else 'FAIL — effectively memoryless'}**")

    a.append("\n## Reading")
    a.append(
        "These are necessary coherence properties of the policy, validated without "
        "any outcome labels: it should be monotone in obvious badness, detect a "
        "constructed deterioration in a graded way, and care about order if it "
        "claims to be a trajectory governor. They do NOT establish that the policy "
        "predicts real bad outcomes (that needs exogenous labels — see "
        "eisv_skeptic_report.py); they establish whether it is internally coherent "
        "and responsive, which is a precondition the outcome eval cannot check."
    )
    return "\n".join(a) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", help="optional markdown output path")
    args = p.parse_args(argv)
    report = build_report(probe_monotonicity(), probe_injection_recall(), probe_time_shuffle())
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report)
        print(f"wrote {path}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
