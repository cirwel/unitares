"""Safety regressions for the synthetic calibration harness."""
from __future__ import annotations

import pytest

from scripts.dev.calibration_harness.grader import grade_script
from scripts.dev.calibration_harness.run_v1 import _guard_not_prod


def test_calibration_harness_grades_are_marked_controlled_fixtures() -> None:
    grade = grade_script("assert 2 + 2 == 5, 'seeded failure'\n", label="overconfidence_probe")

    assert grade.is_bad is True
    assert grade.detail["synthetic_calibration_fixture"] is True
    assert grade.detail["do_not_use_for_live_validation"] is True
    assert grade.detail["fixture_scope"] == "calibration_harness"
    assert grade.detail["red_team_fixture"] == "calibration_harness_seeded_bad_outcome"


def test_calibration_harness_guard_rejects_live_governance_ports() -> None:
    with pytest.raises(SystemExit, match="live governance port"):
        _guard_not_prod("http://127.0.0.1:8767", force=False)

    _guard_not_prod("http://127.0.0.1:8771", force=False)
    _guard_not_prod("http://127.0.0.1:8767", force=True)
