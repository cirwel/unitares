"""Guards for the ablation harness's power probe.

The probe exists to keep `NON_DETECTION` from being read as `no effect`. These
tests hold the two properties that make it usable as evidence: it recovers a
strong planted effect, and it does not manufacture one from noise.
"""

from __future__ import annotations

import random

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
    assert len({row.row_key for row in rows}) == 70
    # Prior state is constant within a cluster -- the property the null relies on.
    by_cluster: dict[str, set[float]] = {}
    for row in rows:
        by_cluster.setdefault(row.row_key or "", set()).add(row.prior_risk or 0.0)
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


def test_probe_does_not_invent_signal_when_none_was_planted():
    """Type-I check: the harness must not clear its own null on pure noise."""
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
    assert result.power == 0.0


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


def test_report_states_the_upper_bound_reading_and_the_null_width():
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
    assert "UPPER BOUND" in report
    # Null width is what makes the upper-bound claim checkable against a real slice.
    assert "Null max median" in report
    assert "Power" in report
    assert f"{FROZEN_BAD} expected bad" in report


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
    assert getattr(parse_args(argv), attr) == expected


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
