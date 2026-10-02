"""
Tests for src/mcp_handlers/error_helpers.py — Standardized error response builders.

All functions are pure (just build dict structures). No mocking needed.
"""

import pytest
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.error_helpers import (
    RECOVERY_PATTERNS,
    agent_not_found_error,
    agent_not_registered_error,
    authentication_error,
    authentication_required_error,
    ownership_error,
    rate_limit_error,
    timeout_error,
    invalid_parameters_error,
    validation_error,
    resource_not_found_error,
    system_error,
    not_connected_error,
    missing_client_session_id_error,
    session_mismatch_error,
    missing_parameter_error,
    invalid_parameter_type_error,
    permission_denied_error,
    tool_not_found_error,
)


# ============================================================================
# RECOVERY_PATTERNS data structure
# ============================================================================

class TestRecoveryPatterns:

    def test_is_dict(self):
        assert isinstance(RECOVERY_PATTERNS, dict)

    def test_has_known_patterns(self):
        expected = [
            "agent_not_found", "agent_not_registered", "authentication_failed",
            "authentication_required", "ownership_required", "rate_limit_exceeded",
            "timeout", "invalid_parameters", "validation_error", "system_error",
            "resource_not_found", "not_connected", "missing_client_session_id",
            "session_mismatch", "missing_parameter", "invalid_parameter_type",
            "permission_denied",
        ]
        for key in expected:
            assert key in RECOVERY_PATTERNS, f"Missing recovery pattern: {key}"

    def test_patterns_have_required_fields(self):
        for key, pattern in RECOVERY_PATTERNS.items():
            assert "action" in pattern, f"Pattern '{key}' missing 'action'"
            assert "related_tools" in pattern, f"Pattern '{key}' missing 'related_tools'"
            assert "workflow" in pattern, f"Pattern '{key}' missing 'workflow'"


# ============================================================================
# Helper to parse TextContent result
# ============================================================================

def _parse_error(result):
    """Parse the error result (list of TextContent) into a dict."""
    assert isinstance(result, list)
    assert len(result) >= 1
    text_content = result[0]
    # TextContent has a .text attribute with JSON string
    data = json.loads(text_content.text)
    return data


# ============================================================================
# agent_not_found_error
# ============================================================================

class TestAgentNotFoundError:

    def test_basic(self):
        result = agent_not_found_error("test-agent")
        data = _parse_error(result)
        assert "test-agent" in data["error"]
        assert data["error_code"] == "AGENT_NOT_FOUND"

    def test_custom_error_code(self):
        result = agent_not_found_error("agent-1", error_code="CUSTOM_CODE")
        data = _parse_error(result)
        assert data["error_code"] == "CUSTOM_CODE"

    def test_with_context(self):
        result = agent_not_found_error("agent-1", context={"tool": "test"})
        data = _parse_error(result)
        assert data["error_code"] == "AGENT_NOT_FOUND"


# ============================================================================
# agent_not_registered_error
# ============================================================================

class TestAgentNotRegisteredError:

    def test_basic(self):
        result = agent_not_registered_error("test-agent")
        data = _parse_error(result)
        assert "test-agent" in data["error"]
        assert "not registered" in data["error"]
        assert data["error_code"] == "AGENT_NOT_REGISTERED"


# ============================================================================
# authentication_error
# ============================================================================

class TestAuthenticationError:

    def test_basic(self):
        result = authentication_error()
        data = _parse_error(result)
        assert data["error_code"] == "AUTHENTICATION_FAILED"
        assert "Authentication failed" in data["error"]

    def test_with_agent_id(self):
        result = authentication_error(agent_id="agent-1")
        data = _parse_error(result)
        assert "agent-1" in data["error"]

    def test_custom_message(self):
        result = authentication_error(message="Custom auth error")
        data = _parse_error(result)
        assert data["error"] == "Custom auth error"


# ============================================================================
# authentication_required_error
# ============================================================================

class TestAuthenticationRequiredError:

    def test_basic(self):
        result = authentication_required_error()
        data = _parse_error(result)
        assert "this operation" in data["error"]
        assert data["error_code"] == "AUTHENTICATION_REQUIRED"

    def test_custom_operation(self):
        result = authentication_required_error(operation="writing data")
        data = _parse_error(result)
        assert "writing data" in data["error"]


