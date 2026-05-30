"""Tests for grounding scale constants — provenance invariants.

Spec §3.4 requires every scale constant to carry measurement metadata.
These tests enforce that requirement at import time.
"""
import math

import pytest

from config.governance_config import (
    ScaleConstant,
    S_SCALE,
    I_SCALE,
    E_SCALE,
    DELTA_NORM_MAX,
    ALL_SCALE_CONSTANTS,
)


def test_scale_constant_has_required_fields():
    sc = S_SCALE
    assert sc.value > 0
    assert sc.measured_on
    assert sc.corpus_size >= 0
    assert sc.percentile in {50, 75, 90, 95, 99, None}
    assert sc.provenance in {"measured", "placeholder", "derived", "alias"}


def test_scale_constant_rejects_unknown_provenance():
    with pytest.raises(ValueError, match="unknown provenance"):
        ScaleConstant(
            name="BOGUS", value=1.0, measured_on="2026-04-20",
            corpus_size=0, percentile=None, provenance="guess",
        )


def test_scale_constant_accepts_alias_provenance():
    sc = ScaleConstant(
        name="DELTA_NORM_MAX[TestAlias]", value=0.2018, measured_on="2026-04-20",
        corpus_size=0, percentile=None, provenance="alias",
        notes="Alias to default for testing.",
    )
    assert sc.provenance == "alias"


def test_all_constants_registered_in_manifest():
    assert S_SCALE in ALL_SCALE_CONSTANTS
    assert I_SCALE in ALL_SCALE_CONSTANTS
    assert E_SCALE in ALL_SCALE_CONSTANTS
    assert DELTA_NORM_MAX in ALL_SCALE_CONSTANTS


def test_scale_constants_are_finite_floats():
    for sc in ALL_SCALE_CONSTANTS:
        assert isinstance(sc.value, float)
        assert math.isfinite(sc.value)
        assert sc.value > 0


def test_fleet_defaults_remain_placeholder():
    """Fleet-wide ALL_SCALE_CONSTANTS are placeholders (fallback only).

    Class-conditional constants in DELTA_NORM_MAX_BY_CLASS carry the
    measured values; fleet defaults exist only as a fallback for
    unclassified agents.
    """
    placeholders = [sc for sc in ALL_SCALE_CONSTANTS if sc.provenance == "placeholder"]
    assert len(placeholders) == len(ALL_SCALE_CONSTANTS)


def test_class_conditional_delta_norm_max_is_measured_or_alias():
    """Phase 2 measurement populated DELTA_NORM_MAX_BY_CLASS with measured
    values. 'alias' entries are permitted only when they mirror DEFAULT —
    they exist so residents with no corpus yet don't silently fall back.
    """
    from config.governance_config import (
        DELTA_NORM_MAX_BY_CLASS, DELTA_NORM_MAX_DEFAULT,
    )
    assert len(DELTA_NORM_MAX_BY_CLASS) >= 5  # Lumen, default, Sentinel, Vigil, Watcher
    for cls_name, sc in DELTA_NORM_MAX_BY_CLASS.items():
        assert sc.provenance in {"measured", "alias"}, (
            f"class-conditional {cls_name} should be measured or alias, got {sc.provenance}"
        )
        if sc.provenance == "measured":
            assert sc.corpus_size > 0
            assert sc.percentile == 95
        else:  # alias
            assert sc.corpus_size == 0, (
                f"alias entry {cls_name} must declare corpus_size=0"
            )
            # Alias must mirror another class's value exactly — not a free guess.
            # Acceptable targets: the fleet placeholder DEFAULT, or any measured
            # class in the same dict.
            peers = {s.value for k, s in DELTA_NORM_MAX_BY_CLASS.items() if k != cls_name}
            allowed = peers | {DELTA_NORM_MAX_DEFAULT.value}
            assert sc.value in allowed, (
                f"alias entry {cls_name} value {sc.value} does not mirror any "
                f"peer or DEFAULT — aliases must not introduce new numeric values"
            )


def test_known_residents_have_explicit_class_entries():
    """Every KNOWN_RESIDENT_LABELS member must appear as a key in the
    class-conditional maps — measured or aliased, but never silently absent.

    Silent absence caused Steward's class baseline to fall back to 'default'
    undetected on the 2026-04-18 calibration run (Steward had 0 state rows
    due to a loop-detection bug; the calibrator skipped it without comment).
    """
    from config.governance_config import (
        DELTA_NORM_MAX_BY_CLASS, HEALTHY_OPERATING_POINT_BY_CLASS,
    )
    from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS
    missing_delta = KNOWN_RESIDENT_LABELS - DELTA_NORM_MAX_BY_CLASS.keys()
    missing_hop = KNOWN_RESIDENT_LABELS - HEALTHY_OPERATING_POINT_BY_CLASS.keys()
    assert not missing_delta, (
        f"Residents missing from DELTA_NORM_MAX_BY_CLASS: {missing_delta}. "
        f"Add an explicit entry (measured or provenance='alias')."
    )
    assert not missing_hop, (
        f"Residents missing from HEALTHY_OPERATING_POINT_BY_CLASS: {missing_hop}. "
        f"Add an explicit entry (measured or alias tuple mirroring default)."
    )


def test_class_conditional_lookup_falls_back_for_unknown_classes():
    """Unknown class falls back to fleet-wide DELTA_NORM_MAX_DEFAULT."""
    from config.governance_config import (
        get_delta_norm_max, DELTA_NORM_MAX_DEFAULT,
    )
    assert get_delta_norm_max("nonexistent_class") is DELTA_NORM_MAX_DEFAULT
    assert get_delta_norm_max("Lumen").provenance == "measured"


def test_healthy_operating_point_class_conditional():
    """Per-class healthy points exist for measured classes; default for others."""
    from config.governance_config import (
        get_healthy_operating_point, HEALTHY_OPERATING_POINT_DEFAULT,
    )
    lumen_hop = get_healthy_operating_point("Lumen")
    assert lumen_hop != HEALTHY_OPERATING_POINT_DEFAULT
    assert all(0.0 <= v <= 1.0 for v in lumen_hop)

    unknown_hop = get_healthy_operating_point("nonexistent")
    assert unknown_hop == HEALTHY_OPERATING_POINT_DEFAULT


def test_engaged_ephemeral_review_cookie_recalibrated():
    """S8a 30-day review replaced the default alias with measured values."""
    from config.governance_config import (
        DELTA_NORM_MAX_BY_CLASS,
        HEALTHY_OPERATING_POINT_BY_CLASS,
    )

    sc = DELTA_NORM_MAX_BY_CLASS["engaged_ephemeral"]
    assert sc.provenance == "measured"
    assert sc.measured_on == "2026-05-30"
    assert sc.corpus_size == 1289
    assert sc.percentile == 95
    assert sc.value == pytest.approx(0.4246)
    assert HEALTHY_OPERATING_POINT_BY_CLASS["engaged_ephemeral"] == pytest.approx(
        (0.7556, 0.6853, 0.3068)
    )


def test_delta_norm_max_default_covers_full_state_space_diagonal():
    """Fleet-wide default must allow full diagonal so unclassified agents can hit C=0."""
    assert DELTA_NORM_MAX.value >= math.sqrt(3) - 0.01
