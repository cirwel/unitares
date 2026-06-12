"""
Tool Modes - Define subsets of tools for different use cases

Minimal mode: 4 essential tools - perfect for getting started (includes list_tools for discovery)
Lite mode: Essential tools only (~10 tools) - optimized for local models
Full mode: All tools (49 tools) - for cloud models with large context windows
Note: Tool-mode filtering is applied by servers that choose to enforce it (e.g. stdio list_tools).
      Full mode should always include *all* schema tools even if categories lag behind.

Client-specific exclusions:
- Claude Desktop: Excludes tools that cause hangs (web search, heavy operations)
"""

from typing import Set
import os

# Read tool mode from environment (default: lite for reduced cognitive load)
TOOL_MODE = os.getenv("GOVERNANCE_TOOL_MODE", "lite").lower()

# Minimal mode: Essential tools + list_tools for discovery
MINIMAL_MODE_TOOLS: Set[str] = {
    # Agent workflow aliases (task verbs over canonical implementation names)
    "start_session",
    "sync_state",
    "check_working_state",
    "onboard",                # 🚀 Portal tool - call this FIRST (Dec 2025)
    "identity",               # Check/set identity (auto-creates on first call)
    "process_agent_update",   # Log your work (ongoing)
    "get_governance_metrics", # Check your state (as needed)
    "list_tools",             # Discover available tools (bootstrap)
    "describe_tool",          # Pull full details for a specific tool (lazy schema)
}

# Core/essential tools for lite mode (optimized for local models)
# THIS IS THE SINGLE SOURCE OF TRUTH - admin.py imports from here
# Updated Feb 2026: Use consolidated tools to reduce cognitive load
LITE_MODE_TOOLS: Set[str] = {
    # Agent workflow aliases (visible, first-class UX names)
    "start_session",              # Alias for onboard
    "sync_state",                 # Alias for process_agent_update
    "check_working_state",        # Alias for get_governance_metrics
    "search_shared_memory",       # Alias for knowledge(action='search')
    "record_result",              # Alias for outcome_event
    "request_review",             # Alias for dialectic(action='request')

    # Core governance
    "process_agent_update",       # Log agent work
    "get_governance_metrics",     # Check agent state

    # Identity (streamlined - Dec 2025)
    "onboard",                    # 🚀 Portal tool - call this FIRST
    "identity",                   # Primary identity tool (auto-creates on first call)

    # Consolidated tools (Feb 2026)
    "agent",                      # Agent lifecycle (list/get/update/archive/delete)
    "knowledge",                  # Knowledge graph (store/search/get/list/update/note/cleanup/stats)
    "observe",                    # Observability (agent/compare/similar/anomalies/aggregate)
    # "pi" moved to unitares-pi-plugin; registered only when the plugin is installed.
    "dialectic",                  # Dialectic (request/get/list/llm)
    "calibration",                # Calibration (check/update/backfill/rebuild)
    "config",                     # Config (get/set thresholds)
    "export",                     # Export (history/file)

    # System health
    "health_check",               # System status
    "list_tools",                 # See available tools
    "describe_tool",              # Pull full tool details on demand

    # Convenient shortcuts (kept for discoverability)
    "leave_note",                 # Quick notes
    "call_model",                 # LLM access

    # Session & calibration hooks (required by automation)
    "bind_session",               # Session-start hook for MCP identity sync
    "outcome_event",              # PostToolUse hook for auto-calibration

    # Recovery
    "self_recovery",              # Primary recovery path for stuck agents

    # Janitorial (used by Vigil groundskeeper — needed in lite so MCP-native SDK clients can call it)
    "archive_orphan_agents",
}

# Operator read-only mode: Observability and detection tools for central operator agent
# Used by operator agent for monitoring, stuck detection, and reporting (Phase 1)
# Updated Feb 2026: Use consolidated tools
OPERATOR_READONLY_MODE_TOOLS: Set[str] = {
    # Consolidated tools
    "agent",                      # Agent lifecycle (list/get/update/archive/delete)
    "observe",                    # Observability (agent/compare/similar/anomalies/aggregate)
    "knowledge",                  # Knowledge graph operations
    "calibration",                # Calibration checks

    # Specialized detection (keep separate)
    "detect_stuck_agents",        # Essential: detect stuck agents

    # Governance metrics
    "get_governance_metrics",     # Check agent state
    "get_telemetry_metrics",      # System telemetry

    # System health
    "health_check",               # System status
    "get_workspace_health",       # Workspace health

    # Identity (for operator itself)
    "identity",                   # Check/set operator identity
    "onboard",                    # Operator onboarding

    # Discovery tools (always available)
    "list_tools",                 # See available tools
    "describe_tool",              # Get tool details
}

