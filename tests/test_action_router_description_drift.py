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
