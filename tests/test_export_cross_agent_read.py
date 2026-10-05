"""export reads only the caller's own history.

``handle_get_system_history`` and ``handle_export_to_file`` take ``agent_id``
as given: neither checks it against the caller. What keeps a bound caller from
exporting another agent's full trajectory is the dispatch step before them,
``inject_identity``, which refuses an ``agent_id`` that is not the session's own
(``identity_mismatch``) unless the tool is in ``_OPERATOR_TARGET_CALLS``. An
unbound caller is refused earlier, by strict identity (``identity_required``).

So the guard lives in one place, and adding ``export`` to the operator-target
set, or routing it around ``inject_identity``, would open the read with no
handler-level check behind it. These tests pin the dispatch behavior for both
export actions. Cross-agent observation stays available by design through
``observe(action='agent', target_agent_id=...)``.
"""

from __future__ import annotations

import json

import pytest

from src.mcp_handlers.middleware import DispatchContext, inject_identity, resolve_alias
from src.mcp_handlers.middleware.params_step import _OPERATOR_TARGET_CALLS

CALLER = "11111111-1111-4111-8111-111111111111"
OTHER = "2222abcd-2222-4222-8222-22222222abcd"

EXPORT_CALLS = [
    ("export", {"action": "history"}),
    ("export", {"action": "file"}),
    ("get_system_history", {}),
    ("export_to_file", {}),
]


async def _identity_step(name: str, arguments: dict):
    ctx = DispatchContext(bound_agent_id=CALLER)
    step = await resolve_alias(name, dict(arguments), ctx)
    assert not isinstance(step, list), step
    name, arguments, ctx = step
    return name, await inject_identity(name, arguments, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", EXPORT_CALLS)
async def test_another_agents_id_is_refused(name, arguments):
    _, step = await _identity_step(name, {**arguments, "agent_id": OTHER})
    assert isinstance(step, list), f"{name} {arguments} with another agent's id was not refused"
    body = json.loads(step[0].text)
    assert body["success"] is False
    assert body["error_type"] == "identity_mismatch"
    assert body["requested_agent_id"] == OTHER


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", EXPORT_CALLS)
async def test_own_id_and_no_id_reach_the_handler_as_the_caller(name, arguments):
    for args in ({**arguments, "agent_id": CALLER}, dict(arguments)):
        _, step = await _identity_step(name, args)
        assert not isinstance(step, list), step
        _, forwarded, _ = step
        assert forwarded["agent_id"] == CALLER


@pytest.mark.parametrize("name,arguments", EXPORT_CALLS)
def test_export_is_not_an_operator_target_call(name, arguments):
    assert not _OPERATOR_TARGET_CALLS.matches(name, arguments)
