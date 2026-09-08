"""
Tool Modes - Define the ADVERTISED tool surface for different use cases

Standard mode (default): the checkpoint loop plus the four capabilities an
    agent cannot reach any other way - shared memory (search / store / revise),
    structured review, advisory inference, and recovery from a pause. Eleven
    names, all task verbs.
Minimal mode: the five-tool checkpoint loop alone - identity binding,
    re-binding, the check-in, outcome evidence, and a read of the verdict.
    Opt in with GOVERNANCE_TOOL_MODE=minimal when context is scarce.
Lite mode: the wider agent surface (the consolidated routers, discovery, and
    inference-host tools on top of standard) - GOVERNANCE_TOOL_MODE=lite.
Full mode: every schema tool - for operators and cloud models with large context windows.

A mode decides what tools/list ADVERTISES. It does not decide what dispatches:
every register=True handler and every workflow alias stays callable by name on
every transport (REST, stdio, and the FastMCP /mcp/ mount, which registers the
whole surface and filters only its listing - see
src/tool_mode_listing.py::mode_filtered_server_class, applied in
src/mcp_server.py). Dropping a name from a
mode set therefore hides it from schema-driven clients; it never deletes it.
Full mode should always include *all* schema tools even if categories lag behind.

Client-specific exclusions:
- Claude Desktop: Excludes tools that cause hangs (web search, heavy operations)
"""

from typing import Set
import os

# Read tool mode from environment. Default: standard - the checkpoint loop plus
# the four capabilities that are unreachable when they are not advertised;
# minimal/lite/full are the opt-in narrower and wider surfaces.
# (Was "lite" (29) until the 2026-09 surface cut, then "minimal" (5) until the
# dormant-capability fix below.)
TOOL_MODE = os.getenv("GOVERNANCE_TOOL_MODE", "standard").lower()

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

