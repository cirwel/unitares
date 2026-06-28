"""
Tests for pure functions in src/mcp_handlers/utils.py.
"""

import pytest
import sys
import numpy as np
from pathlib import Path
from datetime import datetime, date
from enum import Enum

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.utils import (
    _infer_error_code_and_category,
    _sanitize_error_message,
    _make_json_serializable,
    format_metrics_report,
    format_metrics_text,
    generate_actionable_feedback,
)


class TestInferErrorCodeAndCategory:
    """Tests for _infer_error_code_and_category."""

    # --- Validation errors ---

    def test_not_found(self):
        code, cat = _infer_error_code_and_category("Agent not found")
        assert code == "NOT_FOUND"
        assert cat == "validation_error"

    def test_does_not_exist(self):
        code, cat = _infer_error_code_and_category("Resource does not exist")
        assert code == "NOT_FOUND"
        assert cat == "validation_error"

    def test_doesnt_exist(self):
        code, cat = _infer_error_code_and_category("That agent doesn't exist in the registry")
        assert code == "NOT_FOUND"
        assert cat == "validation_error"

    def test_missing_required(self):
        code, cat = _infer_error_code_and_category("Missing required parameter: agent_id")
        assert code == "MISSING_REQUIRED"
        assert cat == "validation_error"

    def test_required_parameter(self):
        code, cat = _infer_error_code_and_category("required parameter agent_id was not supplied")
        assert code == "MISSING_REQUIRED"
        assert cat == "validation_error"

    def test_must_provide(self):
        code, cat = _infer_error_code_and_category("You must provide an agent_id")
        assert code == "MISSING_REQUIRED"
        assert cat == "validation_error"

    def test_invalid(self):
        code, cat = _infer_error_code_and_category("Invalid agent_id format")
        assert code == "INVALID_PARAM"
        assert cat == "validation_error"

    def test_must_be(self):
        code, cat = _infer_error_code_and_category("Value must be a positive integer")
        assert code == "INVALID_PARAM"
        assert cat == "validation_error"

    def test_should_be(self):
        code, cat = _infer_error_code_and_category("Confidence should be between 0 and 1")
        assert code == "INVALID_PARAM"
        assert cat == "validation_error"

    def test_already_exists(self):
        code, cat = _infer_error_code_and_category("Agent already exists with that name")
        assert code == "ALREADY_EXISTS"
        assert cat == "validation_error"

    def test_duplicate(self):
        code, cat = _infer_error_code_and_category("Duplicate entry detected")
        assert code == "ALREADY_EXISTS"
        assert cat == "validation_error"

    def test_too_long(self):
        code, cat = _infer_error_code_and_category("Name is too long")
        assert code == "VALUE_TOO_LARGE"
        assert cat == "validation_error"

    def test_exceeds_maximum(self):
        code, cat = _infer_error_code_and_category("Input exceeds maximum length")
        assert code == "VALUE_TOO_LARGE"
        assert cat == "validation_error"

    def test_too_large(self):
        code, cat = _infer_error_code_and_category("The payload is too large to process")
        assert code == "VALUE_TOO_LARGE"
        assert cat == "validation_error"

    def test_too_short(self):
        code, cat = _infer_error_code_and_category("Password is too short")
        assert code == "VALUE_TOO_SMALL"
        assert cat == "validation_error"

    def test_too_small(self):
        code, cat = _infer_error_code_and_category("Value is too small for this field")
        assert code == "VALUE_TOO_SMALL"
        assert cat == "validation_error"

    def test_minimum(self):
        code, cat = _infer_error_code_and_category("Below minimum threshold")
        assert code == "VALUE_TOO_SMALL"
        assert cat == "validation_error"

    def test_empty(self):
        code, cat = _infer_error_code_and_category("Field is empty")
        assert code == "EMPTY_VALUE"
        assert cat == "validation_error"

    def test_cannot_be_empty(self):
        code, cat = _infer_error_code_and_category("Name cannot be empty")
        assert code == "EMPTY_VALUE"
        assert cat == "validation_error"

    # --- Auth errors ---

    def test_permission(self):
        code, cat = _infer_error_code_and_category("Insufficient permission to access resource")
        assert code == "PERMISSION_DENIED"
        assert cat == "auth_error"

    def test_not_authorized(self):
        code, cat = _infer_error_code_and_category("User is not authorized")
        assert code == "PERMISSION_DENIED"
        assert cat == "auth_error"

    def test_forbidden(self):
        code, cat = _infer_error_code_and_category("Access forbidden for this endpoint")
        assert code == "PERMISSION_DENIED"
        assert cat == "auth_error"

    def test_access_denied(self):
        code, cat = _infer_error_code_and_category("Access denied")
        assert code == "PERMISSION_DENIED"
        assert cat == "auth_error"

    def test_api_key(self):
        code, cat = _infer_error_code_and_category("Your api key has been revoked")
        assert code == "API_KEY_ERROR"
        assert cat == "auth_error"

    def test_apikey(self):
        code, cat = _infer_error_code_and_category("Apikey has expired")
        assert code == "API_KEY_ERROR"
        assert cat == "auth_error"

    def test_session(self):
        code, cat = _infer_error_code_and_category("Session has expired")
        assert code == "SESSION_ERROR"
        assert cat == "auth_error"

    def test_identity_not_resolved(self):
        code, cat = _infer_error_code_and_category("identity not resolved for this request")
        assert code == "SESSION_ERROR"
        assert cat == "auth_error"

    # --- State errors ---

    def test_paused(self):
        code, cat = _infer_error_code_and_category("Agent is paused and cannot process")
        assert code == "AGENT_PAUSED"
        assert cat == "state_error"

    def test_archived(self):
        code, cat = _infer_error_code_and_category("Agent is archived")
        assert code == "AGENT_ARCHIVED"
        assert cat == "state_error"

    def test_deleted(self):
        code, cat = _infer_error_code_and_category("Resource is deleted")
        assert code == "AGENT_DELETED"
        assert cat == "state_error"

    def test_locked(self):
        code, cat = _infer_error_code_and_category("Resource is locked by another process")
        assert code == "RESOURCE_LOCKED"
        assert cat == "state_error"

    def test_already_locked(self):
        code, cat = _infer_error_code_and_category("Resource already locked")
        assert code == "RESOURCE_LOCKED"
        assert cat == "state_error"

    # --- System errors ---

    def test_timeout(self):
        code, cat = _infer_error_code_and_category("Request timeout after 30s")
        assert code == "TIMEOUT"
        assert cat == "system_error"

    def test_timed_out(self):
        code, cat = _infer_error_code_and_category("Operation timed out")
        assert code == "TIMEOUT"
        assert cat == "system_error"

    def test_tool_timeout_with_session_tool_name_is_system_error(self):
        code, cat = _infer_error_code_and_category(
            "Tool 'list_dialectic_sessions' timed out after 15.0 seconds."
        )
        assert code == "TIMEOUT"
        assert cat == "system_error"

    def test_connection(self):
        code, cat = _infer_error_code_and_category("Connection refused by server")
        assert code == "CONNECTION_ERROR"
        assert cat == "system_error"

    def test_connect(self):
        code, cat = _infer_error_code_and_category("Could not connect to backend")
        assert code == "CONNECTION_ERROR"
        assert cat == "system_error"

    def test_database(self):
        code, cat = _infer_error_code_and_category("Database query failed")
        assert code == "DATABASE_ERROR"
        assert cat == "system_error"

    def test_postgres(self):
        code, cat = _infer_error_code_and_category("Postgres pool exhausted")
        assert code == "DATABASE_ERROR"
        assert cat == "system_error"

    def test_db_error(self):
        code, cat = _infer_error_code_and_category("Encountered a db error during save")
        assert code == "DATABASE_ERROR"
        assert cat == "system_error"

    def test_failed_to(self):
        code, cat = _infer_error_code_and_category("Failed to update agent state")
        assert code == "OPERATION_FAILED"
        assert cat == "system_error"

    def test_could_not(self):
        code, cat = _infer_error_code_and_category("Could not serialize response")
        assert code == "OPERATION_FAILED"
        assert cat == "system_error"

    def test_unable_to(self):
        code, cat = _infer_error_code_and_category("Unable to parse input")
        assert code == "OPERATION_FAILED"
        assert cat == "system_error"

    # --- Edge cases ---

    def test_no_match_returns_none_none(self):
        code, cat = _infer_error_code_and_category("Something completely unexpected happened")
        assert code is None
        assert cat is None

    def test_case_insensitive(self):
        code, cat = _infer_error_code_and_category("AGENT NOT FOUND IN REGISTRY")
        assert code == "NOT_FOUND"
        assert cat == "validation_error"

    def test_case_insensitive_mixed(self):
        code, cat = _infer_error_code_and_category("Missing Required field: name")
        assert code == "MISSING_REQUIRED"
        assert cat == "validation_error"

    def test_first_matching_pattern_wins(self):
        code, cat = _infer_error_code_and_category("not found and invalid and timeout")
        assert code == "NOT_FOUND"
        assert cat == "validation_error"

    def test_empty_message(self):
        code, cat = _infer_error_code_and_category("")
        assert code is None
        assert cat is None


