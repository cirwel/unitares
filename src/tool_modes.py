"""
Tool Modes - Define the ADVERTISED tool surface for different use cases

Minimal mode (default): the five-tool checkpoint loop - identity binding,
    re-binding, the check-in, outcome evidence, and a read of the verdict.
Lite mode: the wider agent surface (shared memory, review, advisory inference,
    consolidated routers) - opt in with GOVERNANCE_TOOL_MODE=lite.
Full mode: every schema tool - for operators and cloud models with large context windows.

A mode decides what tools/list ADVERTISES. It does not decide what dispatches:
every register=True handler and every workflow alias stays callable by name on
every transport (REST, stdio, and the FastMCP /mcp/ mount, which registers the
whole surface and filters only its listing - see
src/tool_registration.py::install_tool_mode_listing). Dropping a name from a
mode set therefore hides it from schema-driven clients; it never deletes it.
Full mode should always include *all* schema tools even if categories lag behind.

Client-specific exclusions:
- Claude Desktop: Excludes tools that cause hangs (web search, heavy operations)
"""

from typing import Set
import os

# Read tool mode from environment. Default: minimal - the five-tool checkpoint
# loop is the whole default surface; lite/full are the opt-in wider surfaces.
# (Was "lite", 29 tools, until the surface cut of 2026-09.)
TOOL_MODE = os.getenv("GOVERNANCE_TOOL_MODE", "minimal").lower()

# Minimal mode: the checkpoint loop and nothing else. Five names, all task
# verbs. Discovery tools (list_tools / describe_tool) are deliberately absent:
# with five tools, the MCP client's native tools/list is the discovery surface.
# They remain advertised in lite/full and callable by name in every mode.
MINIMAL_MODE_TOOLS: Set[str] = {
    "start_session",          # Identity binding: mint a process identity (onboard)
    "identity",               # Re-bind a durable process to its existing anchor
    "sync_state",             # The check-in (process_agent_update)
    "record_result",          # Outcome evidence (outcome_event)
    "check_working_state",    # Read the verdict without writing (get_governance_metrics)
}

# Core/essential tools for lite mode (optimized for local models)
# THIS IS THE SINGLE SOURCE OF TRUTH - admin.py imports from here
# Updated Feb 2026: Use consolidated tools to reduce cognitive load
LITE_MODE_TOOLS: Set[str] = {
    # Primary agent workflow tools
    "start_session",              # Start or declare lineage
    "sync_state",                 # Log agent work
    "check_working_state",        # Check agent state
    "search_shared_memory",       # Search shared memory
    "store_finding",              # Write a finding to shared memory
    "update_finding",             # Revise a finding already in shared memory
    "record_result",              # Record task/tool/test outcomes
    "request_review",             # Ask for structured review
    "consult",                    # Ask for advisory model help

    # Identity (streamlined - Dec 2025)
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
    "list_inference_hosts",       # Discover inference hosts
    "describe_inference_host",    # Inspect one inference host
    "call_model",                 # LLM access
    "delegate_inference",         # Strong-model host delegation

    # Session hook (required by automation)
    "bind_session",               # Session-start hook for MCP identity sync

    # Raw implementation tools stay register=True (so the gateway, hooks, and
    # compat wrappers can still call them by name — the server dispatches any
    # registered handler whether or not it is advertised), but are NOT advertised
    # in the lite orientation surface: agents saw both the friendly alias AND its
    # raw twin (start_session+onboard, sync_state+process_agent_update, ...), which
    # is duplicate noise when orienting. The promoted friendly names carry the wire.
    # onboard is kept advertised for now — it sits in the identity/onboarding
    # coupled surface (CLAUDE.md) and the gov-plugin nudges still name it; hiding it
    # is a deliberate cross-repo follow-up, not this change.
    "onboard",                    # Raw implementation for start_session (identity surface — kept)

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
    # get_telemetry_metrics dropped 2026-08-29: it was an alias to
    # admin(action='telemetry') AND a registered tool, and the registration was
    # retired (resolve_alias rewrote the name before handler lookup, so it was
    # never dispatched to). This mode does not carry `admin`, but it does carry
    # `observe`, and observe(action='telemetry') routes to the same handler --
    # so the capability is unchanged here and only the duplicate name is gone.

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
    "self_recovery",              # Recoverability check + recovery (action=check/quick/review)

    # Knowledge graph write (for audit trail)
    # `knowledge` (action="store") is inherited from readonly; `leave_note` is the
    # low-friction path. Both are register=True wire tools — no standalone
    # `store_knowledge_graph` entry, which is register=False and never surfaces.
    "leave_note",                 # Quick notes

    # Agent lifecycle (limited)
    "mark_response_complete",     # Mark agents as waiting_input
}

