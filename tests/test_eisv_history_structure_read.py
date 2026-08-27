"""Deterministic checks for the history-structure read math.

No DB: drive the pure functions with hand-built series so the ACF (positional
and time-indexed), hour-of-day share and its chance floor, census, and report
rendering are verified on known inputs.
"""

import datetime as dt
import math
import random

from scripts.analysis.eisv_history_structure_read import (
    CensusRow,
    build_report,
    hour_of_day_chance_floor,
    hour_of_day_variance_share,
    hourly_means,
    lag_acf,
    percentile,
    sql_like,
    summarize_census,
    time_lag_acf,
)


def _hours(start: dt.datetime, n: int) -> list[dt.datetime]:
    return [start + dt.timedelta(hours=i) for i in range(n)]


def test_lag_acf_constant_series_is_undefined():
    assert lag_acf([0.5] * 50, 1) is None


def test_lag_acf_short_series_is_undefined():
    assert lag_acf([0.1, 0.2, 0.3], 1) is None


def test_lag_acf_alternating_series_is_negative():
    xs = [0.0, 1.0] * 50
    acf = lag_acf(xs, 1)
    assert acf is not None and acf < -0.9


def test_lag_acf_slow_ramp_is_positive():
    xs = [i / 100.0 for i in range(100)]
    acf = lag_acf(xs, 1)
    assert acf is not None and acf > 0.9


def test_time_lag_acf_period_24_signal_peaks_at_lag_24():
    keys = _hours(dt.datetime(2026, 8, 1), 24 * 7)
    values = [math.sin(2 * math.pi * i / 24) for i in range(len(keys))]
    result = time_lag_acf(keys, values, 24)
    assert result is not None
    acf24, pairs = result
    # Pair-normalized: no (n - lag)/n attenuation even over one week.
    assert acf24 > 0.99
    assert pairs == 24 * 6
    r12 = time_lag_acf(keys, values, 12)
    assert r12 is not None and r12[0] < -0.99


def test_time_lag_acf_measures_wall_clock_lag_across_gaps():
    # 3 days of signal, a 2-day gap, 3 more days. A positional estimator on
    # the compacted list would pair points 48 wall-clock hours apart; the
    # time-indexed one must keep pairing at exactly 24h and stay high.
    start = dt.datetime(2026, 8, 1)
    keys = _hours(start, 72) + _hours(start + dt.timedelta(hours=120), 72)
    values = [math.sin(2 * math.pi * k.timestamp() / 86400) for k in keys]
    result = time_lag_acf(keys, values, 24)
    assert result is not None
    acf24, pairs = result
    assert acf24 > 0.99
    # 48 within-block pairs per block; the 2-day gap contributes none at 24h.
    assert pairs == 96
    # The same series through the positional estimator is corrupted by the
    # gap: this is the defect time_lag_acf exists to avoid.
    positional = lag_acf(values, 24)
    assert positional is not None and positional < acf24 - 0.1


def test_time_lag_acf_too_few_pairs_is_undefined():
    keys = _hours(dt.datetime(2026, 8, 1), 25)
    values = [float(i % 5) for i in range(25)]
    assert time_lag_acf(keys, values, 24) is None


def test_time_lag_acf_zero_variance_is_undefined():
    keys = _hours(dt.datetime(2026, 8, 1), 100)
    assert time_lag_acf(keys, [0.7] * 100, 24) is None


def test_hour_of_day_share_pure_clock_is_near_one():
    hours = [i % 24 for i in range(24 * 10)]
    values = [math.sin(2 * math.pi * h / 24) for h in hours]
    share = hour_of_day_variance_share(hours, values)
    assert share is not None and share > 0.99


def test_hour_of_day_share_noise_sits_near_chance_floor():
    rng = random.Random(7)
    hours = [i % 24 for i in range(24 * 50)]
    values = [rng.gauss(0.0, 1.0) for _ in hours]
    share = hour_of_day_variance_share(hours, values)
    floor = hour_of_day_chance_floor(hours)
    assert share is not None and floor is not None
    # Pure noise produces a positive share of about the floor, not zero:
    # this pairing is why the report prints them side by side.
    assert share < 3 * floor


def test_hour_of_day_chance_floor_value():
    hours = [i % 24 for i in range(336)]
    floor = hour_of_day_chance_floor(hours)
    assert floor is not None
    assert abs(floor - 23 / 335) < 1e-12
    assert hour_of_day_chance_floor([3, 3, 3]) is None
    assert hour_of_day_chance_floor([1]) is None


def test_hour_of_day_share_invariant_to_whole_hour_shift():
    hours = [i % 24 for i in range(24 * 10)]
    values = [math.sin(2 * math.pi * h / 24) + 0.01 * (i % 5) for i, h in enumerate(hours)]
    shifted = [(h + 6) % 24 for h in hours]
    a = hour_of_day_variance_share(hours, values)
    b = hour_of_day_variance_share(shifted, values)
    assert a is not None and b is not None
    assert abs(a - b) < 1e-12


def test_hour_of_day_share_zero_variance_is_undefined():
    assert hour_of_day_variance_share([0, 1, 2], [0.5, 0.5, 0.5]) is None


