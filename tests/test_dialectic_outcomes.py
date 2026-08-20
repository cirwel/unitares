"""Terminal-outcome classification for dialectic sessions (issue #1689).

The defect these pin: a reviewer that refutes a thesis correctly, and is never
facilitated, was recorded identically to a dead canary probe, because every
reader keyed on `status`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dialectic_outcomes import (  # noqa: E402
    CANARY,
    FAILED,
    OPEN,
    RESOLVED,
    UNRESOLVED_AWAITING_FACILITATION,
    classify_outcome,
    is_canary_label,
    resolution_rate,
)


class TestClassifyOutcome:
    def test_standing_disagreement_is_not_failure(self):
        """The #1689 case: session c1db868e3323bad5's exact terminal shape.

        phase=failed, status=failed, awaiting_facilitation=true, after a
        reviewer returned a factually correct refutation.
        """
        assert classify_outcome("failed", True, "opus_2f1ecbdf") == (
            UNRESOLVED_AWAITING_FACILITATION
        )

    def test_real_failure_stays_failure(self):
        """No standing objection means nothing is being hidden by `failed`."""
        assert classify_outcome("failed", False, "opus_2f1ecbdf") == FAILED

    def test_canary_wins_over_everything(self):
        """Probe traffic ends `failed` by design; it belongs in no denominator."""
        assert classify_outcome("failed", False, "canary_dialectic_38fd4b73") == CANARY
        assert classify_outcome("failed", True, "canary_dialectic_38fd4b73") == CANARY
        assert classify_outcome("resolved", False, "canary_dialectic_38fd4b73") == CANARY

    def test_resolved_is_resolved(self):
        assert classify_outcome("resolved", False, "opus_2f1ecbdf") == RESOLVED

    def test_non_terminal_status_is_open(self):
        for status in ("active", "reviewing", "synthesis", None):
            assert classify_outcome(status, False, "opus_2f1ecbdf") == OPEN

    def test_timeout_and_abandoned_are_terminal(self):
        assert classify_outcome("timeout", False, "a") == FAILED
        assert classify_outcome("abandoned", False, "a") == FAILED
        # ...but a standing objection still outranks the generic terminal read.
        assert classify_outcome("timeout", True, "a") == UNRESOLVED_AWAITING_FACILITATION

    def test_missing_label_is_not_canary(self):
        """An unjoined label must never be silently treated as probe traffic."""
        assert classify_outcome("failed", False, None) == FAILED
        assert not is_canary_label(None)
        assert not is_canary_label("")
        assert not is_canary_label("dialectic-termination-fork")
        assert is_canary_label("canary_dialectic_f33d8abe")


class TestResolutionRate:
    def test_unresolved_is_excluded_from_both_terms(self):
        """Counting it either way asserts an outcome that has not happened."""
        counts = {RESOLVED: 8, FAILED: 2, UNRESOLVED_AWAITING_FACILITATION: 90}
        assert resolution_rate(counts) == pytest.approx(0.8)

    def test_canary_never_reaches_the_rate(self):
        counts = {RESOLVED: 1, FAILED: 1, CANARY: 1000}
        assert resolution_rate(counts) == pytest.approx(0.5)

    def test_empty_denominator_is_none_not_zero(self):
        """A rate of 0.0 would read as total failure; there is no rate at all."""
        assert resolution_rate({UNRESOLVED_AWAITING_FACILITATION: 5}) is None
        assert resolution_rate({}) is None

    def test_the_live_window_shape(self):
        """Trailing 30d on the live DB as measured 2026-08-19.

        47 sessions, raw status rate 38.3%. Partitioned correctly: 22 canary,
        14 unresolved-awaiting-facilitation, and every one of the 11 sessions
        that reached a genuine terminal state resolved. The rate the gate would
        have pinned was measuring canary volume, not dialectic quality.
        """
        counts = {
            RESOLVED: 11,
            FAILED: 0,
            UNRESOLVED_AWAITING_FACILITATION: 14,
            CANARY: 22,
            OPEN: 0,
        }
        assert sum(counts.values()) == 47
        assert resolution_rate(counts) == pytest.approx(1.0)
        # And the honest caveat: that denominator is below the gate's own floor.
        assert counts[RESOLVED] + counts[FAILED] < 30
