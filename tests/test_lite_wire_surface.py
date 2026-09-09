"""
CI drift guard: the advertised MCP wire surface must match the mode sets.

`GOVERNANCE_TOOL_MODE` defaults to "standard" (src/tool_modes.py): the five-tool
checkpoint loop plus shared memory, structured review, and advisory inference.
Under `GOVERNANCE_TOOL_MODE=lite` the server advertises exactly
the tools in `LITE_MODE_TOOLS` over the MCP protocol. That advertised surface
is composed from two places (src/tool_registration.py + src/tool_mode_listing.py):

  1. `auto_register_all_tools()` registers every `register=True` handler
     (`get_tool_registry()`); the tools/list filter advertises those the
     active mode names.
  2. `_register_common_aliases()` registers every workflow alias
     (start_session, sync_state, ...), which resolve at dispatch time to
     canonical handlers (onboard, process_agent_update, ...); the filter
     advertises those the active mode names.

Registration is mode-independent since the 2026-09 surface cut, so a name
outside the mode still dispatches on /mcp/ (tests/test_tool_mode_listing.py);
what these tests pin is the ADVERTISED set, which is all a schema-driven client
can see.

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

import pytest

from src.mcp_handlers.decorators import get_tool_registry
from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES, resolve_tool_alias
from src.interface_contract import workflow_alias_names_for_mode
from src.tool_modes import (
    LITE_MODE_TOOLS,
    MINIMAL_MODE_TOOLS,
    OPERATOR_READONLY_MODE_TOOLS,
    OPERATOR_RECOVERY_MODE_TOOLS,
    STANDARD_MODE_TOOLS,
    get_tools_for_mode,
)

# The mode sets, TOOL_ORDER and orientation code these tests compare are all
# written against the tools this repo ships. A governance_mcp.plugins package
# installed on the machine registers into the same decorator registry, so
# without this the comparison silently depended on collection/import order —
# see the fixture's docstring in conftest.py.
pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


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
    for alias in workflow_alias_names_for_mode("lite"):
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


def test_recovery_hint_targets_are_advertised_in_lite():
    """A tool named in another tool's recovery hints must be advertised in lite.

    History: on the live streamable-HTTP `/mcp/` transport a `register=True`
    handler that was NOT in the advertised set came back `Unknown tool` —
    verified 2026-08-11 against the deployed server on :8767 for both
    `get_workspace_health` and `process_agent_update`. The 2026-09 surface cut
    removed that coupling: the mount now registers every handler and filters
    only tools/list (src/tool_mode_listing.py), so an unadvertised name
    dispatches on `/mcp/` as it always did on REST.

    The guard stays because a schema-driven MCP client only calls names it was
    shown: a `related_tools` hint that names an unadvertised tool is still a
    dead end for that client, even though the server would dispatch it.
    """
    registry = set(get_tool_registry().keys())

    # call_model's error paths point at these by name (support/model_inference.py).
    for name in ("list_inference_hosts", "describe_inference_host"):
        assert name in registry, f"{name} lost register=True"
        assert name in LITE_MODE_TOOLS, (
            f"{name} is named in call_model's recovery hints but is not advertised "
            "in lite mode — an MCP-native agent following that hint gets "
            "'Unknown tool'. Either re-advertise it or strip the hint."
        )


def test_recovery_hint_targets_are_advertised_in_standard():
    """The same invariant as the lite test above, on the DEFAULT profile.

    The lite sibling has guarded this since the 2026-09 surface cut, but lite
    is opt-in; `standard` is what an adopter gets, so it is the surface where a
    dead-end hint actually reaches people. The pause path is the sharp case:
    the server tells a paused agent, in prose, to call `self_recovery` --

        src/mcp_handlers/updates/phases.py:469
            "Use self_recovery(action='quick') for safe states, or
             self_recovery(action='review', reflection='...') for full recovery"
        src/mcp_handlers/support/agent_auth.py:199
            the same instruction on an auth refusal
        src/mcp_handlers/dialectic/responses.py:46,63
            related_tools: [..., "self_recovery", ...]

    -- and until 2026-09-08 `standard` did not advertise it. A schema-driven
    client (Claude Code, Codex, Cursor) is offered only what tools/list returns,
    so that instruction named a tool the model had never been shown, in the one
    state where it can least improvise its way out.

    If this fails, do NOT just delete the name from the set below: either
    advertise the hinted tool in `standard`, or strip the hint from the handler
    that emits it. A hint the caller cannot act on is worse than no hint.
    """
    registry = set(get_tool_registry().keys())

    for name in ("self_recovery",):
        assert name in registry, f"{name} lost register=True"
        assert name in STANDARD_MODE_TOOLS, (
            f"{name} is named in the pause/auth-refusal recovery hints but is "
            "not advertised in the default `standard` profile — a schema-driven "
            "agent following that instruction has no such tool. Either "
            "re-advertise it or strip the hint."
        )


def test_standard_can_finish_the_review_it_can_start():
    """`standard` advertises `dialectic`, not just `request_review`.

    `request_review` pins action="request". Until 2026-09-08 that was the only
    reachable action of the dialectic router on the default profile, so an
    agent could OPEN a review and reach none of the six actions that finish
    one -- get, list, thesis, antithesis, synthesis, reassign. A loop you can
    enter and not leave is worse than one you were never offered.

    The server walks the agent into it, which is what makes this a defect
    rather than a gap:

        src/mcp_handlers/dialectic/handlers.py:1385
            request_review's own SESSION_EXISTS refusal --
            "Use dialectic(action='get', session_id='...')"
        src/mcp_handlers/middleware/envelope_step.py:1169
            build_experience_envelope tells any agent holding an open session
            to call dialectic(action='thesis', ...) -- middleware, so it fires
            for every experience alias on every profile

    Verified from a Claude Code session on this default 2026-09-08: it called
    request_review, was told to poll dialectic(action='get'), and could not,
    because the name had never been advertised to it.

    If this fails, do NOT drop `dialectic` from the set: either keep it
    advertised, or remove every hint that names an action `request_review`
    cannot reach.
    """
    registry = set(get_tool_registry().keys())
    assert "dialectic" in registry, "dialectic lost register=True"
    assert "dialectic" in STANDARD_MODE_TOOLS, (
        "standard advertises request_review (action='request') but not the "
        "dialectic router, so six of its seven actions are unreachable for a "
        "schema-driven client -- including the get/thesis the server's own "
        "responses tell the agent to call."
    )

    # The alias must still be there: it is the agent-facing name and carries
    # the normalized envelope (is_experience_alias). Advertising the router is
    # additive, not a replacement.
    assert "request_review" in STANDARD_MODE_TOOLS


def test_minimal_wire_surface_is_the_five_tool_loop():
    """The default (minimal) advertised surface is exactly the checkpoint loop."""
    allowed = get_tools_for_mode("minimal")
    registry = set(get_tool_registry().keys())
    surface = {name for name in registry if name in allowed}
    for alias in workflow_alias_names_for_mode("minimal"):
        _actual, info = resolve_tool_alias(alias)
        if info is not None:
            surface.add(alias)

    assert surface == MINIMAL_MODE_TOOLS
    assert {"agent", "observe", "config", "admin", "list_tools", "dialectic"} <= surface



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


@pytest.mark.parametrize(
    "mode_name, mode_tools",
    [
        ("minimal", MINIMAL_MODE_TOOLS),
        ("lite", LITE_MODE_TOOLS),
        ("operator_readonly", OPERATOR_READONLY_MODE_TOOLS),
        ("operator_recovery", OPERATOR_RECOVERY_MODE_TOOLS),
    ],
)
def test_every_mode_tool_is_backed_by_handler_or_alias(mode_name, mode_tools):
    """Every tool listed in ANY deployable mode set must be a real wire tool.

    A name is a wire tool only if it is a register=True handler or a workflow
    alias. A register=False handler (reachable solely via a consolidated
    action-router, e.g. the old `store_knowledge_graph` / `check_recovery_options`
    entries) is NOT standalone-advertisable, so listing it in a mode set silently
    promises a tool the server never exposes. This guards that drift across every
    mode, not just lite.
    """
    registry = set(get_tool_registry().keys())
    aliases = {a for a in AGENT_WORKFLOW_ALIASES if resolve_tool_alias(a)[1] is not None}
    backed = registry | aliases

    unbacked = sorted(mode_tools - backed)
    assert not unbacked, (
        f"{mode_name} mode lists tools with no register=True handler and no alias "
        f"— these would never appear on the wire: {unbacked}"
    )


def test_workflow_aliases_are_lite_visible():
    """All primary workflow aliases are intentionally available in lite mode."""
    alias_names = {a for a in AGENT_WORKFLOW_ALIASES if resolve_tool_alias(a)[1] is not None}
    leaked = sorted(alias_names - LITE_MODE_TOOLS)
    assert not leaked, (
        "Workflow aliases advertised on the wire but absent from LITE_MODE_TOOLS "
        f"(would over-advertise in lite mode): {leaked}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["minimal", "lite", "operator_readonly", "full"])
async def test_orientation_compact_view_matches_the_wire(monkeypatch, mode):
    """list_tools' default view must equal what this deployment advertises.

    Orientation and the wire are filtered by different things, and until
    2026-08-29 neither direction agreed. The compact view filtered on the
    hardcoded LITE_MODE_TOOLS constant while the wire is filtered by
    GOVERNANCE_TOOL_MODE, so under operator_readonly list_tools named 19 tools
    the wire would not dispatch and hid 3 it would. A schema-driven MCP client
    can only call advertised names, so an orientation surface that disagrees
    with the wire either invents tools or conceals them.

    This pins BOTH directions in every deployable mode. It is not a claim that
    unadvertised tools are unwanted: list_tools(lite=false) still lists them,
    flagged advertised=false, with a `not_advertised` block saying why.
    """
    import json

    import src.tool_modes as tool_modes
    from src.interface_contract import get_public_tool_definitions
    from src.mcp_handlers.introspection import tool_introspection

    monkeypatch.setattr(tool_modes, "TOOL_MODE", mode, raising=False)

    result = await tool_introspection.handle_list_tools({"lite": True})
    shown = {tool["name"] for tool in json.loads(result[0].text)["tools"]}
    wire = {tool.name for tool in get_public_tool_definitions(mode)}

    assert shown == wire, (
        f"{mode}: list_tools default view disagrees with the MCP wire.\n"
        f"  named but not dispatchable: {sorted(shown - wire)}\n"
        f"  advertised but hidden:      {sorted(wire - shown)}"
    )


def test_no_alias_name_is_also_a_registered_tool():
    """A name is either a registered dispatch tool or an alias, never both.

    resolve_alias rewrites the tool name before TOOL_HANDLERS is consulted, so
    a name that is both has an unreachable registration. That is how
    direct_resume_if_safe died: it was registered AND aliased to quick_resume,
    which is register=False, so every call resolved to a name absent from
    TOOL_HANDLERS and returned tool_not_found_error while the tool's own
    handler sat there unused.
    """
    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import list_all_aliases

    both = sorted(set(list_all_aliases()) & set(get_tool_registry()))
    assert not both, (
        "these names are both an alias and a registered dispatch tool, so "
        f"their registration is unreachable: {both}"
    )


def test_every_deprecation_surface_names_a_callable_tool():
    """A deprecation may only tell an agent to call a name it can call.

    Sibling of the alias guard above, one surface over. That one checks what a
    name *dispatches* to; this one checks what a deprecation *says*. Both
    failures look identical to the agent -- tool_not_found_error while the
    deprecated tool's own handler sits unused -- but only the first was caught.

    `direct_resume_if_safe` said "use quick_resume()" and
    `request_dialectic_review` said "use self_recovery_review(...)"; both
    targets are register=False delegates of `self_recovery`, so both hints were
    dead ends. Registered tools and aliases both count as callable; a
    register=False delegate does not.

    Scans `superseded_by` and every unqualified `name(` token in `migration`
    text. A dotted reference (`client.leave_note()`) is an SDK method, not a
    tool name, and is deliberately not matched.
    """
    import re

    from src.mcp_handlers.decorators import get_tool_registry, get_tool_definition
    from src.mcp_handlers.introspection.tool_catalog import (
        DEPRECATION_REGISTRY,
        TOOL_RELATIONSHIPS,
    )
    from src.mcp_handlers.tool_stability import list_all_aliases

    callable_names = set(get_tool_registry()) | set(list_all_aliases())
    call_token = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\(")

    def check(surface, tool, field, names):
        return [
            f"{surface}[{tool!r}].{field} -> {n}"
            for n in names
            if n and n not in callable_names
        ]

    broken = []
    for tool, entry in DEPRECATION_REGISTRY.items():
        broken += check("DEPRECATION_REGISTRY", tool, "superseded_by",
                        [entry.get("superseded_by")])
        broken += check("DEPRECATION_REGISTRY", tool, "migration",
                        call_token.findall(entry.get("migration", "")))
    for tool, entry in TOOL_RELATIONSHIPS.items():
        if not entry.get("deprecated"):
            continue
        broken += check("TOOL_RELATIONSHIPS", tool, "superseded_by",
                        [entry.get("superseded_by")])
        broken += check("TOOL_RELATIONSHIPS", tool, "migration",
                        call_token.findall(entry.get("migration", "")))
    for name in get_tool_registry():
        definition = get_tool_definition(name)
        broken += check("@mcp_tool", name, "superseded_by",
                        [definition.superseded_by if definition else None])

    assert not broken, (
        "these deprecation surfaces name a tool that is neither registered nor "
        "an alias, so an agent following the hint gets tool_not_found_error:\n  "
        + "\n  ".join(sorted(broken))
    )
