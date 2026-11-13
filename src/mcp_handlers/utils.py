"""
Common utilities for MCP tool handlers.

Re-export facade — all functions have moved to focused modules.
Existing imports continue to work unchanged.
"""

# Re-export all public names for backward compatibility
from .serialization import _make_json_serializable
from .support.agent_auth import (
    compute_agent_signature,
    check_agent_can_operate,
    check_agent_can_operate_async,
    require_argument,
    require_agent_id,
    require_registered_agent,
    verify_agent_ownership,
)
from .error_handling import (
    error_response,
    _infer_error_code_and_category,
    _sanitize_error_message,
)
from .response_base import (
    format_metrics_report,
    format_metrics_text,
    success_response,
)
from .introspection.feedback import (
    get_calibration_feedback,
    generate_actionable_feedback,
    _calibration_message_cache,
)

# Re-export types that were previously accessible via utils
from .types import (
    ErrorResponseDict,
    SuccessResponseDict,
    AgentMetadataDict,
    GovernanceMetricsDict,
    ToolArgumentsDict,
)

__all__ = [
    "_make_json_serializable",
    "compute_agent_signature",
    "check_agent_can_operate",
    "check_agent_can_operate_async",
    "require_argument",
    "require_agent_id",
    "require_registered_agent",
    "verify_agent_ownership",
    "error_response",
    "_infer_error_code_and_category",
    "_sanitize_error_message",
    "format_metrics_report",
    "format_metrics_text",
    "success_response",
    "get_calibration_feedback",
    "generate_actionable_feedback",
    "_calibration_message_cache",
]