class TestSanitizeErrorMessage:
    """Tests for _sanitize_error_message."""

    def test_removes_full_file_path_keeps_filename(self):
        msg = "Error in /Users/testuser/projects/unitares/src/mcp_handlers/utils.py"
        result = _sanitize_error_message(msg)
        assert "/Users/testuser/" not in result
        assert "utils.py" in result

    def test_removes_unix_path(self):
        msg = "File /home/user/project/deep/nested/module.py caused an issue"
        result = _sanitize_error_message(msg)
        assert "/home/user/" not in result
        assert "module.py" in result

    def test_simplifies_stack_trace_line_references(self):
        msg = "Error occurred, line 42, in process_request"
        result = _sanitize_error_message(msg)
        assert "line 42" not in result
        assert "in process_request" not in result

    def test_replaces_standalone_line_numbers(self):
        msg = "Error at line 123 during execution"
        result = _sanitize_error_message(msg)
        assert "line 123" not in result
        assert "line N" in result

    def test_simplifies_full_traceback(self):
        msg = (
            "Traceback (most recent call last):\n"
            '  File "foo.py", line 10, in bar\n'
            "    do_stuff()\n"
            "ValueError: bad value"
        )
        result = _sanitize_error_message(msg)
        assert "Traceback" not in result
        assert "bad value" in result

    def test_removes_internal_module_paths(self):
        msg = "Error in src.mcp_handlers.utils.foo during processing"
        result = _sanitize_error_message(msg)
        assert "src.mcp_handlers.utils." not in result
        assert "foo" in result

    def test_cleans_governance_core_deep_paths(self):
        msg = "Error in governance_core.module.submodule.func"
        result = _sanitize_error_message(msg)
        assert "governance_core.module.submodule." not in result
        assert "governance_core." in result

    def test_preserves_uppercase_error_codes(self):
        msg = "AGENT_NOT_FOUND: The requested agent could not be located"
        result = _sanitize_error_message(msg)
        assert "AGENT_NOT_FOUND" in result

    def test_cleans_double_spaces(self):
        msg = "Error  occurred   during  processing"
        result = _sanitize_error_message(msg)
        assert "  " not in result

    def test_cleans_extra_blank_lines(self):
        msg = "Error occurred\n\n\nduring processing"
        result = _sanitize_error_message(msg)
        assert "\n\n" not in result

    def test_truncates_long_message_at_sentence_boundary(self):
        long_msg = "First sentence. " * 40
        result = _sanitize_error_message(long_msg)
        assert len(result) <= 500
        assert result.endswith(".")

    def test_truncates_long_message_with_ellipsis_when_no_sentence_boundary(self):
        long_msg = "a" * 600
        result = _sanitize_error_message(long_msg)
        assert len(result) <= 503
        assert result.endswith("...")

    def test_non_string_input_returns_str(self):
        result = _sanitize_error_message(12345)
        assert result == "12345"

    def test_non_string_input_none(self):
        result = _sanitize_error_message(None)
        assert result == "None"

    def test_strips_whitespace(self):
        msg = "  some error message  "
        result = _sanitize_error_message(msg)
        assert result == "some error message"

    def test_preserves_agent_ids_and_tool_names(self):
        msg = "Tool process_agent_update failed for agent abc-123"
        result = _sanitize_error_message(msg)
        assert "process_agent_update" in result
        assert "abc-123" in result

    def test_simple_message_unchanged(self):
        msg = "Agent not found"
        result = _sanitize_error_message(msg)
        assert "Agent not found" in result