def test_hourly_means_buckets_and_averages():
    base = dt.datetime(2026, 8, 27, 10, 0, 0)
    timestamps = [
        base,
        base + dt.timedelta(minutes=30),
        base + dt.timedelta(hours=2),  # hour 11 empty: dropped, not interpolated
    ]
    keys, means = hourly_means(timestamps, [0.2, 0.4, 0.9])
    assert len(keys) == 2
    assert keys[0].hour == 10 and keys[1].hour == 12
    assert abs(means[0] - 0.3) < 1e-12
    assert means[1] == 0.9


def test_hourly_means_skips_none_values():
    base = dt.datetime(2026, 8, 27, 10, 0, 0)
    keys, means = hourly_means([base, base], [None, 0.6])
    assert len(keys) == 1 and means[0] == 0.6


def test_percentile_median_and_extremes():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert percentile(xs, 0.5) == 2.5
    assert percentile(xs, 0.0) == 1.0
    assert percentile(xs, 1.0) == 4.0
    assert percentile([5.0], 0.9) == 5.0


def test_census_summary_buckets_percentiles_and_top_share():
    rows = [
        CensusRow(1, "a", 2, 10.0),
        CensusRow(2, "b", 2, 20.0),
        CensusRow(3, "c", 150, 3600.0),
        CensusRow(4, None, 30000, 86400.0 * 90),
    ]
    summary = summarize_census(rows)
    assert summary is not None
    assert summary.identities == 4
    assert summary.total_rows == 30154
    assert summary.n_p50 == 76.0
    assert summary.n_max == 30000
    assert summary.buckets == {"1-9": 2, "10-99": 0, "100-999": 1, "1000+": 1}
    # Fewer identities than TOP_SHARE_COUNT: the "top" is everyone, share 1.0.
    assert summary.top_share == 1.0
    many = [CensusRow(i, None, 1, 0.0) for i in range(20)] + [
        CensusRow(99, None, 80, 0.0)
    ]
    top_heavy = summarize_census(many)
    assert top_heavy is not None
    # Top 10 by count: the 80-row identity plus nine 1-row ones = 89 of 100.
    assert abs(top_heavy.top_share - 0.89) < 1e-12


def test_census_summary_empty_is_none():
    assert summarize_census([]) is None


def test_sql_like_is_case_sensitive_like_postgres_like():
    assert sql_like("claude-something", "claude%")
    assert sql_like("my-codex-run", "%codex%")
    # Postgres LIKE is case-sensitive; the report labels patterns as LIKE,
    # so the helper must not silently behave as ILIKE.
    assert not sql_like("CLAUDE-x", "claude%")
    assert not sql_like("lumen", "claude%")
    assert sql_like("ab", "a_")
    assert not sql_like("abc", "a_")


def test_build_report_renders_populated_tables():
    stats = [
        {
            "identity_id": 42,
            "label": "example",
            "n": 100,
            "gap_p50_s": 180.0,
            "gap_p90_s": 200.0,
            "gap_max_h": 3.1,
            "hourly_n": 300,
            "epochs": [3],
            "coherence_forms": {"legacy_tanh_v": 100},
            "dims": {
                "S": {
                    "n": 100,
                    "mean": 0.25,
                    "p50": 0.24,
                    "sd": 0.1,
                    "acf_raw1": 0.98,
                    "acf_24h": (0.59, 276),
                    "hod_share": 0.343,
                    "hod_floor": 0.077,
                },
                "coherence": {
                    "n": 100,
                    "mean": 0.4725,
                    "p50": 0.4723,
                    "sd": 0.0003,
                    "acf_raw1": None,
                    "acf_24h": None,
                    "hod_share": None,
                    "hod_floor": None,
                },
            },
        }
    ]
    report = build_report(
        census_all=summarize_census([CensusRow(1, "x", 5, 60.0)]),
        census_patterns={"x%": summarize_census([CensusRow(1, "x", 5, 60.0)])},
        series_stats=stats,
        window_days=14,
        top=3,
        generated_at="2026-08-27T00:00:00-06:00",
    )
    assert "identity 42 (example)" in report
    assert "hourly buckets 300 of <= 337" in report
    assert "epochs [3]" in report and "legacy_tanh_v" in report
    # Populated dim row: value formatting and column order.
    assert "| S | 100 | 0.2500 | 0.2400 | 0.1000 | 0.98 | 0.59 (276) | 0.343 | 0.077 |" in report
    # None-valued cells render as n/a, not crash.
    assert "| coherence | 100 | 0.4725 | 0.4723 | 0.0003 | n/a | n/a | n/a | n/a |" in report


def test_build_report_smoke_contains_guards_and_sections():
    report = build_report(
        census_all=summarize_census([CensusRow(1, "x", 5, 60.0)]),
        census_patterns={"x%": summarize_census([CensusRow(1, "x", 5, 60.0)])},
        series_stats=[],
        window_days=14,
        top=3,
        generated_at="2026-08-27T00:00:00-06:00",
    )
    assert "Descriptive only" in report
    assert "not a stop-rule input" in report
    assert "History-length census" in report
    assert "carries no phase" in report
    assert "x%" in report
