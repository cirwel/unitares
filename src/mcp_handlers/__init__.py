"""
MCP Tool Handlers

Handler registry pattern for elegant tool dispatch.
Each tool handler is a separate function for better testability and maintainability.
"""

from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent

# Import all handlers
from .core import (
    handle_process_agent_update,
    handle_get_governance_metrics,
    handle_simulate_update,
)
from .admin.config import (
    handle_get_thresholds,
    handle_set_thresholds,
)
from .observability.handlers import (
    handle_observe_agent,
    handle_compare_agents,
    handle_compare_me_to_similar,
    handle_detect_anomalies,
    handle_aggregate_metrics,
)
from .lifecycle.handlers import (
    handle_list_agents,
    handle_get_agent_metadata,
    handle_update_agent_metadata,
    handle_archive_agent,
    handle_resume_agent,
    handle_delete_agent,
    handle_archive_old_test_agents,
    # handle_get_agent_api_key REMOVED Dec 2025 - aliased to identity()
    handle_mark_response_complete,
    handle_direct_resume_if_safe,
    handle_self_recovery_review,  # Added per SELF_RECOVERY_SPEC.md
    handle_detect_stuck_agents,
    handle_ping_agent,
)
from .introspection.export import (
    handle_get_system_history,
    handle_export_to_file,
)
from .admin.handlers import (
    handle_reset_monitor,
    handle_get_server_info,
    handle_health_check,
    handle_get_connection_status,
    handle_get_telemetry_metrics,
    handle_cleanup_stale_locks,
    handle_get_workspace_health,
    handle_get_tool_usage_stats,
    handle_validate_file_path,
)
from .admin.calibration import (
    handle_check_calibration,
    handle_rebuild_calibration,
    handle_update_calibration_ground_truth,
    handle_backfill_calibration_from_dialectic,
)
from .introspection.tool_introspection import (
    handle_list_tools,
    handle_describe_tool,
)
from .introspection.skills import handle_skills  # S15-a: server-side skills surface
# Knowledge Graph
from .knowledge.handlers import (
    handle_store_knowledge_graph,
    handle_search_knowledge_graph,
    handle_get_knowledge_graph,
    handle_list_knowledge_graph,
    handle_update_discovery_status_graph,
    handle_get_discovery_details,
    handle_leave_note,
    handle_cleanup_knowledge_graph,
    handle_synthesize_knowledge_graph,
    handle_get_lifecycle_stats,
)
# Dialectic - full protocol restored (Feb 2026)
from .dialectic.handlers import (
    handle_get_dialectic_session,
    handle_list_dialectic_sessions,
    handle_request_dialectic_review,
    handle_submit_thesis,
    handle_submit_antithesis,
    handle_submit_synthesis,
    handle_llm_assisted_dialectic,
)
# Self-Recovery - Simplified recovery without external reviewers (Jan 2026)
# Note: handle_self_recovery_review moved to lifecycle.py per SELF_RECOVERY_SPEC.md
from .lifecycle.self_recovery import (
    handle_self_recovery,  # Consolidated entry point
    handle_quick_resume,  # Hidden, used by dispatcher
    handle_check_recovery_options,  # Hidden, used by dispatcher
    handle_operator_resume_agent,
)
# Identity - v2 simplified (Dec 2025, 3-path architecture)
from .identity.handlers import (
    handle_identity_adapter as handle_identity,
    handle_onboard_v2 as handle_onboard,
    handle_verify_trajectory_identity,
    handle_get_trajectory_status,
)
# Model Inference - Free/low-cost LLM access via Ollama (local) or HF Inference Providers
from .support.model_inference import handle_call_model
# Outcome Events - EISV validation infrastructure (Feb 2026)
from .observability.outcome_events import handle_outcome_event
# Resident Progress - sentinel push-based pulse (Phase 1)
from .resident_progress import handle_record_progress_pulse
# Consolidated tools - reduces cognitive load for agents (Jan 2026)
from .consolidated import (
    handle_knowledge,
    handle_agent,
    handle_calibration,
)
# CIRS Protocol - Multi-agent resonance layer (Feb 2026)
# See: UARG Whitepaper for protocol specification
from .cirs import (
    handle_cirs_protocol,  # Consolidated entry point
    # Individual handlers (hidden, for backwards compat)
    handle_void_alert,
    handle_state_announce,
    handle_coherence_report,
    handle_boundary_contract,
    handle_governance_action,
    maybe_emit_void_alert,  # Hook for process_agent_update
    auto_emit_state_announce,  # Hook for process_agent_update
    maybe_emit_resonance_signal,  # Hook for process_agent_update
)
# Pi orchestration moved out to the ``unitares-pi-plugin`` package — see
# docs/specs/2026-04-17-lumen-decoupling-design.md (Phase B1). Install
# with ``pip install unitares-pi-plugin`` to restore the pi_* tools.

# Keep helper functions from identity_shared.py (used by dispatch_tool)
from .identity.shared import (
    get_bound_agent_id,
    is_session_bound,
)

# Common utilities
from .admin.dashboard import handle_dashboard
from .utils import error_response, success_response

# Error helpers (for exception handlers)
from .error_helpers import timeout_error, system_error, rate_limit_error, tool_not_found_error

# Decorator utilities
from .decorators import get_tool_registry as get_decorator_registry, get_tool_timeout

# Logging
from src.logging_utils import get_logger
logger = get_logger(__name__)

# Re-export for external callers that import from this package
__all__ = ['dispatch_tool', 'TOOL_HANDLERS', 'error_response', 'success_response']

# Handler registry - populated automatically by @mcp_tool decorators
# All tools are decorator-registered, so we start with an empty dict and populate from decorators
# Imports above ensure decorators run and register tools automatically
TOOL_HANDLERS: Dict[str, callable] = {}

# Populate registry from decorator-registered tools
# All handlers use @mcp_tool decorator which auto-registers them
# Note: plugin-provided tools are added later by a second pass triggered
# from src/mcp_server.py once this package is fully initialised (deferring
# plugin loading avoids a circular import: plugins import from
# src.mcp_handlers.* which is still mid-init here).
decorator_registry = get_decorator_registry()
for tool_name, handler in decorator_registry.items():
    TOOL_HANDLERS[tool_name] = handler


def refresh_tool_handlers_from_registry() -> int:
    """Re-sync TOOL_HANDLERS with the decorator registry.

    Called from mcp_server.py after ``plugin_loader.load_plugins()`` has
    run. Returns the number of NEW handlers added (vs the first-party
    snapshot captured at import time).
    """
    before = len(TOOL_HANDLERS)
    current = get_decorator_registry()
    for tool_name, handler in current.items():
        TOOL_HANDLERS.setdefault(tool_name, handler)
    return len(TOOL_HANDLERS) - before


async def dispatch_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Sequence[TextContent] | None:
    """
    Dispatch tool call to appropriate handler.

    Pipeline: kwargs → identity → trajectory → alias → inject → validate → rate limit → patterns → execute.
    Each step is defined in middleware.py for testability.
    """
    from .middleware import PRE_DISPATCH_STEPS, POST_VALIDATION_STEPS, POST_EXECUTION_STEPS
    from src.services.tool_dispatch_service import run_tool_dispatch_pipeline

    return await run_tool_dispatch_pipeline(
        name=name,
        arguments=arguments,
        pre_steps=PRE_DISPATCH_STEPS,
        post_steps=POST_VALIDATION_STEPS,
        post_execution_steps=POST_EXECUTION_STEPS,
    )