class TestMakeJsonSerializable:
    """Tests for _make_json_serializable."""

    def test_none_returns_none(self):
        assert _make_json_serializable(None) is None

    def test_numpy_float64_to_float(self):
        val = np.float64(3.14)
        result = _make_json_serializable(val)
        assert isinstance(result, float)
        assert result == pytest.approx(3.14)

    def test_numpy_int64_to_int(self):
        val = np.int64(42)
        result = _make_json_serializable(val)
        assert isinstance(result, int)
        assert result == 42

    def test_numpy_array_to_list(self):
        arr = np.array([1, 2, 3])
        result = _make_json_serializable(arr)
        assert isinstance(result, list)
        assert result == [1, 2, 3]

    def test_numpy_bool_to_bool(self):
        val = np.bool_(True)
        result = _make_json_serializable(val)
        assert isinstance(result, bool)
        assert result is True

    def test_numpy_bool_false(self):
        val = np.bool_(False)
        result = _make_json_serializable(val)
        assert isinstance(result, bool)
        assert result is False

    def test_datetime_to_isoformat(self):
        dt = datetime(2026, 2, 6, 12, 30, 45)
        result = _make_json_serializable(dt)
        assert isinstance(result, str)
        assert result == "2026-02-06T12:30:45"

    def test_date_to_isoformat(self):
        d = date(2026, 2, 6)
        result = _make_json_serializable(d)
        assert isinstance(result, str)
        assert result == "2026-02-06"

    def test_enum_to_value(self):
        class Color(Enum):
            RED = "red"
            BLUE = "blue"

        result = _make_json_serializable(Color.RED)
        assert result == "red"

    def test_enum_int_value(self):
        class Priority(Enum):
            HIGH = 1
            LOW = 2

        result = _make_json_serializable(Priority.HIGH)
        assert result == 1

    def test_dict_recursive(self):
        data = {"count": np.int64(5), "ratio": np.float64(0.5)}
        result = _make_json_serializable(data)
        assert isinstance(result, dict)
        assert isinstance(result["count"], int)
        assert isinstance(result["ratio"], float)

    def test_dict_nested(self):
        data = {"outer": {"inner": np.float64(1.5)}}
        result = _make_json_serializable(data)
        assert isinstance(result["outer"]["inner"], float)

    def test_list_recursive(self):
        data = [np.int64(1), np.float64(2.0), "three"]
        result = _make_json_serializable(data)
        assert result == [1, 2.0, "three"]
        assert isinstance(result[0], int)
        assert isinstance(result[1], float)

    def test_tuple_recursive(self):
        data = (np.int64(1), np.float64(2.0))
        result = _make_json_serializable(data)
        assert isinstance(result, list)
        assert result == [1, 2.0]

    def test_set_to_list(self):
        data = {1, 2, 3}
        result = _make_json_serializable(data)
        assert isinstance(result, list)
        assert sorted(result) == [1, 2, 3]

    def test_large_list_truncated(self):
        data = list(range(150))
        result = _make_json_serializable(data)
        assert len(result) == 101
        assert result[-1] == "... (50 more items)"
        assert result[0] == 0
        assert result[99] == 99

    def test_large_set_truncated(self):
        data = set(range(150))
        result = _make_json_serializable(data)
        assert len(result) == 101
        assert result[-1] == "... (50 more items)"

    def test_list_at_boundary_not_truncated(self):
        data = list(range(100))
        result = _make_json_serializable(data)
        assert len(result) == 100

    def test_str_passthrough(self):
        assert _make_json_serializable("hello") == "hello"

    def test_int_passthrough(self):
        assert _make_json_serializable(42) == 42

    def test_float_passthrough(self):
        assert _make_json_serializable(3.14) == 3.14

    def test_bool_passthrough(self):
        assert _make_json_serializable(True) is True
        assert _make_json_serializable(False) is False

    def test_non_serializable_object_to_str(self):
        class Custom:
            def __str__(self):
                return "custom_object"

        result = _make_json_serializable(Custom())
        assert result == "custom_object"

    def test_non_serializable_without_str_fallback(self):
        class Opaque:
            pass

        result = _make_json_serializable(Opaque())
        assert isinstance(result, str)
        assert "Opaque" in result

    def test_mixed_nested_structure(self):
        class Status(Enum):
            ACTIVE = "active"

        data = {
            "id": np.int64(1),
            "values": np.array([1.0, 2.0]),
            "status": Status.ACTIVE,
            "created": datetime(2026, 1, 1),
            "tags": {"a", "b"},
            "nested": {"score": np.float64(0.95)},
        }
        result = _make_json_serializable(data)
        assert result["id"] == 1
        assert result["values"] == [1.0, 2.0]
        assert result["status"] == "active"
        assert result["created"] == "2026-01-01T00:00:00"
        assert isinstance(result["tags"], list)
        assert sorted(result["tags"]) == ["a", "b"]
        assert isinstance(result["nested"]["score"], float)


