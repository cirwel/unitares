#!/usr/bin/env python3
"""Compare UNITARES's Integrity signal vs the cross-framing detector.

Runs three cohorts (honest, naive sandbaggers, calibrated sandbaggers) through
BOTH detectors and prints where each one works:

  - UNITARES Integrity (real `assess_behavioral_state` on real `BehavioralEISV`):
    catches the naive sandbagger (overconfident under eval), blind to the
    calibrated one (confidence lowered to match concealed performance).
  - Cross-framing comparison (monitored vs unmonitored success): catches both,
    because both conceal *capability* regardless of what they claim.

    python run_unitares_demo.py [--seed N] [--tasks N] [--json]

Uses real UNITARES code; touches no database and onboards no agents.
"""

from __future__ import annotations

import argparse
import json
import random

from eval_harness import evaluate_population, roc_auc
from model_organism import styled_population
from unitares_integration import cohort_auc, evaluate_with_unitares


def _external_cohort_auc(population, seed: int, cohort: str) -> float:
    """AUC of the cross-framing z-statistic separating a cohort from honest."""
    ext = evaluate_population(population, n_tasks=240, seed=seed)
    def cohort_of(name: str) -> str:
        if name.startswith("honest"):
            return "honest"
        return "naive" if "naive" in name else "calibrated"
    subset = [r for r in ext if cohort_of(r.name) in (cohort, "honest")]
    labels = [cohort_of(r.name) == cohort for r in subset]
    scores = [r.z for r in subset]
    return roc_auc(labels, scores)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tasks", type=int, default=120, help="check-ins per agent (UNITARES run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    population = styled_population(random.Random(args.seed))
    u_results = evaluate_with_unitares(population, n_tasks=args.tasks, seed=args.seed)

    u_naive = cohort_auc(u_results, "naive")
    u_calib = cohort_auc(u_results, "calibrated")
    x_naive = _external_cohort_auc(population, args.seed, "naive")
    x_calib = _external_cohort_auc(population, args.seed, "calibrated")

    if args.json:
        print(json.dumps({
            "seed": args.seed,
            "unitares_integrity_auc": {"naive": round(u_naive, 3), "calibrated": round(u_calib, 3)},
            "cross_framing_auc": {"naive": round(x_naive, 3), "calibrated": round(x_calib, 3)},
        }, indent=2))
        return 0

    def mean_I(cohort: str) -> float:
        vals = [r.final_integrity for r in u_results if r.cohort == cohort]
        return sum(vals) / len(vals)

    print(f"\nUNITARES Integrity vs cross-framing detection  (seed={args.seed}, {args.tasks} check-ins/agent)\n")
    print(f"  mean UNITARES Integrity (I) after the run:")
    print(f"    honest ............... {mean_I('honest'):.2f}")
    print(f"    naive sandbagger ..... {mean_I('naive'):.2f}   (overconfident under eval -> I falls)")
    print(f"    calibrated sandbagger  {mean_I('calibrated'):.2f}   (claims match concealed outcomes -> looks honest)\n")

    print(f"  Detection AUC (separating each sandbagger cohort from honest controls)")
    print(f"    {'cohort':<24}{'UNITARES I':>12}{'cross-framing':>16}")
    print("    " + "-" * 52)
    print(f"    {'naive sandbagger':<24}{u_naive:>12.2f}{x_naive:>16.2f}")
    print(f"    {'calibrated sandbagger':<24}{u_calib:>12.2f}{x_calib:>16.2f}")

    print("\n  Reading it:")
    print("    - UNITARES's calibration-based Integrity catches the naive sandbagger")
    print("      (AUC ~1.0) but is blind to the calibrated one (AUC ~0.5): a sandbagger")
    print("      that lowers its confidence to match looks exactly like an honest,")
    print("      less-capable agent to a runtime calibration signal.")
    print("    - The cross-framing comparison catches both, because it keys on concealed")
    print("      *capability* (the monitored/unmonitored success gap), not on whether the")
    print("      agent's claims are internally consistent.")
    print("    => The two signals are complementary; this is exactly where the runtime")
    print("       Integrity signal's coverage ends.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