# ============================================================================
# ownership_error
# ============================================================================

class TestOwnershipError:

    def test_basic(self):
        result = ownership_error(
            resource_type="discovery",
            resource_id="disc-123",
            owner_agent_id="owner-1",
            caller_agent_id="caller-1"
        )
        data = _parse_error(result)
        assert data["error_code"] == "OWNERSHIP_VIOLATION"
        assert "caller-1" in data["error"]
        assert "owner-1" in data["error"]
        assert "disc-123" in data["error"]


# ============================================================================
# rate_limit_error
# ============================================================================

class TestRateLimitError:

    def test_basic(self):
        result = rate_limit_error("agent-1")
        data = _parse_error(result)
        assert data["error_code"] == "RATE_LIMIT_EXCEEDED"
        assert "agent-1" in data["error"]

    def test_with_stats(self):
        result = rate_limit_error("agent-1", stats={"calls": 100})
        data = _parse_error(result)
        assert data["error_code"] == "RATE_LIMIT_EXCEEDED"


# ============================================================================
# timeout_error
# ============================================================================

class TestTimeoutError:

    def test_basic(self):
        result = timeout_error("process_agent_update", 30.0)
        data = _parse_error(result)
        assert data["error_code"] == "TIMEOUT"
        assert "process_agent_update" in data["error"]
        assert "30" in data["error"]


# ============================================================================
# invalid_parameters_error
# ============================================================================

class TestInvalidParametersError:

    def test_basic(self):
        result = invalid_parameters_error("test_tool")
        data = _parse_error(result)
        assert data["error_code"] == "INVALID_PARAMETERS"
        assert "test_tool" in data["error"]

    def test_with_details(self):
        result = invalid_parameters_error("test_tool", details="missing required field")
        data = _parse_error(result)
        assert "missing required field" in data["error"]

    def test_with_param_name(self):
        result = invalid_parameters_error("test_tool", param_name="agent_id")
        data = _parse_error(result)
        assert data["error_code"] == "INVALID_PARAMETERS"


# ============================================================================
# validation_error
# ============================================================================

class TestValidationError:

    def test_basic(self):
        result = validation_error("Invalid value")
        data = _parse_error(result)
        assert data["error_code"] == "VALIDATION_ERROR"
        assert data["error"] == "Invalid value"

    def test_with_param(self):
        result = validation_error("Bad param", param_name="confidence")
        data = _parse_error(result)
        assert data["error_code"] == "VALIDATION_ERROR"

    def test_with_value(self):
        result = validation_error("Bad value", provided_value=1.5)
        data = _parse_error(result)
        assert data["error_code"] == "VALIDATION_ERROR"

    def test_custom_error_code(self):
        result = validation_error("Custom", error_code="CUSTOM_VALIDATION")
        data = _parse_error(result)
        assert data["error_code"] == "CUSTOM_VALIDATION"


# ============================================================================
# resource_not_found_error
# ============================================================================

class TestResourceNotFoundError:

    def test_basic(self):
        result = resource_not_found_error("discovery", "disc-123")
        data = _parse_error(result)
        assert data["error_code"] == "RESOURCE_NOT_FOUND"
        assert "Discovery" in data["error"]  # capitalized
        assert "disc-123" in data["error"]


# ============================================================================
# system_error
# ============================================================================

class TestSystemError:

    def test_basic(self):
        result = system_error("test_tool", Exception("DB down"))
        data = _parse_error(result)
        assert data["error_code"] == "SYSTEM_ERROR"
        assert "test_tool" in data["error"]
        assert "DB down" in data["error"]

    def test_with_context(self):
        result = system_error("test_tool", ValueError("bad"), context={"detail": "more info"})
        data = _parse_error(result)
        assert data["error_code"] == "SYSTEM_ERROR"


# ============================================================================
# not_connected_error
# ============================================================================

class TestNotConnectedError:

    def test_basic(self):
        result = not_connected_error()
        data = _parse_error(result)
        assert data["error_code"] == "NOT_CONNECTED"
        assert "connection" in data["error"].lower()


# ============================================================================
# missing_client_session_id_error
# ============================================================================

