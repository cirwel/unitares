"""Novelty gate for the mirror's complexity-calibration line.

`_complexity_divergence_novel` (src/monitor_result.py) decides whether
the divergence is worth surfacing AGAIN: first threshold crossing or a
materially changed signed gap only. Dogfood 2026-06-10: a stable
session-long gap repeated the same mirror line on every check-in.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.monitor_result import _complexity_divergence_novel


def _monitor():
    return SimpleNamespace(_last_surfaced_complexity_gap=None)


def _cm(self_cx, derived_cx):
    divergence = abs((self_cx if self_cx is not None else 0.0) - derived_cx)
    return SimpleNamespace(
        self_complexity=self_cx,
        derived_complexity=derived_cx,
        complexity_divergence=divergence,
    )


def test_first_crossing_is_novel():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    assert m._last_surfaced_complexity_gap == pytest.approx(0.4)


def test_stable_gap_not_novel_on_repeat():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is False
    # Small wobble within the delta is still the same gap.
    assert _complexity_divergence_novel(m, _cm(0.72, 0.35)) is False


def test_materially_changed_gap_is_novel_again():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    assert _complexity_divergence_novel(m, _cm(0.9, 0.3)) is True
    assert m._last_surfaced_complexity_gap == pytest.approx(0.6)


def test_direction_flip_is_novel():
    """Signed-gap tracking: over→under-reporting of equal magnitude must
    register even though |divergence| is unchanged."""
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.5, 0.3)) is True   # gap +0.2
    assert _complexity_divergence_novel(m, _cm(0.3, 0.5)) is True   # gap −0.2
    assert m._last_surfaced_complexity_gap == pytest.approx(-0.2)


def test_below_threshold_never_novel_and_keeps_state():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    # Dip below the line threshold: not novel, and the last surfaced gap
    # is retained so returning to the SAME gap does not re-fire.
    assert _complexity_divergence_novel(m, _cm(0.4, 0.3)) is False
    assert m._last_surfaced_complexity_gap == pytest.approx(0.4)
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is False


def test_none_self_complexity_treated_as_zero():
    m = _monitor()
    cm = _cm(None, 0.5)
    assert cm.complexity_divergence == pytest.approx(0.5)
    assert _complexity_divergence_novel(m, cm) is True
    assert m._last_surfaced_complexity_gap == pytest.approx(-0.5)


def test_monitor_without_attribute_uses_getattr_default():
    """Defensive: a monitor object predating the attribute (or a test
    stub) starts from None."""
    bare = SimpleNamespace()
    assert _complexity_divergence_novel(bare, _cm(0.7, 0.3)) is True
    assert bare._last_surfaced_complexity_gap == pytest.approx(0.4)
