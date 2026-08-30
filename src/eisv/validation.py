"""
EISV Response Validator

Automatically validates that all MCP responses include complete EISV metrics.
This prevents selection bias by ensuring V is never omitted.

Usage:
    from src.eisv.validation import validate_governance_response

    response = {...}
    validate_governance_response(response)  # Raises if incomplete
"""

from typing import Dict, Any, List
import logging

__all__ = (
    "IncompleteEISVError",
    "VALIDATION_ENABLED",
    "auto_validate_response",
    "validate_csv_row",
    "validate_eisv_in_dict",
    "validate_governance_response",
    "validate_state_file",
)


# Preserve the logger category used before the package move so existing log
# filters do not change behavior when callers adopt the canonical import.
logger = logging.getLogger("src.eisv_validator")


class IncompleteEISVError(ValueError):
    """Raised when EISV metrics are incomplete in a response."""
    pass


def validate_eisv_in_dict(
    data: Dict[str, Any],
    context: str = "unknown"
) -> List[str]:
    """
    Validate that a dict contains all EISV metrics.

    Args:
        data: Dictionary that should contain E, I, S, V
        context: Description of where this data came from (for error messages)

    Returns:
        List of warnings (empty if all good)

    Raises:
        IncompleteEISVError: If any metric is missing
    """
    required = {'E', 'I', 'S', 'V'}
    present = set(data.keys()) & required
    missing = required - present

    if missing:
        error_msg = (
            f"Incomplete EISV metrics in {context}. "
            f"Missing: {sorted(missing)}. "
            f"Present: {sorted(present)}. "
            f"Always report all four (E, I, S, V) to prevent selection bias."
        )
        raise IncompleteEISVError(error_msg)

    # Check for None values
    none_values = [k for k in required if data.get(k) is None]
    if none_values:
        error_msg = (
            f"EISV metrics in {context} contain None values: {sorted(none_values)}. "
            f"All four metrics (E, I, S, V) must have numeric values."
        )
        raise IncompleteEISVError(error_msg)

    return []


def validate_governance_response(response: Dict[str, Any]) -> None:
    """
    Validate that a governance response includes complete EISV metrics.

    This should be called on every MCP response to ensure completeness.

    Args:
        response: MCP response dict

    Raises:
        IncompleteEISVError: If EISV metrics are incomplete

    Example:
        >>> response = mcp_server.process_agent_update(...)
        >>> validate_governance_response(response)  # Ensures EISV complete
    """
    # Check if response has metrics section
    if 'metrics' not in response:
        logger.warning(f"Response missing 'metrics' section entirely: {list(response.keys())}")
        return  # Some responses may not have metrics (e.g., errors)

    metrics = response['metrics']

    # Validate EISV completeness
    try:
        validate_eisv_in_dict(metrics, context="response['metrics']")
    except IncompleteEISVError as e:
        # Log error for debugging
        logger.error(f"EISV validation failed: {e}")
        logger.error(f"Response keys: {list(response.keys())}")
        logger.error(f"Metrics keys: {list(metrics.keys())}")
        raise

    # Additional validation: check eisv_labels if present
    if 'eisv_labels' in response:
        labels = response['eisv_labels']
        label_keys = set(labels.keys())
        if label_keys != {'E', 'I', 'S', 'V'}:
            logger.warning(
                f"EISV labels incomplete or inconsistent. "
                f"Expected: {{E, I, S, V}}, Got: {sorted(label_keys)}"
            )


def validate_csv_row(row: Dict[str, Any], row_num: int = 0) -> None:
    """
    Validate that a CSV row contains all EISV columns.

    Args:
        row: Dict representing CSV row
        row_num: Row number (for error messages)

    Raises:
        IncompleteEISVError: If any EISV column is missing
    """
    validate_eisv_in_dict(row, context=f"CSV row {row_num}")


def validate_state_file(state: Dict[str, Any], filename: str = "unknown") -> None:
    """
    Validate that a state file contains complete EISV metrics.

    Args:
        state: State dict loaded from JSON
        filename: State file name (for error messages)

    Raises:
        IncompleteEISVError: If EISV metrics are incomplete
    """
    validate_eisv_in_dict(state, context=f"state file {filename}")


# Hook for automatic validation (can be enabled in MCP server)
VALIDATION_ENABLED = True


def auto_validate_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Automatically validate response (decorator/wrapper pattern).

    Args:
        response: MCP response

    Returns:
        Same response (if valid)

    Raises:
        IncompleteEISVError: If validation fails
    """
    if VALIDATION_ENABLED:
        try:
            validate_governance_response(response)
        except IncompleteEISVError as e:
            # Add validation error to response
            response['_eisv_validation_error'] = str(e)
            logger.error(f"Auto-validation failed: {e}")
            raise

    return response


if __name__ == '__main__':
    print("=== EISV Validation Examples ===\n")

    # Example 1: Valid response
    print("1. Valid response (all four metrics):")
    valid_response = {
        'success': True,
        'metrics': {
            'E': 0.8,
            'I': 1.0,
            'S': 0.03,
            'V': -0.07,
            'coherence': 0.47
        }
    }
    try:
        validate_governance_response(valid_response)
        print("✓ Validation passed")
    except IncompleteEISVError as e:
        print(f"✗ Validation failed: {e}")
    print()

    # Example 2: Missing V
    print("2. Invalid response (missing V):")
    invalid_response = {
        'success': True,
        'metrics': {
            'E': 0.8,
            'I': 1.0,
            'S': 0.03,
            # V is missing!
            'coherence': 0.47
        }
    }
    try:
        validate_governance_response(invalid_response)
        print("✗ Validation should have failed!")
    except IncompleteEISVError as e:
        print(f"✓ Caught incomplete metrics: {e}")
    print()

    # Example 3: None value
    print("3. Invalid response (V is None):")
    none_response = {
        'success': True,
        'metrics': {
            'E': 0.8,
            'I': 1.0,
            'S': 0.03,
            'V': None,  # None is not allowed
            'coherence': 0.47
        }
    }
    try:
        validate_governance_response(none_response)
        print("✗ Validation should have failed!")
    except IncompleteEISVError as e:
        print(f"✓ Caught None value: {e}")
