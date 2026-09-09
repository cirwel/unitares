"""One complete public tool catalog; legacy mode APIs are compatibility shims.

GOVERNANCE_TOOL_MODE no longer selects capabilities. Every transport advertises
all registered tools and primary workflow aliases. Authorization belongs to the
dispatch pipeline, never to discovery. Keep the old imports/arguments so deployed
adapters can upgrade independently; category/tier filters belong to list_tools.
"""

from typing import Set
import os

from src.tool_meta import (  # noqa: F401 -- compatibility re-exports
    TOOL_CATEGORIES, TOOL_OPERATIONS, TOOL_TIERS, TOOL_META_BY_NAME,
)

# Stable compatibility label. Reading an old environment setting must not
# fragment the federation handshake or hide the next tool in a workflow.
TOOL_MODE = "full"

# Deprecated import aliases, not independently maintained profiles. Runtime
# discovery below reads the registry and includes installed plugin tools too.
MINIMAL_MODE_TOOLS = set(TOOL_META_BY_NAME)
STANDARD_MODE_TOOLS = set(TOOL_META_BY_NAME)
LITE_MODE_TOOLS = set(TOOL_META_BY_NAME)
OPERATOR_READONLY_MODE_TOOLS = set(TOOL_META_BY_NAME)
OPERATOR_RECOVERY_MODE_TOOLS = set(TOOL_META_BY_NAME)


def advertised_tool_names_full() -> Set[str]:
    """Every registered dispatch tool plus the primary workflow aliases."""
    import src.mcp_handlers  # noqa: F401 -- settles decorators
    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES
    return set(get_tool_registry()) | set(AGENT_WORKFLOW_ALIASES)


def get_tools_for_mode(mode: str = "full") -> Set[str]:
    """Compatibility API: all old mode values resolve to the complete catalog."""
    try:
        return advertised_tool_names_full()
    except ImportError:
        # Dependency-free diagnostics may import this module. A live server
        # has its dependencies and must use the registry, including plugins.
        return set(TOOL_META_BY_NAME)


def build_server_instructions(mode: str = None) -> str:
    """Registry-free orientation, safe during server initialization."""
    return """UNITARES governance: behavioral state estimation for long-lived agents.

Bind once with start_session(force_new=true) and keep the returned client_session_id; pass it on every later call so writes are attributable. sync_state is the check-in and returns the state estimate, a policy action, and a named reason. record_result grades a check-in against a real outcome — without outcomes the estimate is self-report. check_working_state reads the verdict without writing.

search_shared_memory reads the cross-agent knowledge graph and store_finding / update_finding write to it; search before you write. request_review opens a structured review and dialectic reads and advances it (action=get / thesis / antithesis / synthesis / list / reassign). consult asks an advisory model. self_recovery is how a paused agent gets moving again.

One complete catalog advertises every registered tool and primary workflow alias. No tool mode is needed; legacy GOVERNANCE_TOOL_MODE settings are ignored. agent, observe, calibration, config, export, and admin expose lifecycle, diagnostics, calibration, configuration, history, and maintenance. Each action retains its own authorization requirements.

Prefer the workflow names above; their raw implementations remain available for compatibility and specialized callers. list_tools groups the catalog by category and tier. Parameter descriptions are abridged to their first sentence; describe_tool(tool_name=..., action=...) returns full details and the parameters one router action takes."""


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