# ============================================================================
# ONE ROSTER (2026-09-07)
# ----------------------------------------------------------------------------
# TOOL_TIERS, TOOL_CATEGORIES, and the `full` mode are keyed by the ADVERTISED
# roster: every register=True dispatch tool plus the workflow aliases
# (tool_stability.AGENT_WORKFLOW_ALIASES) -- see advertised_tool_names_full().
# Legacy alias names and the register=False delegates that only carry a schema
# for router-action validation are not tools and do not appear in those maps.
# TOOL_OPERATIONS is the one deliberate exception: describe_tool answers a
# legacy name with that name's own read/write class when one is listed, which
# is narrower and more useful than its router's. Until this date the maps had
# drifted three ways -- 23 advertised tools in no category or tier, 26
# pre-consolidation names still listed, and `full` reporting 66 schema
# definitions against 51 advertised names.
# tests/test_tool_registry_bookkeeping.py holds every map to this rule.
# ============================================================================

# ============================================================================
# TOOL_TIERS - Single source of truth for tier-based tool filtering
# admin.py imports this directly to avoid duplication
# ============================================================================
TOOL_TIERS: dict[str, Set[str]] = {
    "essential": {  # Tier 1: the workflow names an agent uses in every session
        "start_session",          # Start or declare lineage
        "sync_state",             # Log agent work
        "check_working_state",    # Check state without updating
        "search_shared_memory",   # Search shared memory
        "store_finding",          # Record a durable finding
        "record_result",          # Record outcomes
        "request_review",         # Ask for structured review
        "consult",                # Primary advisory model help
        "identity",               # Primary identity tool (auto-creates on first call)
        "list_tools",             # Discover available tools
        "describe_tool",          # Get full tool details
        "health_check",           # System status
        "leave_note",             # Quick notes
    },
    "common": {  # Tier 2: regularly used tools
        "onboard",                 # Raw implementation for start_session
        "process_agent_update",    # Raw implementation for sync_state
        "get_governance_metrics",  # Raw implementation for check_working_state
        "outcome_event",           # Raw implementation for record_result
        "update_finding",          # Revise a durable finding
        "knowledge",               # Knowledge graph router
        "search_knowledge_graph",  # Raw implementation for search_shared_memory
        "agent",                   # Agent lifecycle router
        "observe",                 # Observability router
        "dialectic",               # Dialectic router
        "calibration",             # Calibration router
        "self_recovery",           # Recovery router (check / quick / review)
        "mark_response_complete",
        "get_workspace_health",
        "get_thresholds",
        "bind_session",            # Session-start hook
        "skills",                  # Server-authored skill bundle
        "record_progress_pulse",   # Resident progress pulse
        "list_inference_hosts",
        "describe_inference_host",
        "call_model",
        "delegate_inference",
    },
    "advanced": {  # Tier 3: operator, maintenance, and specialist tools
        "admin",                   # Consolidated diagnostics/maintenance (Jun 2026)
        "config",                  # Threshold reads and privileged writes
        "export",                  # History export, inline or to file
        "simulate_update",
        "archive_old_test_agents",
        "archive_orphan_agents",
        "set_thresholds",
        "cirs_protocol",           # Multi-agent coordination protocol
        "dashboard",
        "detect_stuck_agents",
        "get_trajectory_status",
        "verify_trajectory_identity",
        "list_process_bindings",
        "operator_resume_agent",
        "outcome_correlation",
    },
}

