"""Tests for the stop-rule support-condition reachability probe.

The point of this probe is that it can return either answer. A check that only
ever says "unreachable" would be the mirror of the defect it exists to catch --
the coherence-gate positive control that could only ever say PASS. So the
both-directions cases are tested as directly as the headline one.
"""

from __future__ import annotations

from datetime import date

import pytest

from scripts.analysis.support_reachability import (
    FROZEN_BAD_CLUSTERS,
    READ_DATE,
    TARGET_BAD_CLUSTERS,
    accrual_reachability,
    render,
    window_invariance,
)


def test_published_inputs_reproduce_the_disclosed_shortfall():
    """Defaults are the figures already in the repo; this is the headline read."""
    reach = accrual_reachability()
    assert reach.observed_blocks == 28
    assert reach.target_blocks == 150
    assert reach.blocks_still_needed == 122
    # ~3.4/month observed against ~32.6/month required.
    assert reach.observed_per_month == pytest.approx(3.36, abs=0.05)
    assert reach.required_per_month == pytest.approx(32.57, abs=0.1)
    assert reach.acceleration_required == pytest.approx(9.71, abs=0.05)
    assert reach.verdict == "REQUIRES_ACCELERATION"


def test_the_check_can_say_reachable():
    """The falsifiability case: a sufficient rate must return ON_TRACK.

    Without this, the probe would only ever produce the answer it was written
    after — which is the antipattern it is modelled to avoid.
    """
    reach = accrual_reachability(
        observed_blocks=120,
        observation_start=date(2026, 1, 1),
        observed_through=date(2026, 8, 9),
        target_blocks=TARGET_BAD_CLUSTERS,
        read_date=READ_DATE,
    )
    assert reach.verdict == "ON_TRACK_AT_OBSERVED_RATE"
    assert reach.acceleration_required <= 1.0


def test_already_met_short_circuits():
    reach = accrual_reachability(observed_blocks=TARGET_BAD_CLUSTERS + 1)
    assert reach.verdict == "ALREADY_MET"
    assert reach.blocks_still_needed == 0


def test_no_time_remaining_is_distinguished_from_too_slow():
    """Missing for lack of interval is a different finding from missing for rate."""
    reach = accrual_reachability(observed_through=READ_DATE)
    assert reach.verdict == "NO_TIME_REMAINING"
    assert reach.remaining_days == 0


def test_zero_observed_accrual_does_not_divide_by_zero():
    reach = accrual_reachability(observed_blocks=0)
    assert reach.verdict == "NO_OBSERVED_ACCRUAL"
    assert reach.acceleration_required is None


def test_a_degenerate_observation_window_is_rejected():
    with pytest.raises(ValueError):
        accrual_reachability(
            observation_start=date(2026, 8, 9), observed_through=date(2026, 8, 9)
        )


def test_window_invariance_reads_the_frozen_table_as_supply_limited():
    """30d and 90d both returned 28-29 clusters in the frozen trusted read."""
    window = window_invariance()
    assert window["clusters_gained"] == 0
    assert window["supply_limited"] is True
    assert "does not supply the missing blocks" in window["reading"]


def test_window_invariance_can_say_not_supply_limited():
    """The other direction: a window that still admits clusters must say so."""
    window = window_invariance({30: 28, 90: 84})
    assert window["supply_limited"] is False
    assert "may raise the count" in window["reading"]


def test_render_disclaims_the_authority_it_does_not_have():
    """Per CLAUDE.md a count may retire an instrument, never a capability."""
    text = render(accrual_reachability(), window_invariance())
    assert "changes no threshold" in text
    assert "retires nothing" in text
    assert "not a forecast" in text


def test_defaults_match_the_frozen_record():
    assert FROZEN_BAD_CLUSTERS == 28
    assert TARGET_BAD_CLUSTERS == 150
    assert READ_DATE == date(2026, 12, 1)