class TestMissingClientSessionIdError:

    def test_basic(self):
        result = missing_client_session_id_error()
        data = _parse_error(result)
        assert data["error_code"] == "MISSING_CLIENT_SESSION_ID"

    def test_custom_operation(self):
        result = missing_client_session_id_error(operation="threshold modification")
        data = _parse_error(result)
        assert "threshold modification" in data["error"]


# ============================================================================
# session_mismatch_error
# ============================================================================

class TestSessionMismatchError:

    def test_basic(self):
        result = session_mismatch_error("abcdefgh12345678")
        data = _parse_error(result)
        assert data["error_code"] == "SESSION_MISMATCH"
        assert "abcdefgh" in data["error"]

    def test_with_provided_id(self):
        result = session_mismatch_error("abcdefgh12345678", provided_id="12345678abcdefgh")
        data = _parse_error(result)
        assert "abcdefgh" in data["error"]
        assert "12345678" in data["error"]


# ============================================================================
# missing_parameter_error
# ============================================================================

class TestMissingParameterError:

    def test_basic(self):
        result = missing_parameter_error("summary")
        data = _parse_error(result)
        assert data["error_code"] == "MISSING_PARAMETER"
        assert "summary" in data["error"]

    def test_with_tool_name(self):
        result = missing_parameter_error("summary", tool_name="leave_note")
        data = _parse_error(result)
        assert "leave_note" in data["error"]

    def test_leave_note_examples(self):
        result = missing_parameter_error("summary", tool_name="leave_note")
        data = _parse_error(result)
        # details are spread into response via response.update(sanitized_details)
        assert "examples" in data

    def test_store_knowledge_graph_examples(self):
        result = missing_parameter_error("summary", tool_name="store_knowledge_graph")
        data = _parse_error(result)
        assert "examples" in data

    def test_generic_no_examples(self):
        result = missing_parameter_error("agent_id", tool_name="some_random_tool")
        data = _parse_error(result)
        assert "examples" not in data

    def test_custom_message_from_context(self):
        result = missing_parameter_error(
            "query", tool_name="search",
            context={"custom_message": "Use the search query parameter"}
        )
        data = _parse_error(result)
        assert "Use the search query parameter" in data["error"]


# ============================================================================
# invalid_parameter_type_error
# ============================================================================

class TestInvalidParameterTypeError:

    def test_basic(self):
        result = invalid_parameter_type_error("confidence", "float", "string")
        data = _parse_error(result)
        assert data["error_code"] == "INVALID_PARAMETER_TYPE"
        assert "confidence" in data["error"]
        assert "float" in data["error"]
        assert "string" in data["error"]

    def test_with_tool_name(self):
        result = invalid_parameter_type_error("conf", "float", "str", tool_name="update")
        data = _parse_error(result)
        assert "update" in data["error"]


# ============================================================================
# permission_denied_error
# ============================================================================

class TestPermissionDeniedError:

    def test_basic(self):
        result = permission_denied_error("modify thresholds")
        data = _parse_error(result)
        assert data["error_code"] == "PERMISSION_DENIED"
        assert "modify thresholds" in data["error"]

    def test_with_role(self):
        result = permission_denied_error("modify thresholds", required_role="admin")
        data = _parse_error(result)
        assert "admin" in data["error"]


# ============================================================================
# tool_not_found_error
# ============================================================================

class TestToolNotFoundError:

    def test_basic(self):
        tools = ["process_agent_update", "identity", "health_check"]
        result = tool_not_found_error("prcess_agent_updte", tools)
        data = _parse_error(result)
        assert data["error_code"] == "TOOL_NOT_FOUND"
        assert "prcess_agent_updte" in data["error"]

    def test_with_suggestions(self):
        tools = ["process_agent_update", "identity", "health_check", "search_knowledge_graph"]
        result = tool_not_found_error("process_agent_updaet", tools)
        data = _parse_error(result)
        assert "Did you mean" in data["error"]

    def test_no_match(self):
        tools = ["process_agent_update", "identity"]
        result = tool_not_found_error("zzzzzzzzz", tools)
        data = _parse_error(result)
        assert "not found" in data["error"]

    def test_empty_tools_list(self):
        result = tool_not_found_error("test_tool", [])
        data = _parse_error(result)
        assert data["error_code"] == "TOOL_NOT_FOUND"