# Operator recovery mode: Adds recovery capabilities for stuck agents (Phase 2-3)
# Jan 2026: Extends readonly mode with cross-agent recovery tools
OPERATOR_RECOVERY_MODE_TOOLS: Set[str] = OPERATOR_READONLY_MODE_TOOLS | {
    # Recovery tools
    "operator_resume_agent",      # Resume stuck agents (operator-only)
    "check_recovery_options",     # Check if agent is recoverable
    
    # Knowledge graph write (for audit trail)
    "store_knowledge_graph",      # Log interventions
    "leave_note",                 # Quick notes
    
    # Agent lifecycle (limited)
    "mark_response_complete",     # Mark agents as waiting_input
}

# ============================================================================
# TOOL_TIERS - Single source of truth for tier-based tool filtering
# admin.py imports this directly to avoid duplication
# ============================================================================
TOOL_TIERS: dict[str, Set[str]] = {
    "essential": {  # Tier 1: Core workflow tools (~10 tools)
        "start_session",          # Workflow alias for onboard
        "sync_state",             # Workflow alias for process_agent_update
        "check_working_state",    # Workflow alias for get_governance_metrics
        "search_shared_memory",   # Workflow alias for knowledge(search)
        "record_result",          # Workflow alias for outcome_event
        "request_review",         # Workflow alias for dialectic(request)
        "onboard",                # 🚀 Portal tool - call FIRST (Dec 2025)
        "identity",               # Primary identity tool (auto-creates on first call)
        "process_agent_update",   # Log agent work
        "get_governance_metrics", # Check state without updating
        "list_tools",             # Discover available tools
        "describe_tool",          # Get full tool details
        "list_agents",            # View all agents
        "health_check",           # System status
        "store_knowledge_graph",  # Record discoveries
        "leave_note",             # Quick notes
    },
    "common": {  # Tier 2: Regularly used tools
        "update_discovery_status_graph",
        "observe_agent",
        "get_agent_metadata",
        "get_server_info",
        "list_knowledge_graph",
        "get_discovery_details",
        "get_telemetry_metrics",
        "check_calibration",
        "update_calibration_ground_truth",
        "get_tool_usage_stats",
        "detect_anomalies",
        "aggregate_metrics",
        "delete_agent",
        "dialectic",  # Consolidated: get/list dialectic sessions
        "submit_thesis",
        "submit_antithesis",
        "submit_synthesis",
        "mark_response_complete",
        "compare_agents",
        "get_workspace_health",
        "archive_agent",
        "get_system_history",
        "get_thresholds",
        "debug_request_context",
        "get_connection_status",         # Verify MCP connection and tool availability
        "get_lifecycle_stats",           # KG lifecycle stats (Dec 2025)
    },
    "advanced": {  # Tier 3: Rarely used tools
        "cleanup_stale_locks",
        "simulate_update",
        "export_to_file",
        "update_agent_metadata",
        "archive_old_test_agents",
        "direct_resume_if_safe",
        "request_dialectic_review",
        "backfill_calibration_from_dialectic",
        "reset_monitor",
        "set_thresholds",
        "validate_file_path",
        "compare_me_to_similar",
        "get_knowledge_graph",
        "cleanup_knowledge_graph",       # KG lifecycle cleanup (Dec 2025)
    }
}