# ============================================================================
# TOOL_OPERATIONS - Read vs Write classification for agent clarity
# read: Retrieves data without modifying state
# write: Creates, updates, or deletes data
# admin: System administration (may read or write internal state)
#
# Keyed by advertised names AND resolvable legacy alias names: a legacy name
# keeps its own class because it is narrower than its router's (list_agents
# is a read; the agent router it rewrites to also archives and deletes). A
# router carries the most privileged class among its actions.
# ============================================================================
TOOL_OPERATIONS: dict[str, str] = {
    # Primary agent workflow tools
    "start_session": "read",
    "sync_state": "write",
    "check_working_state": "read",
    "search_shared_memory": "read",
    "store_finding": "write",
    "update_finding": "write",
    "record_result": "write",
    "request_review": "write",

    # Identity & Onboarding
    "onboard": "read",                    # Returns identity + templates (creates if new)
    "identity": "read",                   # Returns identity (creates if new)
    "bind_session": "write",              # Binds the transport session to an identity
    "get_trajectory_status": "read",
    "verify_trajectory_identity": "read",
    "list_process_bindings": "read",

    # Core Governance
    "process_agent_update": "write",      # Updates agent state
    "get_governance_metrics": "read",     # Returns metrics without updating
    "outcome_event": "write",             # Records an outcome
    "record_progress_pulse": "write",     # Records a resident progress pulse
    "simulate_update": "read",            # Dry-run, no state change
    "self_recovery": "write",             # check reads; quick / review resume
    "calibration": "write",               # check reads; update / backfill / rebuild write
    "cirs_protocol": "write",             # Coordination protocol state

    # Agent Lifecycle
    "agent": "write",                     # list / get read; update / archive / resume / delete write
    "list_agents": "read",                # List all agents
    "get_agent_metadata": "read",         # Get agent details
    "update_agent_metadata": "write",     # Update tags/notes
    "archive_agent": "write",             # Archive agent
    "delete_agent": "write",              # Delete agent
    "archive_old_test_agents": "write",   # Bulk archive
    "archive_orphan_agents": "write",     # Preview by default; archives on request
    "mark_response_complete": "write",    # Update agent status
    "request_dialectic_review": "write",  # Start dialectic recovery
    "reset_monitor": "write",             # Reset agent state
    "operator_resume_agent": "admin",     # Privileged operator override

    # Configuration
    "config": "write",                    # get reads; set writes
    "get_thresholds": "read",             # Get current thresholds
    "set_thresholds": "write",            # Set threshold overrides

    # Consolidated diagnostics/maintenance
    "admin": "admin",                     # Mixed read/maintenance (action-routed)

    # Knowledge Graph
    "knowledge": "write",                 # search / get / list read; store / update / note / cleanup write
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
    "observe": "read",                    # Every action reads
    "observe_agent": "read",              # View agent state
    "compare_agents": "read",             # Compare agents
    "compare_me_to_similar": "read",      # Compare self to similar
    "detect_anomalies": "read",           # Scan for anomalies
    "aggregate_metrics": "read",          # Fleet overview
    "dashboard": "read",                  # Active agents with their EISV vectors
    "detect_stuck_agents": "write",       # auto_recover=true resumes agents
    "outcome_correlation": "read",        # Correlation study over outcomes

    # Export
    "export": "write",                    # history reads; file writes to disk
    "get_system_history": "read",         # Get history inline
    "export_to_file": "write",            # Write file to disk

    # Calibration (legacy names)
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
    "skills": "read",                     # Server-authored skill bundle
    "cleanup_stale_locks": "admin",       # Clean up locks
    "validate_file_path": "read",         # Validate path
    "debug_request_context": "read",      # Debug context
    "get_connection_status": "read",      # Verify MCP connection and tool availability
    "list_inference_hosts": "read",       # Discover inference hosts
    "describe_inference_host": "read",    # Inspect one inference host
    "consult": "read",                    # Canonical advisory inference facade
    "call_model": "read",                 # Advisory inference; returns evidence artifact
    "delegate_inference": "read",         # Strong advisory inference; returns evidence artifact

    # Dialectic
    "dialectic": "write",                 # get / list read; request / thesis / antithesis / synthesis / reassign write
    "submit_thesis": "write",             # Submit thesis phase
    "submit_antithesis": "write",         # Submit antithesis phase
    "submit_synthesis": "write",          # Submit synthesis phase
}


