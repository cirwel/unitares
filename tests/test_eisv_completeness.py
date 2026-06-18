"""
Tests for EISV Completeness Enforcement

These tests ensure that EISV metrics are never reported incompletely,
preventing selection bias.
"""

import pytest
import sys
import importlib.util
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.eisv_format import (
    EISVMetrics,
    EISVTrajectory,
    format_eisv_compact,
    format_eisv_detailed,
    format_eisv_trajectory,
    validate_eisv_complete,
    eisv_from_dict
)
from src.eisv_validator import (
    validate_governance_response,
    validate_csv_row,
    IncompleteEISVError
)


def _load_check_script():
    script = project_root / "scripts" / "diagnostics" / "check_eisv_completeness.py"
    spec = importlib.util.spec_from_file_location("check_eisv_completeness", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEISVMetrics:
    """Test that EISVMetrics enforces completeness."""

    def test_cannot_create_incomplete_metrics(self):
        """NamedTuple prevents partial construction."""
        # This should fail at type level (IDE will warn)
        # At runtime, it raises TypeError
        with pytest.raises(TypeError):
            # Missing V
            incomplete = EISVMetrics(E=0.8, I=1.0, S=0.03)  # type: ignore

    def test_all_four_required(self):
        """All four metrics must be provided."""
        # This works - all four present
        complete = EISVMetrics(E=0.8, I=1.0, S=0.03, V=-0.07)
        assert complete.E == 0.8
        assert complete.V == -0.07

    def test_validates_ranges(self):
        """E, I in [0,1], S in [0,1], V in [-1,1] per behavioral bounds."""
        # Invalid E
        with pytest.raises(ValueError, match="E must be in"):
            invalid = EISVMetrics(E=1.5, I=1.0, S=0.03, V=-0.07)
            invalid.validate()

        # Invalid I
        with pytest.raises(ValueError, match="I must be in"):
            invalid = EISVMetrics(E=0.8, I=-0.5, S=0.03, V=-0.07)
            invalid.validate()

        # S capped at 1.0 (behavioral sensor produces [0, 1])
        with pytest.raises(ValueError, match="S must be in"):
            invalid = EISVMetrics(E=0.8, I=1.0, S=1.5, V=0.0)
            invalid.validate()

        # V bounded to [-1, 1] (behavioral V = EMA(E-I), both in [0,1])
        with pytest.raises(ValueError, match="V must be in"):
            invalid = EISVMetrics(E=0.8, I=1.0, S=0.03, V=-1.5)
            invalid.validate()


class TestFormatting:
    """Test that formatting functions always include all four."""

    def test_compact_format_includes_all_four(self):
        """Compact format must show E, I, S, V."""
        metrics = EISVMetrics(E=0.8, I=1.0, S=0.03, V=-0.07)
        formatted = format_eisv_compact(metrics)

        assert 'E=' in formatted
        assert 'I=' in formatted
        assert 'S=' in formatted
        assert 'V=' in formatted
        assert formatted.count('=') == 4  # Exactly four metrics

    def test_detailed_format_includes_all_four(self):
        """Detailed format must show E, I, S, V."""
        metrics = EISVMetrics(E=0.8, I=1.0, S=0.03, V=-0.07)
        formatted = format_eisv_detailed(metrics)

        assert 'E' in formatted
        assert 'I' in formatted
        assert 'S' in formatted
        assert 'V' in formatted
        assert formatted.count('\n') == 3  # Four lines (3 newlines)

    def test_trajectory_includes_all_four(self):
        """Trajectory format must show E, I, S, V."""
        start = EISVMetrics(E=0.7, I=0.8, S=0.1, V=-0.01)
        end = EISVMetrics(E=0.8, I=1.0, S=0.03, V=-0.07)
        trajectory = EISVTrajectory(start=start, end=end)

        formatted = format_eisv_trajectory(trajectory)

        assert 'E' in formatted
        assert 'I' in formatted
        assert 'S' in formatted
        assert 'V' in formatted
        assert '→' in formatted  # Shows transition


class TestValidation:
    """Test that validation catches incomplete metrics."""

    def test_validates_complete_dict(self):
        """Complete dict passes validation."""
        complete = {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07}
        assert validate_eisv_complete(complete) is True

    def test_rejects_missing_v(self):
        """Missing V raises error."""
        incomplete = {'E': 0.8, 'I': 1.0, 'S': 0.03}
        with pytest.raises(ValueError, match="Missing.*V"):
            validate_eisv_complete(incomplete)

    def test_rejects_missing_e(self):
        """Missing E raises error."""
        incomplete = {'I': 1.0, 'S': 0.03, 'V': -0.07}
        with pytest.raises(ValueError, match="Missing.*E"):
            validate_eisv_complete(incomplete)

    def test_eisv_from_dict_validates(self):
        """eisv_from_dict enforces completeness."""
        complete = {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07}
        metrics = eisv_from_dict(complete)
        assert metrics.V == -0.07

        incomplete = {'E': 0.8, 'I': 1.0, 'S': 0.03}
        with pytest.raises(ValueError):
            eisv_from_dict(incomplete)


class TestGovernanceResponseValidation:
    """Test validation of actual governance responses."""

    def test_valid_response(self):
        """Valid response with all EISV passes."""
        response = {
            'success': True,
            'metrics': {
                'E': 0.8,
                'I': 1.0,
                'S': 0.03,
                'V': -0.07,
                'coherence': 0.47,
                'lambda1': 0.12
            }
        }
        # Should not raise
        validate_governance_response(response)

    def test_rejects_missing_v_in_response(self):
        """Response missing V fails validation."""
        response = {
            'success': True,
            'metrics': {
                'E': 0.8,
                'I': 1.0,
                'S': 0.03,
                # V missing!
                'coherence': 0.47
            }
        }
        with pytest.raises(IncompleteEISVError, match="Missing.*V"):
            validate_governance_response(response)

    def test_rejects_none_values(self):
        """Response with None V fails validation."""
        response = {
            'success': True,
            'metrics': {
                'E': 0.8,
                'I': 1.0,
                'S': 0.03,
                'V': None,  # None not allowed
                'coherence': 0.47
            }
        }
        with pytest.raises(IncompleteEISVError, match="None values"):
            validate_governance_response(response)


class TestCSVValidation:
    """Test CSV row validation."""

    def test_valid_csv_row(self):
        """CSV row with all EISV passes."""
        row = {
            'agent_id': 'test',
            'E': 0.8,
            'I': 1.0,
            'S': 0.03,
            'V': -0.07
        }
        # Should not raise
        validate_csv_row(row)

    def test_rejects_incomplete_csv_row(self):
        """CSV row missing V fails."""
        row = {
            'agent_id': 'test',
            'E': 0.8,
            'I': 1.0,
            'S': 0.03
            # V missing!
        }
        with pytest.raises(IncompleteEISVError):
            validate_csv_row(row)


class TestIntegration:
    """Integration tests to ensure system-wide enforcement."""

    def test_trajectory_deltas_include_all_four(self):
        """Trajectory deltas must include V."""
        start = EISVMetrics(E=0.7, I=0.8, S=0.1, V=-0.01)
        end = EISVMetrics(E=0.8, I=1.0, S=0.03, V=-0.07)
        trajectory = EISVTrajectory(start=start, end=end)

        deltas = trajectory.deltas()
        assert hasattr(deltas, 'V')
        # Use approximate comparison due to floating point precision
        assert abs(deltas.V - (-0.06)) < 1e-10, f"Expected ~-0.06, got {deltas.V}"  # -0.07 - (-0.01)

    def test_percent_changes_include_all_four(self):
        """Percent changes must include V."""
        start = EISVMetrics(E=0.7, I=0.8, S=0.1, V=-0.01)
        end = EISVMetrics(E=0.8, I=1.0, S=0.03, V=-0.07)
        trajectory = EISVTrajectory(start=start, end=end)

        percent = trajectory.percent_changes()
        assert 'V' in percent
        assert len(percent) == 4  # E, I, S, V


class TestCompletenessScript:
    """Regression tests for the repository scan filter."""

    def test_should_check_file_skips_generated_and_worktree_paths(self):
        script = _load_check_script()
        assert script.should_check_file(Path("/repo/src/state.py")) is True
        assert script.should_check_file(Path("/repo/.venv/lib/site-packages/pkg.py")) is False
        assert script.should_check_file(Path("/repo/.claude/worktrees/task/docs/note.md")) is False
        assert script.should_check_file(Path("/repo/htmlcov/index.json")) is False

    def test_complete_same_line_eisv_does_not_flag(self, tmp_path):
        script = _load_check_script()
        path = tmp_path / "state.py"
        path.write_text(
            'example = {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.0}\n'
            'message = "E=0.1, I=0.2, S=0.3, V=0.0"\n'
        )
        assert script.check_file(path) == []

    def test_incomplete_same_line_eisv_still_flags(self, tmp_path):
        script = _load_check_script()
        path = tmp_path / "state.py"
        path.write_text(
            'example = {"E": 0.1, "I": 0.2, "S": 0.3}\n'
            'message = "E=0.1, I=0.2, S=0.3"\n'
        )
        issues = script.check_file(path)
        assert len(issues) == 2


if __name__ == '__main__':
    # Run tests
    pytest.main([__file__, '-v'])
