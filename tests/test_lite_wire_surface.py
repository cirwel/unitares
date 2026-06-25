"""
CI drift guard: the MCP wire surface must match LITE_MODE_TOOLS.

`GOVERNANCE_TOOL_MODE` defaults to "lite" (src/tool_modes.py), so the deployed
server advertises exactly the tools in `LITE_MODE_TOOLS` over the MCP protocol.
That wire surface is composed from two places in src/mcp_server.py:

  1. `register_dynamic_tools()` advertises every `register=True` handler
     (`get_tool_registry()`) that passes the active mode filter.
  2. `_register_common_aliases()` advertises the workflow aliases in
     `AGENT_WORKFLOW_ALIASES` (start_session, sync_state, ...), which resolve at
     dispatch time to canonical handlers (onboard, process_agent_update, ...).

If someone adds a tool to `LITE_MODE_TOOLS` but forgets `register=True` (or an
alias entry), it would be silently dropped from the wire — the client sees fewer
tools than the mode promises. If a handler/alias is added that the mode set
doesn't list, the wire over-advertises. Either is drift between "the tools" and
"the server". This test reproduces the server's composition and asserts exact
equality, catching both directions before deploy.

Pure registry + data test; no DB or network. See docs/dev/TOOL_REGISTRATION.md.
"""

import sys
from pathlib import Path

# Add project root to path (matches the other tests/ modules)
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Importing the handler package triggers every @mcp_tool decorator, populating
# the registry. Must happen before reading get_tool_registry().
import src.mcp_handlers  # noqa: F401

from src.mcp_handlers.decorators import get_tool_registry
from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES, resolve_tool_alias
from src.tool_modes import LITE_MODE_TOOLS, get_tools_for_mode


def _lite_wire_surface() -> set[str]:
    """Reproduce the set of tool names src/mcp_server.py advertises in lite mode.

    Mirrors register_dynamic_tools() (registered handlers ∩ mode) unioned with
    _register_common_aliases() (resolvable workflow aliases).
    """
    allowed = get_tools_for_mode("lite")
    registry = set(get_tool_registry().keys())

    # (1) register_dynamic_tools: register=True handlers that pass the mode gate.
    surface = {name for name in registry if name in allowed}

    # (2) _register_common_aliases: each resolvable workflow alias is advertised.
    for alias in AGENT_WORKFLOW_ALIASES:
        _actual, info = resolve_tool_alias(alias)
        if info is not None:
            surface.add(alias)

    return surface


def test_lite_wire_surface_equals_lite_mode_tools():
    """The advertised wire surface in lite mode is exactly LITE_MODE_TOOLS."""
    surface = _lite_wire_surface()

    missing = sorted(LITE_MODE_TOOLS - surface)
    extra = sorted(surface - LITE_MODE_TOOLS)

    assert not missing, (
        "Tools in LITE_MODE_TOOLS that the server would NOT advertise "
        f"(missing register=True handler or alias): {missing}"
    )
    assert not extra, (
        "Tools the server would advertise in lite mode that are NOT in "
        f"LITE_MODE_TOOLS (phantom / over-advertised): {extra}"
    )
    assert surface == LITE_MODE_TOOLS


def test_every_lite_tool_is_backed_by_handler_or_alias():
    """Each LITE_MODE_TOOLS name resolves to a real handler or a workflow alias.

    Focused failure message for the most common drift: a tool added to the lite
    set without `register=True` on its handler.
    """
    registry = set(get_tool_registry().keys())
    aliases = {a for a in AGENT_WORKFLOW_ALIASES if resolve_tool_alias(a)[1] is not None}
    backed = registry | aliases

    unbacked = sorted(LITE_MODE_TOOLS - backed)
    assert not unbacked, (
        "LITE_MODE_TOOLS entries with no register=True handler and no alias "
        f"— these would silently never appear on the wire: {unbacked}"
    )


def test_workflow_aliases_are_lite_visible():
    """All workflow aliases live in the lite set (they are registered uncondition-
    ally by _register_common_aliases, so a lite-excluded alias would over-advertise).
    """
    alias_names = {a for a in AGENT_WORKFLOW_ALIASES if resolve_tool_alias(a)[1] is not None}
    leaked = sorted(alias_names - LITE_MODE_TOOLS)
    assert not leaked, (
        "Workflow aliases advertised on the wire but absent from LITE_MODE_TOOLS "
        f"(would over-advertise in lite mode): {leaked}"
    )