# Tool categories: one category per advertised name, agreeing with the
# introspection catalog (tool_catalog.TOOL_RELATIONSHIPS[name]["category"]),
# which is what list_tools(category=...) filters on. A category name is also
# accepted as a GOVERNANCE_TOOL_MODE value.
TOOL_CATEGORIES: dict[str, Set[str]] = {
    "core": {
        "sync_state",
        "check_working_state",
        "record_result",
        "process_agent_update",     # raw implementation
        "get_governance_metrics",   # raw implementation
        "outcome_event",            # raw implementation
        "simulate_update",
        "record_progress_pulse",
        "self_recovery",
        "calibration",
        "cirs_protocol",
    },
    "identity": {
        "start_session",
        "onboard",                  # raw implementation
        "identity",                 # Dec 2025: Primary identity tool (auto-creates on first call)
        "bind_session",
        "get_trajectory_status",
        "verify_trajectory_identity",
        "list_process_bindings",
    },
    "admin": {
        "health_check",
        "list_tools",
        "describe_tool",
        "admin",
        "skills",
    },
    "workspace": {
        "get_workspace_health",
    },
    "export": {
        "export",
    },
    "config": {
        "config",
        "get_thresholds",
        "set_thresholds",
    },
    "lifecycle": {
        "agent",
        "archive_old_test_agents",
        "archive_orphan_agents",
        "mark_response_complete",
        "operator_resume_agent",
    },
    "observability": {
        "observe",
        "dashboard",
        "detect_stuck_agents",
        "outcome_correlation",
    },
    "knowledge": {
        "search_shared_memory",
        "store_finding",
        "update_finding",
        "knowledge",
        "leave_note",
        "search_knowledge_graph",   # raw implementation for search_shared_memory
    },
    "inference": {
        "list_inference_hosts",
        "describe_inference_host",
        "consult",
        "call_model",
        "delegate_inference",
    },
    "dialectic": {
        "request_review",
        "dialectic",
    },
}


def advertised_tool_names_full() -> Set[str]:
    """Every name a full-mode server advertises.

    Registered dispatch tools plus the workflow aliases; not the schema
    definitions, 23 of which are register=False delegates that only validate
    router actions and never reach a wire. Imported lazily because the handler
    package imports this module while it loads.
    """
    import src.mcp_handlers  # noqa: F401  -- settles every @mcp_tool decorator
    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES

    return set(get_tool_registry()) | set(AGENT_WORKFLOW_ALIASES)


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
        # Full is the advertised roster (advertised_tool_names_full): every
        # registered dispatch tool plus the workflow aliases, read from the
        # registry so a new @mcp_tool is in full before anyone categorizes it.
        # It was the schema-definition list until 2026-09-07, which also
        # counted the register=False delegates and put `full` 15 names wider
        # than anything the registrar could advertise.
        try:
            return advertised_tool_names_full()
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
    # No name is force-included: the mode set is the advertised surface, full
    # stop. (list_tools / describe_tool were force-included in every mode until
    # the 2026-09 surface cut; they are now ordinary members of lite/full.)
    allowed_tools = get_tools_for_mode(mode)
    if tool_name not in allowed_tools:
        return False
    
    # Check Claude Desktop exclusions
    if client_type == "claude_desktop" or (client_type is None and is_claude_desktop_client()):
        if tool_name in CLAUDE_DESKTOP_EXCLUDED_TOOLS:
            return False
    
    return True
