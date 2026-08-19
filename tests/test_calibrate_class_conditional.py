"""Tests for scripts/calibrate_class_conditional.py render_python_snippet.

The 2026-04-18 regression: Steward was omitted from the class-conditional
config output without a warning because it had zero rows in core.agent_state
on the calibration date (due to a separate loop-detection bug). The script
iterated `by_class.items()` — a class with N=0 was indistinguishable from
a class that doesn't exist. No skip-comment, no visible breadcrumb.

These tests enforce that every KNOWN_RESIDENT_LABELS member always appears
in the output, either as a measured entry, a below-threshold skip-comment,
or an explicit MISSING comment pointing at the alias-entry remediation.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "calibrate_class_conditional.py"


def _load_module():
    sys.modules.setdefault("psycopg2", type(sys)("psycopg2"))
    sys.modules.setdefault("psycopg2.extras", type(sys)("psycopg2.extras"))
    spec = importlib.util.spec_from_file_location("calibrate_class_conditional", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so @dataclass can resolve
    # cls.__module__ during class creation.
    sys.modules["calibrate_class_conditional"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def calibrate():
    return _load_module()


@pytest.fixture
def sample_stats(calibrate):
    """Three measured classes: Lumen, Sentinel, default. Omit Steward/Vigil/Watcher."""
    ClassStats = calibrate.ClassStats
    return [
        ClassStats("Lumen",    n=7320, e_median=0.75, i_median=0.80, s_median=0.17,
                   e_p90=0.85, i_p90=0.88, s_p90=0.30, delta_p95=0.12),
        ClassStats("Sentinel", n=1870, e_median=0.75, i_median=0.80, s_median=0.19,
                   e_p90=0.82, i_p90=0.87, s_p90=0.33, delta_p95=0.17),
        ClassStats("default",  n=2033, e_median=0.73, i_median=0.79, s_median=0.24,
                   e_p90=0.81, i_p90=0.86, s_p90=0.38, delta_p95=0.20),
    ]


def test_missing_resident_emits_explicit_comment(calibrate, sample_stats):
    """Residents absent from stats must appear as MISSING comments."""
    out = calibrate.render_python_snippet(sample_stats, measured_on="2026-04-20", n_min=30)
    for missing in ("Steward", "Vigil", "Watcher"):
        assert f'# "{missing}": MISSING' in out, (
            f"Resident {missing} had 0 observations but no MISSING comment emitted"
        )


def test_missing_resident_comment_points_at_alias_remediation(calibrate, sample_stats):
    out = calibrate.render_python_snippet(sample_stats, measured_on="2026-04-20", n_min=30)
    assert 'provenance="alias"' in out, (
        "MISSING comment must reference the alias-entry remediation path"
    )


def test_measured_residents_still_render_normally(calibrate, sample_stats):
    out = calibrate.render_python_snippet(sample_stats, measured_on="2026-04-20", n_min=30)
    assert '"Lumen": ScaleConstant(' in out
    assert '"Sentinel": ScaleConstant(' in out
    assert 'provenance="measured"' in out


def test_below_threshold_skip_distinct_from_missing(calibrate, sample_stats):
    """N=5 below threshold gets a 'skipped' comment, not 'MISSING'."""
    ClassStats = calibrate.ClassStats
    stats = sample_stats + [
        ClassStats("Watcher", n=5, e_median=0.74, i_median=0.76, s_median=0.25,
                   e_p90=0.80, i_p90=0.83, s_p90=0.35, delta_p95=0.40),
    ]
    out = calibrate.render_python_snippet(stats, measured_on="2026-04-20", n_min=30)
    assert '"Watcher": skipped — N=5' in out
    assert '"Watcher": MISSING' not in out


def test_known_residents_override_allowed(calibrate, sample_stats):
    """Caller can pass a narrower known_residents set."""
    out = calibrate.render_python_snippet(
        sample_stats, measured_on="2026-04-20", n_min=30,
        known_residents={"Steward"},
    )
    assert '# "Steward": MISSING' in out
    # Lumen is measured → not MISSING. Vigil/Watcher not in override set → not MISSING.
    assert '"Lumen": MISSING' not in out
    assert '"Vigil": MISSING' not in out
    assert '"Watcher": MISSING' not in out


class TestGeneratorEmitsBreadth:
    """The regen must carry the agent count forward, or this decays immediately.

    Hand-filled provenance rots at the next regeneration. The generator has the
    label on every row it reads, so counting distinct agents per class is free
    — what was missing was emitting it.
    """

    def test_single_agent_class_is_flagged_in_the_snippet(self, calibrate):
        ClassStats = calibrate.ClassStats
        stats = [
            ClassStats("embodied", n=12501, e_median=0.32, i_median=0.78,
                       s_median=0.21, e_p90=0.4, i_p90=0.9, s_p90=0.3,
                       delta_p95=0.1635, n_agents=1),
        ]
        out = calibrate.render_python_snippet(stats, "2026-08-19", n_min=30,
                                              known_residents=set())
        assert "distinct_agents=1" in out
        assert "SINGLE AGENT" in out
        assert "1 agent" in out

    def test_broad_class_carries_the_count_without_a_warning(self, calibrate):
        ClassStats = calibrate.ClassStats
        stats = [
            ClassStats("ephemeral", n=2115, e_median=0.77, i_median=0.69,
                       s_median=0.35, e_p90=0.9, i_p90=0.8, s_p90=0.5,
                       delta_p95=0.2952, n_agents=418),
        ]
        out = calibrate.render_python_snippet(stats, "2026-08-19", n_min=30,
                                              known_residents=set())
        assert "distinct_agents=418" in out
        assert "SINGLE AGENT" not in out
        assert "418 agents" in out

    def test_unrecorded_reads_as_unknown_not_as_one(self, calibrate):
        # 0 must not masquerade as a single-agent fit, and must not look broad.
        ClassStats = calibrate.ClassStats
        stats = [
            ClassStats("legacy", n=500, e_median=0.7, i_median=0.7, s_median=0.2,
                       e_p90=0.8, i_p90=0.8, s_p90=0.3, delta_p95=0.2),
        ]
        out = calibrate.render_python_snippet(stats, "2026-08-19", n_min=30,
                                              known_residents=set())
        assert "unrecorded" in out
        assert "SINGLE AGENT" not in out

    def test_agent_counts_come_from_distinct_labels(self, calibrate):
        """fetch_class_observations returns (observations, distinct_agents)."""
        import inspect

        src = inspect.getsource(calibrate.fetch_class_observations)
        assert "labels_by_class" in src
        assert "len(labels_by_class" in src
