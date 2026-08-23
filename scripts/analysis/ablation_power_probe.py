#!/usr/bin/env python3
"""Measure what the EISV ablation harness could have detected.

`eisv_ablation_matrix.py` answers "did the selected candidate separate from its
best-of-candidates null?" A `NOISE-LEVEL` answer has two very different causes,
and the matrix cannot tell them apart:

* there is no association between prior state and outcome, or
* there is one, and this cohort is too small for this instrument to see it.

This probe supplies the missing half. It plants an association of known strength
into synthetic cohorts shaped like a real slice, runs the SAME selective-null
machinery the matrix runs, and reports how often that machinery reaches
`selective p <= alpha`. That rate is the instrument's power; the smallest planted
effect it detects reliably is the minimum detectable effect (MDE).

Read the output as an UPPER BOUND on a real slice's power. The synthetic cohort
is deliberately generous: every prior-state feature carries the planted signal,
the outcome depends on prior state alone, and there is no measurement noise,
missingness, or provenance drift. A real cohort has all of those, so its power is
at most what this reports. The `Null max median` column is what makes the bound
checkable -- compare it with the null median of the slice being interpreted; a
real slice with a WIDER null has strictly less power than the row shown here.

What this is not: it is not a read of production data, does not query any
database, and does not touch the pre-registered 2026-12-01 confirmatory read in
`docs/proposals/eisv-outcome-grounding-stop-rule-v0.md`. It changes no threshold,
date, or PASS condition there. It characterises the instrument that read will
use, so a FAIL can be reported as "no effect detected at power X" instead of the
stronger "no effect".

Usage:
    python3 scripts/analysis/ablation_power_probe.py
    python3 scripts/analysis/ablation_power_probe.py --trials 30 --resamples 200
    python3 scripts/analysis/ablation_power_probe.py --rows 224 --clusters 70 --bad 53
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_ablation_matrix import build_matrix_row
from scripts.analysis.eisv_skeptic_report import OutcomeRow, auc_score

#: Shape of the frozen 2026-08-09 trusted-anchor slice, the cohort every current
#: public statement about predictive lift is drawn from.
FROZEN_ROWS = 224
FROZEN_BAD = 53
FROZEN_AGENTS = 16

#: Cluster-level effect sizes to sweep. beta is the log-odds shift per unit of
#: the cluster latent; the realised row-level AUC is measured, not assumed.
DEFAULT_BETAS = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5)

_EPOCH = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(frozen=True)
class PowerRow:
    """One planted effect size and how often the harness recovered it."""

    beta: float
    trials: int
    true_auc: float
    baseline_auc: float | None
    median_delta: float
    null_median: float | None
    null_p95: float | None
    median_p: float
    power: float


def synthesize_cohort(
    rng: random.Random,
    *,
    beta: float,
    rows: int = FROZEN_ROWS,
    clusters: int = 70,
    agents: int = FROZEN_AGENTS,
    bad_rate: float = FROZEN_BAD / FROZEN_ROWS,
) -> list[OutcomeRow]:
    """Build one cohort whose prior state predicts outcome with strength `beta`.

    The latent is drawn per `(agent, prior-state snapshot)` cluster, matching the
    real dependence structure: prior state is constant within a cluster, so the
    association can only live at cluster granularity. Every EISV coordinate is a
    monotone function of that one latent, which is the most favourable case for
    the harness -- all ~7 candidate models see the same clean signal.
    """
    sizes = [1] * clusters
    for _ in range(max(0, rows - clusters)):
        sizes[rng.randrange(clusters)] += 1

    alpha = math.log(bad_rate / (1.0 - bad_rate))
    out: list[OutcomeRow] = []
    stamp = _EPOCH
    for cluster_id, size in enumerate(sizes):
        latent = rng.gauss(0.0, 1.0)
        risk = _sigmoid(latent)
        p_bad = _sigmoid(alpha + beta * latent)
        agent = f"agent-{cluster_id % agents}"
        for _ in range(size):
            stamp += timedelta(minutes=7)
            is_bad = rng.random() < p_bad
            out.append(
                OutcomeRow(
                    ts=stamp,
                    agent_id=agent,
                    outcome_type="test_failed" if is_bad else "test_passed",
                    is_bad=is_bad,
                    outcome_score=0.0 if is_bad else 1.0,
                    verification_source="external_signal",
                    reported_confidence=0.8,
                    reported_complexity=0.5,
                    detail={},
                    prior_state_age_seconds=60.0,
                    prior_risk=risk,
                    prior_phi=1.0 - risk,
                    prior_verdict="safe",
                    prior_coherence=1.0 - risk,
                    prior_e=1.0 - risk,
                    prior_i=1.0 - risk,
                    prior_s=risk,
                    prior_v=risk,
                    snapshot_verdict="safe",
                    snapshot_e=None,
                    snapshot_i=None,
                    snapshot_s=None,
                    snapshot_v=None,
                    snapshot_phi=None,
                    snapshot_coherence=None,
                    prior_measurement_id=f"snapshot-{cluster_id}",
                    row_key=f"cluster-{cluster_id}",
                    n_prior_snapshots=6,
                    prior_s_disp=risk,
                    prior_e_disp=risk,
                    prior_i_disp=risk,
                    prior_v_disp=risk,
                    prior_risk_disp=risk,
                )
            )
    return out


def planted_auc(rows: Sequence[OutcomeRow]) -> float | None:
    """Row-level AUC of the planted feature -- the effect the harness must find."""
    return auc_score(
        [row.is_bad for row in rows],
        [row.prior_risk if row.prior_risk is not None else 0.0 for row in rows],
    )


def measure_power(
    *,
    beta: float,
    trials: int,
    resamples: int,
    rows: int,
    clusters: int,
    agents: int,
    alpha: float,
    seed: int,
) -> PowerRow | None:
    """Run `trials` synthetic cohorts at one effect size through the real harness."""
    ps: list[float] = []
    aucs: list[float] = []
    baselines: list[float] = []
    deltas: list[float] = []
    null_medians: list[float] = []
    null_p95s: list[float] = []

    for trial in range(trials):
        rng = random.Random((seed, beta, trial).__hash__() & 0xFFFFFFFF)
        cohort = synthesize_cohort(
            rng, beta=beta, rows=rows, clusters=clusters, agents=agents
        )
        realised = planted_auc(cohort)
        row = build_matrix_row(
            cohort,
            scope="task",
            window_days=90,
            lead_minutes=0,
            selective_null_resamples=resamples,
            uncertainty_seed=trial,
        )
        if row.selective_p is None or realised is None or row.best_auc_delta is None:
            continue
        ps.append(row.selective_p)
        aucs.append(realised)
        deltas.append(row.best_auc_delta)
        if row.baseline_auc is not None:
            baselines.append(row.baseline_auc)
        if row.selective_null_median is not None:
            null_medians.append(row.selective_null_median)
        if row.selective_null_p95 is not None:
            null_p95s.append(row.selective_null_p95)

    if not ps:
        return None
    return PowerRow(
        beta=beta,
        trials=len(ps),
        true_auc=statistics.median(aucs),
        baseline_auc=statistics.median(baselines) if baselines else None,
        median_delta=statistics.median(deltas),
        null_median=statistics.median(null_medians) if null_medians else None,
        null_p95=statistics.median(null_p95s) if null_p95s else None,
        median_p=statistics.median(ps),
        power=sum(1 for p in ps if p <= alpha) / len(ps),
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def format_report(
    results: Sequence[PowerRow],
    *,
    trials: int,
    resamples: int,
    rows: int,
    clusters: int,
    alpha: float,
) -> str:
    header = [
        "# EISV ablation harness — measured power",
        "",
        f"Synthetic cohorts of {rows} rows in {clusters} permutable clusters, "
        f"{trials} trials per effect size, {resamples} permutations per trial, "
        f"alpha = {alpha}.",
        "",
        "`Power` is the fraction of trials in which the harness reached "
        "`selective p <= alpha` on a cohort that genuinely contained the planted "
        "effect. It is an UPPER BOUND on a real slice's power: here every "
        "prior-state feature carries the signal cleanly and nothing else does. "
        "Compare `Null max median` with the slice you are interpreting — a real "
        "slice with a wider null has strictly less power than the row shown.",
        "",
        "The `beta = 0` row is the type-I check: power there should sit at or "
        "below alpha, and a value near alpha means the null is calibrated.",
        "",
        "| Planted beta | True AUC | Baseline AUC | Median AUC delta | Null max median | Null max p95 | Median selective p | Power |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        header.append(
            f"| {row.beta:.2f} | {_fmt(row.true_auc)} | {_fmt(row.baseline_auc)} "
            f"| {_fmt(row.median_delta)} | {_fmt(row.null_median)} "
            f"| {_fmt(row.null_p95)} | {_fmt(row.median_p)} | {row.power:.2f} |"
        )
    return "\n".join(header) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, default=25, help="Cohorts per effect size.")
    parser.add_argument(
        "--resamples",
        type=int,
        default=200,
        help="Permutations per cohort; 200 matches the frozen 2026-08-09 run.",
    )
    parser.add_argument("--rows", type=int, default=FROZEN_ROWS, help="Rows per cohort.")
    parser.add_argument(
        "--clusters",
        type=int,
        default=70,
        help=(
            "Permutable (agent, prior-state snapshot) clusters per cohort. Pick a "
            "count whose null is no wider than the slice being interpreted, so the "
            "reported power stays an upper bound."
        ),
    )
    parser.add_argument("--agents", type=int, default=FROZEN_AGENTS, help="Distinct agents.")
    parser.add_argument("--alpha", type=float, default=0.05, help="Significance threshold.")
    parser.add_argument("--seed", type=int, default=0, help="Deterministic seed.")
    parser.add_argument(
        "--betas",
        type=lambda raw: tuple(float(part) for part in raw.split(",") if part.strip()),
        default=DEFAULT_BETAS,
        help="Comma-separated cluster-level log-odds effect sizes to sweep.",
    )
    parser.add_argument("--output", help="Optional markdown output path.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    results = [
        result
        for beta in args.betas
        if (
            result := measure_power(
                beta=beta,
                trials=args.trials,
                resamples=args.resamples,
                rows=args.rows,
                clusters=args.clusters,
                agents=args.agents,
                alpha=args.alpha,
                seed=args.seed,
            )
        )
        is not None
    ]
    report = format_report(
        results,
        trials=args.trials,
        resamples=args.resamples,
        rows=args.rows,
        clusters=args.clusters,
        alpha=args.alpha,
    )
    if args.output:
        Path(args.output).write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
