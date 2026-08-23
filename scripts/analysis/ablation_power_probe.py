#!/usr/bin/env python3
"""Measure what the EISV ablation harness could have detected.

`eisv_ablation_matrix.py` answers "did the selected candidate separate from its
best-of-candidates null?" A `NON_DETECTION` answer has two very different causes,
and the matrix cannot tell them apart:

* there is no association between prior state and outcome, or
* there is one, and this cohort is too small for this instrument to see it.

This probe supplies the missing half. It plants an association of known strength
into synthetic cohorts with a requested row/class/cluster shape, runs the SAME
selective-null machinery the matrix runs, and reports how often that machinery
reaches `selective p <= alpha`. That rate is a Monte Carlo sensitivity estimate
for this synthetic data-generating process. It is not a minimum detectable effect
until a relevant effect and target power have been declared independently.

Read the output as an optimistic scenario, not a proven upper bound on a real
slice's power. The synthetic cohort is deliberately generous: most prior-state
features carry the planted signal, the outcome depends on prior state alone, and
there is no measurement noise, missingness, or provenance drift. Those choices
usually make detection easier, but they do not prove stochastic dominance over
every real data-generating process. The null-width columns are diagnostics for
comparison with a real slice; a wider median alone does not prove an ordering of
power because power depends on the full joint distribution.

What this is not: it is not a read of production data, does not query any
database, and does not touch the pre-registered 2026-12-01 confirmatory read in
`docs/proposals/eisv-outcome-grounding-stop-rule-v0.md`. It changes no threshold,
date, or PASS condition there. It characterises the instrument that read will
use, so a FAIL can be reported as "no effect detected at power X" instead of the
stronger "no effect".

Usage:
    python3 scripts/analysis/ablation_power_probe.py \
        --rows 224 --bad 53 --clusters 70 --agents 16
    python3 scripts/analysis/ablation_power_probe.py \
        --rows 224 --bad 53 --clusters 70 --agents 16 \
        --trials 30 --resamples 200
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

#: Known marginals of the frozen 2026-08-09 trusted-anchor slice. They are not a
#: complete shape: its historical transcription omitted the total null-cluster
#: count and cluster-size distribution.
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
    # `trials` is the requested denominator. A cohort the harness cannot score
    # is a failed detection, not a trial that disappears from the estimate.
    trials: int
    valid_trials: int
    detections: int
    true_auc: float | None
    baseline_auc: float | None
    median_delta: float | None
    null_median: float | None
    null_p95: float | None
    median_p: float | None
    power: float
    power_ci_low: float
    power_ci_high: float


def _validate_cohort_shape(
    *,
    rows: int,
    clusters: int,
    agents: int,
    bad_rate: float,
) -> None:
    if rows < 2:
        raise ValueError("rows must be at least 2")
    if not 1 <= clusters <= rows:
        raise ValueError("clusters must be between 1 and rows")
    if not 1 <= agents <= clusters:
        raise ValueError("agents must be between 1 and clusters")
    if not 0.0 < bad_rate < 1.0:
        raise ValueError("bad rate must be strictly between 0 and 1")


def _wilson_interval(successes: int, total: int) -> tuple[float, float]:
    """Return the two-sided 95% Wilson interval for a binomial proportion."""
    if total <= 0:
        raise ValueError("total must be positive")
    z = statistics.NormalDist().inv_cdf(0.975)
    proportion = successes / total
    denominator = 1.0 + (z * z / total)
    centre = (proportion + z * z / (2.0 * total)) / denominator
    half_width = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return max(0.0, centre - half_width), min(1.0, centre + half_width)


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
    monotone function of that one latent, which is a favourable case for the
    harness: the continuous/binned prior-state candidates see the same clean
    signal, while the previous-outcome baseline and constant verdict do not.
    """
    _validate_cohort_shape(
        rows=rows,
        clusters=clusters,
        agents=agents,
        bad_rate=bad_rate,
    )

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
        for member_index in range(size):
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
                    # Production rows pair models by unique outcome_id. Reusing
                    # a cluster key here makes the baseline index overwrite rows
                    # and silently corrupts the candidate/baseline comparison.
                    row_key=f"outcome-{cluster_id}-{member_index}",
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
    bad: int,
    clusters: int,
    agents: int,
    alpha: float,
    seed: int,
) -> PowerRow:
    """Run `trials` synthetic cohorts at one effect size through the real harness."""
    if trials <= 0:
        raise ValueError("trials must be positive")
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    if not 0 < bad < rows:
        raise ValueError("bad must be greater than 0 and smaller than rows")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")
    _validate_cohort_shape(
        rows=rows,
        clusters=clusters,
        agents=agents,
        bad_rate=bad / rows,
    )
    ps: list[float] = []
    aucs: list[float] = []
    baselines: list[float] = []
    deltas: list[float] = []
    null_medians: list[float] = []
    null_p95s: list[float] = []

    for trial in range(trials):
        rng = random.Random((seed, beta, trial).__hash__() & 0xFFFFFFFF)
        cohort = synthesize_cohort(
            rng,
            beta=beta,
            rows=rows,
            clusters=clusters,
            agents=agents,
            bad_rate=bad / rows,
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
        if realised is not None:
            # The planted effect describes every generated cohort with both
            # classes, not only the subset the harness happened to score.
            aucs.append(realised)
        if row.selective_p is None or row.best_auc_delta is None:
            continue
        ps.append(row.selective_p)
        deltas.append(row.best_auc_delta)
        if row.baseline_auc is not None:
            baselines.append(row.baseline_auc)
        if row.selective_null_median is not None:
            null_medians.append(row.selective_null_median)
        if row.selective_null_p95 is not None:
            null_p95s.append(row.selective_null_p95)

    detections = sum(1 for p in ps if p <= alpha)
    power = detections / trials
    power_ci_low, power_ci_high = _wilson_interval(detections, trials)
    return PowerRow(
        beta=beta,
        trials=trials,
        valid_trials=len(ps),
        detections=detections,
        true_auc=statistics.median(aucs) if aucs else None,
        baseline_auc=statistics.median(baselines) if baselines else None,
        median_delta=statistics.median(deltas) if deltas else None,
        null_median=statistics.median(null_medians) if null_medians else None,
        null_p95=statistics.median(null_p95s) if null_p95s else None,
        median_p=statistics.median(ps) if ps else None,
        power=power,
        power_ci_low=power_ci_low,
        power_ci_high=power_ci_high,
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def format_report(
    results: Sequence[PowerRow],
    *,
    trials: int,
    resamples: int,
    rows: int,
    bad: int,
    clusters: int,
    agents: int,
    alpha: float,
) -> str:
    header = [
        "# EISV ablation harness — measured power",
        "",
        f"Synthetic cohorts of {rows} rows ({bad} expected bad) in {clusters} "
        f"permutable clusters across {agents} agents, "
        f"{trials} trials per effect size, {resamples} permutations per trial, "
        f"alpha = {alpha}.",
        "",
        "`Power` is the fraction of all requested trials in which the harness reached "
        "`selective p <= alpha` on a cohort that genuinely contained the planted "
        "effect. Unscorable trials count as failed detections and remain visible "
        "in `Scorable / requested`. The interval is a 95% Wilson interval over "
        "the requested trials.",
        "",
        "This is an optimistic synthetic scenario, not a proven upper bound on a "
        "real slice. Compare the null-width columns as diagnostics only; a wider "
        "null median by itself does not establish a strict ordering of power.",
        "",
        "The `beta = 0` row is the type-I check: its interval should be compatible "
        "with alpha. With a small trial count, the point estimate need not land at "
        "or below alpha.",
        "",
        "| Planted beta | Scorable / requested | Detections / requested | True AUC | Baseline AUC | Median AUC delta | Null max median | Null max p95 | Median selective p | Power (95% Wilson CI) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        header.append(
            f"| {row.beta:.2f} | {row.valid_trials} / {row.trials} "
            f"| {row.detections} / {row.trials} | {_fmt(row.true_auc)} "
            f"| {_fmt(row.baseline_auc)} "
            f"| {_fmt(row.median_delta)} | {_fmt(row.null_median)} "
            f"| {_fmt(row.null_p95)} | {_fmt(row.median_p)} | {row.power:.2f} "
            f"[{row.power_ci_low:.2f}, {row.power_ci_high:.2f}] |"
        )
    return "\n".join(header) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--trials", type=int, default=25, help="Cohorts per effect size."
    )
    parser.add_argument(
        "--resamples",
        type=int,
        default=200,
        help="Permutations per cohort; 200 matches the frozen 2026-08-09 run.",
    )
    parser.add_argument("--rows", type=int, required=True, help="Rows per cohort.")
    parser.add_argument(
        "--bad",
        type=int,
        required=True,
        help="Expected bad-class rows; sets the simulated class balance.",
    )
    parser.add_argument(
        "--clusters",
        type=int,
        required=True,
        help=(
            "Permutable (agent, prior-state snapshot) clusters per cohort. Use the "
            "observed slice's count when available; the count alone does not recover "
            "its cluster-size distribution."
        ),
    )
    parser.add_argument("--agents", type=int, required=True, help="Distinct agents.")
    parser.add_argument(
        "--alpha", type=float, default=0.05, help="Significance threshold."
    )
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
    try:
        results = [
            measure_power(
                beta=beta,
                trials=args.trials,
                resamples=args.resamples,
                rows=args.rows,
                bad=args.bad,
                clusters=args.clusters,
                agents=args.agents,
                alpha=args.alpha,
                seed=args.seed,
            )
            for beta in args.betas
        ]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = format_report(
        results,
        trials=args.trials,
        resamples=args.resamples,
        rows=args.rows,
        bad=args.bad,
        clusters=args.clusters,
        agents=args.agents,
        alpha=args.alpha,
    )
    if args.output:
        Path(args.output).write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
