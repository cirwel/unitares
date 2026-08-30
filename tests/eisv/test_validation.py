"""
Tests for src.eisv.validation - EISV response validation.

All functions are pure (input -> output). No mocking needed.
"""

import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parents[2]
sys.path.insert(0, str(project_root))

from src.eisv.validation import (
    IncompleteEISVError,
    validate_eisv_in_dict,
    validate_governance_response,
    validate_csv_row,
    validate_state_file,
    auto_validate_response,
)


# ============================================================================
# IncompleteEISVError
# ============================================================================

class TestIncompleteEISVError:

    def test_is_value_error(self):
        err = IncompleteEISVError("test")
        assert isinstance(err, ValueError)

    def test_message(self):
        err = IncompleteEISVError("Missing V metric")
        assert "Missing V metric" in str(err)


# ============================================================================
# validate_eisv_in_dict
# ============================================================================

class TestValidateEISVInDict:

    def test_complete(self):
        data = {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07}
        result = validate_eisv_in_dict(data)
        assert result == []

    def test_extra_keys(self):
        data = {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07, 'coherence': 0.5}
        result = validate_eisv_in_dict(data)
        assert result == []

    def test_missing_v(self):
        data = {'E': 0.8, 'I': 1.0, 'S': 0.03}
        with pytest.raises(IncompleteEISVError, match="Missing"):
            validate_eisv_in_dict(data)

    def test_missing_all(self):
        data = {'coherence': 0.5}
        with pytest.raises(IncompleteEISVError, match="Missing"):
            validate_eisv_in_dict(data)

    def test_missing_e_and_s(self):
        data = {'I': 1.0, 'V': 0.0}
        with pytest.raises(IncompleteEISVError, match="Missing"):
            validate_eisv_in_dict(data)

    def test_none_values(self):
        data = {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': None}
        with pytest.raises(IncompleteEISVError, match="None values"):
            validate_eisv_in_dict(data)

    def test_multiple_none(self):
        data = {'E': None, 'I': None, 'S': 0.03, 'V': -0.07}
        with pytest.raises(IncompleteEISVError, match="None values"):
            validate_eisv_in_dict(data)

    def test_context_in_error(self):
        data = {'E': 0.8, 'I': 1.0, 'S': 0.03}
        with pytest.raises(IncompleteEISVError, match="my_response"):
            validate_eisv_in_dict(data, context="my_response")

    def test_default_context(self):
        data = {'E': 0.8}
        with pytest.raises(IncompleteEISVError, match="unknown"):
            validate_eisv_in_dict(data)


# ============================================================================
# validate_governance_response
# ============================================================================

class TestValidateGovernanceResponse:

    def test_valid(self):
        response = {
            'metrics': {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07}
        }
        validate_governance_response(response)  # Should not raise

    def test_missing_metrics(self):
        response = {'success': True}
        validate_governance_response(response)  # Should not raise (no metrics section)

    def test_incomplete_metrics(self):
        response = {
            'metrics': {'E': 0.8, 'I': 1.0, 'S': 0.03}
        }
        with pytest.raises(IncompleteEISVError):
            validate_governance_response(response)

    def test_none_in_metrics(self):
        response = {
            'metrics': {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': None}
        }
        with pytest.raises(IncompleteEISVError):
            validate_governance_response(response)

    def test_eisv_labels_valid(self):
        response = {
            'metrics': {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07},
            'eisv_labels': {'E': 'Energy', 'I': 'Integrity', 'S': 'Entropy', 'V': 'Void'}
        }
        validate_governance_response(response)  # Should not raise

    def test_eisv_labels_incomplete(self):
        """Incomplete labels should not raise but will log warning."""
        response = {
            'metrics': {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07},
            'eisv_labels': {'E': 'Energy', 'I': 'Integrity'}
        }
        validate_governance_response(response)  # Should not raise (just warns)


# ============================================================================
# validate_csv_row
# ============================================================================

class TestValidateCsvRow:

    def test_valid(self):
        row = {'E': 0.5, 'I': 0.5, 'S': 0.5, 'V': 0.0, 'other': 'data'}
        validate_csv_row(row, row_num=1)  # Should not raise

    def test_invalid(self):
        row = {'E': 0.5, 'S': 0.5}
        with pytest.raises(IncompleteEISVError, match="CSV row 5"):
            validate_csv_row(row, row_num=5)

    def test_default_row_num(self):
        row = {'E': 0.5}
        with pytest.raises(IncompleteEISVError, match="CSV row 0"):
            validate_csv_row(row)


# ============================================================================
# validate_state_file
# ============================================================================

class TestValidateStateFile:

    def test_valid(self):
        state = {'E': 0.5, 'I': 0.5, 'S': 0.5, 'V': 0.0}
        validate_state_file(state, filename="agent_state.json")  # Should not raise

    def test_invalid(self):
        state = {'E': 0.5}
        with pytest.raises(IncompleteEISVError, match="state file myfile.json"):
            validate_state_file(state, filename="myfile.json")

    def test_default_filename(self):
        state = {'I': 0.5}
        with pytest.raises(IncompleteEISVError, match="state file unknown"):
            validate_state_file(state)


# ============================================================================
# auto_validate_response
# ============================================================================

class TestAutoValidateResponse:

    def test_valid(self):
        response = {
            'metrics': {'E': 0.8, 'I': 1.0, 'S': 0.03, 'V': -0.07}
        }
        result = auto_validate_response(response)
        assert result is response

    def test_invalid_annotates(self):
        response = {
            'metrics': {'E': 0.8, 'I': 1.0, 'S': 0.03}
        }
        with pytest.raises(IncompleteEISVError):
            auto_validate_response(response)
        # Response should have been annotated before raise
        assert '_eisv_validation_error' in response

    def test_no_metrics(self):
        """Response without metrics section passes validation."""
        response = {'success': True}
        result = auto_validate_response(response)
        assert result is response
