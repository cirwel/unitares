"""
MCP Handlers for Agent Lifecycle Management — re-export hub.

Handler implementations are split into focused modules:
  - query.py      — handle_list_agents, handle_get_agent_metadata
  - mutation.py   — handle_update_agent_metadata, handle_archive_agent, handle_delete_agent
  - operations.py — handle_resume_agent, handle_mark_response_complete,
                     handle_self_recovery_review, handle_ping_agent,
                     handle_archive_old_test_agents, handle_archive_orphan_agents
  - stuck.py      — handle_detect_stuck_agents, _detect_stuck_agents

This file re-exports all public names so that existing consumers
(tests, __init__.py, consolidated.py, background_tasks.py) continue to
work with ``from .handlers import handle_X`` unchanged.
"""

# Shared helpers (importable for backward compat and tests that patch them)
from .helpers import _invalidate_agent_cache, _is_test_agent

# --- Re-export: query handlers ---
from .query import handle_list_agents, handle_get_agent_metadata

# --- Re-export: mutation handlers ---
from .mutation import (
    handle_update_agent_metadata,
    handle_archive_agent,
    handle_delete_agent,
)

# --- Re-export: operational handlers ---
from .operations import (
    handle_resume_agent,
    handle_mark_response_complete,
    handle_self_recovery_review,
    handle_ping_agent,
    handle_archive_old_test_agents,
    handle_archive_orphan_agents,
)

# --- Re-export: extracted handlers (pre-existing splits) ---
from .stuck import handle_detect_stuck_agents, _detect_stuck_agents

# Keep module-level imports that existing patch targets depend on.
# Tests patch e.g. "src.mcp_handlers.lifecycle.handlers.mcp_server"
# and "src.mcp_handlers.lifecycle.handlers.agent_storage", so we must
# keep these names importable from this module.
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server  # noqa: F401
from src import agent_storage  # noqa: F401
from ..utils import require_registered_agent, success_response, error_response  # noqa: F401
from ..support.coerce import resolve_agent_uuid  # noqa: F401
from src.logging_utils import get_logger

logger = get_logger(__name__)

# REMOVED: get_agent_api_key (Dec 2025)
# API keys deprecated - UUID-based session auth is now primary.
# Calls to get_agent_api_key are aliased to identity() via tool_stability.py