class TestFormatMetricsReport:
    """Tests for format_metrics_report."""

    def test_basic_agent_id_included(self):
        metrics = {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}
        result = format_metrics_report(metrics, "agent_123")
        assert result["agent_id"] == "agent_123"

    def test_timestamp_included_by_default(self):
        metrics = {"E": 0.5}
        result = format_metrics_report(metrics, "agent_1")
        assert "timestamp" in result

    def test_timestamp_excluded_when_false(self):
        metrics = {"E": 0.5}
        result = format_metrics_report(metrics, "agent_1", include_timestamp=False)
        assert "timestamp" not in result

    def test_include_context_creates_eisv_dict(self):
        metrics = {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}
        result = format_metrics_report(metrics, "agent_1", include_timestamp=False)
        assert "eisv" in result
        assert result["eisv"] == {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}

    def test_include_context_preserves_flat_eisv(self):
        metrics = {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}
        result = format_metrics_report(metrics, "agent_1", include_timestamp=False)
        assert result["E"] == 0.8
        assert result["I"] == 0.9

    def test_include_context_no_eisv_when_not_present(self):
        metrics = {"coherence": 0.7}
        result = format_metrics_report(metrics, "agent_1", include_timestamp=False)
        assert "eisv" not in result

    def test_include_context_preserves_health_status(self):
        metrics = {"health_status": "healthy", "coherence": 0.9}
        result = format_metrics_report(metrics, "agent_1", include_timestamp=False)
        assert result["health_status"] == "healthy"

    def test_include_context_false_no_eisv(self):
        metrics = {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}
        result = format_metrics_report(
            metrics, "agent_1", include_timestamp=False, include_context=False
        )
        assert "eisv" not in result

    def test_format_style_text_returns_string(self):
        metrics = {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}
        result = format_metrics_report(metrics, "agent_1", format_style="text")
        assert isinstance(result, str)
        assert "agent_1" in result

    def test_agent_id_overrides_metrics_agent_id(self):
        metrics = {"agent_id": "old_id", "coherence": 0.5}
        result = format_metrics_report(metrics, "new_id", include_timestamp=False)
        assert result["agent_id"] == "new_id"

    def test_original_metrics_preserved(self):
        metrics = {"coherence": 0.7, "risk_score": 0.3, "phi": 1.2}
        result = format_metrics_report(
            metrics, "agent_1", include_timestamp=False, include_context=False
        )
        assert result["coherence"] == 0.7
        assert result["risk_score"] == 0.3
        assert result["phi"] == 1.2


