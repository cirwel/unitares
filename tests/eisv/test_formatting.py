"""
Tests for src.eisv.formatting - EISV metric formatting utilities.

All functions are pure (input -> output). No mocking needed.
"""

import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parents[2]
sys.path.insert(0, str(project_root))

from src.eisv.formatting import (
    EISVMetrics,
    EISVTrajectory,
    format_eisv_compact,
    format_eisv_detailed,
    format_eisv_trajectory,
    validate_eisv_complete,
    eisv_from_dict,
    format_eisv,
)


# ============================================================================
# EISVMetrics NamedTuple
# ============================================================================

class TestEISVMetrics:

    def test_creation(self):
        m = EISVMetrics(E=0.5, I=0.6, S=0.3, V=-0.1)
        assert m.E == 0.5
        assert m.I == 0.6
        assert m.S == 0.3
        assert m.V == -0.1

    def test_is_namedtuple(self):
        m = EISVMetrics(E=0.5, I=0.6, S=0.3, V=0.0)
        assert isinstance(m, tuple)
        assert hasattr(m, '_fields')
        assert m._fields == ('E', 'I', 'S', 'V')

    def test_validate_valid(self):
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        m.validate()  # Should not raise

    def test_validate_boundary_zero(self):
        m = EISVMetrics(E=0.0, I=0.0, S=0.0, V=0.0)
        m.validate()  # Should not raise

    def test_validate_boundary_one(self):
        m = EISVMetrics(E=1.0, I=1.0, S=1.0, V=0.0)
        m.validate()  # Should not raise

    def test_validate_e_out_of_range(self):
        m = EISVMetrics(E=1.5, I=0.5, S=0.5, V=0.0)
        with pytest.raises(ValueError, match="E must be in"):
            m.validate()

    def test_validate_e_negative(self):
        m = EISVMetrics(E=-0.1, I=0.5, S=0.5, V=0.0)
        with pytest.raises(ValueError, match="E must be in"):
            m.validate()

    def test_validate_i_out_of_range(self):
        m = EISVMetrics(E=0.5, I=2.0, S=0.5, V=0.0)
        with pytest.raises(ValueError, match="I must be in"):
            m.validate()

    def test_validate_s_out_of_range(self):
        m = EISVMetrics(E=0.5, I=0.5, S=-0.5, V=0.0)
        with pytest.raises(ValueError, match="S must be in"):
            m.validate()

    def test_validate_s_boundary(self):
        """S capped at 1.0 — behavioral sensor produces [0, 1]."""
        m = EISVMetrics(E=0.5, I=0.5, S=1.0, V=0.0)
        m.validate()  # Should not raise

        with pytest.raises(ValueError, match="S must be in"):
            m2 = EISVMetrics(E=0.5, I=0.5, S=1.5, V=0.0)
            m2.validate()

    def test_validate_v_bounded(self):
        """V bounded to [-1, 1] — behavioral V = EMA(E-I), both in [0, 1]."""
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=-1.0)
        m.validate()  # Should not raise

        m2 = EISVMetrics(E=0.5, I=0.5, S=0.5, V=1.0)
        m2.validate()  # Should not raise

        with pytest.raises(ValueError, match="V must be in"):
            m3 = EISVMetrics(E=0.5, I=0.5, S=0.5, V=-1.5)
            m3.validate()

        with pytest.raises(ValueError, match="V must be in"):
            m4 = EISVMetrics(E=0.5, I=0.5, S=0.5, V=1.5)
            m4.validate()


# ============================================================================
# EISVTrajectory
# ============================================================================

class TestEISVTrajectory:

    def test_deltas(self):
        start = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        end = EISVMetrics(E=0.8, I=0.7, S=0.2, V=-0.1)
        traj = EISVTrajectory(start=start, end=end)
        d = traj.deltas()
        assert abs(d.E - 0.3) < 1e-9
        assert abs(d.I - 0.2) < 1e-9
        assert abs(d.S - (-0.3)) < 1e-9
        assert abs(d.V - (-0.1)) < 1e-9

    def test_deltas_no_change(self):
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        traj = EISVTrajectory(start=m, end=m)
        d = traj.deltas()
        assert d.E == 0.0
        assert d.I == 0.0
        assert d.S == 0.0
        assert d.V == 0.0

    def test_percent_changes(self):
        start = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.1)
        end = EISVMetrics(E=1.0, I=0.5, S=0.25, V=0.2)
        traj = EISVTrajectory(start=start, end=end)
        pct = traj.percent_changes()
        assert abs(pct['E'] - 100.0) < 1e-9
        assert abs(pct['I'] - 0.0) < 1e-9
        assert abs(pct['S'] - (-50.0)) < 1e-9
        assert abs(pct['V'] - 100.0) < 1e-9

    def test_percent_changes_zero_start(self):
        """When start is 0, percent change is 0 for E/I/S, inf for V."""
        start = EISVMetrics(E=0.0, I=0.0, S=0.0, V=0.0)
        end = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.1)
        traj = EISVTrajectory(start=start, end=end)
        pct = traj.percent_changes()
        assert pct['E'] == 0
        assert pct['I'] == 0
        assert pct['S'] == 0
        assert pct['V'] == float('inf')


# ============================================================================
# format_eisv_compact
# ============================================================================

