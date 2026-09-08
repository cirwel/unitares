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

# TOOL_TIERS, TOOL_OPERATIONS and TOOL_CATEGORIES are derived from the one
# record per tool in src/tool_meta.py and re-exported here under their
# historical names: every reader imports them from this module, and the
# admin-handler tests patch `src.tool_modes.TOOL_TIERS` and friends. They were
# hand-maintained literals here until 2026-09-07.
#
# TOOL_TIERS:      essential / common / advanced, a partition of the roster.
# TOOL_OPERATIONS: read / write / admin per advertised name; a router carries
#                  the most privileged class among its actions. A legacy alias
#                  that pins a narrower action declares its own class on its
#                  ToolAlias entry (tool_stability.py), not here.
# TOOL_CATEGORIES: one category per advertised name, the same category the
#                  introspection catalog reports and list_tools(category=...)
#                  filters on. A category name is also accepted as a
#                  GOVERNANCE_TOOL_MODE value.
from src.tool_meta import TOOL_CATEGORIES, TOOL_OPERATIONS, TOOL_TIERS  # noqa: E402,F401


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
