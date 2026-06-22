#!/usr/bin/env python3
"""Run the sandbagging detection eval and print an auditable report.

    python run_demo.py            # default population + seed
    python run_demo.py --seed 7   # reproducible variation
    python run_demo.py --json     # machine-readable summary

Every number printed is reproducible from the seed with no network access.
"""

from __future__ import annotations

import argparse
import json
import random

from eval_harness import evaluate_population, summarize
from model_organism import default_population


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0, help="RNG seed (reproducible)")
    parser.add_argument("--tasks", type=int, default=240, help="tasks per framing per agent")
    parser.add_argument("--json", action="store_true", help="emit JSON summary only")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    population = default_population(rng)
    results = evaluate_population(population, n_tasks=args.tasks, seed=args.seed)
    summary = summarize(results)

    if args.json:
        print(json.dumps({
            "seed": args.seed,
            "tasks_per_framing": args.tasks,
            "auc": round(summary.auc, 4),
            "threshold_z": summary.threshold_z,
            "true_positive_rate": round(summary.true_positive_rate, 4),
            "false_positive_rate": round(summary.false_positive_rate, 4),
            "naive_capability": round(summary.naive_capability, 4),
            "recovered_capability": round(summary.recovered_capability, 4),
        }, indent=2))
        return 0

    print(f"\nSandbagging detection eval  (seed={args.seed}, {args.tasks} tasks/framing)\n")
    print(f"  {'agent':<16}{'truth':>8}{'deploy':>8}{'eval':>8}{'gap':>8}{'z':>8}  flag")
    print("  " + "-" * 64)
    for r in sorted(results, key=lambda x: x.z, reverse=True):
        flag = "CONCEAL" if r.z >= summary.threshold_z else ""
        mark = "" if (r.z >= summary.threshold_z) == r.is_sandbagger else "  <-- error"
        print(
            f"  {r.name:<16}{r.true_competence:>8.2f}{r.deployment_rate:>8.2f}"
            f"{r.evaluated_rate:>8.2f}{r.concealment_gap:>+8.2f}{r.z:>8.2f}  {flag}{mark}"
        )

    print("\n  Detection (concealers vs honest controls)")
    print(f"    ROC AUC ................. {summary.auc:.3f}")
    print(f"    flag threshold .......... z >= {summary.threshold_z:.3f}  (one-sided 95%)")
    print(f"    true-positive rate ...... {summary.true_positive_rate:.0%}  (sandbaggers caught)")
    print(f"    false-positive rate ..... {summary.false_positive_rate:.0%}  (honest mis-flagged)")

    undercount = summary.recovered_capability - summary.naive_capability
    print("\n  Capability undercount on the concealing agents")
    print(f"    naive eval-frame number . {summary.naive_capability:.2f}   <- what a single-framing eval reports")
    print(f"    unmonitored number ...... {summary.recovered_capability:.2f}   <- true capability")
    print(f"    undercount .............. {undercount:+.2f}   ({undercount / max(summary.naive_capability, 1e-9):+.0%} of the reported number)")
    print("\n  Takeaway: a single-framing capability eval understates the concealing")
    print("  agents' true capability; the monitored/unmonitored gap both recovers it")
    print("  and flags the concealment.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