# ============================================================================
# TOOL_OPERATIONS - Read vs Write classification for agent clarity
# read: Retrieves data without modifying state
# write: Creates, updates, or deletes data
# admin: System administration (may read or write internal state)
# ============================================================================
TOOL_OPERATIONS: dict[str, str] = {
    # Agent workflow aliases
    "start_session": "read",
    "sync_state": "write",
    "check_working_state": "read",
    "search_shared_memory": "read",
    "record_result": "write",
    "request_review": "write",

    # Identity & Onboarding
    "onboard": "read",                    # Returns identity + templates (creates if new)
    "identity": "read",                   # Returns identity (creates if new)

    # Core Governance
    "process_agent_update": "write",      # Updates agent state
    "get_governance_metrics": "read",     # Returns metrics without updating
    "simulate_update": "read",            # Dry-run, no state change

    # Agent Lifecycle
    "list_agents": "read",                # List all agents
    "get_agent_metadata": "read",         # Get agent details
    "update_agent_metadata": "write",     # Update tags/notes
    "archive_agent": "write",             # Archive agent
    "delete_agent": "write",              # Delete agent
    "archive_old_test_agents": "write",   # Bulk archive
    "mark_response_complete": "write",    # Update agent status
    "direct_resume_if_safe": "write",     # Resume agent
    "request_dialectic_review": "write",  # Start dialectic recovery
    "reset_monitor": "write",             # Reset agent state

    # Configuration
    "get_thresholds": "read",             # Get current thresholds
    "set_thresholds": "write",            # Set threshold overrides

    # Knowledge Graph
    "store_knowledge_graph": "write",     # Store discovery
    "search_knowledge_graph": "read",     # Search discoveries
    "get_knowledge_graph": "read",        # Get agent's knowledge
    "list_knowledge_graph": "read",       # List statistics
    "get_discovery_details": "read",      # Get discovery details
    "update_discovery_status_graph": "write",  # Update discovery status
    "leave_note": "write",                # Store quick note
    "cleanup_knowledge_graph": "write",   # Run lifecycle cleanup
    "get_lifecycle_stats": "read",        # Get lifecycle statistics

    # Observability
    "observe_agent": "read",              # View agent state
    "compare_agents": "read",             # Compare agents
    "compare_me_to_similar": "read",      # Compare self to similar
    "detect_anomalies": "read",           # Scan for anomalies
    "aggregate_metrics": "read",          # Fleet overview

    # Export
    "get_system_history": "read",         # Get history inline
    "export_to_file": "write",            # Write file to disk

    # Calibration
    "check_calibration": "read",          # Check calibration
    "update_calibration_ground_truth": "write",  # Update calibration
    "backfill_calibration_from_dialectic": "write",  # Backfill calibration

    # Admin & Diagnostics
    "health_check": "read",               # System status
    "get_server_info": "read",            # Server info
    "get_telemetry_metrics": "read",      # Telemetry data
    "get_tool_usage_stats": "read",       # Tool usage stats
    "get_workspace_health": "read",       # Workspace health
    "list_tools": "read",                 # List available tools
    "describe_tool": "read",              # Describe single tool
    "cleanup_stale_locks": "admin",       # Clean up locks
    "validate_file_path": "read",         # Validate path
    "debug_request_context": "read",      # Debug context
    "get_connection_status": "read",      # Verify MCP connection and tool availability

    # Dialectic
    "request_dialectic_review": "write",  # Create dialectic session
    "submit_thesis": "write",             # Submit thesis phase
    "submit_antithesis": "write",         # Submit antithesis phase
    "submit_synthesis": "write",          # Submit synthesis phase
    "dialectic": "read",                  # Consolidated: get/list sessions

    # SSE-only
    "get_connected_clients": "read",      # List connected clients
    "get_connection_diagnostics": "read", # Connection diagnostics
}


# Tool categories for selective loading (excludes deprecated tools)
TOOL_CATEGORIES = {
    "core": {
        "sync_state",
        "check_working_state",
        "record_result",
        "process_agent_update",
        "get_governance_metrics",
        "simulate_update",
    },
    "identity": {
        "start_session",
        "onboard",                # Dec 2025: Portal tool - call FIRST
        "identity",               # Dec 2025: Primary identity tool (auto-creates on first call)
        "list_agents",
        "get_agent_metadata",
    },
    "admin": {
        "health_check",
        "get_server_info",
        "get_connection_status",
        "list_tools",
        "describe_tool",
        "get_tool_usage_stats",
        "cleanup_stale_locks",
        "get_workspace_health",
        "check_calibration",
        "get_telemetry_metrics",
        "update_calibration_ground_truth",
    },
    "export": {
        "get_system_history",
        "export_to_file",
    },
    "config": {
        "get_thresholds",
        "set_thresholds",
    },
    "lifecycle": {
        "archive_agent",
        "update_agent_metadata",
        "archive_old_test_agents",
    },
    "observability": {
        "observe_agent",
        "compare_agents",
        "detect_anomalies",
        "aggregate_metrics",
    },
    "knowledge": {
        "search_shared_memory",
        "store_knowledge_graph",   # Primary: add knowledge (handles responses via response_to)
        "get_discovery_details",   # Drill into discovery (includes related/chain)
        "leave_note",
        "update_discovery_status_graph",
        "cleanup_knowledge_graph",   # Lifecycle cleanup (Dec 2025)
        "get_lifecycle_stats",       # Lifecycle statistics (Dec 2025)
    },
    "dialectic": {
        "request_review",
        "request_dialectic_review",      # Create dialectic session
        "submit_thesis",                 # Paused agent explains reasoning
        "submit_antithesis",             # Reviewer raises concerns
        "submit_synthesis",              # Negotiate resolution
        "dialectic",                     # Consolidated: get/list sessions
    },
}


