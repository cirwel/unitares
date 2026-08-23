"""Guards for the ablation harness's power probe.

The probe exists to keep `NON_DETECTION` from being read as `no effect`. These
tests hold the two properties that make it usable as evidence: it recovers a
strong planted effect, and it does not manufacture one from noise.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from scripts.analysis.ablation_power_probe import (
    FROZEN_BAD,
    FROZEN_ROWS,
    format_report,
    measure_power,
    planted_auc,
    parse_args,
    synthesize_cohort,
)


def test_synthetic_cohort_matches_the_requested_shape():
    rows = synthesize_cohort(random.Random(0), beta=1.0, rows=FROZEN_ROWS, clusters=70)
    assert len(rows) == FROZEN_ROWS
    # row_key is the production-shaped outcome identity used to pair model
    # scores; cluster identity belongs in prior_measurement_id.
    assert len({row.row_key for row in rows}) == FROZEN_ROWS
    assert len({row.prior_measurement_id for row in rows}) == 70
    # Prior state is constant within a cluster -- the property the null relies on.
    by_cluster: dict[str, set[float]] = {}
    for row in rows:
        by_cluster.setdefault(row.prior_measurement_id or "", set()).add(
            row.prior_risk or 0.0
        )
    assert all(len(values) == 1 for values in by_cluster.values())


def test_planted_effect_is_actually_present_at_the_requested_strength():
    """beta scales the association; beta=0 must leave the feature uninformative."""
    flat = [
        planted_auc(synthesize_cohort(random.Random(seed), beta=0.0))
        for seed in range(12)
    ]
    strong = [
        planted_auc(synthesize_cohort(random.Random(seed), beta=1.5))
        for seed in range(12)
    ]
    assert 0.42 < sum(flat) / len(flat) < 0.58
    assert sum(strong) / len(strong) > 0.70


@pytest.mark.parametrize("beta", [0.0, 0.75, 1.5, -1.5])
def test_intercept_calibration_preserves_expected_class_balance(beta):
    """Changing effect strength must not silently change outcome prevalence."""
    import scripts.analysis.ablation_power_probe as probe

    latents = [-1.8, -0.4, 0.2, 1.3]
    sizes = [3, 11, 5, 8]
    target = 0.237
    intercept = probe._calibrate_intercept(
        beta=beta,
        latents=latents,
        sizes=sizes,
        target_bad_rate=target,
    )
    realised_expectation = sum(
        size * probe._sigmoid(intercept + beta * latent)
        for size, latent in zip(sizes, latents, strict=True)
    ) / sum(sizes)

    assert realised_expectation == pytest.approx(target, abs=1e-12)


def test_synthetic_cohort_keeps_prevalence_fixed_at_a_strong_effect():
    """Integration guard: the calibrated intercept must be wired into generation."""
    target = FROZEN_BAD / FROZEN_ROWS
    rows = synthesize_cohort(
        random.Random(17),
        beta=3.0,
        rows=20_000,
        clusters=500,
        agents=16,
        bad_rate=target,
    )

    observed = sum(row.is_bad for row in rows) / len(rows)
    assert observed == pytest.approx(target, abs=0.015)


def test_probe_does_not_invent_signal_when_none_was_planted():
    """A small null run must remain compatible with the nominal type-I rate."""
    result = measure_power(
        beta=0.0,
        trials=6,
        resamples=40,
        rows=FROZEN_ROWS,
        bad=FROZEN_BAD,
        clusters=70,
        agents=16,
        alpha=0.05,
        seed=3,
    )
    assert result is not None
    assert result.true_auc == pytest.approx(0.5, abs=0.08)
    assert result.power_ci_low <= 0.05 <= result.power_ci_high


def test_probe_recovers_a_strong_planted_effect():
    """A large effect must be detectable, or the probe measures nothing."""
    result = measure_power(
        beta=2.0,
        trials=6,
        resamples=40,
        rows=FROZEN_ROWS,
        bad=FROZEN_BAD,
        clusters=70,
        agents=16,
        alpha=0.05,
        seed=3,
    )
    assert result is not None
    assert result.power >= 0.5
    assert result.true_auc > 0.75


def test_report_discloses_scenario_limits_trial_attrition_and_uncertainty():
    result = measure_power(
        beta=1.0,
        trials=2,
        resamples=20,
        rows=FROZEN_ROWS,
        bad=FROZEN_BAD,
        clusters=70,
        agents=16,
        alpha=0.05,
        seed=1,
    )
    assert result is not None
    report = format_report(
        [result],
        trials=2,
        resamples=20,
        rows=FROZEN_ROWS,
        bad=FROZEN_BAD,
        clusters=70,
        agents=16,
        alpha=0.05,
    )
    assert "not a proven upper bound" in report
    assert "Scorable / requested" in report
    assert "95% Wilson" in report
    assert f"{result.valid_trials} / {result.trials}" in report
    # Null width remains a useful diagnostic, but no longer purports to prove a bound.
    assert "Null max median" in report
    assert "Power" in report
    assert f"{FROZEN_BAD} expected bad" in report


def test_unscorable_trials_remain_in_the_power_denominator(monkeypatch):
    """A sparse cohort is a non-detection, not permission to shrink n."""
    import scripts.analysis.ablation_power_probe as probe

    rows = iter(
        [
            SimpleNamespace(
                selective_p=0.01,
                best_auc_delta=0.2,
                baseline_auc=0.5,
                selective_null_median=0.1,
                selective_null_p95=0.3,
            ),
            SimpleNamespace(
                selective_p=None,
                best_auc_delta=None,
                baseline_auc=None,
                selective_null_median=None,
                selective_null_p95=None,
            ),
            SimpleNamespace(
                selective_p=0.20,
                best_auc_delta=0.1,
                baseline_auc=0.5,
                selective_null_median=0.1,
                selective_null_p95=0.3,
            ),
            SimpleNamespace(
                selective_p=None,
                best_auc_delta=None,
                baseline_auc=None,
                selective_null_median=None,
                selective_null_p95=None,
            ),
        ]
    )
    monkeypatch.setattr(probe, "build_matrix_row", lambda *_args, **_kwargs: next(rows))

    result = probe.measure_power(
        beta=1.0,
        trials=4,
        resamples=10,
        rows=80,
        bad=20,
        clusters=40,
        agents=8,
        alpha=0.05,
        seed=4,
    )

    assert result.trials == 4
    assert result.valid_trials == 2
    assert result.detections == 1
    assert result.power == 0.25
    assert result.power_ci_low < result.power < result.power_ci_high


def test_probe_never_reads_a_database():
    """The probe must stay runnable with no deployment and no credentials."""
    import scripts.analysis.ablation_power_probe as probe

    source = probe.__file__
    with open(source) as handle:
        text = handle.read()
    assert "DATABASE_URL" not in text
    assert "fetch_rows" not in text


@pytest.mark.parametrize(
    "argv,attr,expected",
    [
        (["--trials", "7"], "trials", 7),
        (["--resamples", "50"], "resamples", 50),
        (["--bad", "17"], "bad", 17),
        (["--betas", "0,0.5,2"], "betas", (0.0, 0.5, 2.0)),
    ],
)
def test_parse_args_reads_the_sweep_controls(argv, attr, expected):
    shape = ["--rows", "80", "--bad", "20", "--clusters", "40", "--agents", "8"]
    assert getattr(parse_args([*shape, *argv]), attr) == expected


def test_parse_args_requires_the_observed_shape():
    """Unknown cluster geometry must not silently inherit a frozen default."""
    with pytest.raises(SystemExit):
        parse_args([])


@pytest.mark.parametrize("raw", ["", "nan", "inf", "nope"])
def test_parse_args_rejects_an_empty_or_nonfinite_effect_sweep(raw):
    shape = ["--rows", "80", "--bad", "20", "--clusters", "40", "--agents", "8"]
    with pytest.raises(SystemExit):
        parse_args([*shape, "--betas", raw])


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"trials": 0}, "trials must be positive"),
        ({"resamples": 0}, "resamples must be positive"),
        ({"clusters": 81}, "clusters must be between 1 and rows"),
        ({"agents": 41}, "agents must be between 1 and clusters"),
        ({"alpha": 1.0}, "alpha must be strictly between 0 and 1"),
    ],
)
def test_measure_power_rejects_invalid_design_shapes(overrides, error):
    args = {
        "beta": 1.0,
        "trials": 1,
        "resamples": 10,
        "rows": 80,
        "bad": 20,
        "clusters": 40,
        "agents": 8,
        "alpha": 0.05,
        "seed": 4,
    }
    args.update(overrides)
    with pytest.raises(ValueError, match=error):
        measure_power(**args)


def test_measure_power_uses_the_requested_bad_class_balance(monkeypatch):
    """A future read must not silently reuse the frozen August bad rate."""
    import scripts.analysis.ablation_power_probe as probe

    observed_bad_rates: list[float] = []
    original = probe.synthesize_cohort

    def capture_bad_rate(*args, bad_rate: float, **kwargs):
        observed_bad_rates.append(bad_rate)
        return original(*args, bad_rate=bad_rate, **kwargs)

    monkeypatch.setattr(probe, "synthesize_cohort", capture_bad_rate)
    probe.measure_power(
        beta=1.0,
        trials=1,
        resamples=10,
        rows=80,
        bad=20,
        clusters=40,
        agents=8,
        alpha=0.05,
        seed=4,
    )

    assert observed_bad_rates == [0.25]