class TestFormatMetricsText:
    """Tests for format_metrics_text."""

    def test_header_has_agent_id(self):
        metrics = {"agent_id": "agent_xyz"}
        result = format_metrics_text(metrics)
        assert "Agent: agent_xyz" in result

    def test_default_agent_id_when_missing(self):
        result = format_metrics_text({})
        assert "Agent: unknown" in result

    def test_timestamp_included_if_present(self):
        metrics = {"agent_id": "a1", "timestamp": "2026-02-06T12:00:00"}
        result = format_metrics_text(metrics)
        assert "Timestamp: 2026-02-06T12:00:00" in result

    def test_no_timestamp_line_when_absent(self):
        metrics = {"agent_id": "a1"}
        result = format_metrics_text(metrics)
        assert "Timestamp" not in result

    def test_health_status_shown_if_present(self):
        metrics = {"agent_id": "a1", "health_status": "degraded"}
        result = format_metrics_text(metrics)
        assert "Health: degraded" in result

    def test_no_health_line_when_absent(self):
        metrics = {"agent_id": "a1"}
        result = format_metrics_text(metrics)
        assert "Health" not in result

    def test_eisv_from_eisv_dict(self):
        metrics = {
            "agent_id": "a1",
            "eisv": {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05},
        }
        result = format_metrics_text(metrics)
        assert "EISV:" in result
        assert "E=0.800" in result
        assert "I=0.900" in result
        assert "S=0.100" in result
        assert "V=-0.050" in result

    def test_eisv_from_flat_keys(self):
        metrics = {"agent_id": "a1", "E": 0.5, "I": 0.6, "S": 0.2, "V": 0.0}
        result = format_metrics_text(metrics)
        assert "EISV:" in result
        assert "E=0.500" in result

    def test_eisv_nested_takes_priority_over_flat(self):
        metrics = {
            "agent_id": "a1",
            "eisv": {"E": 0.9, "I": 0.9, "S": 0.0, "V": 0.0},
            "E": 0.1,
            "I": 0.1,
        }
        result = format_metrics_text(metrics)
        assert "E=0.900" in result

    def test_key_metrics_formatted_as_float(self):
        metrics = {"agent_id": "a1", "coherence": 0.75, "risk_score": 0.3}
        result = format_metrics_text(metrics)
        assert "coherence: 0.750" in result
        assert "risk_score: 0.300" in result

    def test_non_float_key_metric(self):
        metrics = {"agent_id": "a1", "verdict": "continue"}
        result = format_metrics_text(metrics)
        assert "verdict: continue" in result

    def test_full_metrics(self):
        metrics = {
            "agent_id": "test-agent",
            "timestamp": "2026-01-15",
            "health_status": "healthy",
            "eisv": {"E": 0.5, "I": 0.6, "S": 0.1, "V": 0.0},
            "coherence": 0.8,
            "risk_score": 0.2,
        }
        result = format_metrics_text(metrics)
        assert "test-agent" in result
        assert "healthy" in result
        assert "E=0.500" in result
        assert "coherence: 0.800" in result
        assert "risk_score: 0.200" in result

    def test_empty_metrics(self):
        result = format_metrics_text({})
        assert "Agent: unknown" in result