def get_tools_for_mode(mode: str = "full") -> Set[str]:
    """
    Get tool set for specified mode

    Args:
        mode: "minimal", "lite", "full", "operator_readonly", or category name (e.g., "core", "admin")

    Returns:
        Set of tool names to include
    """
    if mode == "minimal":
        return MINIMAL_MODE_TOOLS.copy()
    
    if mode == "lite":
        return LITE_MODE_TOOLS.copy()
    
    if mode == "operator_readonly":
        return OPERATOR_READONLY_MODE_TOOLS.copy()

    if mode == "operator_recovery":
        return OPERATOR_RECOVERY_MODE_TOOLS.copy()

    if mode == "full":
        # IMPORTANT: Full mode must include *all* tools defined in the schema, not just
        # what happens to be listed in TOOL_CATEGORIES. This prevents accidental
        # omissions when new tools are added but categories aren't updated yet.
        try:
            from src.tool_schemas import get_tool_definitions
            return {t.name for t in get_tool_definitions()}
        except Exception:
            # Fallback (best-effort): union of categories
            all_tools = set()
            for tools in TOOL_CATEGORIES.values():
                all_tools.update(tools)
            return all_tools

    # Check if it's a category name
    if mode in TOOL_CATEGORIES:
        return TOOL_CATEGORIES[mode].copy()

    # Default to full
    all_tools = set()
    for tools in TOOL_CATEGORIES.values():
        all_tools.update(tools)
    return all_tools


def is_claude_desktop_client() -> bool:
    """
    Detect if MCP client is Claude Desktop (vs Cursor or other clients).
    
    Claude Desktop is more sensitive to hangs, so we exclude problematic tools.
    
    Returns:
        True if client appears to be Claude Desktop
    """
    # Check parent process name (most reliable)
    try:
        import psutil
        current_process = psutil.Process()
        parent = current_process.parent()
        if parent:
            parent_name = parent.name().lower()
            if "claude" in parent_name:
                return True
            # Check up the process tree
            for _ in range(3):
                try:
                    if parent:
                        parent = parent.parent()
                        if parent:
                            parent_name = parent.name().lower()
                            if "claude" in parent_name:
                                return True
                except (psutil.NoSuchProcess, AttributeError):
                    break
    except (ImportError, AttributeError, psutil.NoSuchProcess):
        pass
    
    # Check environment variables
    if os.getenv("CLAUDE_DESKTOP") or os.getenv("ANTHROPIC_CLAUDE"):
        return True
    
    return False


# Tools to exclude for Claude Desktop (causes hangs/freezes)
CLAUDE_DESKTOP_EXCLUDED_TOOLS: Set[str] = {
    # Add tools here that cause Claude Desktop to hang
    # Example: "web_search", "heavy_operation", etc.
    # Currently empty - add tools as issues are discovered
}


def should_include_tool(tool_name: str, mode: str = "full", client_type: str = None) -> bool:
    """
    Check if a tool should be included in the specified mode and client type

    Args:
        tool_name: Name of the tool
        mode: "minimal", "lite", "full", or category name
        client_type: Optional client type override ("claude_desktop" or None for auto-detect)

    Returns:
        True if tool should be included
    """
    # Always include discovery tools so agents can recover from over-filtering.
    # This matches the onboarding docs: list_tools should be available in any mode.
    if tool_name in {"list_tools", "describe_tool"}:
        return True

    # Check mode filtering first
    allowed_tools = get_tools_for_mode(mode)
    if tool_name not in allowed_tools:
        return False
    
    # Check Claude Desktop exclusions
    if client_type == "claude_desktop" or (client_type is None and is_claude_desktop_client()):
        if tool_name in CLAUDE_DESKTOP_EXCLUDED_TOOLS:
            return False
    
    return True