# Standard mode: the default advertised surface. The checkpoint loop plus the
# six names that carry a capability an agent cannot reach any other way.
#
# WHY THESE AND NOT OTHERS. A mode filters tools/list, and a schema-driven
# client (Claude Code, Codex, Cursor) offers the model only what tools/list
# returns. "Still callable by name" is therefore a property no such client can
# use: a capability that is never advertised is, for them, a capability that
# does not exist. Under the five-tool default, shared memory, structured
# review, and advisory inference were registered, reachable, and dormant.
#
# The line is drawn at capability, not at count, and not at tool SHAPE. Each
# name below is the only advertised way to reach something the server does;
# every name NOT here is either a router whose actions these already cover, a
# discovery tool (list_tools / describe_tool), or an operator surface. Those
# stay in lite/full, where an operator opts into a wider listing.
#
# self_recovery is a router (check / quick / review) and is here anyway, which
# is not an exception to the rule above but an application of it: NO other
# advertised name reaches recovery, so "a router over actions these already
# cover" does not describe it. It earns the slot the same way the other five
# did -- the server itself tells a paused agent to call it
# (src/mcp_handlers/updates/phases.py:469 and
# src/mcp_handlers/support/agent_auth.py:199 both name it in the pause and
# auth-refusal paths), and under the ten-name default a schema-driven client
# was handed that instruction for a tool it had never been offered, in the one
# state where it can least improvise. tests/test_lite_wire_surface.py already
# encodes the invariant -- a tool named in another tool's recovery hints must
# be advertised -- and only happened to scope it to lite; see the standard-mode
# sibling in tests/test_tool_modes.py.
STANDARD_MODE_TOOLS: Set[str] = MINIMAL_MODE_TOOLS | {
    "search_shared_memory",   # Read shared memory (knowledge(action="search"))
    "store_finding",          # Write a durable finding
    "update_finding",         # Revise a finding already stored
    "request_review",         # Structured review (dialectic(action="request"))
    "consult",                # Advisory model help
    "self_recovery",          # Recover from a pause the server just imposed
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
        mode: "minimal", "standard", "lite", "full", "operator_readonly", or
            category name (e.g., "core", "admin")

    Returns:
        Set of tool names to include
    """
    if mode == "minimal":
        return MINIMAL_MODE_TOOLS.copy()

    if mode == "standard":
        return STANDARD_MODE_TOOLS.copy()
    
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


# ============================================================================
# Server instructions — the one in-band description of the wider surface
# ============================================================================
# A mode filters tools/list, so a schema-driven client offers the model only
# the advertised names. The MCP `instructions` string is the one channel that
# reaches every client at initialize, before any tool call, and it costs
# nothing per call. It is where a narrow advertised surface says what the
# server can still do and how to have the rest listed.
#
# Deliberately registry-free: this runs while src/mcp_server.py is building the
# server object, before the handler package is imported, so it must not touch
# the tool registry. Every number below comes from a static set in this module.

_MODE_SUMMARY = {
    "minimal": "the checkpoint loop only",
    "standard": (
        "the checkpoint loop, shared memory, review, advisory inference, "
        "and recovery"
    ),
    "lite": "the agent surface, including the consolidated routers and discovery tools",
    "full": "every registered tool",
}


def build_server_instructions(mode: str = None) -> str:
    """The MCP ``instructions`` string for a server running ``mode``.

    Names the workflow, then says plainly which capabilities exist but are not
    listed on this profile and how to list them. Without this, an agent on a
    narrow profile has no way to learn that shared memory, review, or advisory
    inference exist at all.
    """
    mode = (mode or TOOL_MODE or "standard").lower()
    known = {
        "minimal": MINIMAL_MODE_TOOLS,
        "standard": STANDARD_MODE_TOOLS,
        "lite": LITE_MODE_TOOLS,
        "operator_readonly": OPERATOR_READONLY_MODE_TOOLS,
        "operator_recovery": OPERATOR_RECOVERY_MODE_TOOLS,
    }
    lines = [
        "UNITARES governance: behavioral state estimation for long-lived agents.",
        "",
        "Bind once with start_session(force_new=true) and keep the returned "
        "client_session_id; pass it on every later call so writes are "
        "attributable. sync_state is the check-in and returns the state "
        "estimate, a policy action, and a named reason. record_result grades a "
        "check-in against a real outcome — without outcomes the estimate is "
        "self-report. check_working_state reads the verdict without writing.",
    ]
    if mode in ("standard", "lite", "full"):
        lines += [
            "",
            "search_shared_memory reads the cross-agent knowledge graph and "
            "store_finding / update_finding write to it; search before you "
            "write. request_review opens a structured review. consult asks an "
            "advisory model. self_recovery is how a paused agent gets moving "
            "again.",
        ]
    summary = _MODE_SUMMARY.get(mode)
    advertised = known.get(mode)
    lines.append("")
    if advertised is not None:
        lines.append(
            f"This server advertises {len(advertised)} tools "
            f"(GOVERNANCE_TOOL_MODE={mode}"
            + (f": {summary}" if summary else "")
            + ")."
        )
    elif mode == "full":
        lines.append(
            "This server advertises every registered tool "
            "(GOVERNANCE_TOOL_MODE=full)."
        )
    else:
        lines.append(f"This server runs GOVERNANCE_TOOL_MODE={mode}.")
    not_listed = []
    if mode == "minimal":
        not_listed.append(
            "shared memory (search_shared_memory, store_finding, "
            "update_finding), structured review (request_review), "
            "advisory inference (consult), and recovery (self_recovery)"
        )
    if mode in ("minimal", "standard"):
        not_listed.append(
            "the consolidated routers (knowledge, agent, observe, dialectic, "
            "calibration, config, export) and the discovery tools "
            "(list_tools, describe_tool)"
        )
    if mode != "full" and not not_listed:
        not_listed.append("the operator and admin tools")
    if not_listed:
        widen = "full" if mode == "lite" else "lite or full"
        lines.append(
            "Not listed here, but registered and callable by name on every "
            "transport: " + "; ".join(not_listed) + ". "
            f"Run the server with GOVERNANCE_TOOL_MODE={widen} to have them "
            "advertised; list_tools() enumerates whatever the profile lists."
        )
    return "\n".join(lines)


def is_claude_desktop_client() -> bool:
    """
    Detect if MCP client is Claude Desktop (vs Cursor or other clients).

    Claude Desktop is more sensitive to hangs, so we exclude problematic tools.

    Returns:
        True if client appears to be Claude Desktop
    """
    # psutil is an OPTIONAL dependency (pyproject.toml, [project.optional-
    # dependencies].full), so a core install does not have it. Import it OUTSIDE
    # the try whose except clause names psutil.NoSuchProcess: `import psutil`
    # inside that try makes the name a function local, so on ImportError the
    # except TUPLE ITSELF raises UnboundLocalError, which propagates out of
    # should_include_tool and get_public_tool_definitions and takes tools/list
    # down entirely instead of degrading to "not Claude Desktop".
    try:
        import psutil
    except ImportError:
        psutil = None

    # Check parent process name (most reliable)
    if psutil is not None:
        try:
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
        except (AttributeError, psutil.NoSuchProcess):
            pass

    # Check environment variables. Reached on a core install too: the process
    # walk is best-effort, this is not.
    if os.getenv("CLAUDE_DESKTOP") or os.getenv("ANTHROPIC_CLAUDE"):
        return True

    return False


# Tools to exclude for Claude Desktop (causes hangs/freezes)
#
# CAUTION before adding a name: is_claude_desktop_client() matches the substring
# "claude" anywhere in the parent process tree, so it returns True under Claude
# CODE as well as Claude Desktop (verified 2026-09-08 from a Claude Code
# session). While this set is empty that mis-detection decides nothing; the
# first name added here is excluded from BOTH clients, not just Desktop.
#
# `{}` here was an empty DICT annotated as Set[str] until 2026-09-08. Falsy and
# `in`-compatible either way, so nothing behaved differently -- but the first
# name added would have had to be added as a dict key.
# Add names as hangs are discovered, e.g. {"web_search"}.
CLAUDE_DESKTOP_EXCLUDED_TOOLS: Set[str] = set()


def should_include_tool(tool_name: str, mode: str = "full", client_type: str = None) -> bool:
    """
    Check if a tool should be included in the specified mode and client type

    Args:
        tool_name: Name of the tool
        mode: "minimal", "standard", "lite", "full", or category name
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
    
    # Check Claude Desktop exclusions. The empty-set guard comes FIRST: this
    # runs once per included tool per listing (42 calls for full), and
    # is_claude_desktop_client() walks up to four psutil process hops with no
    # caching. While CLAUDE_DESKTOP_EXCLUDED_TOOLS is empty the whole walk
    # decides nothing, so skipping it is behavior-preserving. The mechanism
    # stays wired: add a name to the set and detection resumes.
    if CLAUDE_DESKTOP_EXCLUDED_TOOLS and (
        client_type == "claude_desktop"
        or (client_type is None and is_claude_desktop_client())
    ):
        if tool_name in CLAUDE_DESKTOP_EXCLUDED_TOOLS:
            return False
    
    return True