class TestGenerateActionableFeedback:
    """Tests for generate_actionable_feedback."""

    # --- Coherence feedback ---

    def test_first_update_no_coherence_feedback(self):
        metrics = {"coherence": 0.2, "updates": 1}
        result = generate_actionable_feedback(metrics)
        coherence_msgs = [f for f in result if "coherence" in f.lower() or "Coherence" in f]
        assert len(coherence_msgs) == 0

    def test_first_update_zero_no_coherence_feedback(self):
        metrics = {"coherence": 0.2, "updates": 0}
        result = generate_actionable_feedback(metrics)
        coherence_msgs = [f for f in result if "coherence" in f.lower() or "Coherence" in f]
        assert len(coherence_msgs) == 0

    def test_exploration_low_coherence_dropped(self):
        metrics = {"coherence": 0.2, "regime": "exploration", "updates": 5}
        result = generate_actionable_feedback(metrics, previous_coherence=0.5)
        assert any("dropped significantly" in f for f in result)
        assert any("most promising direction" in f for f in result)

    def test_exploration_low_coherence_not_dropped(self):
        metrics = {"coherence": 0.2, "regime": "exploration", "updates": 5}
        result = generate_actionable_feedback(metrics, previous_coherence=0.2)
        assert any("Very low coherence" in f and "exploration" in f for f in result)
        assert any("hypotheses" in f for f in result)

    def test_exploration_low_coherence_no_previous(self):
        metrics = {"coherence": 0.2, "regime": "exploration", "updates": 5}
        result = generate_actionable_feedback(metrics, previous_coherence=None)
        assert any("Very low coherence" in f for f in result)

    def test_stable_regime_low_coherence_dropped(self):
        metrics = {"coherence": 0.5, "regime": "stable", "updates": 5}
        result = generate_actionable_feedback(metrics, previous_coherence=0.8)
        assert any("Unexpected coherence drop" in f for f in result)
        assert any("disrupted your flow" in f for f in result)

    def test_stable_regime_low_coherence_not_dropped(self):
        metrics = {"coherence": 0.5, "regime": "stable", "updates": 5}
        result = generate_actionable_feedback(metrics, previous_coherence=0.55)
        assert any("drift" in f.lower() for f in result)
        assert any("original plan" in f for f in result)

    def test_locked_regime_low_coherence_dropped(self):
        metrics = {"coherence": 0.5, "regime": "locked", "updates": 5}
        result = generate_actionable_feedback(metrics, previous_coherence=0.8)
        assert any("Unexpected coherence drop" in f for f in result)

    def test_locked_regime_low_coherence_not_dropped(self):
        metrics = {"coherence": 0.6, "regime": "locked", "updates": 5}
        result = generate_actionable_feedback(metrics, previous_coherence=0.55)
        assert any("drift" in f.lower() for f in result)

    def test_convergent_task_low_coherence(self):
        metrics = {"coherence": 0.4, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(metrics, task_type="convergent")
        assert any("convergent task" in f for f in result)
        assert any("one sentence" in f for f in result)

    def test_divergent_task_very_low_coherence(self):
        metrics = {"coherence": 0.3, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(metrics, task_type="divergent")
        assert any("top 3" in f for f in result)

    def test_divergent_task_moderate_low_coherence_no_feedback(self):
        metrics = {"coherence": 0.45, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(metrics, task_type="divergent")
        coherence_msgs = [f for f in result if "coherence" in f.lower() or "Coherence" in f]
        assert len(coherence_msgs) == 0

    def test_mixed_task_low_coherence(self):
        metrics = {"coherence": 0.4, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(metrics, task_type="mixed")
        assert any("articulate your current goal" in f for f in result)

    def test_unknown_task_defaults_to_mixed(self):
        metrics = {"coherence": 0.4, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(metrics)
        assert any("articulate your current goal" in f for f in result)

    # --- Risk feedback ---

    def test_high_risk_void_basin(self):
        metrics = {"risk_score": 0.8, "updates": 5}
        state = {"health": "unknown", "mode": "unknown", "basin": "void"}
        result = generate_actionable_feedback(metrics, interpreted_state=state)
        assert any("wrong thing" in f for f in result)

    def test_high_risk_other_basin(self):
        metrics = {"risk_score": 0.8, "updates": 5}
        state = {"health": "unknown", "mode": "unknown", "basin": "active"}
        result = generate_actionable_feedback(metrics, interpreted_state=state)
        assert any("Break task" in f or "smaller pieces" in f for f in result)

    def test_moderate_risk_degraded_health(self):
        metrics = {"risk_score": 0.6, "updates": 5}
        state = {"health": "degraded", "mode": "unknown", "basin": "unknown"}
        result = generate_actionable_feedback(metrics, interpreted_state=state)
        assert any("checkpoint" in f for f in result)

    def test_moderate_risk_healthy_no_feedback(self):
        metrics = {"risk_score": 0.6, "updates": 5}
        state = {"health": "healthy", "mode": "unknown", "basin": "unknown"}
        result = generate_actionable_feedback(metrics, interpreted_state=state)
        risk_msgs = [f for f in result if "complexity" in f.lower()]
        assert len(risk_msgs) == 0

    def test_low_risk_no_feedback(self):
        metrics = {"risk_score": 0.3, "updates": 5}
        result = generate_actionable_feedback(metrics)
        risk_msgs = [f for f in result if "complexity" in f.lower()]
        assert len(risk_msgs) == 0

    # --- Void detection ---

    def test_void_active_high_energy_low_integrity(self):
        metrics = {"void_active": True, "E": 0.8, "I": 0.3, "updates": 5}
        result = generate_actionable_feedback(metrics)
        assert any("Slow down" in f or "review your recent work" in f for f in result)

    def test_void_active_high_integrity_low_energy(self):
        metrics = {"void_active": True, "E": 0.3, "I": 0.8, "updates": 5}
        result = generate_actionable_feedback(metrics)
        assert any("blocking" in f.lower() or "slow" in f.lower() for f in result)

    def test_void_active_balanced(self):
        metrics = {"void_active": True, "E": 0.5, "I": 0.5, "updates": 5}
        result = generate_actionable_feedback(metrics)
        assert any("disconnect" in f or "misaligned" in f for f in result)

    def test_void_not_active_no_void_feedback(self):
        metrics = {"void_active": False, "E": 0.8, "I": 0.3, "updates": 5}
        result = generate_actionable_feedback(metrics)
        void_msgs = [f for f in result if "void" in f.lower() or "Void" in f]
        assert len(void_msgs) == 0

    def test_void_default_false_when_missing(self):
        metrics = {"E": 0.8, "I": 0.3, "updates": 5}
        result = generate_actionable_feedback(metrics)
        void_msgs = [f for f in result if "void" in f.lower() or "Void" in f]
        assert len(void_msgs) == 0

    # --- Response text patterns ---

    def test_not_sure_pattern(self):
        metrics = {"updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="I'm not sure how to proceed"
        )
        assert any("uncertainty" in f for f in result)
        assert any("smallest next step" in f for f in result)

    def test_dont_understand_pattern(self):
        metrics = {"updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="I don't understand the requirement"
        )
        assert any("confusion" in f for f in result)
        assert any("rephrasing" in f for f in result)

    def test_struggling_pattern(self):
        metrics = {"updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="I am struggling with this problem"
        )
        assert any("struggling" in f for f in result)
        assert any("smaller parts" in f for f in result)

    def test_stuck_pattern(self):
        metrics = {"updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="I'm stuck on this issue"
        )
        assert any("stuck" in f for f in result)
        assert any("rubber duck" in f for f in result)

    def test_only_one_confusion_feedback_per_response(self):
        metrics = {"updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="I'm not sure and I'm struggling and stuck"
        )
        confusion_msgs = [f for f in result if "smallest next step" in f]
        assert len(confusion_msgs) == 1

    def test_definitely_with_low_coherence(self):
        metrics = {"coherence": 0.4, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="This is definitely the right approach"
        )
        assert any("overconfidence" in f.lower() or "assumptions" in f for f in result)

    def test_obviously_with_low_coherence(self):
        metrics = {"coherence": 0.4, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="Obviously the answer is 42"
        )
        assert any("assumptions" in f for f in result)

    def test_clearly_with_low_coherence(self):
        metrics = {"coherence": 0.4, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="Clearly this is the best option"
        )
        assert any("assumptions" in f for f in result)

    def test_certainly_with_low_coherence(self):
        metrics = {"coherence": 0.4, "regime": "transition", "updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="This is certainly correct"
        )
        assert any("assumptions" in f for f in result)

    def test_definitely_with_high_coherence_no_overconfidence(self):
        metrics = {"coherence": 0.8, "regime": "stable", "updates": 5}
        result = generate_actionable_feedback(
            metrics, response_text="This is definitely the right approach"
        )
        overconfidence_msgs = [f for f in result if "assumptions" in f]
        assert len(overconfidence_msgs) == 0

    def test_no_response_text_no_pattern_feedback(self):
        metrics = {"updates": 5}
        result = generate_actionable_feedback(metrics, response_text=None)
        pattern_msgs = [
            f
            for f in result
            if "uncertainty" in f or "struggling" in f or "stuck" in f or "assumptions" in f
        ]
        assert len(pattern_msgs) == 0

    # --- Combined scenarios ---

    def test_empty_metrics_returns_empty_list(self):
        result = generate_actionable_feedback({})
        assert isinstance(result, list)

    def test_all_none_optionals(self):
        metrics = {"coherence": 0.5, "updates": 5}
        result = generate_actionable_feedback(
            metrics,
            interpreted_state=None,
            task_type=None,
            response_text=None,
            previous_coherence=None,
        )
        assert isinstance(result, list)

    def test_multiple_feedback_items(self):
        metrics = {
            "coherence": 0.2,
            "regime": "exploration",
            "risk_score": 0.8,
            "void_active": True,
            "E": 0.8,
            "I": 0.3,
            "updates": 5,
        }
        state = {"health": "degraded", "mode": "unknown", "basin": "void"}
        result = generate_actionable_feedback(
            metrics,
            interpreted_state=state,
            response_text="I'm stuck on this",
            previous_coherence=0.5,
        )
        assert len(result) >= 3
