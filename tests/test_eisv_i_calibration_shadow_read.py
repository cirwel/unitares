from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.analysis import eisv_i_calibration_shadow_read as mod


def _ts(offset_hours: float = 0.0) -> datetime:
    return datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc) + timedelta(hours=offset_hours)


def test_agent_pooled_error_no_evidence_returns_prior_only():
    e_pool, n, w, e_agent = mod.agent_pooled_error([], prior_error=0.25, k=10.0, mode="ece")
    assert n == 0
    assert w == 0.0
    assert e_agent is None
    assert e_pool == pytest.approx(0.25)


def test_agent_pooled_error_causal_cutoff_excludes_future_pairs():
    as_of = _ts(0.0)
    pairs = [
        (_ts(-2.0), 0.80, 1.0),
        (_ts(+1.0), 0.90, 0.0),  # future relative to as_of
    ]
    e_pool, n, w, e_agent = mod.agent_pooled_error(pairs, as_of=as_of, prior_error=0.25, k=9.0, mode="ece")
    assert n == 1
    assert w == pytest.approx(0.1)
    assert e_agent == pytest.approx(0.20)
    assert e_pool == pytest.approx(0.9 * 0.25 + 0.1 * 0.20)


def test_agent_pooled_error_shrinkage_washes_out_prior_as_n_grows():
    # Calibrated agent in [0.7, 0.8) bin: 4 passes, 1 fail at c=0.80 -> empirical 0.80, e_ece=0.0
    base_pairs = [
        (_ts(-5.0), 0.80, 1.0),
        (_ts(-4.0), 0.80, 1.0),
        (_ts(-3.0), 0.80, 1.0),
        (_ts(-2.0), 0.80, 1.0),
        (_ts(-1.0), 0.80, 0.0),
    ]
    e_small, n_small, w_small, e_agent_small = mod.agent_pooled_error(
        base_pairs, prior_error=0.25, k=10.0, mode="ece"
    )
    e_large, n_large, w_large, e_agent_large = mod.agent_pooled_error(
        base_pairs * 20, prior_error=0.25, k=10.0, mode="ece"
    )
    assert e_agent_small == pytest.approx(0.0)
    assert e_agent_large == pytest.approx(0.0)
    assert n_small == 5 and n_large == 100
    assert w_small < w_large
    assert e_large < e_small
    assert e_large == pytest.approx(0.25 * (10.0 / 110.0))


def test_agent_pooled_error_restores_between_agent_spread():
    # Calibrated agent vs overconfident failing agent
    good_pairs = [(_ts(-i), 0.80, 1.0 if i % 5 != 0 else 0.0) for i in range(1, 11)]
    bad_pairs = [(_ts(-i), 0.95, 0.0) for i in range(1, 11)]

    e_good, _, _, _ = mod.agent_pooled_error(good_pairs, prior_error=0.25, k=10.0, mode="ece")
    e_bad, _, _, _ = mod.agent_pooled_error(bad_pairs, prior_error=0.25, k=10.0, mode="ece")
    cal_good = 1.0 - e_good
    cal_bad = 1.0 - e_bad
    assert cal_good > cal_bad
    assert (cal_good - cal_bad) == pytest.approx(0.5 * 0.95)
