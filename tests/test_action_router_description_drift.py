"""Drift-guard: every action_router tool's registered description must name
all of its routed actions, and its examples must reference only real actions.

The description/action list is now DERIVED from the action map in
``action_router`` (decorators.py), so this can only regress if that derivation
is removed or bypassed. The guard pins the invariant directly: it caught the
class where the hand-maintained ``description=`` string dropped ``quick`` from
``dialectic`` and ``synthesize`` from ``knowledge``, and where an example
referenced a non-existent ``dialectic(action='vote')``.

The actual routed actions are recovered from the router's own error-recovery
response (an unknown action returns ``recovery.valid_actions``) rather than
hardcoded here — hardcoding would just reintroduce the drift this test exists
to prevent.
"""

from __future__ import annotations

import json

import pytest

import src.mcp_handlers.consolidated  # noqa: F401  (registers routers)
from src.mcp_handlers import TOOL_HANDLERS
from src.mcp_handlers.decorators import get_tool_description

# The consolidated action_router tools registered by importing consolidated.py.
CONSOLIDATED_TOOLS = [
    "knowledge",
    "agent",
    "calibration",
    "config",
    "export",
    "observe",
    "admin",
    "dialectic",
]


async def _routed_actions(tool: str) -> list[str]:
    """Recover a router's real action list from its unknown-action recovery."""
    handler = TOOL_HANDLERS[tool]
    result = await handler({"action": "__definitely_not_an_action__"})
    payload = json.loads(result[0].text)
    return payload["recovery"]["valid_actions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", CONSOLIDATED_TOOLS)
async def test_description_names_every_routed_action(tool):
    actions = await _routed_actions(tool)
    assert actions, f"{tool} reported no valid_actions"
    desc = get_tool_description(tool)
    assert desc, f"{tool} has no registered description"
    missing = [a for a in actions if a not in desc]
    assert not missing, (
        f"{tool} description omits routed actions {missing}; description={desc!r}"
    )


@pytest.mark.asyncio
async def test_dialectic_describes_quick_and_drops_dead_vote():
    """The two concrete regressions that motivated the derive-from-map fix."""
    desc = get_tool_description("dialectic")
    assert "quick" in desc, "dialectic must advertise the 'quick' action"
    assert "vote" not in desc, (
        "dialectic must not advertise 'vote' — there is no vote handler "
        "(the quorum_voting phase is vestigial)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", CONSOLIDATED_TOOLS)
async def test_description_override_does_not_contradict_routed_actions(tool):
    """The override table outranks the derived description — guard it too.

    ``tool_introspection.py`` resolves ``TOOL_DESCRIPTION_OVERRIDES`` at priority
    1, above the schema description that ``tool_descriptions.json`` feeds. So the
    derive-from-map fix above does NOT protect the string an agent actually sees
    from ``list_tools``: the override is hand-maintained and silently wins.

    That gap was live — the dialectic override advertised ``vote`` (removed) and
    omitted ``quick`` (routed) long after the derived description was corrected,
    leaving ``list_tools`` and ``describe_tool`` contradicting each other about
    the same tool.

    Asserting the override names *every* action would force churn on a
    deliberately short one-liner, so this pins the direction that misleads: an
    override may abbreviate, but must never name an action that does not route.
    """
    routed = set(await _routed_actions(tool))
    assert routed, f"{tool} reported no valid_actions"

    from src.mcp_handlers.introspection.tool_catalog import TOOL_DESCRIPTION_OVERRIDES

    override = TOOL_DESCRIPTION_OVERRIDES.get(tool)
    if not override:
        pytest.skip(f"{tool} has no description override")

    # Only inspect the action-list clause, so ordinary prose can't trip this.
    _, _, listed = override.partition(":")
    claimed = {
        word.strip().strip(".")
        for word in listed.split(",")
        if word.strip().strip(".").isidentifier()
    }
    phantom = sorted(claimed - routed)
    assert not phantom, (
        f"{tool} override advertises actions that do not route: {phantom}. "
        f"Routed actions: {sorted(routed)}. Override: {override!r}"
    )