class TestFormatEISVCompact:

    def test_basic(self):
        m = EISVMetrics(E=0.80, I=1.00, S=0.03, V=-0.07)
        result = format_eisv_compact(m)
        assert result == "E=0.800000 I=1.000000 S=0.030000 V=-0.070000"

    def test_zero_values(self):
        m = EISVMetrics(E=0.0, I=0.0, S=0.0, V=0.0)
        result = format_eisv_compact(m)
        assert result == "E=0.000000 I=0.000000 S=0.000000 V=0.000000"

    def test_precision(self):
        m = EISVMetrics(E=0.1, I=0.2, S=0.3, V=0.4)
        result = format_eisv_compact(m)
        assert "E=0.100000" in result
        assert "V=0.400000" in result


# ============================================================================
# format_eisv_detailed
# ============================================================================

class TestFormatEISVDetailed:

    def test_with_labels(self):
        m = EISVMetrics(E=0.80, I=1.00, S=0.03, V=-0.07)
        result = format_eisv_detailed(m, include_labels=True)
        assert "Energy" in result
        assert "Integrity" in result
        assert "Entropy" in result
        assert "Valence" in result
        assert "Void" not in result
        assert "0.800000" in result

    def test_without_labels(self):
        m = EISVMetrics(E=0.80, I=1.00, S=0.03, V=-0.07)
        result = format_eisv_detailed(m, include_labels=False)
        assert "Energy" not in result
        assert "E:" in result

    def test_user_friendly(self):
        m = EISVMetrics(E=0.80, I=1.00, S=0.03, V=-0.07)
        result = format_eisv_detailed(m, include_user_friendly=True)
        # Each dimension uses the canonical client-visible field contract.
        assert "capacity" in result.lower()  # E: productive capacity
        assert "claims" in result.lower()  # I: claims matching results
        assert "drifting" in result.lower()  # S: drift from the agent's normal
        assert "imbalance" in result.lower()  # V: E-I imbalance

    def test_multiline(self):
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        result = format_eisv_detailed(m)
        lines = result.strip().split('\n')
        assert len(lines) == 4


# ============================================================================
# format_eisv_trajectory
# ============================================================================

class TestFormatEISVTrajectory:

    def test_increasing(self):
        start = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        end = EISVMetrics(E=0.8, I=0.7, S=0.2, V=-0.01)
        traj = EISVTrajectory(start=start, end=end)
        result = format_eisv_trajectory(traj)
        assert "Energy" in result
        assert "Valence" in result
        assert "Void" not in result

    def test_no_change(self):
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        traj = EISVTrajectory(start=m, end=m)
        result = format_eisv_trajectory(traj)
        assert "=" in result or "+0.0%" in result

    def test_all_four_present(self):
        start = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.1)
        end = EISVMetrics(E=0.6, I=0.6, S=0.4, V=0.2)
        traj = EISVTrajectory(start=start, end=end)
        result = format_eisv_trajectory(traj)
        lines = result.strip().split('\n')
        assert len(lines) == 4

    def test_v_multiplier(self):
        """V shows multiplier when ratio > 2x."""
        start = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.01)
        end = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.05)
        traj = EISVTrajectory(start=start, end=end)
        result = format_eisv_trajectory(traj)
        assert "x" in result or "+" in result


# ============================================================================
# validate_eisv_complete
# ============================================================================

class TestValidateEISVComplete:

    def test_valid(self):
        data = {'E': 0.5, 'I': 0.5, 'S': 0.5, 'V': 0.0}
        assert validate_eisv_complete(data) is True

    def test_extra_keys(self):
        data = {'E': 0.5, 'I': 0.5, 'S': 0.5, 'V': 0.0, 'coherence': 0.8}
        assert validate_eisv_complete(data) is True

    def test_missing_v(self):
        data = {'E': 0.5, 'I': 0.5, 'S': 0.5}
        with pytest.raises(ValueError, match="Missing"):
            validate_eisv_complete(data)

    def test_missing_all(self):
        data = {'coherence': 0.8}
        with pytest.raises(ValueError, match="Missing"):
            validate_eisv_complete(data)

    def test_missing_e_and_i(self):
        data = {'S': 0.5, 'V': 0.0}
        with pytest.raises(ValueError, match="Missing"):
            validate_eisv_complete(data)


# ============================================================================
# eisv_from_dict
# ============================================================================

class TestEISVFromDict:

    def test_valid(self):
        data = {'E': 0.5, 'I': 0.6, 'S': 0.3, 'V': -0.1}
        m = eisv_from_dict(data)
        assert isinstance(m, EISVMetrics)
        assert m.E == 0.5
        assert m.V == -0.1

    def test_string_conversion(self):
        data = {'E': '0.5', 'I': '0.6', 'S': '0.3', 'V': '-0.1'}
        m = eisv_from_dict(data)
        assert m.E == 0.5
        assert m.V == -0.1

    def test_missing_key(self):
        data = {'E': 0.5, 'I': 0.6, 'S': 0.3}
        with pytest.raises(ValueError, match="Missing"):
            eisv_from_dict(data)

    def test_validates_ranges(self):
        data = {'E': 1.5, 'I': 0.5, 'S': 0.5, 'V': 0.0}
        with pytest.raises(ValueError, match="E must be in"):
            eisv_from_dict(data)


# ============================================================================
# format_eisv (router)
# ============================================================================

class TestFormatEISV:

    def test_compact_style(self):
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        result = format_eisv(m, style='compact')
        assert "E=0.500000" in result

    def test_detailed_style(self):
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        result = format_eisv(m, style='detailed')
        assert "Energy" in result

    def test_unknown_style(self):
        m = EISVMetrics(E=0.5, I=0.5, S=0.5, V=0.0)
        with pytest.raises(ValueError, match="Unknown style"):
            format_eisv(m, style='fancy')
