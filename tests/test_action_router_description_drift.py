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

TWO DESCRIPTIONS EXIST PER ROUTER, AND ONLY ONE IS SERVED
---------------------------------------------------------
``get_tool_description`` returns what ``@mcp_tool`` recorded, which for an
``action_router`` is the text ``action_router`` derives from the action map, so
it names every routed action by construction. That text is NOT what any client
reads. Every one of these eight routers has a ``*Params`` model and sits in
``TOOL_ORDER``, and for such a name ``get_tool_definitions`` takes the
description from ``src/tool_descriptions.py`` first; the derived text is only
reached by the auto-discovery branch for tools outside ``TOOL_ORDER``. So the
derived-text tests below pin the DERIVATION, and the served-text tests pin what
an agent actually reads. Both are needed, and conflating them is how this file
came to look protective while checking a string nobody is shown.

A third guard lived here until 2026-09-12: the hand-maintained
``TOOL_DESCRIPTION_OVERRIDES`` entry for a router outranked the derived
description in ``list_tools``, and the ``dialectic`` entry advertised ``vote``
(removed) and omitted ``quick`` (routed) long after the derived text was
corrected. That table may no longer carry an advertised name
(``test_override_table_carries_no_advertised_name`` in
tests/test_describe_tool_drift.py), which closes the override vector but NOT
the class: the served text, authored in ``src/tool_descriptions.json`` or the
Python override dict in ``src/tool_descriptions.py``, is hand-written and
nothing derives it from the action map, so an action added to a router without a
description edit is invisible again. ``test_served_description_*`` below is the
replacement guard, and it is deliberately on the served text rather than the
derived text the removed guard's neighbours check.
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


def _served_description(tool: str) -> str:
    """The description this deployment actually advertises for ``tool``."""
    import src.tool_modes
    from src.interface_contract import get_public_tool_definitions

    for definition in get_public_tool_definitions(src.tool_modes.TOOL_MODE):
        if definition.name == tool:
            return definition.description or ""
    raise AssertionError(f"{tool} is not advertised; the roster changed")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", CONSOLIDATED_TOOLS)
async def test_served_description_names_every_routed_action(tool):
    """What a client reads must name every action it can route.

    dialectic is the case that makes this worth pinning: its catalog override
    listed all eight actions, and when #2176 made list_tools serve the wire
    text instead, `reassign` stopped being named anywhere an agent looks. The
    repair was to the served text, so the guard belongs there too.

    This ran with a ``SERVED_ACTION_COVERAGE_GAPS`` xfail set for admin and
    knowledge until 2026-09-12, covering six omitted actions. None of them was
    undiscoverable by name: every router's advertised ``action`` enum lists all
    of its routed actions. What an enum cannot carry is an action's contract —
    what it does and what it requires — and that is the half this guard pins.
    """
    actions = await _routed_actions(tool)
    assert actions, f"{tool} reported no valid_actions"
    served = _served_description(tool)
    missing = sorted(a for a in actions if a not in served)
    assert not missing, (
        f"{tool} is advertised to clients with a description that omits routed "
        f"actions {missing}. The served text is authored in "
        f"src/tool_descriptions.json, or in _INFERENCE_DESCRIPTION_OVERRIDES in "
        f"src/tool_descriptions.py, which wins for any name it carries; only the "
        f"first non-empty line ships. The derived text that names every action "
        f"is not what ships. Served text: {served!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", CONSOLIDATED_TOOLS)
async def test_served_description_names_no_phantom_action(tool):
    """The direction the removed override guard protected, on the served text.

    An advertised description may abbreviate, but must never instruct a caller
    to pass an action that does not route — that is the `dialectic(action='vote')`
    defect, and it is worse than an omission because the caller acts on it.
    """
    import re

    actions = set(await _routed_actions(tool))
    served = _served_description(tool)
    claimed = set(re.findall(r"action=['\"](\w+)['\"]", served))
    phantom = sorted(claimed - actions)
    assert not phantom, (
        f"{tool} is advertised with action(s) {phantom} that do not route. "
        f"Routed: {sorted(actions)}. Served text: {served!r}"
    )
